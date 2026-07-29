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
import zipfile
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.build_ticket_product_review import (  # noqa: E402
    build_review_exports,
    source_case_fingerprint,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DATA_ROOT = (PROJECT_ROOT / "data" / "private").resolve()
DEFAULT_KB_SEED = PROJECT_ROOT / "data" / "knowledge_base_seed.json"

_SAFE_HASH_RE = re.compile(r"^[0-9a-f]{12,64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FREEZE_HASH_SCOPE = "canonical_json_without_freeze_contract_sha256_fields"
_FREEZE_HASH_FIELDS = frozenset(
    {
        "freeze_contract_sha256",
        "freeze_contract_hash_scope",
    }
)
_SELECTION_REPRODUCTION_CONFIG = {
    "top_n": 20,
    "min_per_stratum": 0,
    "split": "holdout",
    "selection_mode": "profile_route_frequency",
    "multiturn_status": "single_turn",
}
_FORBIDDEN_MANIFEST_FIELDS = frozenset(
    {
        "query",
        "question",
        "text",
        "messages",
        "messages_masked",
        "operator_answer",
        "raw_operator_answer",
        "responsible_name",
    }
)
_REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "case_id_hash",
        "duplicate_cluster_id",
        "source_schema_version",
        "source_case_fingerprint",
        "aspect",
        "expected_route",
        "multiturn_status",
        "time_bucket",
    }
)
_ALLOWED_PRE_RUN_EXCLUSION_REASONS = frozenset(
    {
        "not_user_turn",
        "residual_pii",
        "unsafe_composite",
    }
)


def freeze_holdout_selection(
    *,
    source_path: Path,
    selection_path: Path,
    comparison_paths: list[Path],
    output_path: Path,
    runtime_git_sha: str,
    kb_seed_path: Path = DEFAULT_KB_SEED,
    stress_cases_path: Path | None = None,
    artifact_manifest_path: Path | None = None,
    review_workbook_path: Path | None = None,
    expected_total: int = 80,
    expected_route_counts: dict[str, int] | None = None,
    expected_profile_counts: dict[str, int] | None = None,
    pre_run_exclusions: Mapping[str, str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Seal private selection identities without exporting ticket text."""

    source = _private_file(source_path, suffix=".jsonl")
    selection = _private_file(selection_path, suffix=".csv")
    if len(comparison_paths) != 2:
        raise ValueError(
            "Independent holdout requires exactly two comparison inputs: calibration and validation"
        )
    comparisons = [_private_file(path, suffix=".jsonl") for path in comparison_paths]
    output = _private_output(output_path)
    if len(set(comparisons)) != len(comparisons):
        raise ValueError("Comparison inputs must be distinct")
    if source in comparisons or selection in comparisons:
        raise ValueError("Holdout and comparison inputs must be distinct")
    if artifact_manifest_path is None:
        raise ValueError("Independent holdout requires an artifact manifest")
    if stress_cases_path is None:
        raise ValueError("Independent holdout requires a separate stress suite")
    if review_workbook_path is None:
        raise ValueError("Independent holdout requires a private review workbook")
    if output in {
        Path(artifact_manifest_path).resolve(),
        Path(stress_cases_path).resolve(),
        Path(review_workbook_path).resolve(),
    }:
        raise ValueError("Freeze output must be distinct from its evidence inputs")
    if output.exists() and not overwrite:
        raise ValueError(f"Freeze output already exists: {output}")
    if not _GIT_SHA_RE.fullmatch(runtime_git_sha):
        raise ValueError("runtime_git_sha must be a full lowercase Git SHA")
    if expected_total <= 0:
        raise ValueError("expected_total must be positive")

    holdout_cases = _read_source_cases(source, expected_split="holdout")
    normalized_exclusions = _validated_pre_run_exclusions(
        pre_run_exclusions,
        holdout_cases=holdout_cases,
    )
    selection_rows, selection_sha256 = _read_selection(selection)
    if len(selection_rows) != expected_total:
        raise ValueError(f"Selection has {len(selection_rows)} rows; expected {expected_total}")

    selected_cases: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    selected_clusters: set[str] = set()
    selected_components: set[str] = set()
    route_counts: Counter[str] = Counter()
    profile_counts: Counter[str] = Counter()
    time_bucket_counts: Counter[str] = Counter()
    timestamp_status_counts: Counter[str] = Counter()
    multiturn_status_counts: Counter[str] = Counter()
    for row_number, row in enumerate(selection_rows, start=2):
        case_id = _required_hash(
            row.get("case_id_hash"),
            location=f"selection row {row_number}",
        )
        if case_id in selected_ids:
            raise ValueError(f"Duplicate selected case ID at row {row_number}")
        if case_id in normalized_exclusions:
            raise ValueError(
                f"Selection row {row_number} contains a pre-run excluded case"
            )
        source_case = holdout_cases.get(case_id)
        if source_case is None:
            raise ValueError(f"Selection row {row_number} is missing from holdout source")
        source_cluster = _required_hash(
            source_case.get("duplicate_cluster_id"),
            location=f"holdout case {case_id}",
        )
        manifest_cluster = _required_hash(
            row.get("duplicate_cluster_id"),
            location=f"selection row {row_number}",
        )
        if manifest_cluster != source_cluster:
            raise ValueError(f"Selection row {row_number} has stale duplicate cluster")
        if source_cluster in selected_clusters:
            raise ValueError("Selection contains duplicate clusters")
        source_component = _required_hash(
            source_case.get("duplicate_component_id"),
            location=f"holdout case {case_id}",
        )
        if source_component in selected_components:
            raise ValueError("Selection contains duplicate components")
        schema_version = str(source_case.get("schema_version") or "").strip()
        if str(row.get("source_schema_version") or "").strip() != schema_version:
            raise ValueError(f"Selection row {row_number} has stale source schema version")
        if str(row.get("source_case_fingerprint") or "").strip() != source_case_fingerprint(
            source_case
        ):
            raise ValueError(f"Selection row {row_number} has stale source fingerprint")
        profile = str(row.get("aspect") or "").strip()
        route = str(row.get("expected_route") or "").strip()
        if profile != str(source_case.get("expected_response_profile") or "").strip():
            raise ValueError(f"Selection row {row_number} has stale response profile")
        if route != str(source_case.get("expected_route") or "").strip():
            raise ValueError(f"Selection row {row_number} has stale route")
        source_multiturn_status = str(
            source_case.get("multiturn_status") or ""
        ).strip()
        if str(row.get("multiturn_status") or "").strip() != source_multiturn_status:
            raise ValueError(
                f"Selection row {row_number} has stale multiturn status"
            )
        if source_multiturn_status != "single_turn":
            raise ValueError(
                "Independent first-turn baseline may contain only single_turn cases"
            )
        source_time_bucket, timestamp_status = _source_time_bucket(
            source_case,
            case_id=case_id,
        )
        if str(row.get("time_bucket") or "").strip() != source_time_bucket:
            raise ValueError(f"Selection row {row_number} has stale time bucket")

        selected_ids.add(case_id)
        selected_clusters.add(source_cluster)
        selected_components.add(source_component)
        selected_cases.append(source_case)
        profile_counts[profile] += 1
        route_counts[route] += 1
        time_bucket_counts[source_time_bucket] += 1
        timestamp_status_counts[timestamp_status] += 1
        multiturn_status_counts[source_multiturn_status] += 1

    _require_expected_counts(
        route_counts,
        expected_route_counts,
        label="route",
    )
    _require_expected_counts(
        profile_counts,
        expected_profile_counts,
        label="profile",
    )
    selection_reproduction = _reproduce_selection(
        source_path=source,
        selection_rows=selection_rows,
        expected_total=expected_total,
        excluded_case_ids=frozenset(normalized_exclusions),
    )

    comparison_ids: set[str] = set()
    comparison_clusters: set[str] = set()
    comparison_components: set[str] = set()
    comparison_splits: set[str] = set()
    for comparison in comparisons:
        cases = _read_source_cases(comparison, expected_split=None)
        file_splits = {str(case.get("split") or "").strip() for case in cases.values()}
        if len(file_splits) != 1:
            raise ValueError("Each comparison file must contain exactly one split")
        file_ids = set(cases)
        file_clusters = {
            _required_hash(
                case.get("duplicate_cluster_id"),
                location=f"comparison case {case_id}",
            )
            for case_id, case in cases.items()
        }
        file_components = {
            _required_hash(
                case.get("duplicate_component_id"),
                location=f"comparison case {case_id}",
            )
            for case_id, case in cases.items()
        }
        if (
            file_ids & comparison_ids
            or file_clusters & comparison_clusters
            or file_components & comparison_components
        ):
            raise ValueError("Calibration and validation comparisons overlap")
        for case in cases.values():
            split = str(case.get("split") or "").strip()
            if split == "holdout":
                raise ValueError("Comparison input must not contain holdout cases")
            comparison_splits.add(split)
        comparison_ids.update(file_ids)
        comparison_clusters.update(file_clusters)
        comparison_components.update(file_components)
    if comparison_splits != {"calibration", "validation"}:
        raise ValueError(
            "Independent holdout requires exact calibration and validation comparisons"
        )

    overlap = {
        "case_ids": len(selected_ids & comparison_ids),
        "duplicate_clusters": len(selected_clusters & comparison_clusters),
        "duplicate_components": len(selected_components & comparison_components),
    }
    if any(overlap.values()):
        raise ValueError("Selected holdout overlaps a comparison split")

    seed_sha256, published_yonote_chunks = _canonical_seed_sha256(kb_seed_path)
    stress_evidence = _optional_json_array_evidence(
        stress_cases_path,
        expected_total=20,
    )
    artifact_manifest_evidence = _validated_artifact_manifest(
        artifact_manifest_path,
        required_artifacts=[source, *comparisons],
    )
    review_workbook_evidence = _validated_review_workbook_evidence(review_workbook_path)
    sorted_ids = sorted(selected_ids)
    selected_ids_sha256 = hashlib.sha256(("\n".join(sorted_ids) + "\n").encode("utf-8")).hexdigest()
    payload = {
        "schema_version": "1.0.0",
        "baseline_id": output.stem,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "runtime_git_sha": runtime_git_sha,
        "selection_status": "sealed_pending_human_review",
        "execution_allowed": False,
        "measurement_scope": (
            "independent_directional_single_turn_first_response_holdout; "
            "not ticket conversion and not a final 50-60% conversion claim"
        ),
        "cases_total": len(selected_cases),
        "unique_case_ids": len(selected_ids),
        "unique_duplicate_clusters": len(selected_clusters),
        "unique_duplicate_components": len(selected_components),
        "profile_counts": dict(sorted(profile_counts.items())),
        "route_counts": dict(sorted(route_counts.items())),
        "time_bucket_counts": dict(sorted(time_bucket_counts.items())),
        "multiturn_status_counts": dict(
            sorted(multiturn_status_counts.items())
        ),
        "pre_run_exclusions": {
            "count": len(normalized_exclusions),
            "cases": [
                {
                    "case_id_hash": case_id,
                    "reason": normalized_exclusions[case_id],
                }
                for case_id in sorted(normalized_exclusions)
            ],
        },
        "source_timestamp_policy": ("latest_of_created_updated_closed_v1_raw_source_month"),
        "source_timestamp_status_counts": dict(sorted(timestamp_status_counts.items())),
        "comparison_splits": sorted(comparison_splits),
        "cross_split_overlap": overlap,
        "privacy": {
            "queries_in_freeze_manifest": False,
            "operator_answers_in_selection": False,
            "operator_answers_used_as_facts": False,
            "deidentified_query_review": "pending",
        },
        "source": {
            "path": str(source),
            "sha256": _file_sha256(source),
            "cases_total": len(holdout_cases),
        },
        "selection": {
            "path": str(selection),
            "sha256": selection_sha256,
            "sampling_spec_sha256": _file_sha256(
                Path(__file__).with_name("build_ticket_product_review.py")
            ),
            "selected_case_ids_sha256": selected_ids_sha256,
            "case_id_hashes": sorted_ids,
            "reproduction": selection_reproduction,
        },
        "knowledge_snapshot": {
            "path": str(Path(kb_seed_path).resolve()),
            "canonical_sha256": seed_sha256,
            "published_yonote_chunks": published_yonote_chunks,
        },
        "stress_suite": stress_evidence,
        "source_artifact_manifest": artifact_manifest_evidence,
        "pre_run_review_workbook": review_workbook_evidence,
        "release_rule": (
            "Run once on the bound runtime SHA only after role, label, "
            "privacy and Yonote chunk review; keep stress results separate."
        ),
    }
    payload["freeze_contract_hash_scope"] = _FREEZE_HASH_SCOPE
    payload["freeze_contract_sha256"] = _freeze_contract_sha256(payload)
    _verify_freeze_contract(payload)
    _write_json(output, payload, overwrite=overwrite)
    try:
        written_payload = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not verify freeze output: {output}") from exc
    _verify_freeze_contract(written_payload)
    return {
        "cases_total": len(selected_cases),
        "pre_run_exclusions": payload["pre_run_exclusions"],
        "route_counts": dict(sorted(route_counts.items())),
        "profile_counts": dict(sorted(profile_counts.items())),
        "cross_split_overlap": overlap,
        "runtime_git_sha": runtime_git_sha,
        "freeze_contract_sha256": payload["freeze_contract_sha256"],
        "execution_allowed": False,
        "output": str(output),
    }


def _read_source_cases(
    path: Path,
    *,
    expected_split: str | None,
) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    try:
        source_file = path.open("r", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"Could not read source cases: {path}") from exc
    with source_file:
        for line_number, line in enumerate(source_file, start=1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"Invalid source JSON at line {line_number}") from None
            if not isinstance(case, dict):
                raise ValueError(f"Source line {line_number} must be an object")
            split = str(case.get("split") or "").strip()
            if expected_split is not None and split != expected_split:
                raise ValueError(f"Source line {line_number} is not in {expected_split}")
            if not split:
                raise ValueError(f"Source line {line_number} has no split")
            for flag in (
                "operator_answer_included",
                "operator_answer_used_as_fact",
            ):
                if case.get(flag) is not False:
                    raise ValueError(f"Source line {line_number} has unsafe {flag}")
            case_id = _required_hash(
                case.get("ticket_id_hash"),
                location=f"source line {line_number}",
            )
            if case_id in cases:
                raise ValueError(f"Duplicate source case at line {line_number}")
            cases[case_id] = case
    if not cases:
        raise ValueError(f"Source cases are empty: {path}")
    return cases


def _read_selection(path: Path) -> tuple[list[dict[str, str]], str]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Could not read selection manifest: {path}") from exc
    with io.StringIO(text, newline="") as selection_file:
        reader = csv.DictReader(selection_file)
        fields = set(reader.fieldnames or [])
        missing = sorted(_REQUIRED_MANIFEST_FIELDS - fields)
        if missing:
            raise ValueError("Selection manifest is missing fields: " + ", ".join(missing))
        forbidden = sorted(_FORBIDDEN_MANIFEST_FIELDS & fields)
        if forbidden:
            raise ValueError(
                "Selection manifest contains private text fields: " + ", ".join(forbidden)
            )
        return list(reader), hashlib.sha256(raw).hexdigest()


def _validated_pre_run_exclusions(
    values: Mapping[str, str] | None,
    *,
    holdout_cases: Mapping[str, dict[str, Any]],
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_case_id, raw_reason in (values or {}).items():
        case_id = str(raw_case_id).strip()
        reason = str(raw_reason).strip().casefold()
        if not _SAFE_HASH_RE.fullmatch(case_id):
            raise ValueError("Pre-run exclusion has an invalid case ID")
        if reason not in _ALLOWED_PRE_RUN_EXCLUSION_REASONS:
            raise ValueError(
                "Pre-run exclusion reason must be one of: "
                + ", ".join(sorted(_ALLOWED_PRE_RUN_EXCLUSION_REASONS))
            )
        if case_id not in holdout_cases:
            raise ValueError(
                f"Pre-run excluded case is missing from the holdout source: {case_id}"
            )
        normalized[case_id] = reason
    return normalized


def _reproduce_selection(
    *,
    source_path: Path,
    selection_rows: list[dict[str, str]],
    expected_total: int,
    excluded_case_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Re-run the declared deterministic selector and require the same case set."""

    with tempfile.TemporaryDirectory(
        prefix=".holdout-selection-reproduction-",
        dir=source_path.parent,
    ) as temporary_directory:
        temporary_root = Path(temporary_directory)
        summary_path = temporary_root / "selection_summary.csv"
        manifest_path = temporary_root / "selection_manifest.csv"
        stats = build_review_exports(
            source_path,
            summary_path,
            manifest_path,
            top_n=_SELECTION_REPRODUCTION_CONFIG["top_n"],
            min_per_stratum=_SELECTION_REPRODUCTION_CONFIG[
                "min_per_stratum"
            ],
            total=expected_total,
            split=_SELECTION_REPRODUCTION_CONFIG["split"],
            selection_mode=_SELECTION_REPRODUCTION_CONFIG[
                "selection_mode"
            ],
            multiturn_status=_SELECTION_REPRODUCTION_CONFIG[
                "multiturn_status"
            ],
            excluded_case_ids=excluded_case_ids,
        )
        reproduced_rows, reproduced_manifest_sha256 = _read_selection(
            manifest_path
        )

    selected_ids = sorted(
        _required_hash(
            row.get("case_id_hash"),
            location=f"selection row {row_number}",
        )
        for row_number, row in enumerate(selection_rows, start=2)
    )
    reproduced_ids = sorted(
        _required_hash(
            row.get("case_id_hash"),
            location=f"reproduced selection row {row_number}",
        )
        for row_number, row in enumerate(reproduced_rows, start=2)
    )
    if selected_ids != reproduced_ids:
        raise ValueError(
            "Selection case IDs do not match the deterministic selector"
        )
    return {
        **_SELECTION_REPRODUCTION_CONFIG,
        "total": expected_total,
        "excluded_cases": len(excluded_case_ids),
        "excluded_case_ids_sha256": hashlib.sha256(
            ("\n".join(sorted(excluded_case_ids)) + "\n").encode("utf-8")
        ).hexdigest(),
        "input_cases": int(stats["input_cases"]),
        "eligible_cases": int(stats["eligible_cases"]),
        "selected_cases": int(stats["selected_cases"]),
        "selected_case_ids_sha256": hashlib.sha256(
            ("\n".join(reproduced_ids) + "\n").encode("utf-8")
        ).hexdigest(),
        "reproduced_manifest_sha256": reproduced_manifest_sha256,
    }


def _require_expected_counts(
    actual: Counter[str],
    expected: dict[str, int] | None,
    *,
    label: str,
) -> None:
    if expected is None:
        return
    normalized = {key: value for key, value in expected.items() if value}
    if dict(sorted(actual.items())) != dict(sorted(normalized.items())):
        raise ValueError(f"Selection {label} counts do not match the freeze contract")


def _source_time_bucket(
    case: dict[str, Any],
    *,
    case_id: str,
) -> tuple[str, str]:
    value = str(case.get("available_at") or case.get("first_timestamp") or "").strip()
    if not value:
        raise ValueError(f"Holdout case {case_id} has no source timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Holdout case {case_id} has an invalid source timestamp") from None
    status = (
        "timezone_aware"
        if parsed.tzinfo is not None and parsed.utcoffset() is not None
        else "naive_source"
    )
    return f"{parsed.year:04d}-{parsed.month:02d}", status


def _canonical_seed_sha256(path: Path) -> tuple[str, int]:
    resolved = Path(path).resolve()
    if not resolved.is_relative_to((PROJECT_ROOT / "data").resolve()):
        raise ValueError("KB seed must stay under versioned project data")
    if resolved.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("KB seed must stay outside data/private")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read KB seed: {resolved}") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError("KB seed must be a non-empty JSON array")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    published = sum(
        isinstance(item, dict)
        and item.get("status") == "published"
        and item.get("source_type") == "yonote"
        for item in payload
    )
    return hashlib.sha256(canonical).hexdigest(), published


def _optional_json_array_evidence(
    path: Path | None,
    *,
    expected_total: int,
) -> dict[str, Any]:
    if path is None:
        raise ValueError("Stress cases path is required")
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError("Stress cases must stay under the project root")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read stress cases: {resolved}") from exc
    if not isinstance(payload, list) or len(payload) != expected_total:
        raise ValueError(f"Stress cases must contain exactly {expected_total} cases")
    case_ids: set[str] = set()
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Stress case {index} must be an object")
        case_id = str(item.get("case_id") or item.get("id") or "").strip()
        if not case_id:
            raise ValueError(f"Stress case {index} has no case ID")
        if case_id in case_ids:
            raise ValueError("Stress cases must have unique case IDs")
        case_ids.add(case_id)
    return {
        "path": str(resolved),
        "sha256": _file_sha256(resolved),
        "cases_total": len(payload),
        "included_in_conversion": False,
    }


def _validated_artifact_manifest(
    path: Path | None,
    *,
    required_artifacts: list[Path],
) -> dict[str, Any]:
    if path is None:
        raise ValueError("Source artifact manifest is required")
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("Source artifact manifest must stay under data/private")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read source artifact manifest: {resolved}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("complete") is not True
        or payload.get("schema_version") != "1.0.0"
    ):
        raise ValueError("Source artifact manifest is not complete")
    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise ValueError("Source artifact manifest has no artifacts")
    artifact_count = payload.get("artifact_count")
    if (
        isinstance(artifact_count, bool)
        or not isinstance(artifact_count, int)
        or artifact_count != len(raw_artifacts)
    ):
        raise ValueError("Source artifact manifest count mismatch")

    artifacts: dict[Path, dict[str, Any]] = {}
    for index, item in enumerate(raw_artifacts, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Source artifact manifest item {index} is invalid")
        raw_path = str(item.get("path") or "").strip()
        relative_path = Path(raw_path)
        artifact = (resolved.parent / relative_path).resolve()
        if (
            not raw_path
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or not artifact.is_relative_to(resolved.parent)
            or artifact in artifacts
        ):
            raise ValueError(f"Source artifact manifest item {index} has an unsafe path")
        expected_sha256 = str(item.get("sha256") or "").strip()
        expected_size = item.get("size_bytes")
        if not _SHA256_RE.fullmatch(expected_sha256):
            raise ValueError(f"Source artifact manifest item {index} has an invalid hash")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise ValueError(f"Source artifact manifest item {index} has an invalid size")
        if not artifact.is_file():
            raise ValueError(f"Source artifact manifest item {index} does not exist")
        if expected_sha256 != _file_sha256(artifact):
            raise ValueError(f"Source artifact manifest has stale hash for {artifact.name}")
        if expected_size != artifact.stat().st_size:
            raise ValueError(f"Source artifact manifest has stale size for {artifact.name}")
        artifacts[artifact] = item

    for artifact in required_artifacts:
        item = artifacts.get(artifact.resolve())
        if item is None:
            raise ValueError(f"Source artifact manifest does not bind {artifact.name}")
    return {
        "path": str(resolved),
        "sha256": _file_sha256(resolved),
        "validated": True,
        "artifacts_total": len(raw_artifacts),
        "required_artifacts": sorted(artifact.name for artifact in required_artifacts),
    }


def _validated_review_workbook_evidence(
    path: Path | None,
) -> dict[str, str | int]:
    if path is None:
        raise ValueError("Private review workbook is required")
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("Review workbook must stay under data/private")
    if resolved.suffix.casefold() != ".xlsx":
        raise ValueError("Review workbook must be an .xlsx file")
    if not resolved.is_file():
        raise ValueError(f"Review workbook does not exist: {resolved}")
    try:
        with zipfile.ZipFile(resolved) as workbook:
            names = set(workbook.namelist())
            if workbook.testzip() is not None or not {
                "[Content_Types].xml",
                "xl/workbook.xml",
            }.issubset(names):
                raise ValueError("Review workbook is not a valid XLSX file")
    except (OSError, zipfile.BadZipFile):
        raise ValueError("Review workbook is not a valid XLSX file") from None
    return {
        "path": str(resolved),
        "sha256": _file_sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _freeze_contract_sha256(payload: dict[str, Any]) -> str:
    contract = {key: value for key, value in payload.items() if key not in _FREEZE_HASH_FIELDS}
    canonical = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _verify_freeze_contract(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("Freeze contract must be a JSON object")
    if payload.get("freeze_contract_hash_scope") != _FREEZE_HASH_SCOPE:
        raise ValueError("Freeze contract has an unsupported hash scope")
    actual = str(payload.get("freeze_contract_sha256") or "").strip()
    if not _SHA256_RE.fullmatch(actual):
        raise ValueError("Freeze contract has an invalid self-hash")
    if actual != _freeze_contract_sha256(payload):
        raise ValueError("Freeze contract self-hash mismatch")


def _private_file(path: Path, *, suffix: str) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("Ticket artifacts must stay under data/private")
    if resolved.suffix.casefold() != suffix:
        raise ValueError(f"Ticket artifact must be a {suffix} file")
    return resolved


def _private_output(path: Path) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("Freeze output must stay under data/private")
    if resolved.suffix.casefold() != ".json":
        raise ValueError("Freeze output must be a .json file")
    return resolved


def _required_hash(value: Any, *, location: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_HASH_RE.fullmatch(text):
        raise ValueError(f"{location} has an invalid private hash")
    return text


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            raise ValueError(f"Freeze output already exists: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_counts(values: list[str], *, label: str) -> dict[str, int] | None:
    if not values:
        return None
    parsed: dict[str, int] = {}
    for value in values:
        key, separator, raw_count = value.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"{label} counts must use name=integer")
        try:
            count = int(raw_count)
        except ValueError:
            raise ValueError(f"{label} counts must use name=integer") from None
        if count < 0 or key in parsed:
            raise ValueError(f"Invalid duplicate {label} count")
        parsed[key] = count
    return parsed


def _parse_pre_run_exclusions(values: list[str]) -> dict[str, str] | None:
    if not values:
        return None
    parsed: dict[str, str] = {}
    for value in values:
        case_id, separator, reason = value.partition("=")
        case_id = case_id.strip()
        reason = reason.strip().casefold()
        if (
            not separator
            or not _SAFE_HASH_RE.fullmatch(case_id)
            or reason not in _ALLOWED_PRE_RUN_EXCLUSION_REASONS
            or case_id in parsed
        ):
            raise ValueError(
                "Pre-run exclusions must use unique "
                "case_hash=allowed_reason values"
            )
        parsed[case_id] = reason
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seal a private independent ticket holdout selection."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument(
        "--comparison",
        type=Path,
        action="append",
        default=[],
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-git-sha", required=True)
    parser.add_argument("--kb-seed", type=Path, default=DEFAULT_KB_SEED)
    parser.add_argument("--stress-cases", type=Path)
    parser.add_argument("--artifact-manifest", type=Path)
    parser.add_argument("--review-workbook", type=Path)
    parser.add_argument("--expected-total", type=int, default=80)
    parser.add_argument(
        "--expected-route-count",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--expected-profile-count",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--pre-run-exclusion",
        action="append",
        default=[],
        help=(
            "Bind a rejected source row as case_hash=reason before the "
            "deterministic replacement selection."
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    stats = freeze_holdout_selection(
        source_path=args.source,
        selection_path=args.selection,
        comparison_paths=args.comparison,
        output_path=args.output,
        runtime_git_sha=args.runtime_git_sha,
        kb_seed_path=args.kb_seed,
        stress_cases_path=args.stress_cases,
        artifact_manifest_path=args.artifact_manifest,
        review_workbook_path=args.review_workbook,
        expected_total=args.expected_total,
        expected_route_counts=_parse_counts(
            args.expected_route_count,
            label="route",
        ),
        expected_profile_counts=_parse_counts(
            args.expected_profile_count,
            label="profile",
        ),
        pre_run_exclusions=_parse_pre_run_exclusions(
            args.pre_run_exclusion
        ),
        overwrite=args.overwrite,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
