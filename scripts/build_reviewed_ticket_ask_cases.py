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

from eval.run_ask import holdout_cases_payload_sha256  # noqa: E402
from scripts.build_ticket_product_review import source_case_fingerprint  # noqa: E402
from scripts.import_ticket_holdout_review_workbook import (  # noqa: E402
    load_holdout_review_workbook_rows,
)
from src.response_contract import ResponseProfileName  # noqa: E402
from src.security.pii_masker import PIIMasker, PIIMaskingUnavailable  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSIONED_DATA_ROOT = (PROJECT_ROOT / "data").resolve()
PRIVATE_DATA_ROOT = (VERSIONED_DATA_ROOT / "private").resolve()

DEFAULT_INPUT = Path(
    "data/private/tickets/product_baseline_20260728/product_calibration_cases.jsonl"
)
DEFAULT_MANIFEST = DEFAULT_INPUT.with_name("top20_review_manifest.csv")
DEFAULT_OUTPUT_NAME = "product_calibration_reviewed_ask_cases.json"
DEFAULT_KB_SEED = Path("data/knowledge_base_seed.json")
DEFAULT_SPLIT = "calibration"
KB_SEED_HASH_CANONICALIZATION = "json_sort_keys_compact_utf8_v1"

_SAFE_HASH_RE = re.compile(r"^[0-9a-f]{12,64}$")
_SAFE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,200}$")
_SAFE_BASELINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_REVIEWER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
_RESIDUAL_PII_PATTERNS = (
    re.compile(r"(?<![\w@])@[A-Za-zА-Яа-яЁё0-9_.-]{2,64}\b"),
    re.compile(
        r"(?i)\b(?:https?://)?(?:www\.)?"
        r"(?:vk\.com|m\.vk\.com|t\.me|ok\.ru|instagram\.com)/\S+"
    ),
    re.compile(
        r"(?i)\b(?:vk|вк|telegram|телеграм|social)"
        r"(?:\s+|[:=_-])+(?:id\s*)?\d{3,}\b"
    ),
    re.compile(
        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
    ),
    re.compile(
        r"(?i)\b(?:заявк\w*|аккаунт\w*|профил\w*|пользовател\w*|"
        r"лицев\w*\s+сч[её]т\w*)"
        r"\s*(?:№|номер|id|[:#=-])\s*[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9_-]{5,}\b"
    ),
    re.compile(
        r"(?i)\b(?:заявк\w*|аккаунт\w*|профил\w*|пользовател\w*|"
        r"лицев\w*\s+сч[её]т\w*)\s+"
        r"(?=[A-ZА-ЯЁ0-9_-]{6,}\b)(?=[A-ZА-ЯЁ0-9_-]*\d)"
        r"[A-ZА-ЯЁ0-9_-]{6,}\b"
    ),
    re.compile(
        r"(?i)\b(?:id|uid|user_id)[:#=_-]?[A-Z0-9][A-Z0-9_-]{5,}\b"
    ),
    re.compile(
        r"(?i)\b(?:дата\s+рождения|день\s+рождения|"
        r"родил(?:ся|ась)|д\.?\s*р\.?)"
        r"\s*[:=-]?\s*\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b"
    ),
    re.compile(
        r"(?i)\b(?:мой\s+)?(?:день|дата)\s+рождения\b"
        r"\s*[:=-]?\s*\d{1,2}\s+"
        r"(?:января|февраля|марта|апреля|мая|июня|июля|августа|"
        r"сентября|октября|ноября|декабря)"
        r"(?:\s+\d{4}(?:\s+года)?)?\b"
    ),
    re.compile(
        r"(?i)\bмне\s+\d{1,3}\s+лет\b"
        r"[^0-9\n]{0,30}\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b"
    ),
    re.compile(
        r"(?i)\b(?:адрес|улица|ул\.|проспект|пр-т|переулок|дом|д\.)"
        r"\s*[:=-]?\s+[^,\n]{0,60}\d+[A-Za-zА-Яа-яЁё]?\b"
    ),
    re.compile(
        r"(?i)\b(?:г\.?|город)\s+[А-ЯЁ][А-Яа-яЁё -]{1,60},"
        r"\s*[А-ЯЁ][А-Яа-яЁё -]{1,60}\s+\d+[A-Za-zА-Яа-яЁё]?\b"
    ),
    re.compile(r"(?i)\b(?:мой\s+адрес|живу\s+по\s+адресу)\b"),
    re.compile(r"(?<!\d)\d{6,}(?!\d)"),
)
_DATE_PATTERN = re.compile(
    r"(?i)(?:\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|"
    r"\b20\d{2}-\d{2}-\d{2}\b|"
    r"\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|"
    r"июля|августа|сентября|октября|ноября|декабря)"
    r"(?:\s+20\d{2}(?:\s+года)?)?\b)"
)
_ALLOWED_SOURCE_LABEL_STATUSES = frozenset({"human_reviewed", "weak_unreviewed"})
_ALLOWED_BEHAVIORS = frozenset({"answer", "clarify", "escalate"})
_ALLOWED_SPLITS = frozenset({"calibration", "validation", "holdout"})
_INCLUDE_FIELD_BY_SPLIT = {
    "calibration": "include_in_calibration",
    "validation": "include_in_validation",
    "holdout": "include_in_holdout",
}
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
    }
)
_HOLDOUT_MANIFEST_FIELDS = frozenset(
    {
        "corrected_route",
        "corrected_escalation_reason",
        "deidentified_query",
        "privacy_verdict",
        "date_privacy_verdict",
        "review_workbook_sha256",
        "review_source_sha256",
        "review_selection_sha256",
        "review_freeze_contract_sha256",
        "review_payload_sha256",
        "include_in_holdout",
    }
)
_RESPONSE_PROFILES = frozenset(profile.value for profile in ResponseProfileName)
_FREEZE_HASH_SCOPE = "canonical_json_without_freeze_contract_sha256_fields"
_HOLDOUT_REVIEW_PAYLOAD_FIELDS = (
    "case_id_hash",
    "intent",
    "aspect",
    "entity_class",
    "expected_route",
    "expected_escalation_reason",
    "time_sensitive",
    "role_reconstruction_status",
    "multiturn_status",
    "role_verdict",
    "label_verdict",
    "reviewer",
    "reviewed_at",
    "source_schema_version",
    "source_case_fingerprint",
    "approved_kb_seed_sha256",
    "corrected_intent",
    "corrected_aspect",
    "corrected_entity_class",
    "corrected_route",
    "corrected_escalation_reason",
    "deidentified_query",
    "privacy_verdict",
    "date_privacy_verdict",
    "review_workbook_sha256",
    "review_source_sha256",
    "review_selection_sha256",
    "review_freeze_contract_sha256",
    "answerable_from_snapshot",
    "approved_chunk_ids",
    "forbidden_profiles",
    "include_in_holdout",
)
_CASE_ID_DIGEST_CANONICALIZATION = "sorted_case_ids_newline_terminated_utf8_v1"
_EXPECTED_HOLDOUT_CASES_TOTAL = 80
_HOLDOUT_MEASUREMENT_SCOPE = (
    "independent_directional_single_turn_first_response_holdout; "
    "not ticket conversion and not a final 50-60% conversion claim"
)
_FROZEN_SELECTION_FIELDS = (
    "case_id_hash",
    "duplicate_cluster_id",
    "source_schema_version",
    "source_case_fingerprint",
    "stratum_rank",
    "intent",
    "aspect",
    "entity_class",
    "channel",
    "time_bucket",
    "expected_route",
    "expected_escalation_reason",
    "time_sensitive",
    "difficulty",
    "role_reconstruction_status",
    "multiturn_status",
)


def build_reviewed_ticket_ask_cases(
    input_path: Path,
    manifest_path: Path,
    output_path: Path,
    kb_seed_path: Path = DEFAULT_KB_SEED,
    *,
    split: str = DEFAULT_SPLIT,
    freeze_path: Path | None = None,
    review_workbook_path: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    if split not in _ALLOWED_SPLITS:
        raise ValueError(f"Unsupported product split: {split!r}")
    source_path, review_path, destination, seed_path = _validate_paths(
        input_path,
        manifest_path,
        output_path,
        kb_seed_path,
    )
    if destination.exists() and not overwrite:
        raise ValueError(f"Output already exists: {destination}")

    source_cases = _read_source_cases(source_path, expected_split=split)
    manifest_rows, manifest_sha256 = _read_manifest(
        review_path,
        split=split,
    )
    seed_chunks, kb_seed_sha256 = _read_kb_seed(seed_path)
    freeze_payload: dict[str, Any] | None = None
    holdout_contract: dict[str, Any] | None = None
    review_receipt: dict[str, str] | None = None
    holdout_label_margins: dict[str, Any] | None = None
    pii_masker: PIIMasker | None = None
    if split == "holdout":
        if freeze_path is None:
            raise ValueError("Holdout export requires --freeze")
        if review_workbook_path is None:
            raise ValueError("Holdout export requires --review-workbook")
        freeze = _validate_freeze_path(
            freeze_path,
            adjacent_to=review_path.parent,
        )
        review_workbook = _validate_review_workbook_path(review_workbook_path)
        freeze_payload = _validated_freeze_contract(
            freeze,
            source_path=source_path,
            source_cases=source_cases,
            manifest_rows=manifest_rows,
            kb_seed_sha256=kb_seed_sha256,
            review_workbook_path=review_workbook,
            review_manifest_path=review_path,
            output_path=destination,
        )
        selection_path = _validated_private_evidence_file(
            freeze_payload["selection"],
            field="selection",
            suffix=".csv",
        )
        expected_fields, workbook_rows = load_holdout_review_workbook_rows(
            workbook_path=review_workbook,
            selection_path=selection_path,
            source_path=source_path,
            freeze_path=freeze,
            expected_total=int(freeze_payload["cases_total"]),
        )
        _validated_manifest_matches_filled_workbook(
            manifest_rows,
            expected_fields=expected_fields,
            workbook_rows=workbook_rows,
        )
        review_receipt = _holdout_review_receipt(
            freeze_payload,
            review_workbook_path=review_workbook,
        )
        holdout_label_margins = {
            "pre_review": {
                "profile_counts": dict(freeze_payload["profile_counts"]),
                "route_counts": dict(freeze_payload["route_counts"]),
            },
            "reviewed": _reviewed_holdout_margins(manifest_rows),
        }
        pii_masker = PIIMasker()
    elif freeze_path is not None or review_workbook_path is not None:
        raise ValueError(
            "--freeze and --review-workbook are only valid for the holdout split"
        )
    include_field = _INCLUDE_FIELD_BY_SPLIT[split]

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
            row.get(include_field),
            field=include_field,
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
        if split == "holdout" and (
            verdict != "approved" or include is not True
        ):
            raise ValueError(
                "Every frozen holdout case must be approved and included"
            )
        if verdict == "approved":
            approved_rows += 1
            if include is None:
                raise ValueError(
                    f"Approved case {case_id_hash} is missing boolean "
                    f"{include_field}"
                )
            _required_reviewer(
                row.get("reviewer"),
                case_id_hash=case_id_hash,
            )
            _required_reviewed_at(row.get("reviewed_at"), case_id_hash=case_id_hash)
            if split == "holdout":
                assert review_receipt is not None
                _validated_holdout_review_receipt(
                    row,
                    case_id_hash=case_id_hash,
                    expected=review_receipt,
                )
                _validated_holdout_review_payload(
                    row,
                    case_id_hash=case_id_hash,
                )
                assert pii_masker is not None
                _validated_holdout_privacy(
                    row,
                    case_id_hash=case_id_hash,
                    pii_masker=pii_masker,
                )
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
                split=split,
            )
            if include is True:
                selected.append((case_id_hash, row, source))
                exported.append(export_case)

    if not selected:
        raise ValueError(
            "Manifest contains no rows with label_verdict='approved' and "
            f"{include_field}=true"
        )

    exported.sort(key=lambda item: str(item["case_id_hash"]))
    if freeze_payload is not None:
        exported_ids = [str(item["case_id_hash"]) for item in exported]
        if len(exported_ids) != freeze_payload["cases_total"]:
            raise ValueError("Holdout export count differs from the freeze contract")
        if (
            _selected_case_ids_sha256(exported_ids)
            != freeze_payload["selection"]["selected_case_ids_sha256"]
        ):
            raise ValueError("Holdout export IDs differ from the freeze contract")
        assert review_receipt is not None
        cases_payload_sha256 = holdout_cases_payload_sha256(exported)
        holdout_contract = _export_holdout_contract(
            freeze_payload,
            review_manifest_sha256=manifest_sha256,
            review_receipt=review_receipt,
            knowledge_base_seed_sha256=kb_seed_sha256,
            cases_payload_sha256=cases_payload_sha256,
        )
        for item in exported:
            item["holdout_contract"] = dict(holdout_contract)
    _write_json_array(destination, exported, overwrite=overwrite)
    cases_payload_sha256 = holdout_cases_payload_sha256(exported)
    output_file_sha256 = _file_sha256(destination)

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
            bool(str(row.get("corrected_entity_class") or "").strip())
            for _, row, _ in selected
        ),
        "behavior_counts": dict(sorted(behavior_counts.items())),
        "profile_counts": dict(sorted(profile_counts.items())),
        "cases_payload_sha256": cases_payload_sha256,
        "output_file_sha256": output_file_sha256,
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
            "split": split,
            **(
                {"holdout_contract": holdout_contract}
                if holdout_contract is not None
                else {}
            ),
            **(
                {"holdout_label_margins": holdout_label_margins}
                if holdout_label_margins is not None
                else {}
            ),
            "published_yonote_chunks": sum(
                chunk.get("status") == "published"
                and chunk.get("source_type") == "yonote"
                for chunk in seed_chunks.values()
            ),
        },
        "output": str(destination),
    }


def _read_source_cases(
    path: Path,
    *,
    expected_split: str,
) -> dict[str, dict[str, Any]]:
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
            _validate_source_case(
                payload,
                line_number=line_number,
                expected_split=expected_split,
            )
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


def _validate_source_case(
    payload: dict[str, Any],
    *,
    line_number: int,
    expected_split: str,
) -> None:
    location = f"source line {line_number}"
    if payload.get("split") != expected_split:
        raise ValueError(f"{location} is not in the {expected_split} split")

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


def _read_manifest(
    path: Path,
    *,
    split: str,
) -> tuple[list[dict[str, str]], str]:
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Could not read UTF-8 review manifest: {path}") from exc
    with io.StringIO(text, newline="") as manifest_file:
        reader = csv.DictReader(manifest_file)
        fields = set(reader.fieldnames or [])
        required_fields = set(_REQUIRED_MANIFEST_FIELDS)
        required_fields.add(_INCLUDE_FIELD_BY_SPLIT[split])
        if split == "holdout":
            required_fields.update(_HOLDOUT_MANIFEST_FIELDS)
        missing_fields = sorted(required_fields - fields)
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
    split: str,
) -> dict[str, Any]:
    source_schema_version = _required_metadata(
        source.get("schema_version"),
        field="source schema_version",
        case_id_hash=case_id_hash,
    )
    case_fingerprint = source_case_fingerprint(source)
    reviewer = _required_reviewer(
        row.get("reviewer"),
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

    corrected_route = str(row.get("corrected_route") or "").strip().casefold()
    expected_behavior = str(
        corrected_route or row.get("expected_route") or ""
    ).strip().casefold()
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
    corrected_escalation_reason = str(
        row.get("corrected_escalation_reason") or ""
    ).strip()
    if corrected_route and expected_behavior != "escalate":
        escalation_reason_value = corrected_escalation_reason or None
    else:
        escalation_reason_value = (
            corrected_escalation_reason
            or row.get("expected_escalation_reason")
        )
    if expected_escalated:
        expected_escalation_reason = _required_metadata(
            escalation_reason_value,
            field="expected_escalation_reason",
            case_id_hash=case_id_hash,
        )
        if expected_escalation_reason == "unknown":
            raise ValueError(
                f"Approved case {case_id_hash} has unknown escalation reason"
            )
    elif str(escalation_reason_value or "").strip():
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
        f"split:{split}",
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

    query = (
        _validated_holdout_query(row, case_id_hash=case_id_hash)
        if split == "holdout"
        else str(source["query"]).strip()
    )

    export_case = {
        "id": case_id_hash,
        "case_id_hash": case_id_hash,
        "query": query,
        "user_id": f"reviewed-{split}-{case_id_hash}",
        "channel": "api",
        "split": split,
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
        "allowed_cited_source_types": ["yonote"],
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
            **(
                {
                    "privacy_verdict": "approved",
                    "date_privacy_verdict": str(
                        row["date_privacy_verdict"]
                    ).strip().casefold(),
                    "review_payload_sha256": str(
                        row["review_payload_sha256"]
                    ).strip(),
                }
                if split == "holdout"
                else {}
            ),
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
    return export_case


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
    if manifest.parent != output.parent:
        raise ValueError(
            "Review manifest and output must be adjacent in one private directory"
        )
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


def _validate_freeze_path(path: Path, *, adjacent_to: Path) -> Path:
    freeze = path.expanduser().resolve()
    if freeze.suffix.casefold() != ".json":
        raise ValueError("Holdout freeze must be a .json file")
    if freeze.parent != adjacent_to.resolve():
        raise ValueError(
            "Holdout freeze must be adjacent to the review manifest and output"
        )
    if not freeze.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("Holdout freeze must stay under data/private")
    if not freeze.is_file():
        raise ValueError(f"Holdout freeze does not exist: {freeze}")
    return freeze


def _validate_review_workbook_path(path: Path) -> Path:
    workbook = path.expanduser().resolve()
    if workbook.suffix.casefold() != ".xlsx":
        raise ValueError("Holdout review workbook must be an .xlsx file")
    if not workbook.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("Holdout review workbook must stay under data/private")
    if not workbook.is_file():
        raise ValueError(f"Holdout review workbook does not exist: {workbook}")
    return workbook


def _read_freeze_contract(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read valid holdout freeze: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Holdout freeze must be a JSON object")

    claimed_sha256 = _required_contract_sha256(
        payload.get("freeze_contract_sha256"),
        field="freeze_contract_sha256",
    )
    if payload.get("freeze_contract_hash_scope") != _FREEZE_HASH_SCOPE:
        raise ValueError("Holdout freeze has an unsupported hash scope")
    hash_payload = dict(payload)
    hash_payload.pop("freeze_contract_sha256", None)
    hash_payload.pop("freeze_contract_hash_scope", None)
    actual_sha256 = hashlib.sha256(
        json.dumps(
            hash_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if actual_sha256 != claimed_sha256:
        raise ValueError("Holdout freeze self-hash mismatch")

    if payload.get("schema_version") != "1.0.0":
        raise ValueError("Holdout freeze has an unsupported schema_version")
    baseline_id = str(payload.get("baseline_id") or "").strip()
    if not _SAFE_BASELINE_ID_RE.fullmatch(baseline_id):
        raise ValueError("Holdout freeze has an invalid baseline_id")
    runtime_git_sha = str(payload.get("runtime_git_sha") or "").strip()
    if not _GIT_SHA_RE.fullmatch(runtime_git_sha):
        raise ValueError("Holdout freeze has an invalid runtime_git_sha")
    if payload.get("selection_status") != "sealed_pending_human_review":
        raise ValueError("Holdout freeze is not pending human review")
    if payload.get("execution_allowed") is not False:
        raise ValueError("Holdout freeze must have execution_allowed=false")
    if payload.get("cases_total") != _EXPECTED_HOLDOUT_CASES_TOTAL:
        raise ValueError(
            "Holdout freeze must contain exactly "
            f"{_EXPECTED_HOLDOUT_CASES_TOTAL} cases"
        )
    if payload.get("unique_case_ids") != _EXPECTED_HOLDOUT_CASES_TOTAL:
        raise ValueError("Holdout freeze unique case count mismatch")
    if payload.get("measurement_scope") != _HOLDOUT_MEASUREMENT_SCOPE:
        raise ValueError(
            "Holdout freeze must bind the single-turn first-response scope"
        )
    if payload.get("multiturn_status_counts") != {
        "single_turn": _EXPECTED_HOLDOUT_CASES_TOTAL
    }:
        raise ValueError(
            "Holdout freeze must contain only single_turn cases"
        )
    if payload.get("comparison_splits") != ["calibration", "validation"]:
        raise ValueError(
            "Holdout freeze must bind calibration and validation comparisons"
        )
    overlap = payload.get("cross_split_overlap")
    if not isinstance(overlap, dict) or any(
        overlap.get(field) != 0
        for field in (
            "case_ids",
            "duplicate_clusters",
            "duplicate_components",
        )
    ):
        raise ValueError("Holdout freeze has cross-split overlap")

    selection = payload.get("selection")
    if not isinstance(selection, dict):
        raise ValueError("Holdout freeze has no selection evidence")
    raw_ids = selection.get("case_id_hashes")
    if not isinstance(raw_ids, list):
        raise ValueError("Holdout freeze has no selected case IDs")
    selected_ids = [
        _required_hash(
            value,
            field="case_id_hash",
            location="holdout freeze selection",
        )
        for value in raw_ids
    ]
    if (
        selected_ids != sorted(selected_ids)
        or len(selected_ids) != len(set(selected_ids))
        or len(selected_ids) != _EXPECTED_HOLDOUT_CASES_TOTAL
    ):
        raise ValueError("Holdout freeze selected case IDs are invalid")
    selected_ids_sha256 = _required_contract_sha256(
        selection.get("selected_case_ids_sha256"),
        field="selected_case_ids_sha256",
    )
    if selected_ids_sha256 != _selected_case_ids_sha256(selected_ids):
        raise ValueError("Holdout freeze selected case ID digest mismatch")
    _validated_count_mapping(
        payload.get("profile_counts"),
        field="profile_counts",
        allowed=_RESPONSE_PROFILES,
    )
    _validated_count_mapping(
        payload.get("route_counts"),
        field="route_counts",
        allowed=_ALLOWED_BEHAVIORS,
    )
    return payload


def _validated_freeze_contract(
    path: Path,
    *,
    source_path: Path,
    source_cases: dict[str, dict[str, Any]],
    manifest_rows: list[dict[str, str]],
    kb_seed_sha256: str,
    review_workbook_path: Path,
    review_manifest_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    payload = _read_freeze_contract(path)
    source_evidence = payload.get("source")
    if not isinstance(source_evidence, dict):
        raise ValueError("Holdout freeze has no source evidence")
    source_sha256 = _required_contract_sha256(
        source_evidence.get("sha256"),
        field="source.sha256",
    )
    if source_sha256 != _file_sha256(source_path):
        raise ValueError("Holdout source hash differs from the freeze contract")
    if source_evidence.get("cases_total") != len(source_cases):
        raise ValueError("Holdout source count differs from the freeze contract")

    manifest_ids = [
        _required_hash(
            row.get("case_id_hash"),
            field="case_id_hash",
            location=f"manifest row {row_number}",
        )
        for row_number, row in enumerate(manifest_rows, start=2)
    ]
    if len(manifest_ids) != len(set(manifest_ids)):
        raise ValueError("Holdout review manifest has duplicate case IDs")
    frozen_ids = payload["selection"]["case_id_hashes"]
    if sorted(manifest_ids) != frozen_ids:
        raise ValueError(
            "Holdout review manifest must contain the exact frozen case set"
        )
    if _selected_case_ids_sha256(manifest_ids) != payload["selection"][
        "selected_case_ids_sha256"
    ]:
        raise ValueError("Holdout review manifest case ID digest mismatch")

    selection_evidence = payload["selection"]
    selection_path = _validated_private_evidence_file(
        selection_evidence,
        field="selection",
        suffix=".csv",
    )
    if selection_path == source_path or selection_path == review_workbook_path:
        raise ValueError("Holdout freeze selection evidence aliases another input")
    _validated_review_manifest_derivation(
        manifest_rows,
        selection_path=selection_path,
    )

    template_evidence = payload.get("pre_run_review_workbook")
    if not isinstance(template_evidence, dict):
        raise ValueError("Holdout freeze has no review workbook template evidence")
    template_workbook = _validated_private_evidence_file(
        template_evidence,
        field="pre_run_review_workbook_template",
        suffix=".xlsx",
    )
    _validate_filled_workbook_aliases(
        review_workbook_path,
        forbidden_paths={
            template_workbook,
            source_path,
            selection_path,
            review_manifest_path,
            output_path,
        },
    )

    knowledge_evidence = payload.get("knowledge_snapshot")
    if not isinstance(knowledge_evidence, dict):
        raise ValueError("Holdout freeze has no knowledge snapshot")
    if knowledge_evidence.get("canonical_sha256") != kb_seed_sha256:
        raise ValueError("KB seed hash differs from the holdout freeze contract")
    return payload


def _export_holdout_contract(
    freeze_payload: dict[str, Any],
    *,
    review_manifest_sha256: str,
    review_receipt: dict[str, str],
    knowledge_base_seed_sha256: str,
    cases_payload_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "baseline_id": str(freeze_payload["baseline_id"]),
        "runtime_git_sha": str(freeze_payload["runtime_git_sha"]),
        "freeze_contract_sha256": str(
            freeze_payload["freeze_contract_sha256"]
        ),
        "review_manifest_sha256": review_manifest_sha256,
        "selection_manifest_sha256": review_receipt[
            "review_selection_sha256"
        ],
        "review_workbook_sha256": review_receipt[
            "review_workbook_sha256"
        ],
        "source_cases_sha256": review_receipt["review_source_sha256"],
        "knowledge_base_seed_sha256": knowledge_base_seed_sha256,
        "cases_payload_sha256": cases_payload_sha256,
        "selected_case_ids_sha256": str(
            freeze_payload["selection"]["selected_case_ids_sha256"]
        ),
        "cases_total": int(freeze_payload["cases_total"]),
        "execution_allowed": True,
    }


def _holdout_review_receipt(
    freeze_payload: dict[str, Any],
    *,
    review_workbook_path: Path,
) -> dict[str, str]:
    return {
        "review_workbook_sha256": _file_sha256(review_workbook_path),
        "review_source_sha256": _required_contract_sha256(
            freeze_payload["source"].get("sha256"),
            field="source.sha256",
        ),
        "review_selection_sha256": _required_contract_sha256(
            freeze_payload["selection"].get("sha256"),
            field="selection.sha256",
        ),
        "review_freeze_contract_sha256": _required_contract_sha256(
            freeze_payload.get("freeze_contract_sha256"),
            field="freeze_contract_sha256",
        ),
    }


def _validate_filled_workbook_aliases(
    review_workbook_path: Path,
    *,
    forbidden_paths: set[Path],
) -> None:
    resolved_forbidden = {path.resolve() for path in forbidden_paths}
    if review_workbook_path.resolve() in resolved_forbidden:
        raise ValueError(
            "Filled review workbook must be distinct from the frozen template "
            "and all source, selection, manifest, and output artifacts"
        )


def _validated_private_evidence_file(
    evidence: dict[str, Any],
    *,
    field: str,
    suffix: str,
) -> Path:
    raw_path = str(evidence.get("path") or "").strip()
    resolved = Path(raw_path).expanduser().resolve()
    if (
        not raw_path
        or resolved.suffix.casefold() != suffix
        or not resolved.is_relative_to(PRIVATE_DATA_ROOT)
        or not resolved.is_file()
    ):
        raise ValueError(f"Holdout freeze has invalid {field} path evidence")
    expected_sha256 = _required_contract_sha256(
        evidence.get("sha256"),
        field=f"{field}.sha256",
    )
    if _file_sha256(resolved) != expected_sha256:
        raise ValueError(f"Holdout freeze has stale {field} file evidence")
    return resolved


def _reviewed_holdout_margins(
    manifest_rows: list[dict[str, str]],
) -> dict[str, dict[str, int]]:
    profile_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    for row_number, row in enumerate(manifest_rows, start=2):
        profile = str(
            row.get("corrected_aspect") or row.get("aspect") or ""
        ).strip()
        route = str(
            row.get("corrected_route") or row.get("expected_route") or ""
        ).strip().casefold()
        if profile not in _RESPONSE_PROFILES:
            raise ValueError(
                f"Holdout manifest row {row_number} has invalid corrected profile"
            )
        if route not in _ALLOWED_BEHAVIORS:
            raise ValueError(
                f"Holdout manifest row {row_number} has invalid corrected route"
            )
        profile_counts[profile] += 1
        route_counts[route] += 1

    return {
        "profile_counts": dict(sorted(profile_counts.items())),
        "route_counts": dict(sorted(route_counts.items())),
    }


def _validated_count_mapping(
    value: Any,
    *,
    field: str,
    allowed: frozenset[str],
) -> dict[str, int]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"Holdout freeze has invalid {field}")
    result: dict[str, int] = {}
    for raw_key, raw_count in value.items():
        key = str(raw_key or "").strip()
        if (
            key not in allowed
            or isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count <= 0
        ):
            raise ValueError(f"Holdout freeze has invalid {field}")
        result[key] = raw_count
    if sum(result.values()) != _EXPECTED_HOLDOUT_CASES_TOTAL:
        raise ValueError(f"Holdout freeze {field} total is invalid")
    return dict(sorted(result.items()))


def _validated_review_manifest_derivation(
    manifest_rows: list[dict[str, str]],
    *,
    selection_path: Path,
) -> None:
    selection_rows, _ = _read_manifest(selection_path, split="holdout")
    selection_by_id = {
        str(row.get("case_id_hash") or "").strip(): row
        for row in selection_rows
    }
    if len(selection_by_id) != len(selection_rows):
        raise ValueError("Frozen selection contains duplicate case IDs")
    for row_number, row in enumerate(manifest_rows, start=2):
        case_id_hash = str(row.get("case_id_hash") or "").strip()
        selected = selection_by_id.get(case_id_hash)
        if selected is None:
            raise ValueError(
                f"Holdout manifest row {row_number} is outside the frozen selection"
            )
        changed = [
            field
            for field in _FROZEN_SELECTION_FIELDS
            if str(row.get(field) or "").strip()
            != str(selected.get(field) or "").strip()
        ]
        if changed:
            raise ValueError(
                f"Holdout manifest row {row_number} changed frozen selection "
                f"fields: {', '.join(changed)}"
            )


def holdout_review_payload_sha256(row: dict[str, Any]) -> str:
    """Hash the canonical human-reviewed values before holdout export."""

    payload = {
        field: _canonical_review_payload_value(field, row.get(field))
        for field in _HOLDOUT_REVIEW_PAYLOAD_FIELDS
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonical_review_payload_value(field: str, value: Any) -> str:
    text = str(value or "").strip()
    if field in {
        "time_sensitive",
        "answerable_from_snapshot",
        "include_in_holdout",
        "role_verdict",
        "label_verdict",
        "privacy_verdict",
        "date_privacy_verdict",
        "expected_route",
        "corrected_route",
    }:
        return text.casefold()
    if field in {"approved_chunk_ids", "forbidden_profiles"}:
        return "|".join(sorted(set(_pipe_separated_values(text))))
    if field == "reviewed_at" and text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return text


def _validated_holdout_review_payload(
    row: dict[str, str],
    *,
    case_id_hash: str,
) -> None:
    claimed = _required_sha256(
        row.get("review_payload_sha256"),
        field="review_payload_sha256",
        case_id_hash=case_id_hash,
    )
    if claimed != holdout_review_payload_sha256(row):
        raise ValueError(
            f"Approved holdout case {case_id_hash} has stale "
            "review_payload_sha256"
        )


def _validated_holdout_review_receipt(
    row: dict[str, str],
    *,
    case_id_hash: str,
    expected: dict[str, str],
) -> None:
    for field, expected_sha256 in expected.items():
        claimed = _required_sha256(
            row.get(field),
            field=field,
            case_id_hash=case_id_hash,
        )
        if claimed != expected_sha256:
            raise ValueError(
                f"Approved holdout case {case_id_hash} has stale {field}"
            )


def _selected_case_ids_sha256(case_ids: list[str]) -> str:
    return hashlib.sha256(
        ("\n".join(sorted(case_ids)) + "\n").encode("utf-8")
    ).hexdigest()


def _required_contract_sha256(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_SHA256_RE.fullmatch(text):
        raise ValueError(f"Holdout freeze has invalid {field}")
    return text


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _required_reviewer(value: Any, *, case_id_hash: str) -> str:
    reviewer = str(value or "").strip()
    if not _SAFE_REVIEWER_ID_RE.fullmatch(reviewer):
        raise ValueError(
            f"Approved case {case_id_hash} has invalid pseudonymous reviewer ID"
        )
    return reviewer


def _validated_holdout_privacy(
    row: dict[str, str],
    *,
    case_id_hash: str,
    pii_masker: PIIMasker,
) -> None:
    verdict = str(row.get("privacy_verdict") or "").strip().casefold()
    if verdict != "approved":
        raise ValueError(
            f"Approved holdout case {case_id_hash} must have "
            "privacy_verdict=approved"
        )
    query = _validated_holdout_query(row, case_id_hash=case_id_hash)
    try:
        masked_query, findings = pii_masker.mask(query)
    except PIIMaskingUnavailable as exc:
        raise ValueError(
            f"Approved holdout case {case_id_hash} could not be PII-scanned"
        ) from exc
    non_date_findings = {
        finding_type: values
        for finding_type, values in findings.items()
        if finding_type != "date" and values
    }
    only_date_was_masked = bool(findings.get("date")) and not non_date_findings
    has_explicit_residual = any(
        pattern.search(query) for pattern in _RESIDUAL_PII_PATTERNS
    )
    if (
        non_date_findings
        or has_explicit_residual
        or (masked_query != query and not only_date_was_masked)
    ):
        raise ValueError(
            f"Approved holdout case {case_id_hash} still contains PII"
        )
    has_date = bool(findings.get("date")) or bool(_DATE_PATTERN.search(query))
    date_verdict = str(
        row.get("date_privacy_verdict") or ""
    ).strip().casefold()
    expected_date_verdict = "event_date_only" if has_date else "not_present"
    if date_verdict != expected_date_verdict:
        raise ValueError(
            f"Approved holdout case {case_id_hash} must have "
            f"date_privacy_verdict={expected_date_verdict}"
        )


def _validated_holdout_query(
    row: dict[str, str],
    *,
    case_id_hash: str,
) -> str:
    query = str(row.get("deidentified_query") or "").strip()
    if not query or len(query) > 4000:
        raise ValueError(
            f"Approved holdout case {case_id_hash} has invalid "
            "deidentified_query"
        )
    if any(
        ord(character) < 32
        and character not in {"\n", "\r", "\t"}
        for character in query
    ):
        raise ValueError(
            f"Approved holdout case {case_id_hash} has invalid "
            "deidentified_query"
        )
    return query


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


def seal_holdout_review_payload_hashes(
    manifest_path: Path,
    source_path: Path,
    freeze_path: Path,
    review_workbook_path: Path,
) -> dict[str, Any]:
    """Atomically seal reviewed CSV values after the XLSX-to-CSV import step."""

    manifest = manifest_path.expanduser().resolve()
    if (
        manifest.suffix.casefold() != ".csv"
        or not manifest.is_relative_to(PRIVATE_DATA_ROOT)
        or not manifest.is_file()
    ):
        raise ValueError(
            "Holdout review manifest must be an existing private CSV file"
        )
    freeze = _validate_freeze_path(
        freeze_path,
        adjacent_to=manifest.parent,
    )
    freeze_payload = _read_freeze_contract(freeze)
    review_workbook = _validate_review_workbook_path(review_workbook_path)
    template_evidence = freeze_payload.get("pre_run_review_workbook")
    if not isinstance(template_evidence, dict):
        raise ValueError("Holdout freeze has no review workbook template evidence")
    template_workbook = _validated_private_evidence_file(
        template_evidence,
        field="pre_run_review_workbook_template",
        suffix=".xlsx",
    )
    source_evidence = freeze_payload.get("source")
    if not isinstance(source_evidence, dict):
        raise ValueError("Holdout freeze has no source evidence")
    requested_source = Path(source_path).expanduser().resolve()
    source_path = _validated_private_evidence_file(
        source_evidence,
        field="source",
        suffix=".jsonl",
    )
    if requested_source != source_path:
        raise ValueError(
            "Holdout source path differs from the freeze contract"
        )
    selection_path = _validated_private_evidence_file(
        freeze_payload["selection"],
        field="selection",
        suffix=".csv",
    )
    _validate_filled_workbook_aliases(
        review_workbook,
        forbidden_paths={
            template_workbook,
            source_path,
            selection_path,
            manifest,
        },
    )
    review_receipt = _holdout_review_receipt(
        freeze_payload,
        review_workbook_path=review_workbook,
    )
    rows, _ = _read_manifest(manifest, split="holdout")
    expected_fields, workbook_rows = load_holdout_review_workbook_rows(
        workbook_path=review_workbook,
        selection_path=selection_path,
        source_path=source_path,
        freeze_path=freeze,
        expected_total=int(freeze_payload["cases_total"]),
    )
    _validated_manifest_matches_filled_workbook(
        rows,
        expected_fields=expected_fields,
        workbook_rows=workbook_rows,
    )
    _validated_review_manifest_derivation(
        rows,
        selection_path=selection_path,
    )
    frozen_ids = freeze_payload["selection"]["case_id_hashes"]
    manifest_ids = [
        _required_hash(
            row.get("case_id_hash"),
            field="case_id_hash",
            location=f"manifest row {row_number}",
        )
        for row_number, row in enumerate(rows, start=2)
    ]
    if (
        len(manifest_ids) != len(set(manifest_ids))
        or sorted(manifest_ids) != frozen_ids
    ):
        raise ValueError(
            "Holdout review manifest must contain the exact frozen case set"
        )

    pii_masker = PIIMasker()
    for row in rows:
        case_id_hash = str(row["case_id_hash"]).strip()
        if (
            str(row.get("label_verdict") or "").strip().casefold()
            != "approved"
            or _optional_csv_bool(
                row.get("include_in_holdout"),
                field="include_in_holdout",
                location=f"manifest case {case_id_hash}",
            )
            is not True
        ):
            raise ValueError(
                "Every frozen holdout case must be approved and included "
                "before sealing"
            )
        _required_reviewer(
            row.get("reviewer"),
            case_id_hash=case_id_hash,
        )
        _required_reviewed_at(
            row.get("reviewed_at"),
            case_id_hash=case_id_hash,
        )
        _validated_holdout_privacy(
            row,
            case_id_hash=case_id_hash,
            pii_masker=pii_masker,
        )
        _validated_holdout_review_receipt(
            row,
            case_id_hash=case_id_hash,
            expected=review_receipt,
        )
        row["review_payload_sha256"] = holdout_review_payload_sha256(row)

    reviewed_margins = _reviewed_holdout_margins(rows)
    _write_manifest_csv(manifest, rows)
    return {
        "sealed_rows": len(rows),
        "selected_case_ids_sha256": _selected_case_ids_sha256(manifest_ids),
        "review_manifest_sha256": _file_sha256(manifest),
        "reviewed_profile_counts": reviewed_margins["profile_counts"],
        "reviewed_route_counts": reviewed_margins["route_counts"],
        "digest_canonicalization": _CASE_ID_DIGEST_CANONICALIZATION,
        "output": str(manifest),
    }


def _validated_manifest_matches_filled_workbook(
    manifest_rows: list[dict[str, str]],
    *,
    expected_fields: list[str],
    workbook_rows: list[dict[str, str]],
) -> None:
    if not manifest_rows or list(manifest_rows[0]) != expected_fields:
        raise ValueError(
            "Holdout review manifest schema differs from the filled workbook import"
        )
    manifest_by_id = {
        str(row.get("case_id_hash") or "").strip(): row
        for row in manifest_rows
    }
    workbook_by_id = {
        str(row.get("case_id_hash") or "").strip(): row
        for row in workbook_rows
    }
    if (
        len(manifest_by_id) != len(manifest_rows)
        or len(workbook_by_id) != len(workbook_rows)
        or set(manifest_by_id) != set(workbook_by_id)
    ):
        raise ValueError(
            "Holdout review manifest case set differs from the filled workbook"
        )
    compared_fields = [
        field
        for field in expected_fields
        if field != "review_payload_sha256"
    ]
    for case_id_hash in sorted(manifest_by_id):
        manifest_row = manifest_by_id[case_id_hash]
        workbook_row = workbook_by_id[case_id_hash]
        changed = [
            field
            for field in compared_fields
            if str(manifest_row.get(field) or "")
            != str(workbook_row.get(field) or "")
        ]
        if changed:
            raise ValueError(
                f"Holdout review manifest case {case_id_hash} differs from "
                "the filled workbook import: "
                + ", ".join(changed)
            )


def _write_manifest_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        raise ValueError("Holdout review manifest is empty")
    fieldnames = list(rows[0])
    if "review_payload_sha256" not in fieldnames:
        raise ValueError(
            "Holdout review manifest has no review_payload_sha256 column"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=fieldnames,
                extrasaction="raise",
            )
            writer.writeheader()
            writer.writerows(rows)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--review-workbook", type=Path)
    parser.add_argument(
        "--split",
        choices=sorted(_ALLOWED_SPLITS),
        default=DEFAULT_SPLIT,
    )
    parser.add_argument("--print-kb-seed-sha256", action="store_true")
    parser.add_argument(
        "--seal-review-payload-hashes",
        action="store_true",
        help=(
            "Atomically stamp review_payload_sha256 after the reviewed "
            "XLSX has been imported to the private CSV manifest."
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.seal_review_payload_hashes:
        if args.split != "holdout":
            parser.error("--seal-review-payload-hashes requires --split holdout")
        if args.freeze is None:
            parser.error("--seal-review-payload-hashes requires --freeze")
        if args.review_workbook is None:
            parser.error(
                "--seal-review-payload-hashes requires --review-workbook"
            )
        stats = seal_holdout_review_payload_hashes(
            args.manifest,
            args.input,
            args.freeze,
            args.review_workbook,
        )
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return

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

    output = args.output or args.manifest.with_name(
        DEFAULT_OUTPUT_NAME
        if args.split == DEFAULT_SPLIT
        else f"product_{args.split}_reviewed_ask_cases.json"
    )
    stats = build_reviewed_ticket_ask_cases(
        args.input,
        args.manifest,
        output,
        args.kb_seed,
        split=args.split,
        freeze_path=args.freeze,
        review_workbook_path=args.review_workbook,
        overwrite=args.overwrite,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
