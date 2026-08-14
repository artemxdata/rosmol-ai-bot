from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from eval.cost_governance import ROUTINE_ROLLING_24H_CAP_RUB, _scan_records
from scripts import semantic_recovery10

SCHEMA_VERSION = "semantic-recovery10-failed-diagnostics-v1"
EXPECTED_SCOPE = "ask-eval"
FULL_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
REPORT_VALIDATION_FAILURES = frozenset(
    {
        "report_json_unreadable",
        "manifest_invalid",
        "cases_invalid",
        "manifest_cases_binding_mismatch",
        "report_cardinality_mismatch",
        "report_cases_binding_mismatch",
        "report_target_mismatch",
        "result_identity_mismatch",
        "runtime_identity_mismatch",
        "pricing_incomplete",
        "reservation_invalid",
        "reservation_binding_mismatch",
        "reservation_run_id_mismatch",
        "eval_run_id_mismatch",
        "llm_cost_invalid",
        "llm_budget_exceeded",
        "llm_budget_stopped",
        "llm_pricing_stopped",
    }
)

_PREFLIGHT_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_sha",
        "production_runtime_sha",
        "production_snapshot_sha256",
        "cases_sha256",
        "manifest_sha256",
        "kb_seed_sha256",
        "cases_total",
        "cost_cap_rub",
        "channels_status",
        "capacity_status",
        "mem_available_mib",
        "swap_free_mib",
        "load1",
        "nproc",
        "docker_free_gib",
    }
)
_STARTED_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_sha",
        "cases_sha256",
        "manifest_sha256",
        "approval_id",
        "cost_cap_rub",
    }
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt(path: Path, *, fields: frozenset[str]) -> dict[str, str]:
    raw = path.read_bytes()
    if not raw or len(raw) > 4096 or not raw.isascii() or not raw.endswith(b"\n"):
        raise ValueError("receipt encoding is invalid")
    text = raw.decode("ascii")
    if "\r" in text:
        raise ValueError("receipt newline is invalid")
    lines = text.splitlines()
    if not lines or any(line.count("=") != 1 for line in lines):
        raise ValueError("receipt syntax is invalid")
    payload = dict(line.split("=", 1) for line in lines)
    if len(payload) != len(lines) or set(payload) != fields:
        raise ValueError("receipt fields are invalid")
    return payload


def _validate_receipts(
    *,
    preflight_receipt_path: Path,
    started_receipt_path: Path,
    expected_runtime_git_sha: str,
    expected_cases_sha256: str,
    expected_manifest_sha256: str,
    expected_approval_id: str,
) -> None:
    preflight = _receipt(preflight_receipt_path, fields=_PREFLIGHT_FIELDS)
    started = _receipt(started_receipt_path, fields=_STARTED_FIELDS)
    if preflight != {
        **preflight,
        "schema_version": "semantic-recovery10-preflight-v1",
        "candidate_sha": expected_runtime_git_sha,
        "cases_sha256": expected_cases_sha256,
        "manifest_sha256": expected_manifest_sha256,
        "cases_total": "10",
        "cost_cap_rub": "200",
        "channels_status": "HDE_VK_DISABLED",
        "capacity_status": "GO",
    }:
        raise ValueError("preflight receipt binding is invalid")
    if FULL_GIT_SHA_RE.fullmatch(preflight["production_runtime_sha"]) is None:
        raise ValueError("production runtime binding is invalid")
    for key in (
        "production_snapshot_sha256",
        "kb_seed_sha256",
    ):
        if SHA256_RE.fullmatch(preflight[key]) is None:
            raise ValueError("preflight digest binding is invalid")
    for key in ("mem_available_mib", "swap_free_mib", "nproc"):
        if not preflight[key].isdigit() or int(preflight[key]) <= 0:
            raise ValueError("preflight capacity binding is invalid")
    for key in ("load1", "docker_free_gib"):
        value = float(preflight[key])
        if not math.isfinite(value) or value < 0:
            raise ValueError("preflight capacity binding is invalid")
    if started != {
        "schema_version": "semantic-recovery10-run-started-v1",
        "candidate_sha": expected_runtime_git_sha,
        "cases_sha256": expected_cases_sha256,
        "manifest_sha256": expected_manifest_sha256,
        "approval_id": expected_approval_id,
        "cost_cap_rub": "200",
    }:
        raise ValueError("started receipt binding is invalid")


def _ledger_diagnostic(
    *,
    ledger_dir: Path,
    started_at: datetime,
    expected_runtime_git_sha: str,
    expected_cases_sha256: str,
    expected_approval_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    records = _scan_records(ledger_dir, now=datetime.now(UTC) + timedelta(minutes=1))
    matches = [
        record
        for record in records
        if all(
            (
                record.get("scope") == EXPECTED_SCOPE,
                record.get("runtime_git_sha") == expected_runtime_git_sha,
                record.get("manifest_sha256") == expected_cases_sha256,
                record.get("case_count") == semantic_recovery10.CASES_TOTAL,
                float(record.get("approved_cap_rub", -1))
                == semantic_recovery10.COST_CAP_RUB,
                record.get("private_full") is False,
                record.get("reservation_class") == "routine",
                record.get("high_cost_approval_id") == expected_approval_id,
            )
        )
    ]
    if len(matches) > 1:
        raise ValueError("multiple matching cost reservations")
    matching = matches[0] if matches else None
    cutoff_anchor = (
        matching["_reserved_at_datetime"] if matching is not None else started_at
    )
    cutoff = cutoff_anchor - timedelta(hours=24)
    routine_before = sum(
        (
            Decimal(str(record["approved_cap_rub"]))
            for record in records
            if record is not matching
            and record.get("reservation_class") == "routine"
            and cutoff <= record["_reserved_at_datetime"] <= cutoff_anchor
        ),
        start=Decimal("0"),
    )
    requested = Decimal(str(semantic_recovery10.COST_CAP_RUB))
    rolling_cap = Decimal(str(ROUTINE_ROLLING_24H_CAP_RUB))
    approval_elsewhere = any(
        record is not matching
        and record.get("high_cost_approval_id") == expected_approval_id
        for record in records
    )
    return (
        {
            "status": "exact" if matching is not None else "missing",
            "matching_records": len(matches),
            "approval_consumed_elsewhere": approval_elsewhere,
            "ledger_lock_present": (ledger_dir / ".cost-governance.lock").exists(),
            "rolling_24h_routine_reserved_before_rub": float(routine_before),
            "requested_cap_rub": float(requested),
            "rolling_24h_cap_rub": float(rolling_cap),
            "requested_would_fit": routine_before + requested <= rolling_cap,
        },
        matching,
    )


async def _fetch_trace_aggregate(run_id: str) -> dict[str, Any]:
    import asyncpg

    dsn = str(os.getenv("ASK_EVAL_POSTGRES_DSN") or "").strip()
    if not dsn:
        return {"status": "unavailable"}
    connection = None
    try:
        connection = await asyncpg.connect(dsn, timeout=15, command_timeout=15)
        async with connection.transaction(readonly=True):
            row = await connection.fetchrow(
                """
                SELECT
                    COUNT(*) AS traces_total,
                    COUNT(DISTINCT eval_case_id) AS distinct_cases,
                    COUNT(*) FILTER (WHERE eval_case_id IS NULL) AS null_case_ids,
                    COUNT(*) FILTER (WHERE cache_hit IS TRUE) AS cache_hits,
                    COUNT(*) FILTER (WHERE cache_hit IS FALSE) AS cache_misses,
                    COUNT(*) FILTER (WHERE error IS NOT NULL) AS errors,
                    COALESCE(SUM(llm_estimated_cost_rub), 0) AS llm_cost_rub
                FROM request_traces
                WHERE eval_run_id = $1
                """,
                run_id,
                timeout=15,
            )
    except Exception:
        return {"status": "unavailable"}
    finally:
        if connection is not None:
            await connection.close()
    if row is None:
        return {"status": "unavailable"}
    cost = float(row["llm_cost_rub"] or 0)
    if not math.isfinite(cost) or cost < 0:
        return {"status": "unavailable"}
    return {
        "status": "ok",
        "traces_total": int(row["traces_total"]),
        "distinct_cases": int(row["distinct_cases"]),
        "null_case_ids": int(row["null_case_ids"]),
        "cache_hits": int(row["cache_hits"]),
        "cache_misses": int(row["cache_misses"]),
        "errors": int(row["errors"]),
        "llm_cost_rub": round(cost, 6),
    }


def _fetch_trace_aggregate_sync(run_id: str) -> dict[str, Any]:
    return asyncio.run(_fetch_trace_aggregate(run_id))


def _recover_safe_result(
    *,
    manifest_path: Path,
    cases_path: Path,
    report_path: Path,
    expected_runtime_git_sha: str,
    expected_approval_id: str,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="semantic-recovery10-diagnostic-") as raw:
        output_path = Path(raw) / "safe-result.json"
        recovered = semantic_recovery10.summarize(
            manifest_path=manifest_path,
            cases_path=cases_path,
            report_path=report_path,
            output_path=output_path,
            expected_runtime_git_sha=expected_runtime_git_sha,
            expected_approval_id=expected_approval_id,
        )
        if semantic_recovery10.show_safe(output_path) != recovered:
            raise ValueError("recovered safe result changed after serialization")
    return recovered


def _bool_or_none(value: object) -> bool | None:
    return value if type(value) is bool else None


def _finite_float_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _report_validation_diagnostic(
    *,
    manifest_path: Path,
    cases_path: Path,
    report_path: Path,
    expected_runtime_git_sha: str,
    expected_approval_id: str,
    matching_record: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return only allowlisted structure and aggregates from a raw ask report."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        cases = json.loads(cases_path.read_text(encoding="utf-8-sig"))
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "status": "unreadable",
            "validation_failures": ["report_json_unreadable"],
            "cases_total": None,
            "results_total": None,
            "cases_binding_match": None,
            "target_match": None,
            "result_identity_match": None,
            "runtime_identity": None,
            "cost_control": None,
            "result_counts": None,
            "failure_reason_counts": {},
        }

    failures: list[str] = []
    cases_sha256 = _sha256(cases_path)
    manifest_valid = (
        isinstance(manifest, dict)
        and manifest.get("dataset_id") == semantic_recovery10.DATASET_ID
    )
    if not manifest_valid:
        failures.append("manifest_invalid")
    cases_valid = (
        isinstance(cases, list)
        and len(cases) == semantic_recovery10.CASES_TOTAL
    )
    if not cases_valid:
        failures.append("cases_invalid")
    if not isinstance(manifest, dict) or manifest.get("cases_sha256") != cases_sha256:
        failures.append("manifest_cases_binding_mismatch")

    report_dict = report if isinstance(report, dict) else {}
    raw_cases_total = report_dict.get("cases_total")
    cases_total = (
        raw_cases_total
        if type(raw_cases_total) is int and raw_cases_total >= 0
        else None
    )
    if not isinstance(report, dict) or raw_cases_total != semantic_recovery10.CASES_TOTAL:
        failures.append("report_cardinality_mismatch")

    cases_binding_match = (
        report_dict.get("cases_file_sha256") == cases_sha256
        if isinstance(report, dict)
        else None
    )
    if cases_binding_match is not True:
        failures.append("report_cases_binding_mismatch")
    target_match = (
        report_dict.get("target") == semantic_recovery10.TARGET
        if isinstance(report, dict)
        else None
    )
    if target_match is not True:
        failures.append("report_target_mismatch")

    raw_results = report_dict.get("results")
    results = raw_results if isinstance(raw_results, list) else None
    results_total = len(results) if results is not None else None
    result_identity_match: bool | None = None
    if cases_valid and results is not None:
        case_ids = [str(case.get("id") or "") for case in cases if isinstance(case, dict)]
        result_ids = [
            str(row.get("id") or "") for row in results if isinstance(row, dict)
        ]
        result_identity_match = (
            len(case_ids) == semantic_recovery10.CASES_TOTAL
            and len(result_ids) == semantic_recovery10.CASES_TOTAL
            and case_ids == result_ids
            and len(set(case_ids)) == semantic_recovery10.CASES_TOTAL
        )
    if results_total != semantic_recovery10.CASES_TOTAL or result_identity_match is not True:
        failures.append("result_identity_mismatch")

    raw_runtime = report_dict.get("runtime_identity")
    runtime = raw_runtime if isinstance(raw_runtime, dict) else {}
    raw_runtime_status = runtime.get("status")
    runtime_status = (
        raw_runtime_status
        if raw_runtime_status in {
            "verified",
            "invalid",
            "observed_unbound",
            "not_checked",
        }
        else "missing" if not runtime else "other"
    )
    runtime_diagnostic = {
        "status": runtime_status,
        "expected_match": (
            runtime.get("expected_runtime_git_sha") == expected_runtime_git_sha
        ),
        "verified_match": (
            runtime.get("verified_release_git_sha") == expected_runtime_git_sha
        ),
        "matched_expected": _bool_or_none(
            runtime.get("matched_expected_runtime")
        ),
    }
    if not all(
        (
            runtime_diagnostic["expected_match"] is True,
            runtime_diagnostic["verified_match"] is True,
            runtime_diagnostic["matched_expected"] is True,
        )
    ):
        failures.append("runtime_identity_mismatch")

    raw_cost_control = report_dict.get("cost_control")
    cost_control = raw_cost_control if isinstance(raw_cost_control, dict) else {}
    raw_reservation = cost_control.get("reservation")
    reservation = raw_reservation if isinstance(raw_reservation, dict) else {}
    pricing_complete = _bool_or_none(cost_control.get("pricing_complete"))
    reservation_valid = _bool_or_none(reservation.get("valid"))
    approved_cap = _finite_float_or_none(reservation.get("approved_cap_rub"))
    reservation_binding_match = all(
        (
            reservation.get("runtime_git_sha") == expected_runtime_git_sha,
            reservation.get("manifest_sha256") == cases_sha256,
            reservation.get("case_count") == semantic_recovery10.CASES_TOTAL,
            approved_cap == semantic_recovery10.COST_CAP_RUB,
            reservation.get("high_cost_approval_id") == expected_approval_id,
        )
    )
    expected_run_id = matching_record.get("run_id") if matching_record else None
    reservation_run_id_match = (
        reservation.get("run_id") == expected_run_id
        if expected_run_id is not None
        else None
    )
    eval_run_id_match = (
        report_dict.get("eval_run_id") == expected_run_id
        if expected_run_id is not None
        else None
    )
    if pricing_complete is not True:
        failures.append("pricing_incomplete")
    if reservation_valid is not True:
        failures.append("reservation_invalid")
    if reservation_binding_match is not True:
        failures.append("reservation_binding_mismatch")
    if reservation_run_id_match is False:
        failures.append("reservation_run_id_mismatch")
    if eval_run_id_match is False:
        failures.append("eval_run_id_mismatch")

    cost = _finite_float_or_none(report_dict.get("llm_estimated_cost_rub"))
    if cost is None or cost < 0 or cost > semantic_recovery10.COST_CAP_RUB:
        failures.append("llm_cost_invalid")
    budget_exceeded = _bool_or_none(report_dict.get("llm_budget_exceeded"))
    budget_stopped = _bool_or_none(report_dict.get("llm_budget_stopped"))
    pricing_stopped = _bool_or_none(report_dict.get("llm_pricing_stopped"))
    if budget_exceeded is True:
        failures.append("llm_budget_exceeded")
    if budget_stopped is True:
        failures.append("llm_budget_stopped")
    if pricing_stopped is True:
        failures.append("llm_pricing_stopped")

    result_counts: dict[str, int] | None = None
    failure_counts: Counter[str] = Counter()
    if results is not None:
        safe_rows = [row for row in results if isinstance(row, dict)]
        for row in safe_rows:
            failure_counts.update(semantic_recovery10._safe_failure_reasons(row))
        result_counts = {
            "passed": sum(row.get("passed") is True for row in safe_rows),
            "trace_found": sum(row.get("trace_found") is True for row in safe_rows),
            "http_success": sum(row.get("http_success") is True for row in safe_rows),
            "http_error": sum(row.get("http_success") is False for row in safe_rows),
            "was_escalated": sum(row.get("was_escalated") is True for row in safe_rows),
            "semantic_recovery_attempted": sum(
                row.get("semantic_recovery_attempted") is True for row in safe_rows
            ),
            "semantic_recovery_succeeded": sum(
                semantic_recovery10._semantic_status(row)[1] for row in safe_rows
            ),
        }

    unique_failures = list(dict.fromkeys(failures))
    if not set(unique_failures) <= REPORT_VALIDATION_FAILURES:
        raise ValueError("unsafe report diagnostic failure code")
    return {
        "status": "valid" if not unique_failures else "invalid",
        "validation_failures": unique_failures,
        "cases_total": cases_total,
        "results_total": results_total,
        "cases_binding_match": cases_binding_match,
        "target_match": target_match,
        "result_identity_match": result_identity_match,
        "runtime_identity": runtime_diagnostic,
        "cost_control": {
            "pricing_complete": pricing_complete,
            "reservation_valid": reservation_valid,
            "reservation_binding_match": reservation_binding_match,
            "reservation_run_id_match": reservation_run_id_match,
            "eval_run_id_match": eval_run_id_match,
            "budget_exceeded": budget_exceeded,
            "budget_stopped": budget_stopped,
            "pricing_stopped": pricing_stopped,
            "llm_cost_rub": cost if cost is not None and cost >= 0 else None,
        },
        "result_counts": result_counts,
        "failure_reason_counts": dict(sorted(failure_counts.items())),
    }


def diagnose_failed(
    *,
    evidence_dir: Path,
    preflight_receipt_path: Path,
    started_receipt_path: Path,
    ledger_dir: Path,
    expected_runtime_git_sha: str,
    expected_cases_sha256: str,
    expected_manifest_sha256: str,
    expected_approval_id: str,
) -> dict[str, Any]:
    if FULL_GIT_SHA_RE.fullmatch(expected_runtime_git_sha) is None:
        raise ValueError("runtime SHA is invalid")
    if any(
        SHA256_RE.fullmatch(value) is None
        for value in (expected_cases_sha256, expected_manifest_sha256)
    ):
        raise ValueError("artifact SHA-256 is invalid")
    cases_path = evidence_dir / "semantic-recovery10-cases.json"
    manifest_path = evidence_dir / "semantic-recovery10-manifest.json"
    report_path = evidence_dir / "semantic-recovery10-ask-report.json"
    safe_path = evidence_dir / "semantic-recovery10-safe-result.json"
    _validate_receipts(
        preflight_receipt_path=preflight_receipt_path,
        started_receipt_path=started_receipt_path,
        expected_runtime_git_sha=expected_runtime_git_sha,
        expected_cases_sha256=expected_cases_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_approval_id=expected_approval_id,
    )
    if _sha256(cases_path) != expected_cases_sha256:
        raise ValueError("cases digest mismatch")
    if _sha256(manifest_path) != expected_manifest_sha256:
        raise ValueError("manifest digest mismatch")
    if safe_path.exists() or safe_path.is_symlink():
        raise ValueError("failed run contains finalized artifacts")

    started_at = datetime.fromtimestamp(
        started_receipt_path.stat().st_mtime,
        tz=UTC,
    )
    reservation, matching_record = _ledger_diagnostic(
        ledger_dir=ledger_dir,
        started_at=started_at,
        expected_runtime_git_sha=expected_runtime_git_sha,
        expected_cases_sha256=expected_cases_sha256,
        expected_approval_id=expected_approval_id,
    )
    trace = {"status": "not_bound"}
    if matching_record is not None:
        run_id = str(matching_record["run_id"])
        trace = _fetch_trace_aggregate_sync(run_id)

    report_present = report_path.is_file() and not report_path.is_symlink()
    report_sha256 = _sha256(report_path) if report_present else None
    report_diagnostic: dict[str, Any] | None = None
    recovered: dict[str, Any] | None = None
    reasons: list[str] = []
    if report_present:
        report_diagnostic = _report_validation_diagnostic(
            manifest_path=manifest_path,
            cases_path=cases_path,
            report_path=report_path,
            expected_runtime_git_sha=expected_runtime_git_sha,
            expected_approval_id=expected_approval_id,
            matching_record=matching_record,
        )
        try:
            recovered = _recover_safe_result(
                manifest_path=manifest_path,
                cases_path=cases_path,
                report_path=report_path,
                expected_runtime_git_sha=expected_runtime_git_sha,
                expected_approval_id=expected_approval_id,
            )
            if matching_record is None:
                raise ValueError("report exists without the exact reservation")
            if recovered.get("eval_run_id") != matching_record.get("run_id"):
                raise ValueError("report and reservation run identity mismatch")
        except (OSError, TypeError, ValueError, AttributeError, json.JSONDecodeError):
            recovered = None
            reasons = ["report_validation_failed"]
            stage = "report_present_invalid"
        else:
            counts = recovered["counts"]
            if counts["trace_found"] != semantic_recovery10.CASES_TOTAL:
                reasons.append("trace_coverage_below_100_percent")
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
            if report.get("llm_pricing_stopped") is True:
                reasons.append("llm_cost_accounting_incomplete")
            if report.get("llm_budget_stopped") is True:
                reasons.append("llm_budget_stopped")
            if not reasons:
                reasons.append("unexplained_nonzero_exit_after_report")
            stage = "post_report_cli_gate"
    elif matching_record is None:
        if reservation["approval_consumed_elsewhere"]:
            reasons = ["approval_replay_rejected"]
        elif reservation["ledger_lock_present"]:
            reasons = ["cost_ledger_locked"]
        elif reservation["requested_would_fit"] is False:
            reasons = ["rolling_24h_cap_rejected"]
        else:
            reasons = ["pre_reservation_failure"]
        stage = "before_cost_reservation"
    else:
        reasons = ["runtime_or_case_execution_failed"]
        if trace.get("status") != "ok" or trace.get("traces_total") == 0:
            stage = "after_reservation_before_case_trace"
        elif int(trace["traces_total"]) < semantic_recovery10.CASES_TOTAL:
            stage = "case_execution_incomplete"
        else:
            stage = "post_case_pre_report"

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "execution_rejected",
        "bindings": {
            "candidate_sha": expected_runtime_git_sha,
            "cases_sha256": expected_cases_sha256,
            "manifest_sha256": expected_manifest_sha256,
            "approval_id": expected_approval_id,
        },
        "artifacts": {
            "run_started": True,
            "run_completed": False,
            "raw_report_present": report_present,
            "raw_report_sha256": report_sha256,
            "safe_result_present": False,
        },
        "reservation": reservation,
        "trace_aggregate": trace,
        "failure_stage": stage,
        "failure_reasons": reasons,
        "report_diagnostic": report_diagnostic,
        "quality_verdict_available": recovered is not None,
        "retry_forbidden": True,
        "diagnostic_new_ask_calls": 0,
        "recovered_safe_result": recovered,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only diagnosis of the sealed failed Recovery10 run"
    )
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--preflight-receipt", type=Path, required=True)
    parser.add_argument("--started-receipt", type=Path, required=True)
    parser.add_argument("--ledger-dir", type=Path, required=True)
    parser.add_argument("--expected-runtime-git-sha", required=True)
    parser.add_argument("--expected-cases-sha256", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-approval-id", required=True)
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    payload = diagnose_failed(
        evidence_dir=args.evidence_dir,
        preflight_receipt_path=args.preflight_receipt,
        started_receipt_path=args.started_receipt,
        ledger_dir=args.ledger_dir,
        expected_runtime_git_sha=args.expected_runtime_git_sha,
        expected_cases_sha256=args.expected_cases_sha256,
        expected_manifest_sha256=args.expected_manifest_sha256,
        expected_approval_id=args.expected_approval_id,
    )
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
