from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from src.security.pii_masker import PIIMasker, PIIMaskingUnavailable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "eval" / "cases" / "pilot50_balanced_v1.json"
MANIFEST_SCHEMA_VERSION = "1.0.0"
SAFE_SCHEMA_VERSION = "pilot50-safe-result-v1"
DATASET_ID = "pilot50_balanced_v1"
CLASSIFICATION = "calibration_only"
PILOT50_TARGET = "http://app-ml:8000/ask"
EXPECTED_MANIFEST_CANONICAL_SHA256 = (
    "d591a02da2b616c1dc89931371184c762e0c9e1d3b68a50fd9ae33f9a5cf98f4"
)
DISCLAIMER = (
    "Tracked regression calibration only. This is a mechanical first-turn "
    "closure result for the balanced Pilot50 set, not an independent holdout, "
    "a human product verdict, ticket-level conversion, or production traffic conversion."
)
EXPECTED_CASES_TOTAL = 50
EXPECTED_TYPE_COUNTS = {"typical": 25, "atypical": 25}
EXPECTED_BEHAVIOR = "answer"
EXPECTED_ESCALATED = False
MAX_LLM_COST_RUB = 20.0
MAX_MANIFEST_BYTES = 128 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_CASES_BYTES = 4 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_SAFE_BYTES = 128 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EVAL_RUN_ID_RE = re.compile(r"^ask-eval-[0-9a-f-]{36}$")
FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_APPROVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
SAFE_TAG_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,160}$")
ALLOWED_SOURCE_PATHS = frozenset(
    {
        "eval/cases/pre_pilot_yonote.json",
        "eval/cases/product_calibration_synthetic_pilot_20.json",
        "eval/cases/pre_pilot_forums.json",
        "eval/cases/pre_pilot_adversarial.json",
        "eval/cases/product_date_aspect_regression_v1.json",
    }
)
FORBIDDEN_CASE_FIELDS = frozenset(
    {
        "ticket_id",
        "ticket_id_hash",
        "upstream_ticket_id",
        "source_ticket_id",
        "operator_answer",
        "operator_response",
        "user_hash",
        "user_id_hash",
        "holdout_contract",
    }
)
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "dataset_id",
        "classification",
        "human_product_verdict",
        "strata_contract",
        "expected_contract",
        "sources",
        "disclaimer",
    }
)
SOURCE_FIELDS = frozenset(
    {"path", "sha256", "type", "selection_rule", "case_ids"}
)
RUNTIME_IDENTITY_FIELDS = frozenset(
    {
        "required",
        "status",
        "expected_runtime_git_sha",
        "preflight_release_git_sha",
        "postflight_release_git_sha",
        "verified_release_git_sha",
        "matched_expected_runtime",
    }
)
SAFE_FIELDS = frozenset(
    {
        "schema_version",
        "dataset_id",
        "eval_run_id",
        "runtime_git_sha",
        "approval_id",
        "run_window_utc",
        "billing_status",
        "status",
        "classification",
        "human_product_verdict",
        "denominator",
        "counts",
        "mechanical_first_turn_closure",
        "policy_pass",
        "trace_coverage",
        "cache_hits",
        "budget",
        "pricing",
        "latency_ms",
        "llm_cost_rub",
        "cases_sha256",
        "report_sha256",
        "disclaimer",
    }
)


class Pilot50Error(ValueError):
    """Pilot50 input, integrity, or execution evidence is invalid."""


@lru_cache(maxsize=1)
def _pilot50_pii_masker() -> PIIMasker:
    return PIIMasker()


@lru_cache(maxsize=512)
def _query_has_pii(query: str) -> bool:
    try:
        _masked_query, mapping = _pilot50_pii_masker().mask(query)
    except PIIMaskingUnavailable as exc:
        raise Pilot50Error("Pilot50 PII scan is unavailable") from exc
    return bool(mapping)


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Pilot50Error("duplicate JSON keys are not allowed")
        result[key] = value
    return result


def _reject_non_finite_number(value: str) -> Any:
    raise Pilot50Error(f"non-finite JSON number is not allowed: {value}")


def _read_regular_bytes(path: Path, *, max_bytes: int, label: str) -> bytes:
    candidate = path.expanduser()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise Pilot50Error(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise Pilot50Error(f"{label} must be a regular file")
    if metadata.st_size <= 0 or metadata.st_size > max_bytes:
        raise Pilot50Error(f"{label} size is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise Pilot50Error(f"{label} cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise Pilot50Error(f"{label} must remain a regular file")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise Pilot50Error(f"{label} changed before read")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise Pilot50Error(f"{label} is oversized")
        final = os.fstat(descriptor)
        if (final.st_dev, final.st_ino, final.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise Pilot50Error(f"{label} changed during read")
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if not payload:
        raise Pilot50Error(f"{label} is empty")
    return payload


def _load_json_bytes(payload: bytes, *, label: str) -> Any:
    try:
        text = payload.decode("utf-8-sig")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Pilot50Error(f"{label} must be valid UTF-8 JSON") from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _manifest_snapshot(path: Path) -> tuple[dict[str, Any], bytes, str]:
    payload = _read_regular_bytes(path, max_bytes=MAX_MANIFEST_BYTES, label="manifest")
    manifest = _load_json_bytes(payload, label="manifest")
    if not isinstance(manifest, dict):
        raise Pilot50Error("manifest must be a JSON object")
    _validate_manifest(manifest)
    return manifest, payload, _sha256(payload)


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    if set(manifest) != MANIFEST_FIELDS:
        raise Pilot50Error("manifest fields do not match the Pilot50 schema")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise Pilot50Error("manifest schema version is invalid")
    if manifest.get("dataset_id") != DATASET_ID:
        raise Pilot50Error("manifest dataset id is invalid")
    if manifest.get("classification") != CLASSIFICATION:
        raise Pilot50Error("manifest classification is invalid")
    if manifest.get("human_product_verdict") is not False:
        raise Pilot50Error("manifest must not claim a human product verdict")
    if manifest.get("disclaimer") != (
        "Tracked regression calibration only; not an independent holdout or a "
        "human product-conversion verdict."
    ):
        raise Pilot50Error("manifest disclaimer is invalid")
    strata = manifest.get("strata_contract")
    if not isinstance(strata, dict) or set(strata) != set(EXPECTED_TYPE_COUNTS):
        raise Pilot50Error("manifest strata contract is invalid")
    if not all(isinstance(value, str) and value.strip() for value in strata.values()):
        raise Pilot50Error("manifest strata descriptions are invalid")
    contract = manifest.get("expected_contract")
    expected_contract = {
        "cases_total": EXPECTED_CASES_TOTAL,
        "type_counts": EXPECTED_TYPE_COUNTS,
        "expected_behavior": EXPECTED_BEHAVIOR,
        "expected_escalated": EXPECTED_ESCALATED,
    }
    if contract != expected_contract:
        raise Pilot50Error("manifest expected contract is invalid")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or len(sources) != len(ALLOWED_SOURCE_PATHS):
        raise Pilot50Error("manifest sources are invalid")
    observed_paths: set[str] = set()
    selected_ids: list[str] = []
    type_counts: Counter[str] = Counter()
    for source in sources:
        if not isinstance(source, dict) or set(source) != SOURCE_FIELDS:
            raise Pilot50Error("manifest source fields are invalid")
        source_path = source.get("path")
        source_hash = source.get("sha256")
        group = source.get("type")
        rule = source.get("selection_rule")
        case_ids = source.get("case_ids")
        if source_path not in ALLOWED_SOURCE_PATHS or source_path in observed_paths:
            raise Pilot50Error("manifest source path is invalid")
        if not isinstance(source_hash, str) or SHA256_RE.fullmatch(source_hash) is None:
            raise Pilot50Error("manifest source hash is invalid")
        if group not in EXPECTED_TYPE_COUNTS:
            raise Pilot50Error("manifest source type is invalid")
        if not isinstance(rule, str) or not rule.strip():
            raise Pilot50Error("manifest source selection rule is invalid")
        if (
            not isinstance(case_ids, list)
            or not case_ids
            or any(not isinstance(case_id, str) or not case_id for case_id in case_ids)
            or len(case_ids) != len(set(case_ids))
        ):
            raise Pilot50Error("manifest source case IDs are invalid")
        observed_paths.add(source_path)
        selected_ids.extend(case_ids)
        type_counts[group] += len(case_ids)
    if observed_paths != ALLOWED_SOURCE_PATHS:
        raise Pilot50Error("manifest source membership is invalid")
    if len(selected_ids) != EXPECTED_CASES_TOTAL or len(set(selected_ids)) != len(
        selected_ids
    ):
        raise Pilot50Error("manifest selected case membership is invalid")
    if dict(type_counts) != EXPECTED_TYPE_COUNTS:
        raise Pilot50Error("manifest type counts are invalid")


def _source_path(relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise Pilot50Error("source path escapes the project")
    candidate = PROJECT_ROOT.joinpath(relative)
    project = PROJECT_ROOT.resolve(strict=True)
    resolved_parent = candidate.parent.resolve(strict=True)
    try:
        resolved_parent.relative_to(project)
    except ValueError as exc:
        raise Pilot50Error("source path escapes the project") from exc
    return candidate


def _case_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise Pilot50Error("case tags must be an array")
    tags: list[str] = []
    for tag in value:
        if not isinstance(tag, str) or SAFE_TAG_RE.fullmatch(tag) is None:
            raise Pilot50Error("case contains an invalid tag")
        tags.append(tag)
    return tags


def build_materialized_cases(manifest_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest, _manifest_bytes, manifest_sha = _manifest_snapshot(manifest_path)
    materialized: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    seen_ids: set[str] = set()
    type_counts: Counter[str] = Counter()
    for source in manifest["sources"]:
        source_path = _source_path(source["path"])
        source_bytes = _read_regular_bytes(
            source_path,
            max_bytes=MAX_SOURCE_BYTES,
            label="case source",
        )
        source_rows = _load_json_bytes(source_bytes, label="case source")
        if not isinstance(source_rows, list) or not all(
            isinstance(row, dict) for row in source_rows
        ):
            raise Pilot50Error("case source must contain a JSON object array")
        if _sha256(_canonical_json_bytes(source_rows)) != source["sha256"]:
            raise Pilot50Error("case source canonical hash mismatch")
        by_id: dict[str, dict[str, Any]] = {}
        for row in source_rows:
            case_id = row.get("id")
            if not isinstance(case_id, str) or not case_id or case_id in by_id:
                raise Pilot50Error("case source IDs are invalid")
            by_id[case_id] = row
        selected_ids = source["case_ids"]
        if source["selection_rule"] in {"all_cases", "all_multi_aspect_cases"} and set(
            selected_ids
        ) != set(by_id):
            raise Pilot50Error("all-cases selection rule does not match its source")
        for case_id in selected_ids:
            source_case = by_id.get(case_id)
            if source_case is None:
                raise Pilot50Error("selected case is missing from its source")
            if FORBIDDEN_CASE_FIELDS & set(source_case):
                raise Pilot50Error("selected case contains forbidden identity fields")
            privacy_class = str(source_case.get("privacy_class") or "standard").strip()
            if privacy_class != "standard":
                raise Pilot50Error("selected case is not a standard synthetic regression")
            if source_case.get("expected_behavior") != EXPECTED_BEHAVIOR:
                raise Pilot50Error("selected case is not expected to answer")
            if source_case.get("expected_escalated") is not EXPECTED_ESCALATED:
                raise Pilot50Error("selected case is not expected to stay with the bot")
            query = source_case.get("query")
            if not isinstance(query, str) or not query.strip() or len(query) > 4000:
                raise Pilot50Error("selected case query is invalid")
            if _query_has_pii(query):
                raise Pilot50Error("selected case query failed the PII scan")
            normalized_query = " ".join(query.casefold().split())
            if case_id in seen_ids or normalized_query in seen_queries:
                raise Pilot50Error("selected cases are not unique")
            tags = _case_tags(source_case.get("tags"))
            if any("holdout" in tag.casefold() for tag in tags):
                raise Pilot50Error("holdout-marked case cannot enter Pilot50")
            group = source["type"]
            enriched = dict(source_case)
            enriched["privacy_class"] = "standard"
            enriched["user_id"] = f"pilot50-v1-{len(materialized) + 1:02d}"
            enriched["pilot50_group"] = group
            enriched["tags"] = list(
                dict.fromkeys([*tags, "pilot50:v1", f"type:{group}"])
            )
            materialized.append(enriched)
            seen_ids.add(case_id)
            seen_queries.add(normalized_query)
            type_counts[group] += 1
    if _sha256(_canonical_json_bytes(manifest)) != EXPECTED_MANIFEST_CANONICAL_SHA256:
        raise Pilot50Error("manifest differs from the frozen Pilot50 v1 selection")
    if len(materialized) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("materialized case count is invalid")
    if dict(type_counts) != EXPECTED_TYPE_COUNTS:
        raise Pilot50Error("materialized type counts are invalid")
    cases_bytes = _canonical_json_bytes(materialized)
    receipt = {
        "status": "OK",
        "operation": "prepare",
        "dataset_id": DATASET_ID,
        "cases_total": EXPECTED_CASES_TOTAL,
        "type_counts": EXPECTED_TYPE_COUNTS,
        "expected_behavior": EXPECTED_BEHAVIOR,
        "expected_escalated": EXPECTED_ESCALATED,
        "manifest_sha256": manifest_sha,
        "cases_sha256": _sha256(cases_bytes),
    }
    return materialized, receipt


def _validate_materialized_cases(
    manifest_path: Path,
    cases_path: Path,
) -> tuple[list[dict[str, Any]], bytes, str]:
    expected_cases, _receipt = build_materialized_cases(manifest_path)
    expected_bytes = _canonical_json_bytes(expected_cases)
    observed_bytes = _read_regular_bytes(
        cases_path,
        max_bytes=MAX_CASES_BYTES,
        label="materialized cases",
    )
    if observed_bytes != expected_bytes:
        raise Pilot50Error("materialized cases differ from the frozen selection")
    observed = _load_json_bytes(observed_bytes, label="materialized cases")
    if observed != expected_cases:
        raise Pilot50Error("materialized cases payload is invalid")
    return expected_cases, observed_bytes, _sha256(observed_bytes)


def _finite_nonnegative_number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Pilot50Error(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise Pilot50Error(f"{label} must be finite") from exc
    if not math.isfinite(number) or number < 0:
        raise Pilot50Error(f"{label} must be finite and non-negative")
    return number


def _validated_eval_run_id(value: Any) -> str:
    eval_run_id = str(value or "")
    if EVAL_RUN_ID_RE.fullmatch(eval_run_id) is None:
        raise Pilot50Error("ask report eval run ID is invalid")
    try:
        parsed = str(UUID(eval_run_id.removeprefix("ask-eval-")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise Pilot50Error("ask report eval run ID is invalid") from exc
    if eval_run_id != f"ask-eval-{parsed}":
        raise Pilot50Error("ask report eval run ID is invalid")
    return eval_run_id


def _validated_runtime_git_sha(value: Any) -> str:
    runtime_sha = str(value or "")
    if (
        FULL_GIT_SHA_RE.fullmatch(runtime_sha) is None
        or runtime_sha == "0" * 40
    ):
        raise Pilot50Error("expected runtime Git SHA is invalid")
    return runtime_sha


def _validated_approval_id(value: Any) -> str:
    approval_id = str(value or "")
    if SAFE_APPROVAL_ID_RE.fullmatch(approval_id) is None:
        raise Pilot50Error("expected approval reference is invalid")
    return approval_id


def _parse_utc_timestamp(value: Any, *, label: str) -> tuple[str, datetime]:
    raw = str(value or "")
    if not raw or len(raw) > 40:
        raise Pilot50Error(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Pilot50Error(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise Pilot50Error(f"{label} must be UTC")
    return raw, parsed.astimezone(UTC)


def _validated_run_window(report: Mapping[str, Any]) -> dict[str, str]:
    started_raw, started = _parse_utc_timestamp(
        report.get("run_started_at"),
        label="ask report run start",
    )
    completed_raw, completed = _parse_utc_timestamp(
        report.get("run_completed_at"),
        label="ask report run completion",
    )
    if completed < started or completed - started > timedelta(hours=4):
        raise Pilot50Error("ask report run window is invalid")
    _generated_raw, generated = _parse_utc_timestamp(
        report.get("generated_at"),
        label="ask report generation time",
    )
    if generated < started or generated > completed:
        raise Pilot50Error("ask report generation time is outside the run window")
    return {"started_at": started_raw, "completed_at": completed_raw}


def _rate_row(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "closed": numerator,
        "total": denominator,
        "rate": round(numerator / denominator, 6),
    }


def _pass_row(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "passed": numerator,
        "total": denominator,
        "rate": round(numerator / denominator, 6),
    }


def _percentile(values: Sequence[int], percentile: int) -> int:
    ordered = sorted(values)
    if not ordered:
        raise Pilot50Error("latency evidence is empty")
    index = round((len(ordered) - 1) * percentile / 100)
    return ordered[max(0, min(len(ordered) - 1, index))]


def validate_trace_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    eval_run_id: str,
    expected_results: Sequence[Mapping[str, Any]],
) -> None:
    if len(rows) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("database trace cardinality is not exactly 50")
    expected_pairs: set[tuple[str, str]] = set()
    expected_case_ids: set[str] = set()
    for result in expected_results:
        request_id = str(result.get("request_id") or "")
        case_id = str(result.get("id") or "")
        try:
            request_id = str(UUID(request_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise Pilot50Error("report request ID is invalid") from exc
        expected_pairs.add((request_id, case_id))
        expected_case_ids.add(case_id)
    if len(expected_pairs) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("report request/case pairs are not unique")
    observed_pairs: set[tuple[str, str]] = set()
    observed_request_ids: set[str] = set()
    observed_case_ids: list[str] = []
    for row in rows:
        if str(row.get("eval_run_id") or "") != eval_run_id:
            raise Pilot50Error("database trace run ID mismatch")
        try:
            request_id = str(UUID(str(row.get("request_id") or "")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise Pilot50Error("database request ID is invalid") from exc
        case_id = str(row.get("eval_case_id") or "")
        if row.get("cache_hit") is not False:
            raise Pilot50Error("database trace cache invariant failed")
        if row.get("error_present") is not False:
            raise Pilot50Error("database trace contains an execution error")
        observed_pairs.add((request_id, case_id))
        observed_request_ids.add(request_id)
        observed_case_ids.append(case_id)
    if len(observed_pairs) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("database trace request/case pairs are not unique")
    if len(observed_request_ids) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("database trace request IDs are not unique")
    if Counter(observed_case_ids) != Counter({case_id: 1 for case_id in expected_case_ids}):
        raise Pilot50Error("database trace case membership is invalid")
    if observed_pairs != expected_pairs:
        raise Pilot50Error("database trace pairs do not match the report")


async def _fetch_trace_rows(eval_run_id: str) -> list[dict[str, Any]]:
    dsn = str(os.getenv("ASK_EVAL_POSTGRES_DSN") or "").strip()
    if not dsn:
        raise Pilot50Error("trace DSN is unavailable inside acceptance")
    try:
        connection = await asyncpg.connect(dsn, timeout=15, command_timeout=15)
    except Exception as exc:
        raise Pilot50Error("trace database connection failed") from exc
    try:
        async with connection.transaction(readonly=True):
            records = await connection.fetch(
                """
                SELECT
                    eval_run_id,
                    request_id,
                    eval_case_id,
                    cache_hit,
                    (error IS NOT NULL) AS error_present
                FROM request_traces
                WHERE eval_run_id = $1
                ORDER BY eval_case_id NULLS FIRST, request_id
                """,
                eval_run_id,
                timeout=15,
            )
    except Exception as exc:
        raise Pilot50Error("trace database query failed") from exc
    finally:
        await connection.close()
    return [dict(record) for record in records]


def build_safe_result(
    *,
    manifest_path: Path,
    cases_path: Path,
    report_path: Path,
    trace_rows: Sequence[Mapping[str, Any]],
    expected_runtime_git_sha: str,
    expected_approval_id: str,
    report_snapshot: bytes | None = None,
) -> dict[str, Any]:
    runtime_git_sha = _validated_runtime_git_sha(expected_runtime_git_sha)
    approval_id = _validated_approval_id(expected_approval_id)
    cases, cases_bytes, cases_sha = _validate_materialized_cases(
        manifest_path,
        cases_path,
    )
    report_bytes = report_snapshot
    if report_bytes is None:
        report_bytes = _read_regular_bytes(
            report_path,
            max_bytes=MAX_REPORT_BYTES,
            label="ask report",
        )
    elif not report_bytes or len(report_bytes) > MAX_REPORT_BYTES:
        raise Pilot50Error("ask report snapshot size is invalid")
    report = _load_json_bytes(report_bytes, label="ask report")
    if not isinstance(report, dict):
        raise Pilot50Error("ask report must be a JSON object")
    results = report.get("results")
    if not isinstance(results, list) or len(results) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("ask report must contain exactly 50 result rows")
    if report.get("cases_total") != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("ask report case count is invalid")
    eval_run_id = _validated_eval_run_id(report.get("eval_run_id"))
    if report.get("target") != PILOT50_TARGET:
        raise Pilot50Error("ask report target is invalid")
    run_window = _validated_run_window(report)
    runtime_identity = report.get("runtime_identity")
    if (
        not isinstance(runtime_identity, dict)
        or set(runtime_identity) != RUNTIME_IDENTITY_FIELDS
        or runtime_identity.get("required") is not False
        or runtime_identity.get("status") != "observed_unbound"
        or runtime_identity.get("expected_runtime_git_sha") is not None
        or runtime_identity.get("preflight_release_git_sha") != runtime_git_sha
        or runtime_identity.get("postflight_release_git_sha") is not None
        or runtime_identity.get("verified_release_git_sha") != runtime_git_sha
        or runtime_identity.get("matched_expected_runtime") is not None
    ):
        raise Pilot50Error("ask report runtime identity does not bind Pilot50")
    expected_by_id = {str(case["id"]): case for case in cases}
    observed_by_id: dict[str, dict[str, Any]] = {}
    latencies: list[int] = []
    group_closed: Counter[str] = Counter()
    group_passed: Counter[str] = Counter()
    group_totals: Counter[str] = Counter()
    total_cost = 0.0
    for result in results:
        if not isinstance(result, dict):
            raise Pilot50Error("ask result row must be an object")
        case_id = str(result.get("id") or "")
        expected = expected_by_id.get(case_id)
        if expected is None or case_id in observed_by_id:
            raise Pilot50Error("ask result membership is invalid")
        observed_by_id[case_id] = result
        group = str(expected.get("pilot50_group") or "")
        if group not in EXPECTED_TYPE_COUNTS:
            raise Pilot50Error("materialized case type is invalid")
        tags = result.get("tags")
        if not isinstance(tags, list) or f"type:{group}" not in tags:
            raise Pilot50Error("ask result type binding is invalid")
        required_bools = {
            "http_success": True,
            "trace_found": True,
            "cache_hit": False,
        }
        for field, expected_value in required_bools.items():
            if result.get(field) is not expected_value:
                raise Pilot50Error(f"ask result {field} invariant failed")
        if result.get("error") is not None or result.get("trace_error") not in (None, ""):
            raise Pilot50Error("ask result contains an execution error")
        if result.get("trace_eval_run_id") != eval_run_id:
            raise Pilot50Error("ask result trace run binding failed")
        if result.get("trace_eval_case_id") != case_id:
            raise Pilot50Error("ask result trace case binding failed")
        if result.get("trace_binding_match") is not True:
            raise Pilot50Error("ask result trace binding is incomplete")
        passed = result.get("passed")
        was_escalated = result.get("was_escalated")
        if type(passed) is not bool or type(was_escalated) is not bool:
            raise Pilot50Error("ask result verdict fields must be typed booleans")
        observed_behavior = result.get("observed_behavior")
        if not isinstance(observed_behavior, str):
            raise Pilot50Error("ask result behavior is missing")
        latency = result.get("trace_total_latency_ms")
        if isinstance(latency, bool) or not isinstance(latency, int) or latency < 0:
            raise Pilot50Error("ask result latency is invalid")
        latencies.append(latency)
        total_cost += _finite_nonnegative_number(
            result.get("llm_estimated_cost_rub"),
            label="case LLM cost",
        )
        group_totals[group] += 1
        if passed:
            group_passed[group] += 1
        if passed and observed_behavior == "answer" and was_escalated is False:
            group_closed[group] += 1
    if set(observed_by_id) != set(expected_by_id):
        raise Pilot50Error("ask report case membership does not match Pilot50")
    if dict(group_totals) != EXPECTED_TYPE_COUNTS:
        raise Pilot50Error("ask report type counts are invalid")
    validate_trace_rows(
        trace_rows,
        eval_run_id=eval_run_id,
        expected_results=results,
    )
    if report.get("trace_coverage_rate") != 1.0:
        raise Pilot50Error("ask report trace coverage is incomplete")
    if report.get("cache_hit_rate") != 0.0:
        raise Pilot50Error("ask report cache hit rate is not zero")
    if report.get("llm_budget_stopped") is True or report.get("llm_budget_exceeded") is True:
        raise Pilot50Error("ask report stopped on budget")
    if report.get("llm_pricing_stopped") is True:
        raise Pilot50Error("ask report stopped on pricing")
    budget_value = report.get("llm_budget_rub")
    if _finite_nonnegative_number(budget_value, label="report LLM budget") != MAX_LLM_COST_RUB:
        raise Pilot50Error("ask report budget differs from the Pilot50 cap")
    reported_cost = _finite_nonnegative_number(
        report.get("llm_estimated_cost_rub"),
        label="report LLM cost",
    )
    if abs(reported_cost - total_cost) > 0.000001 or reported_cost > MAX_LLM_COST_RUB:
        raise Pilot50Error("ask report cost accounting is inconsistent")
    cost_control = report.get("cost_control")
    if (
        not isinstance(cost_control, dict)
        or cost_control.get("strict_live") is not True
        or cost_control.get("pricing_complete") is not True
        or cost_control.get("high_cost_approval_id") != approval_id
    ):
        raise Pilot50Error("ask report cost-control evidence is incomplete")
    reservation = cost_control.get("reservation")
    if not isinstance(reservation, dict):
        raise Pilot50Error("ask report cost reservation is missing")
    if (
        reservation.get("valid") is not True
        or reservation.get("run_id") != eval_run_id
        or reservation.get("scope") != "ask-eval"
        or reservation.get("runtime_git_sha") != runtime_git_sha
        or reservation.get("case_count") != EXPECTED_CASES_TOTAL
        or reservation.get("approved_cap_rub") != MAX_LLM_COST_RUB
        or reservation.get("approval_required") is not True
        or reservation.get("high_cost_approval_id") != approval_id
        or reservation.get("cases_file_sha256") != cases_sha
        or reservation.get("manifest_sha256") != cases_sha
        or reservation.get("manifest_matches_cases_file") is not True
    ):
        raise Pilot50Error("ask report cost reservation does not bind Pilot50")

    closure = {
        group: _rate_row(group_closed[group], EXPECTED_TYPE_COUNTS[group])
        for group in ("typical", "atypical")
    }
    closure["overall"] = _rate_row(sum(group_closed.values()), EXPECTED_CASES_TOTAL)
    policy = {
        group: _pass_row(group_passed[group], EXPECTED_TYPE_COUNTS[group])
        for group in ("typical", "atypical")
    }
    policy["overall"] = _pass_row(sum(group_passed.values()), EXPECTED_CASES_TOTAL)
    return {
        "schema_version": SAFE_SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "eval_run_id": eval_run_id,
        "runtime_git_sha": runtime_git_sha,
        "approval_id": approval_id,
        "run_window_utc": run_window,
        "billing_status": "pending_provider_reconciliation",
        "status": "OK",
        "classification": CLASSIFICATION,
        "human_product_verdict": False,
        "denominator": EXPECTED_CASES_TOTAL,
        "counts": EXPECTED_TYPE_COUNTS,
        "mechanical_first_turn_closure": closure,
        "policy_pass": policy,
        "trace_coverage": {
            "found": EXPECTED_CASES_TOTAL,
            "total": EXPECTED_CASES_TOTAL,
            "rate": 1.0,
        },
        "cache_hits": 0,
        "budget": {
            "max_rub": int(MAX_LLM_COST_RUB),
            "exceeded": False,
            "stopped": False,
        },
        "pricing": {"complete": True, "stopped": False},
        "latency_ms": {"p50": _percentile(latencies, 50), "p95": _percentile(latencies, 95)},
        "llm_cost_rub": round(reported_cost, 6),
        "cases_sha256": _sha256(cases_bytes),
        "report_sha256": _sha256(report_bytes),
        "disclaimer": DISCLAIMER,
    }


def validate_safe_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != SAFE_FIELDS:
        raise Pilot50Error("safe result fields are invalid")
    if value.get("schema_version") != SAFE_SCHEMA_VERSION:
        raise Pilot50Error("safe result schema is invalid")
    if value.get("dataset_id") != DATASET_ID or value.get("status") != "OK":
        raise Pilot50Error("safe result identity is invalid")
    _validated_eval_run_id(value.get("eval_run_id"))
    _validated_runtime_git_sha(value.get("runtime_git_sha"))
    _validated_approval_id(value.get("approval_id"))
    run_window = value.get("run_window_utc")
    if not isinstance(run_window, dict) or set(run_window) != {
        "started_at",
        "completed_at",
    }:
        raise Pilot50Error("safe result run window is invalid")
    _validated_run_window(
        {
            "run_started_at": run_window.get("started_at"),
            "run_completed_at": run_window.get("completed_at"),
            "generated_at": run_window.get("completed_at"),
        }
    )
    if value.get("billing_status") != "pending_provider_reconciliation":
        raise Pilot50Error("safe result billing status is invalid")
    if value.get("classification") != CLASSIFICATION:
        raise Pilot50Error("safe result classification is invalid")
    if value.get("human_product_verdict") is not False:
        raise Pilot50Error("safe result contains an invalid product verdict")
    if value.get("denominator") != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("safe result denominator is invalid")
    if value.get("counts") != EXPECTED_TYPE_COUNTS:
        raise Pilot50Error("safe result group counts are invalid")
    numerators: dict[str, dict[str, int]] = {}
    for field, count_key in (
        ("mechanical_first_turn_closure", "closed"),
        ("policy_pass", "passed"),
    ):
        table = value.get(field)
        if not isinstance(table, dict) or set(table) != {"typical", "atypical", "overall"}:
            raise Pilot50Error(f"safe result {field} is invalid")
        field_numerators: dict[str, int] = {}
        for group, denominator in {**EXPECTED_TYPE_COUNTS, "overall": 50}.items():
            row = table.get(group)
            if not isinstance(row, dict) or set(row) != {count_key, "total", "rate"}:
                raise Pilot50Error(f"safe result {field} row is invalid")
            numerator = row.get(count_key)
            if type(numerator) is not int or not 0 <= numerator <= denominator:
                raise Pilot50Error(f"safe result {field} numerator is invalid")
            if row.get("total") != denominator:
                raise Pilot50Error(f"safe result {field} total is invalid")
            expected_rate = round(numerator / denominator, 6)
            if row.get("rate") != expected_rate:
                raise Pilot50Error(f"safe result {field} rate is invalid")
            field_numerators[group] = numerator
        if field_numerators["overall"] != (
            field_numerators["typical"] + field_numerators["atypical"]
        ):
            raise Pilot50Error(f"safe result {field} totals are inconsistent")
        numerators[field] = field_numerators
    if numerators["mechanical_first_turn_closure"] != numerators["policy_pass"]:
        raise Pilot50Error("safe closure and policy totals are inconsistent")
    if value.get("trace_coverage") != {"found": 50, "total": 50, "rate": 1.0}:
        raise Pilot50Error("safe result trace coverage is invalid")
    if value.get("cache_hits") != 0:
        raise Pilot50Error("safe result cache count is invalid")
    if value.get("budget") != {"max_rub": 20, "exceeded": False, "stopped": False}:
        raise Pilot50Error("safe result budget is invalid")
    if value.get("pricing") != {"complete": True, "stopped": False}:
        raise Pilot50Error("safe result pricing is invalid")
    latency = value.get("latency_ms")
    if not isinstance(latency, dict) or set(latency) != {"p50", "p95"}:
        raise Pilot50Error("safe result latency is invalid")
    if any(type(item) is not int or item < 0 for item in latency.values()):
        raise Pilot50Error("safe result latency values are invalid")
    if latency["p50"] > latency["p95"]:
        raise Pilot50Error("safe result latency percentiles are inconsistent")
    if (
        _finite_nonnegative_number(value.get("llm_cost_rub"), label="safe LLM cost")
        > MAX_LLM_COST_RUB
    ):
        raise Pilot50Error("safe LLM cost exceeds the Pilot50 cap")
    for field in ("cases_sha256", "report_sha256"):
        if not isinstance(value.get(field), str) or SHA256_RE.fullmatch(value[field]) is None:
            raise Pilot50Error(f"safe result {field} is invalid")
    if value.get("disclaimer") != DISCLAIMER:
        raise Pilot50Error("safe result disclaimer is invalid")
    return dict(value)


def _validated_output_parent(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise Pilot50Error("output already exists")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise Pilot50Error("output parent must already exist") from exc
    if not parent.is_dir() or parent.is_symlink():
        raise Pilot50Error("output parent must be a real directory")
    return parent


def _write_exclusive_json(path: Path, value: Any) -> str:
    parent = _validated_output_parent(path)
    payload = _canonical_json_bytes(value)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _validated_output_parent(path)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise Pilot50Error("output already exists") from exc
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return _sha256(payload)


def _prepare(args: argparse.Namespace) -> dict[str, Any]:
    cases, receipt = build_materialized_cases(args.manifest)
    written_sha = _write_exclusive_json(args.output, cases)
    if written_sha != receipt["cases_sha256"]:
        raise Pilot50Error("written case hash mismatch")
    return receipt


async def _summarize(args: argparse.Namespace) -> dict[str, Any]:
    report_bytes = _read_regular_bytes(
        args.report,
        max_bytes=MAX_REPORT_BYTES,
        label="ask report",
    )
    report = _load_json_bytes(report_bytes, label="ask report")
    if not isinstance(report, dict):
        raise Pilot50Error("ask report must be a JSON object")
    eval_run_id = _validated_eval_run_id(report.get("eval_run_id"))
    trace_rows = await _fetch_trace_rows(eval_run_id)
    safe = build_safe_result(
        manifest_path=args.manifest,
        cases_path=args.cases,
        report_path=args.report,
        trace_rows=trace_rows,
        expected_runtime_git_sha=args.expected_runtime_git_sha,
        expected_approval_id=args.expected_approval_id,
        report_snapshot=report_bytes,
    )
    validate_safe_result(safe)
    _write_exclusive_json(args.output, safe)
    return safe


def _show_safe(args: argparse.Namespace) -> dict[str, Any]:
    payload = _read_regular_bytes(
        args.input,
        max_bytes=MAX_SAFE_BYTES,
        label="safe result",
    )
    value = _load_json_bytes(payload, label="safe result")
    return validate_safe_result(value)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare and summarize Pilot50 evidence.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    prepare.add_argument("--output", type=Path, required=True)
    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    summarize.add_argument("--cases", type=Path, required=True)
    summarize.add_argument("--report", type=Path, required=True)
    summarize.add_argument("--output", type=Path, required=True)
    summarize.add_argument("--expected-runtime-git-sha", required=True)
    summarize.add_argument("--expected-approval-id", required=True)
    show_safe = subparsers.add_parser("show-safe")
    show_safe.add_argument("--input", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "prepare":
            result = _prepare(args)
        elif args.command == "summarize":
            result = asyncio.run(_summarize(args))
        else:
            result = _show_safe(args)
    except (Pilot50Error, OSError) as exc:
        reason = "output_exists" if "output already exists" in str(exc) else "validation_failed"
        print(f"pilot50={args.command.upper()} reason={reason}")
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
