from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.build_ticket_product_review import source_case_fingerprint  # noqa: E402
from src.response_contract import ResponseProfileName  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSIONED_DATA_ROOT = (PROJECT_ROOT / "data").resolve()
PRIVATE_DATA_ROOT = (VERSIONED_DATA_ROOT / "private").resolve()

DEFAULT_INPUT = Path(
    "data/private/tickets/product_baseline_20260728/product_calibration_cases.jsonl"
)
DEFAULT_MANIFEST = DEFAULT_INPUT.with_name("top20_review_manifest.csv")
DEFAULT_OUTPUT_NAME = "product_calibration_reviewed_ask_cases.json"
DEFAULT_KB_SEED = Path("data/knowledge_base_seed.json")
KB_SEED_HASH_CANONICALIZATION = "json_sort_keys_compact_utf8_v1"

_SAFE_HASH_RE = re.compile(r"^[0-9a-f]{12,64}$")
_SAFE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,200}$")
_ALLOWED_SOURCE_LABEL_STATUSES = frozenset({"human_reviewed", "weak_unreviewed"})
_ALLOWED_BEHAVIORS = frozenset({"answer", "clarify", "escalate"})
_REQUIRED_SAFETY_FLAGS = (
    "operator_answer_included",
    "operator_answer_used_as_fact",
)
_REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "case_id_hash",
        "intent",
        "aspect",
        "entity_class",
        "expected_route",
        "expected_escalation_reason",
        "time_sensitive",
        "role_reconstruction_status",
        "role_verdict",
        "multiturn_status",
        "label_verdict",
        "reviewer",
        "reviewed_at",
        "source_schema_version",
        "source_case_fingerprint",
        "approved_kb_seed_sha256",
        "corrected_intent",
        "corrected_aspect",
        "corrected_entity_class",
        "answerable_from_snapshot",
        "approved_chunk_ids",
        "forbidden_profiles",
        "include_in_calibration",
    }
)
_RESPONSE_PROFILES = frozenset(profile.value for profile in ResponseProfileName)


def build_reviewed_ticket_ask_cases(
    input_path: Path,
    manifest_path: Path,
    output_path: Path,
    kb_seed_path: Path = DEFAULT_KB_SEED,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    source_path, review_path, destination, seed_path = _validate_paths(
        input_path,
        manifest_path,
        output_path,
        kb_seed_path,
    )
    if destination.exists() and not overwrite:
        raise ValueError(f"Output already exists: {destination}")

    source_cases = _read_source_cases(source_path)
    manifest_rows, manifest_sha256 = _read_manifest(review_path)
    seed_chunks, kb_seed_sha256 = _read_kb_seed(seed_path)

    seen_manifest_ids: set[str] = set()
    selected: list[tuple[str, dict[str, str], dict[str, Any]]] = []
    exported: list[dict[str, Any]] = []
    approved_rows = 0
    for row_number, row in enumerate(manifest_rows, start=2):
        case_id_hash = _required_hash(
            row.get("case_id_hash"),
            field="case_id_hash",
            location=f"manifest row {row_number}",
        )
        if case_id_hash in seen_manifest_ids:
            raise ValueError(f"Duplicate manifest case_id_hash at row {row_number}")
        seen_manifest_ids.add(case_id_hash)

        source = source_cases.get(case_id_hash)
        if source is None:
            raise ValueError(
                f"Manifest row {row_number} references a missing source case"
            )

        verdict = str(row.get("label_verdict") or "").strip().casefold()
        include = _optional_csv_bool(
            row.get("include_in_calibration"),
            field="include_in_calibration",
            location=f"manifest row {row_number}",
        )
        if verdict not in {"", "approved", "rejected"}:
            raise ValueError(
                f"Manifest row {row_number} has unknown label_verdict"
            )
        if include is True and verdict != "approved":
            raise ValueError(
                f"Manifest row {row_number} includes a non-approved case"
            )
        if verdict == "approved":
            approved_rows += 1
            if include is None:
                raise ValueError(
                    f"Approved case {case_id_hash} is missing boolean "
                    "include_in_calibration"
                )
            _required_metadata(
                row.get("reviewer"),
                field="reviewer",
                case_id_hash=case_id_hash,
            )
            _required_reviewed_at(row.get("reviewed_at"), case_id_hash=case_id_hash)
            source_schema_version = _required_metadata(
                source.get("schema_version"),
                field="source schema_version",
                case_id_hash=case_id_hash,
            )
            manifest_schema_version = _required_metadata(
                row.get("source_schema_version"),
                field="source_schema_version",
                case_id_hash=case_id_hash,
            )
            if manifest_schema_version != source_schema_version:
                raise ValueError(
                    f"Approved case {case_id_hash} has stale source_schema_version"
                )
            manifest_fingerprint = _required_sha256(
                row.get("source_case_fingerprint"),
                field="source_case_fingerprint",
                case_id_hash=case_id_hash,
            )
            if manifest_fingerprint != source_case_fingerprint(source):
                raise ValueError(
                    f"Approved case {case_id_hash} has stale source_case_fingerprint"
                )
            approved_seed_sha256 = _required_sha256(
                row.get("approved_kb_seed_sha256"),
                field="approved_kb_seed_sha256",
                case_id_hash=case_id_hash,
            )
            if approved_seed_sha256 != kb_seed_sha256:
                raise ValueError(
                    f"Approved case {case_id_hash} has stale "
                    "approved_kb_seed_sha256"
                )
            _validated_role_status(
                row,
                source,
                case_id_hash=case_id_hash,
            )
            export_case = _build_export_case(
                case_id_hash,
                row,
                source,
                seed_chunks=seed_chunks,
                manifest_sha256=manifest_sha256,
                kb_seed_sha256=kb_seed_sha256,
            )
            if include is True:
                selected.append((case_id_hash, row, source))
                exported.append(export_case)

    if not selected:
        raise ValueError(
            "Manifest contains no rows with label_verdict='approved' and "
            "include_in_calibration=true"
        )

    exported.sort(key=lambda item: str(item["case_id_hash"]))
    _write_json_array(destination, exported, overwrite=overwrite)

    behavior_counts = Counter(str(item["expected_behavior"]) for item in exported)
    profile_counts = Counter(str(item["expected_response_profile"]) for item in exported)
    return {
        "source_cases": len(source_cases),
        "manifest_rows": len(manifest_rows),
        "manifest_approved_rows": approved_rows,
        "exported_cases": len(exported),
        "excluded_manifest_rows": len(manifest_rows) - len(exported),
        "corrected_intent_cases": sum(
            bool(str(row.get("corrected_intent") or "").strip())
            for _, row, _ in selected
        ),
        "corrected_aspect_cases": sum(
            bool(str(row.get("corrected_aspect") or "").strip())
            for _, row, _ in selected
        ),
        "corrected_entity_cases": sum(
            bool(
                str(
                    row.get("corrected_entity_class")
                    or row.get("corrected_entity")
                    or ""
                ).strip()
            )
            for _, row, _ in selected
        ),
        "behavior_counts": dict(sorted(behavior_counts.items())),
        "profile_counts": dict(sorted(profile_counts.items())),
        "provenance": {
            "review_manifest_path": str(review_path),
            "review_manifest_sha256": manifest_sha256,
            "kb_seed_path": str(seed_path),
            "kb_seed_sha256": kb_seed_sha256,
            "source_fingerprint_algorithm": "sha256",
            "source_schema_versions": sorted(
                {
                    str(item["source_provenance"]["schema_version"])
                    for item in exported
                }
            ),
            "kb_seed_chunks": len(seed_chunks),
            "published_yonote_chunks": sum(
                chunk.get("status") == "published"
                and chunk.get("source_type") == "yonote"
                for chunk in seed_chunks.values()
            ),
        },
        "output": str(destination),
    }


def _read_source_cases(path: Path) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    try:
        source_file = path.open("r", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"Could not read private calibration cases: {path}") from exc

    with source_file:
        for line_number, line in enumerate(source_file, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"Invalid JSON object at source line {line_number}") from None
            if not isinstance(payload, dict):
                raise ValueError(f"Source line {line_number} must contain a JSON object")
            _validate_source_case(payload, line_number=line_number)
            case_id_hash = _required_hash(
                payload.get("ticket_id_hash"),
                field="ticket_id_hash",
                location=f"source line {line_number}",
            )
            if case_id_hash in cases:
                raise ValueError(f"Duplicate source ticket_id_hash at line {line_number}")
            cases[case_id_hash] = payload

    if not cases:
        raise ValueError("Private calibration input is empty")
    return cases


def _validate_source_case(payload: dict[str, Any], *, line_number: int) -> None:
    location = f"source line {line_number}"
    if payload.get("split") != "calibration":
        raise ValueError(f"{location} is not in the calibration split")

    label_status = str(payload.get("label_status") or "").strip()
    if label_status not in _ALLOWED_SOURCE_LABEL_STATUSES:
        raise ValueError(f"{location} has an unsupported label_status")

    for flag in _REQUIRED_SAFETY_FLAGS:
        if flag not in payload:
            raise ValueError(f"{location} is missing required safety flag {flag!r}")
        value = payload[flag]
        if not isinstance(value, bool):
            raise ValueError(f"{location} safety flag {flag!r} must be boolean")
        if value:
            raise ValueError(f"{location} has unsafe safety flag {flag!r}=true")

    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"{location} has no private source query")
    if len(query) > 4000:
        raise ValueError(f"{location} query exceeds the /ask input limit")


def _read_manifest(path: Path) -> tuple[list[dict[str, str]], str]:
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Could not read UTF-8 review manifest: {path}") from exc
    with io.StringIO(text, newline="") as manifest_file:
        reader = csv.DictReader(manifest_file)
        fields = set(reader.fieldnames or [])
        missing_fields = sorted(_REQUIRED_MANIFEST_FIELDS - fields)
        if missing_fields:
            raise ValueError(
                "Review manifest is missing required fields: "
                + ", ".join(missing_fields)
            )
        return [dict(row) for row in reader], hashlib.sha256(payload).hexdigest()


def _read_kb_seed(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    try:
        raw_payload = path.read_bytes()
        payload = json.loads(raw_payload.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read valid knowledge base seed: {path}") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError("Knowledge base seed must be a non-empty JSON array")

    chunks: dict[str, dict[str, Any]] = {}
    for index, raw_chunk in enumerate(payload, start=1):
        if not isinstance(raw_chunk, dict):
            raise ValueError(f"Knowledge base seed item {index} must be an object")
        chunk_id = str(raw_chunk.get("chunk_id") or "").strip()
        if not _SAFE_IDENTIFIER_RE.fullmatch(chunk_id):
            raise ValueError(f"Knowledge base seed item {index} has invalid chunk_id")
        if chunk_id in chunks:
            raise ValueError(f"Knowledge base seed has duplicate chunk_id {chunk_id!r}")
        chunks[chunk_id] = raw_chunk
    canonical_payload = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return chunks, hashlib.sha256(canonical_payload).hexdigest()


def _build_export_case(
    case_id_hash: str,
    row: dict[str, str],
    source: dict[str, Any],
    *,
    seed_chunks: dict[str, dict[str, Any]],
    manifest_sha256: str,
    kb_seed_sha256: str,
) -> dict[str, Any]:
    source_schema_version = _required_metadata(
        source.get("schema_version"),
        field="source schema_version",
        case_id_hash=case_id_hash,
    )
    case_fingerprint = source_case_fingerprint(source)
    reviewer = _required_metadata(
        row.get("reviewer"),
        field="reviewer",
        case_id_hash=case_id_hash,
    )
    reviewed_at = _required_reviewed_at(
        row.get("reviewed_at"),
        case_id_hash=case_id_hash,
    )
    role_status = _validated_role_status(
        row,
        source,
        case_id_hash=case_id_hash,
    )

    expected_behavior = str(row.get("expected_route") or "").strip().casefold()
    if expected_behavior not in _ALLOWED_BEHAVIORS:
        raise ValueError(f"Approved case {case_id_hash} has invalid expected_route")

    intent = _required_metadata(
        row.get("corrected_intent") or row.get("intent"),
        field="intent",
        case_id_hash=case_id_hash,
    )
    expected_profile = _required_metadata(
        row.get("corrected_aspect") or row.get("aspect"),
        field="expected_response_profile",
        case_id_hash=case_id_hash,
    )
    if expected_profile not in _RESPONSE_PROFILES:
        raise ValueError(
            f"Approved case {case_id_hash} has an unknown response profile"
        )
    entity_class = _required_metadata(
        row.get("corrected_entity_class")
        or row.get("corrected_entity")
        or row.get("entity_class"),
        field="entity_class",
        case_id_hash=case_id_hash,
    )

    approved_chunk_ids = _pipe_separated_identifiers(
        row.get("approved_chunk_ids"),
        field="approved_chunk_ids",
        case_id_hash=case_id_hash,
    )
    if expected_behavior == "answer":
        if not approved_chunk_ids:
            raise ValueError(
                f"Approved answer case {case_id_hash} has no approved_chunk_ids"
            )
        for chunk_id in approved_chunk_ids:
            chunk = seed_chunks.get(chunk_id)
            if chunk is None:
                raise ValueError(
                    f"Approved answer case {case_id_hash} references missing "
                    f"KB chunk {chunk_id!r}"
                )
            if chunk.get("status") != "published":
                raise ValueError(
                    f"Approved answer case {case_id_hash} references unpublished "
                    f"KB chunk {chunk_id!r}"
                )
            if chunk.get("source_type") != "yonote":
                raise ValueError(
                    f"Approved answer case {case_id_hash} references non-Yonote "
                    f"KB chunk {chunk_id!r}"
                )
    elif approved_chunk_ids:
        raise ValueError(
            f"Approved {expected_behavior} case {case_id_hash} must not have "
            "approved_chunk_ids"
        )

    forbidden_profiles = _pipe_separated_profiles(
        row.get("forbidden_profiles"),
        case_id_hash=case_id_hash,
    )
    if expected_profile in forbidden_profiles:
        raise ValueError(
            f"Approved case {case_id_hash} forbids its expected response profile"
        )

    expected_escalated = expected_behavior == "escalate"
    expected_escalation_reason: str | None = None
    if expected_escalated:
        expected_escalation_reason = _required_metadata(
            row.get("expected_escalation_reason"),
            field="expected_escalation_reason",
            case_id_hash=case_id_hash,
        )
        if expected_escalation_reason == "unknown":
            raise ValueError(
                f"Approved case {case_id_hash} has unknown escalation reason"
            )
    elif str(row.get("expected_escalation_reason") or "").strip():
        raise ValueError(
            f"Approved case {case_id_hash} has escalation reason for a non-escalation"
        )

    time_sensitive = _required_csv_bool(
        row.get("time_sensitive"),
        field="time_sensitive",
        case_id_hash=case_id_hash,
    )
    answerable_from_snapshot = _optional_csv_bool(
        row.get("answerable_from_snapshot"),
        field="answerable_from_snapshot",
        location=f"approved case {case_id_hash}",
    )
    if answerable_from_snapshot is None:
        answerable_from_snapshot = _optional_source_bool(
            source.get("answerable_from_snapshot"),
            field="answerable_from_snapshot",
            case_id_hash=case_id_hash,
        )
    if expected_behavior == "answer" and answerable_from_snapshot is not True:
        raise ValueError(
            f"Approved answer case {case_id_hash} must have "
            "answerable_from_snapshot=true"
        )

    source_channel = _required_metadata(
        source.get("channel"),
        field="source_channel",
        case_id_hash=case_id_hash,
    )
    multiturn_status = _required_metadata(
        row.get("multiturn_status"),
        field="multiturn_status",
        case_id_hash=case_id_hash,
    )
    source_label_status = str(source["label_status"])
    tags = {
        "label_verdict:approved",
        "split:calibration",
        f"intent:{intent}",
        f"profile:{expected_profile}",
        f"entity_class:{entity_class}",
        f"role_status:{role_status}",
        "role_verdict:confirmed_user_turn",
        f"multiturn_status:{multiturn_status}",
        f"source_channel:{source_channel}",
        f"source_label_status:{source_label_status}",
    }
    tags.update(f"forbidden_profile:{profile}" for profile in forbidden_profiles)

    return {
        "id": case_id_hash,
        "case_id_hash": case_id_hash,
        "query": str(source["query"]).strip(),
        "user_id": f"reviewed-calibration-{case_id_hash}",
        "channel": "api",
        "split": "calibration",
        "privacy_class": "private_ticket_derived",
        "label_status": "human_reviewed",
        "requires_human_review": False,
        "intent": intent,
        "entity_class": entity_class,
        "expected_response_profile": expected_profile,
        "expected_behavior": expected_behavior,
        "expected_escalated": expected_escalated,
        "expected_escalation_reason": expected_escalation_reason,
        "expected_chunk_ids": list(approved_chunk_ids),
        "expected_cited_chunk_ids": list(approved_chunk_ids),
        "approved_chunk_ids": list(approved_chunk_ids),
        "forbidden_response_profiles": list(forbidden_profiles),
        "time_sensitive": time_sensitive,
        "answerable_from_snapshot": answerable_from_snapshot,
        "role_reconstruction_status": role_status,
        "role_verdict": "confirmed_user_turn",
        "multiturn_status": multiturn_status,
        "source_channel": source_channel,
        "source_label_status": source_label_status,
        "source_provenance": {
            "schema_version": source_schema_version,
            "case_fingerprint": case_fingerprint,
            "fingerprint_algorithm": "sha256",
        },
        "review_provenance": {
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "manifest_sha256": manifest_sha256,
        },
        "knowledge_provenance": {
            "seed_sha256": kb_seed_sha256,
            "approved_seed_sha256": str(
                row["approved_kb_seed_sha256"]
            ).strip(),
            "hash_canonicalization": KB_SEED_HASH_CANONICALIZATION,
            "status": "published",
            "source_type": "yonote",
        },
        "tags": sorted(tags),
    }


def _validate_paths(
    input_path: Path,
    manifest_path: Path,
    output_path: Path,
    kb_seed_path: Path,
) -> tuple[Path, Path, Path, Path]:
    source = input_path.expanduser().resolve()
    manifest = manifest_path.expanduser().resolve()
    output = output_path.expanduser().resolve()
    seed = _validate_versioned_seed_path(kb_seed_path)
    if source.parent != manifest.parent or source.parent != output.parent:
        raise ValueError("Input, manifest and output must be adjacent in one private directory")
    if not all(
        path.is_relative_to(PRIVATE_DATA_ROOT)
        for path in (source, manifest, output)
    ):
        raise ValueError(
            "Input, manifest and output must stay under the project data/private"
        )
    if output.suffix.casefold() != ".json":
        raise ValueError("Output must be a .json file")
    if output in {source, manifest, seed}:
        raise ValueError("Output must not overwrite an input file")
    return source, manifest, output, seed


def _validate_versioned_seed_path(kb_seed_path: Path) -> Path:
    seed = kb_seed_path.expanduser().resolve()
    if seed.suffix.casefold() != ".json":
        raise ValueError("Knowledge base seed must be a .json file")
    if not seed.is_relative_to(VERSIONED_DATA_ROOT):
        raise ValueError(
            "Knowledge base seed must stay under the project versioned data root"
        )
    if seed.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("Knowledge base seed must stay outside data/private")
    return seed


def _required_hash(value: Any, *, field: str, location: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_HASH_RE.fullmatch(text):
        raise ValueError(f"{location} has invalid {field}")
    return text


def _required_sha256(value: Any, *, field: str, case_id_hash: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_SHA256_RE.fullmatch(text):
        raise ValueError(f"Approved case {case_id_hash} has invalid {field}")
    return text


def _required_metadata(value: Any, *, field: str, case_id_hash: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 200 or any(ord(character) < 32 for character in text):
        raise ValueError(f"Approved case {case_id_hash} has invalid {field}")
    return text


def _validated_role_status(
    row: dict[str, str],
    source: dict[str, Any],
    *,
    case_id_hash: str,
) -> str:
    role_verdict = str(row.get("role_verdict") or "").strip()
    if role_verdict != "confirmed_user_turn":
        raise ValueError(
            f"Approved case {case_id_hash} must have "
            "role_verdict=confirmed_user_turn"
        )
    source_role_status = _required_metadata(
        source.get("role_reconstruction_status"),
        field="source role_reconstruction_status",
        case_id_hash=case_id_hash,
    )
    manifest_role_status = _required_metadata(
        row.get("role_reconstruction_status"),
        field="role_reconstruction_status",
        case_id_hash=case_id_hash,
    )
    if manifest_role_status != source_role_status:
        raise ValueError(
            f"Approved case {case_id_hash} has stale role_reconstruction_status"
        )
    if source_role_status not in {"complete", "partial"}:
        raise ValueError(
            f"Approved case {case_id_hash} has unsafe source "
            "role_reconstruction_status"
        )
    return source_role_status


def _required_reviewed_at(value: Any, *, case_id_hash: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Approved case {case_id_hash} has no reviewed_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"Approved case {case_id_hash} has invalid reviewed_at"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"Approved case {case_id_hash} reviewed_at must include a timezone"
        )
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_csv_bool(value: Any, *, field: str, location: str) -> bool | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    raise ValueError(f"{location} has invalid boolean {field}")


def _required_csv_bool(value: Any, *, field: str, case_id_hash: str) -> bool:
    parsed = _optional_csv_bool(
        value,
        field=field,
        location=f"approved case {case_id_hash}",
    )
    if parsed is None:
        raise ValueError(f"Approved case {case_id_hash} is missing boolean {field}")
    return parsed


def _optional_source_bool(
    value: Any,
    *,
    field: str,
    case_id_hash: str,
) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"Source case {case_id_hash} has invalid boolean {field}")
    return value


def _pipe_separated_identifiers(
    value: Any,
    *,
    field: str,
    case_id_hash: str,
) -> tuple[str, ...]:
    values = _pipe_separated_values(value)
    for item in values:
        if not _SAFE_IDENTIFIER_RE.fullmatch(item):
            raise ValueError(f"Approved case {case_id_hash} has invalid {field}")
    return tuple(sorted(set(values)))


def _pipe_separated_profiles(
    value: Any,
    *,
    case_id_hash: str,
) -> tuple[str, ...]:
    profiles = _pipe_separated_values(value)
    unknown = sorted(set(profiles) - _RESPONSE_PROFILES)
    if unknown:
        raise ValueError(
            f"Approved case {case_id_hash} has unknown forbidden response profiles"
        )
    return tuple(sorted(set(profiles)))


def _pipe_separated_values(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    values = [item.strip() for item in text.split("|")]
    if any(not item for item in values):
        raise ValueError("Pipe-separated manifest values must not contain empty items")
    return values


def _write_json_array(path: Path, payload: list[dict[str, Any]], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise ValueError(f"Output already exists: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        if path.exists() and not overwrite:
            raise ValueError(f"Output already exists: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export human-approved private ticket cases for local /ask evaluation."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--kb-seed", type=Path, default=DEFAULT_KB_SEED)
    parser.add_argument("--print-kb-seed-sha256", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.print_kb_seed_sha256:
        seed_path = _validate_versioned_seed_path(args.kb_seed)
        seed_chunks, seed_sha256 = _read_kb_seed(seed_path)
        print(
            json.dumps(
                {
                    "kb_seed_path": str(seed_path),
                    "kb_seed_sha256": seed_sha256,
                    "hash_canonicalization": KB_SEED_HASH_CANONICALIZATION,
                    "published_yonote_chunks": sum(
                        chunk.get("status") == "published"
                        and chunk.get("source_type") == "yonote"
                        for chunk in seed_chunks.values()
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    output = args.output or args.input.with_name(DEFAULT_OUTPUT_NAME)
    stats = build_reviewed_ticket_ask_cases(
        args.input,
        args.manifest,
        output,
        args.kb_seed,
        overwrite=args.overwrite,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
