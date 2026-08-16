from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

WORKSPACE = Path("/workspace")
EXPECTED_PILOT50_PATH = WORKSPACE / "scripts" / "pilot50.py"
FAILURE_DIAGNOSTIC_SCHEMA_VERSION = "pilot50-v5-failure-diagnostics-v2"
V5_ADDITIONAL_DIAGNOSTIC_BOOLEAN_CHECK_FIELDS = frozenset(
    {"answer_contains_match", "temporal_polarity_match"}
)
V5_DIAGNOSTIC_GENERATION_MODES = frozenset(
    {
        "bounded_published_source_chunk",
        "complex_deterministic_source_chunk",
        "complex_partial_source_chunk",
        "complex_single_official_source_chunk",
        "complex_source_chunk",
        "fact_card_source",
        "general_catalog_source_chunk",
        "partial_source_chunk",
        "request_bound_published_source_chunk",
        "source_chunk",
    }
)
V5_DIAGNOSTIC_RESPONSE_LENGTH_BUCKETS = frozenset(
    {"empty", "1-200", "201-450", "451-1000", ">1000"}
)


class RecoveryError(ValueError):
    pass


def _v5_diagnostic_boolean_checks(
    *,
    pilot50: ModuleType,
    case: Mapping[str, Any],
    result: Mapping[str, Any],
    was_escalated: bool,
) -> tuple[str, ...]:
    """Return every boolean that the v5 eval used for its sealed verdict."""

    fields = list(
        pilot50._diagnostic_boolean_checks(
            case,
            result,
            was_escalated=was_escalated,
        )
    )
    additional_fields: list[str] = []
    if case.get("expected_answer_fact_groups") and "answer_contains_match" not in fields:
        additional_fields.append("answer_contains_match")
    if case.get("expected_temporal_polarity"):
        additional_fields.append("temporal_polarity_match")
    for field in additional_fields:
        if field not in V5_ADDITIONAL_DIAGNOSTIC_BOOLEAN_CHECK_FIELDS:
            raise RecoveryError("v5 diagnostic check is not allowlisted")
        if type(result.get(field)) is not bool:
            raise RecoveryError("v5 diagnostic check is not a boolean")
        fields.append(field)
    if len(fields) != len(set(fields)):
        raise RecoveryError("v5 diagnostic check membership is invalid")
    return tuple(fields)


def _v5_answer_fact_group_matches(
    case: Mapping[str, Any],
    result: Mapping[str, Any],
) -> list[bool]:
    groups = case.get("expected_answer_fact_groups")
    matches = result.get("answer_fact_group_matches")
    if (
        not isinstance(groups, list)
        or not groups
        or not isinstance(matches, list)
        or len(matches) != len(groups)
        or any(type(value) is not bool for value in matches)
    ):
        raise RecoveryError("v5 answer fact group evidence is invalid")
    return list(matches)


def _v5_source_count(value: Any, *, label: str) -> int:
    if (
        not isinstance(value, list)
        or len(value) > 100
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise RecoveryError(f"v5 {label} source evidence is invalid")
    return len(value)


def _v5_response_length_bucket(value: Any) -> str:
    if not isinstance(value, str):
        raise RecoveryError("v5 response evidence is invalid")
    length = len(value)
    if length == 0:
        return "empty"
    if length <= 200:
        return "1-200"
    if length <= 450:
        return "201-450"
    if length <= 1_000:
        return "451-1000"
    return ">1000"


def trace_rows_from_sealed_report(
    report: Mapping[str, Any],
    *,
    expected_cases_total: int,
) -> list[dict[str, Any]]:
    eval_run_id = report.get("eval_run_id")
    results = report.get("results")
    if not isinstance(eval_run_id, str) or not eval_run_id:
        raise RecoveryError("report eval run identity is invalid")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise RecoveryError("report results are invalid")
    if len(results) != expected_cases_total:
        raise RecoveryError("report result cardinality is invalid")

    trace_rows: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, Mapping):
            raise RecoveryError("report result row is invalid")
        case_id = result.get("id")
        if (
            not isinstance(case_id, str)
            or not case_id
            or result.get("trace_found") is not True
            or result.get("trace_binding_match") is not True
            or result.get("trace_eval_run_id") != eval_run_id
            or result.get("trace_eval_case_id") != case_id
            or result.get("cache_hit") is not False
            or result.get("trace_lookup_error") not in (None, "")
            or result.get("trace_error") not in (None, "")
            or result.get("error") not in (None, "")
        ):
            raise RecoveryError("sealed trace evidence is invalid")
        trace_rows.append(
            {
                "eval_run_id": eval_run_id,
                "request_id": result.get("request_id"),
                "eval_case_id": case_id,
                "cache_hit": False,
                "error_present": False,
            }
        )
    return trace_rows


def _load_candidate_pilot50() -> ModuleType:
    workspace = WORKSPACE.resolve(strict=True)
    expected = EXPECTED_PILOT50_PATH.resolve(strict=True)
    if expected.parent.parent != workspace:
        raise RecoveryError("candidate module path is invalid")
    sys.path.insert(0, str(workspace))
    module = importlib.import_module("scripts.pilot50")
    actual = Path(str(module.__file__ or "")).resolve(strict=True)
    if actual != expected:
        raise RecoveryError("candidate module binding is invalid")
    return module


def build_failure_diagnostics(
    *,
    pilot50: ModuleType,
    cases: Sequence[Mapping[str, Any]],
    report: Mapping[str, Any],
    review_rows: Sequence[Mapping[str, Any]],
    safe: Mapping[str, Any],
    bindings: Mapping[str, str],
) -> dict[str, Any]:
    results = report.get("results")
    if not isinstance(results, list) or len(results) != pilot50.EXPECTED_CASES_TOTAL:
        raise RecoveryError("diagnostic report results are invalid")
    by_id: dict[str, Mapping[str, Any]] = {}
    for result in results:
        if not isinstance(result, Mapping):
            raise RecoveryError("diagnostic report row is invalid")
        case_id = result.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in by_id:
            raise RecoveryError("diagnostic report membership is invalid")
        by_id[case_id] = result

    failures: list[dict[str, Any]] = []
    failed_groups: Counter[str] = Counter()
    for ordinal, (case, review_row) in enumerate(
        zip(cases, review_rows, strict=True),
        start=1,
    ):
        result = by_id.get(str(case.get("id") or ""))
        if result is None:
            raise RecoveryError("diagnostic report membership is invalid")
        was_escalated = review_row.get("was_escalated")
        if type(was_escalated) is not bool:
            raise RecoveryError("diagnostic escalation evidence is invalid")
        check_fields = _v5_diagnostic_boolean_checks(
            pilot50=pilot50,
            case=case,
            result=result,
            was_escalated=was_escalated,
        )
        failed_checks = sorted(
            field for field in check_fields if result.get(field) is False
        )
        passed = review_row.get("passed")
        if type(passed) is not bool or passed is not (not failed_checks):
            raise RecoveryError("diagnostic checks do not bind the verdict")
        if passed:
            continue
        group = review_row.get("group")
        if group not in pilot50.EXPECTED_TYPE_COUNTS:
            raise RecoveryError("diagnostic group is invalid")
        escalation_reason = pilot50._diagnostic_escalation_reason(
            review_row.get("escalation_reason")
        )
        generation_mode = result.get("generation_mode")
        if generation_mode not in V5_DIAGNOSTIC_GENERATION_MODES:
            raise RecoveryError("v5 diagnostic generation mode is invalid")
        answer_fact_group_matches = _v5_answer_fact_group_matches(case, result)
        response_length_bucket = _v5_response_length_bucket(result.get("response"))
        if response_length_bucket not in V5_DIAGNOSTIC_RESPONSE_LENGTH_BUCKETS:
            raise RecoveryError("v5 diagnostic response length is invalid")
        row = {
            "ordinal": ordinal,
            "group": group,
            "critical": pilot50._candidate_case_is_critical(case),
            "was_escalated": was_escalated,
            "escalation_reason": escalation_reason,
            "observed_behavior": review_row.get("observed_behavior"),
            "failed_boolean_checks": failed_checks,
            "generator_path": pilot50._diagnostic_generator_path(
                result.get("generator_model")
            ),
            "generate_retry_reasons": pilot50._diagnostic_generate_retry_reasons(
                result.get("generate_retry_reasons")
            ),
            "latency_bucket": pilot50._diagnostic_latency_bucket(
                result.get("trace_total_latency_ms")
            ),
            "generation_mode": generation_mode,
            "answer_fact_group_matches": answer_fact_group_matches,
            "response_length_bucket": response_length_bucket,
            "retrieved_source_count": _v5_source_count(
                result.get("retrieved_chunk_ids"),
                label="retrieved",
            ),
            "reranked_source_count": _v5_source_count(
                result.get("reranked_chunk_ids"),
                label="reranked",
            ),
            "selected_source_count": _v5_source_count(
                result.get("selected_source_ids"),
                label="selected",
            ),
            "cited_source_count": _v5_source_count(
                result.get("ordered_cited_source_ids"),
                label="cited",
            ),
        }
        failures.append(row)
        failed_groups[str(group)] += 1

    quality = safe.get("quality_gate")
    criteria = quality.get("criteria") if isinstance(quality, Mapping) else None
    policy = safe.get("policy_pass")
    if (
        safe.get("dataset_id") != pilot50.V5_DATASET_ID
        or not isinstance(quality, Mapping)
        or quality.get("status") != "STOP"
        or not isinstance(criteria, Mapping)
        or not isinstance(policy, Mapping)
    ):
        raise RecoveryError("diagnostic safe result is invalid")
    expected_failed = pilot50.EXPECTED_CASES_TOTAL - int(
        policy["overall"]["passed"]
    )
    expected_critical = int(criteria["critical_case_failures"]["actual"])
    if (
        len(failures) != expected_failed
        or sum(row["critical"] is True for row in failures) != expected_critical
        or set(by_id) != {str(case.get("id") or "") for case in cases}
    ):
        raise RecoveryError("diagnostic failure counts are inconsistent")

    summary = {
        "failed_total": len(failures),
        "critical_failed": expected_critical,
        "typical_failed": failed_groups["typical"],
        "atypical_failed": failed_groups["atypical"],
    }
    return {
        "schema_version": FAILURE_DIAGNOSTIC_SCHEMA_VERSION,
        "bindings": dict(bindings),
        "summary": summary,
        "failures": failures,
    }


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expected-runtime-git-sha", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    recover = commands.add_parser("recover")
    _add_common_arguments(recover)
    recover.add_argument("--output", type=Path, required=True)
    recover.add_argument("--expected-approval-id", required=True)
    recover.add_argument("--candidate-contract", required=True)
    diagnose = commands.add_parser("diagnose")
    _add_common_arguments(diagnose)
    diagnose.add_argument("--safe-result", type=Path, required=True)
    diagnose.add_argument("--expected-manifest-sha256", required=True)
    diagnose.add_argument("--expected-cases-sha256", required=True)
    diagnose.add_argument("--expected-report-sha256", required=True)
    diagnose.add_argument("--expected-safe-result-sha256", required=True)
    return parser


def _recover(args: argparse.Namespace, pilot50: ModuleType) -> int:
    report_bytes = pilot50._read_regular_bytes(
        args.report,
        max_bytes=pilot50.MAX_REPORT_BYTES,
        label="ask report",
    )
    report = pilot50._load_json_bytes(report_bytes, label="ask report")
    if not isinstance(report, dict):
        raise RecoveryError("ask report must be a JSON object")
    trace_rows = trace_rows_from_sealed_report(
        report,
        expected_cases_total=pilot50.EXPECTED_CASES_TOTAL,
    )
    safe = pilot50.build_safe_result(
        manifest_path=args.manifest,
        cases_path=args.cases,
        report_path=args.report,
        trace_rows=trace_rows,
        expected_runtime_git_sha=args.expected_runtime_git_sha,
        expected_approval_id=args.expected_approval_id,
        candidate_contract=args.candidate_contract,
        report_snapshot=report_bytes,
    )
    pilot50.validate_safe_result(safe)
    pilot50._write_exclusive_json(args.output, safe)
    return 0


def _diagnose(args: argparse.Namespace, pilot50: ModuleType) -> int:
    paths = {
        "manifest": (args.manifest, pilot50.MAX_MANIFEST_BYTES),
        "cases": (args.cases, pilot50.MAX_CASES_BYTES),
        "report": (args.report, pilot50.MAX_REPORT_BYTES),
        "safe_result": (args.safe_result, pilot50.MAX_SAFE_BYTES),
    }
    expected_hashes = {
        "manifest": args.expected_manifest_sha256,
        "cases": args.expected_cases_sha256,
        "report": args.expected_report_sha256,
        "safe_result": args.expected_safe_result_sha256,
    }
    snapshots: dict[str, bytes] = {}
    for label, (path, maximum) in paths.items():
        snapshot = pilot50._read_regular_bytes(path, max_bytes=maximum, label=label)
        if pilot50._sha256(snapshot) != expected_hashes[label]:
            raise RecoveryError("diagnostic artifact binding is invalid")
        snapshots[label] = snapshot
    cases, cases_bytes, cases_sha, receipt = pilot50._validate_materialized_cases(
        args.manifest,
        args.cases,
    )
    if (
        receipt.get("dataset_id") != pilot50.V5_DATASET_ID
        or receipt.get("manifest_sha256") != expected_hashes["manifest"]
        or cases_bytes != snapshots["cases"]
        or cases_sha != expected_hashes["cases"]
    ):
        raise RecoveryError("diagnostic candidate selection is invalid")
    report = pilot50._load_json_bytes(snapshots["report"], label="ask report")
    safe = pilot50.validate_safe_result(
        pilot50._load_json_bytes(snapshots["safe_result"], label="safe result")
    )
    if not isinstance(report, dict):
        raise RecoveryError("diagnostic report is invalid")
    if (
        safe.get("runtime_git_sha") != args.expected_runtime_git_sha
        or safe.get("cases_sha256") != expected_hashes["cases"]
        or safe.get("report_sha256") != expected_hashes["report"]
    ):
        raise RecoveryError("diagnostic safe binding is invalid")
    review_rows = pilot50.build_review_rows(
        manifest_path=args.manifest,
        cases_path=args.cases,
        report_path=args.report,
        safe_result_path=args.safe_result,
        expected_runtime_git_sha=args.expected_runtime_git_sha,
    )
    for label, (path, maximum) in paths.items():
        if (
            pilot50._read_regular_bytes(path, max_bytes=maximum, label=label)
            != snapshots[label]
        ):
            raise RecoveryError("diagnostic artifacts changed during validation")
    payload = build_failure_diagnostics(
        pilot50=pilot50,
        cases=cases,
        report=report,
        review_rows=review_rows,
        safe=safe,
        bindings={
            "candidate_sha": args.expected_runtime_git_sha,
            "manifest_sha256": expected_hashes["manifest"],
            "cases_sha256": expected_hashes["cases"],
            "report_sha256": expected_hashes["report"],
            "safe_result_sha256": expected_hashes["safe_result"],
            "quality_status": "STOP",
        },
    )
    output = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(output.encode("ascii")) > 16 * 1024:
        raise RecoveryError("diagnostic output is oversized")
    print(output)
    return 0


def main() -> int:
    args = _parser().parse_args()
    pilot50 = _load_candidate_pilot50()
    if args.command == "recover":
        return _recover(args, pilot50)
    if args.command == "diagnose":
        return _diagnose(args, pilot50)
    raise RecoveryError("unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())
