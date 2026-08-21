from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "balanced50-global-analysis-v1"
DATASET_ID = "pilot50_balanced_v5"
EXPECTED_CASES_TOTAL = 50
EXPECTED_GROUP_TOTAL = 25
MAX_REPORT_BYTES = 64 * 1024 * 1024
SHA_RE = re.compile(r"[0-9a-f]{40}")
GROUPS = ("typical", "atypical")


class AnalysisError(ValueError):
    pass


def _read_json_object(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise AnalysisError("report_not_regular")
    payload = path.read_bytes()
    if not payload or len(payload) > MAX_REPORT_BYTES:
        raise AnalysisError("report_size_invalid")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisError("report_json_invalid") from exc
    if not isinstance(value, dict):
        raise AnalysisError("report_shape_invalid")
    return value, payload


def _group_for_result(result: Mapping[str, Any]) -> str:
    tags = result.get("tags")
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise AnalysisError("result_tags_invalid")
    matched = [group for group in GROUPS if f"type:{group}" in tags]
    if len(matched) != 1:
        raise AnalysisError("result_group_invalid")
    return matched[0]


def _nonnegative_int(value: Any, *, reason: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AnalysisError(reason)
    return value


def _finite_nonnegative(value: Any, *, reason: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AnalysisError(reason)
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise AnalysisError(reason)
    return numeric


def _percentile(values: Sequence[int], quantile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index]


def _distribution(values: Sequence[int]) -> dict[str, int | None]:
    if not values:
        return {"minimum": None, "p50": None, "p95": None, "maximum": None}
    return {
        "minimum": min(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "maximum": max(values),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _boolean_metric(
    results: Sequence[Mapping[str, Any]],
    *keys: str,
) -> dict[str, int | float | None]:
    values: list[bool] = []
    for result in results:
        value: Any = None
        for key in keys:
            if key in result:
                value = result.get(key)
                break
        if isinstance(value, bool):
            values.append(value)
    passed = sum(values)
    return {
        "passed": passed,
        "scored": len(values),
        "rate": _rate(passed, len(values)),
    }


def _string_counter(
    results: Iterable[Mapping[str, Any]],
    key: str,
    *,
    default: str,
) -> dict[str, int]:
    counts = Counter(str(result.get(key) or default) for result in results)
    return dict(sorted(counts.items()))


def _list_counter(
    results: Iterable[Mapping[str, Any]],
    key: str,
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for result in results:
        values = result.get(key) or []
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise AnalysisError(f"{key}_invalid")
        counts.update(values)
    return dict(sorted(counts.items()))


def _validate_runtime_identity(report: Mapping[str, Any], expected_sha: str) -> None:
    identity = report.get("runtime_identity")
    if not isinstance(identity, dict):
        raise AnalysisError("runtime_identity_missing")
    if identity.get("required") is not False:
        raise AnalysisError("runtime_identity_contract_invalid")
    if identity.get("status") != "observed_unbound":
        raise AnalysisError("runtime_identity_status_invalid")
    if identity.get("preflight_release_git_sha") != expected_sha:
        raise AnalysisError("runtime_preflight_sha_mismatch")
    if identity.get("postflight_release_git_sha") != expected_sha:
        raise AnalysisError("runtime_postflight_sha_mismatch")
    if identity.get("verified_release_git_sha") != expected_sha:
        raise AnalysisError("runtime_observed_sha_mismatch")


def _validate_report(
    report: Mapping[str, Any],
    *,
    expected_runtime_sha: str,
) -> list[dict[str, Any]]:
    if report.get("cases_total") != EXPECTED_CASES_TOTAL:
        raise AnalysisError("cases_total_invalid")
    results = report.get("results")
    if not isinstance(results, list) or len(results) != EXPECTED_CASES_TOTAL:
        raise AnalysisError("results_total_invalid")
    if any(not isinstance(result, dict) for result in results):
        raise AnalysisError("result_shape_invalid")
    identifiers = [str(result.get("id") or "") for result in results]
    if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(
        identifiers
    ):
        raise AnalysisError("result_identity_invalid")
    group_counts = Counter(_group_for_result(result) for result in results)
    if group_counts != Counter({group: EXPECTED_GROUP_TOTAL for group in GROUPS}):
        raise AnalysisError("group_counts_invalid")
    if any(result.get("trace_found") is not True for result in results):
        raise AnalysisError("trace_coverage_incomplete")
    if any(result.get("cache_hit") is not False for result in results):
        raise AnalysisError("cache_bypass_invalid")
    if any(result.get("http_success") is not True for result in results):
        raise AnalysisError("http_execution_incomplete")
    if report.get("trace_coverage_rate") != 1.0:
        raise AnalysisError("trace_coverage_aggregate_invalid")
    if report.get("cache_hit_rate") not in (0, 0.0):
        raise AnalysisError("cache_hit_aggregate_invalid")
    if report.get("llm_budget_exceeded") is True:
        raise AnalysisError("llm_budget_exceeded")
    if report.get("llm_budget_stopped") is True:
        raise AnalysisError("llm_budget_stopped")
    if report.get("llm_pricing_stopped") is True:
        raise AnalysisError("llm_pricing_stopped")
    cost = _finite_nonnegative(
        report.get("llm_estimated_cost_rub", 0), reason="llm_cost_invalid"
    )
    maximum = _finite_nonnegative(
        report.get("llm_budget_rub"), reason="llm_budget_invalid"
    )
    if maximum != 200.0 or cost > maximum:
        raise AnalysisError("llm_budget_binding_invalid")
    _validate_runtime_identity(report, expected_runtime_sha)
    return results


def _group_summary(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(result.get("passed") is True for result in results)
    escalated = sum(result.get("was_escalated") is True for result in results)
    answered = sum(result.get("observed_behavior") == "answer" for result in results)
    latencies = [
        _nonnegative_int(result.get("latency_ms"), reason="latency_invalid")
        for result in results
    ]
    lengths = [len(str(result.get("response") or "").strip()) for result in results]
    return {
        "total": total,
        "passed": passed,
        "pass_rate": _rate(passed, total),
        "answered": answered,
        "answer_rate": _rate(answered, total),
        "escalated": escalated,
        "escalation_rate": _rate(escalated, total),
        "expected_retrieval": _boolean_metric(
            results,
            "expected_or_equivalent_chunk_hit",
            "expected_chunk_hit",
        ),
        "expected_citation": _boolean_metric(
            results,
            "expected_cited_or_equivalent_chunk_hit",
            "expected_cited_chunk_hit",
        ),
        "answer_contract": _boolean_metric(results, "answer_contains_match"),
        "behavior_match": _boolean_metric(results, "behavior_match"),
        "response_profile_match": _boolean_metric(
            results, "routing_response_profile_match"
        ),
        "latency_ms": _distribution(latencies),
        "response_characters": {
            **_distribution(lengths),
            "empty": sum(length == 0 for length in lengths),
            "over_1000": sum(length > 1000 for length in lengths),
        },
        "failure_reason_counts": _list_counter(results, "failure_reasons"),
        "escalation_reason_counts": _string_counter(
            (result for result in results if result.get("was_escalated") is True),
            "escalation_reason",
            default="unspecified",
        ),
        "generator_model_counts": _string_counter(
            results, "generator_model", default="unknown"
        ),
        "generate_retry_reason_counts": _list_counter(
            results, "generate_retry_reasons"
        ),
    }


def build_global_analysis(
    report: Mapping[str, Any],
    *,
    expected_runtime_sha: str,
    report_sha256: str,
) -> dict[str, Any]:
    results = _validate_report(report, expected_runtime_sha=expected_runtime_sha)
    grouped = {
        group: [result for result in results if _group_for_result(result) == group]
        for group in GROUPS
    }
    overall = _group_summary(results)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "OK",
        "dataset_id": DATASET_ID,
        "classification": "exposed_calibration_regression",
        "human_product_verdict": False,
        "runtime_git_sha": expected_runtime_sha,
        "report_sha256": report_sha256,
        "execution": {
            "cases_total": EXPECTED_CASES_TOTAL,
            "trace_coverage": 1.0,
            "cache_hits": 0,
            "llm_cost_rub": round(float(report.get("llm_estimated_cost_rub", 0)), 6),
            "llm_cost_cap_rub": 200,
        },
        "outcomes": {
            "overall": overall,
            **{group: _group_summary(grouped[group]) for group in GROUPS},
        },
        "failure_reason_counts": _list_counter(results, "failure_reasons"),
        "escalation_reason_counts": _string_counter(
            (result for result in results if result.get("was_escalated") is True),
            "escalation_reason",
            default="unspecified",
        ),
        "generator_model_counts": _string_counter(
            results, "generator_model", default="unknown"
        ),
        "generate_retry_reason_counts": _list_counter(
            results, "generate_retry_reasons"
        ),
        "observed_behavior_counts": _string_counter(
            results, "observed_behavior", default="unknown"
        ),
        "manual_review_required": True,
        "disclaimer": (
            "Global automated regression analysis of the exposed balanced 50-case set. "
            "It is not an independent holdout, a human product verdict or production "
            "ticket conversion. The full responses remain in private server evidence."
        ),
    }


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write_idempotent(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise AnalysisError("summary_output_conflict")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise AnalysisError("summary_output_conflict") from None
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a text-free global analysis for the balanced 50-case runtime run."
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-runtime-git-sha", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if SHA_RE.fullmatch(args.expected_runtime_git_sha) is None:
            raise AnalysisError("runtime_sha_invalid")
        report, report_bytes = _read_json_object(args.report)
        summary = build_global_analysis(
            report,
            expected_runtime_sha=args.expected_runtime_git_sha,
            report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        )
        payload = _canonical_bytes(summary)
        _write_idempotent(args.output, payload)
    except (AnalysisError, OSError):
        print("balanced50_global_analysis=FAIL reason=validation_failed")
        return 2
    print("balanced50_global_analysis=OK")
    print(payload.decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
