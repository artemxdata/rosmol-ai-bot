from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eval.cost_governance import reserve_live_eval_cost
from scripts import diagnose_semantic_recovery10_failed as diagnostic
from scripts import semantic_recovery10

RUNTIME_SHA = "b37f462f240b65cc1de76bae7fb4ff2a63235458"
CASES_SHA_PLACEHOLDER = "f" * 64
APPROVAL_ID = (
    "owner-chat-20260814-semantic10-"
    f"{RUNTIME_SHA}-f2168c9e8721-cap200"
)


def _write_json(path: Path, payload: object) -> str:
    raw = semantic_recovery10._canonical_json_bytes(payload)
    path.write_bytes(raw)
    return semantic_recovery10._sha256_bytes(raw)


def _write_receipt(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="ascii",
        newline="\n",
    )


def _workspace(tmp_path: Path) -> dict[str, Any]:
    evidence = tmp_path / "evidence"
    ledger = tmp_path / "ledger"
    evidence.mkdir()
    ledger.mkdir()
    cases = [
        {
            "id": f"case-{index:02d}",
            "query": f"question {index}",
            "user_id": f"user-{index}",
            "channel": "api",
        }
        for index in range(10)
    ]
    cases_path = evidence / "semantic-recovery10-cases.json"
    cases_sha = _write_json(cases_path, cases)
    manifest_path = evidence / "semantic-recovery10-manifest.json"
    manifest_sha = _write_json(
        manifest_path,
        {
            "schema_version": semantic_recovery10.SCHEMA_VERSION,
            "dataset_id": semantic_recovery10.DATASET_ID,
            "classification": "exposed_targeted_regression_diagnostic",
            "human_product_verdict": False,
            "disclaimer": "Targeted regression only.",
            "cases_total": 10,
            "cases_sha256": cases_sha,
        },
    )
    preflight = tmp_path / "preflight.receipt"
    started = tmp_path / "run.started"
    _write_receipt(
        preflight,
        {
            "schema_version": "semantic-recovery10-preflight-v1",
            "candidate_sha": RUNTIME_SHA,
            "production_runtime_sha": "a" * 40,
            "production_snapshot_sha256": "b" * 64,
            "cases_sha256": cases_sha,
            "manifest_sha256": manifest_sha,
            "kb_seed_sha256": "c" * 64,
            "cases_total": "10",
            "cost_cap_rub": "200",
            "channels_status": "HDE_VK_DISABLED",
            "capacity_status": "GO",
            "mem_available_mib": "9000",
            "swap_free_mib": "7000",
            "load1": "0.10",
            "nproc": "6",
            "docker_free_gib": "20.00",
        },
    )
    _write_receipt(
        started,
        {
            "schema_version": "semantic-recovery10-run-started-v1",
            "candidate_sha": RUNTIME_SHA,
            "cases_sha256": cases_sha,
            "manifest_sha256": manifest_sha,
            "approval_id": APPROVAL_ID,
            "cost_cap_rub": "200",
        },
    )
    marker_time = datetime.now(UTC) - timedelta(minutes=3)
    timestamp = marker_time.timestamp()
    os.utime(started, (timestamp, timestamp))
    return {
        "evidence": evidence,
        "ledger": ledger,
        "cases": cases,
        "cases_sha": cases_sha,
        "manifest_sha": manifest_sha,
        "preflight": preflight,
        "started": started,
        "marker_time": marker_time,
    }


def _reserve_exact(workspace: dict[str, Any]):
    return reserve_live_eval_cost(
        scope="ask-eval",
        run_id="ask-eval-recovery10-test",
        runtime_git_sha=RUNTIME_SHA,
        manifest_sha256=workspace["cases_sha"],
        case_count=10,
        approved_cap_rub=200,
        private_full=False,
        high_cost_approval_id=APPROVAL_ID,
        ledger_dir=workspace["ledger"],
        now=workspace["marker_time"] + timedelta(seconds=1),
    )


def _report(
    workspace: dict[str, Any],
    *,
    run_id: str,
    trace_count: int,
) -> dict[str, Any]:
    results = []
    for index, case in enumerate(workspace["cases"]):
        passed = index < 6
        results.append(
            {
                "id": case["id"],
                "passed": passed,
                "trace_found": index < trace_count,
                "cache_hit": False,
                "observed_behavior": "answer" if passed else "escalate",
                "was_escalated": not passed,
                "failure_reasons": [] if passed else ["unexpected_escalation"],
                "semantic_recovery_attempted": index < 8,
                "semantic_recovery_status": "ok" if index < 7 else "failed",
            }
        )
    return {
        "target": semantic_recovery10.TARGET,
        "cases_total": 10,
        "cases_file_sha256": workspace["cases_sha"],
        "results": results,
        "runtime_identity": {
            "expected_runtime_git_sha": RUNTIME_SHA,
            "verified_release_git_sha": RUNTIME_SHA,
            "matched_expected_runtime": True,
        },
        "cost_control": {
            "pricing_complete": True,
            "reservation": {
                "valid": True,
                "scope": "ask-eval",
                "run_id": run_id,
                "runtime_git_sha": RUNTIME_SHA,
                "manifest_sha256": workspace["cases_sha"],
                "case_count": 10,
                "approved_cap_rub": 200.0,
                "high_cost_approval_id": APPROVAL_ID,
            },
        },
        "llm_estimated_cost_rub": 12.5,
        "llm_budget_exceeded": False,
        "llm_budget_stopped": False,
        "llm_pricing_stopped": False,
        "latency_ms": {"p50": 1000, "p95": 2000},
        "eval_run_id": run_id,
        "run_started_at": "2026-08-14T00:00:00+00:00",
        "run_completed_at": "2026-08-14T00:01:00+00:00",
    }


def _diagnose(workspace: dict[str, Any]) -> dict[str, Any]:
    return diagnostic.diagnose_failed(
        evidence_dir=workspace["evidence"],
        preflight_receipt_path=workspace["preflight"],
        started_receipt_path=workspace["started"],
        ledger_dir=workspace["ledger"],
        expected_runtime_git_sha=RUNTIME_SHA,
        expected_cases_sha256=workspace["cases_sha"],
        expected_manifest_sha256=workspace["manifest_sha"],
        expected_approval_id=APPROVAL_ID,
    )


def test_recovers_safe_result_when_cli_failed_only_on_trace_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    reservation = _reserve_exact(workspace)
    report_path = workspace["evidence"] / "semantic-recovery10-ask-report.json"
    _write_json(
        report_path,
        _report(workspace, run_id=reservation.record["run_id"], trace_count=9),
    )
    monkeypatch.setattr(
        diagnostic,
        "_fetch_trace_aggregate_sync",
        lambda _run_id: {
            "status": "ok",
            "traces_total": 9,
            "distinct_cases": 9,
            "null_case_ids": 0,
            "cache_hits": 0,
            "cache_misses": 9,
            "errors": 0,
            "llm_cost_rub": 12.5,
        },
    )

    result = _diagnose(workspace)

    assert result["failure_stage"] == "post_report_cli_gate"
    assert result["failure_reasons"] == ["trace_coverage_below_100_percent"]
    assert result["quality_verdict_available"] is True
    assert result["recovered_safe_result"]["counts"]["passed"] == 6
    assert result["recovered_safe_result"]["diagnostic_gate"]["status"] == "STOP"
    assert result["retry_forbidden"] is True
    assert result["diagnostic_new_ask_calls"] == 0
    encoded = json.dumps(result, ensure_ascii=False).casefold()
    for forbidden in ('"query"', '"response"', '"request_id"', '"chunk_text"'):
        assert forbidden not in encoded


def test_classifies_missing_reservation_as_rolling_cap_rejection(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    reserve_live_eval_cost(
        scope="prior-diagnostic",
        run_id="prior-routine-run",
        runtime_git_sha="d" * 40,
        manifest_sha256=CASES_SHA_PLACEHOLDER,
        case_count=10,
        approved_cap_rub=150,
        private_full=False,
        high_cost_approval_id="owner-prior-routine-cap150",
        ledger_dir=workspace["ledger"],
        now=workspace["marker_time"] - timedelta(minutes=1),
    )

    result = _diagnose(workspace)

    assert result["reservation"]["status"] == "missing"
    assert result["reservation"]["requested_would_fit"] is False
    assert result["failure_stage"] == "before_cost_reservation"
    assert result["failure_reasons"] == ["rolling_24h_cap_rejected"]
    assert result["trace_aggregate"] == {"status": "not_bound"}
    assert result["quality_verdict_available"] is False


def test_classifies_partial_traces_after_exact_reservation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    _reserve_exact(workspace)
    monkeypatch.setattr(
        diagnostic,
        "_fetch_trace_aggregate_sync",
        lambda _run_id: {
            "status": "ok",
            "traces_total": 4,
            "distinct_cases": 4,
            "null_case_ids": 0,
            "cache_hits": 0,
            "cache_misses": 4,
            "errors": 1,
            "llm_cost_rub": 3.0,
        },
    )

    result = _diagnose(workspace)

    assert result["reservation"]["status"] == "exact"
    assert result["failure_stage"] == "case_execution_incomplete"
    assert result["failure_reasons"] == ["runtime_or_case_execution_failed"]
    assert result["trace_aggregate"]["traces_total"] == 4
    assert result["artifacts"]["raw_report_present"] is False
