from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
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
    recovered: dict[str, Any] | None = None
    reasons: list[str] = []
    if report_present:
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
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
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
