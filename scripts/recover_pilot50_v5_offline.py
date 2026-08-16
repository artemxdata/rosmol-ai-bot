from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

WORKSPACE = Path("/workspace")
EXPECTED_PILOT50_PATH = WORKSPACE / "scripts" / "pilot50.py"


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-runtime-git-sha", required=True)
    parser.add_argument("--expected-approval-id", required=True)
    parser.add_argument("--candidate-contract", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    pilot50 = _load_candidate_pilot50()
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


if __name__ == "__main__":
    raise SystemExit(main())
