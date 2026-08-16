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
FAILURE_DIAGNOSTIC_SCHEMA_VERSION = "pilot50-v5-failure-diagnostics-v1"


class RecoveryError(ValueError):
    pass


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
        check_fields = pilot50._diagnostic_boolean_checks(
            case,
            result,
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
