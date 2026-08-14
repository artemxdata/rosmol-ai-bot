from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import semantic_recovery10 as recovery10


def _write(path: Path, value: object) -> str:
    data = recovery10._canonical_json_bytes(value)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _prior_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    cases: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for index in range(50):
        group = "typical" if index < 25 else "atypical"
        case_id = f"case-{index:02d}"
        cases.append(
            {
                "id": case_id,
                "query": f"question {index}",
                "user_id": f"prior-{index}",
                "channel": "api",
                "pilot50_group": group,
                "tags": [f"type:{group}"],
                "expected_behavior": "answer",
                "expected_escalated": False,
            }
        )
        within_group = index if group == "typical" else index - 25
        recoverable = within_group < 6
        results.append(
            {
                "id": case_id,
                "passed": False if within_group < 8 else True,
                "was_escalated": recoverable,
                "escalation_reason": "low_confidence" if recoverable else None,
                "failure_reasons": (
                    ["unexpected_escalation"]
                    if recoverable
                    else ["answer_contains_mismatch"]
                ),
            }
        )
    cases_path = tmp_path / "prior-cases.json"
    cases_sha = _write(cases_path, cases)
    report_path = tmp_path / "prior-report.json"
    report_sha = _write(
        report_path,
        {"cases_file_sha256": cases_sha, "results": results},
    )
    recovery10.PRIOR_CASES_SHA256 = cases_sha
    recovery10.PRIOR_REPORT_SHA256 = report_sha
    return cases_path, report_path


def test_prepare_selects_five_failed_recoverable_rows_per_group(tmp_path: Path) -> None:
    prior_cases, prior_report = _prior_artifacts(tmp_path)
    cases_path = tmp_path / "cases.json"
    manifest_path = tmp_path / "manifest.json"

    receipt = recovery10.prepare(
        prior_cases_path=prior_cases,
        prior_report_path=prior_report,
        output_cases_path=cases_path,
        output_manifest_path=manifest_path,
    )

    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [case["id"] for case in cases] == [
        "case-00",
        "case-01",
        "case-02",
        "case-03",
        "case-04",
        "case-25",
        "case-26",
        "case-27",
        "case-28",
        "case-29",
    ]
    assert receipt["cases_total"] == 10
    assert manifest["group_counts"] == {"atypical": 5, "typical": 5}
    assert manifest["human_product_verdict"] is False
    assert all(row["passed"] is False for row in manifest["baseline"])


def _candidate_report(
    cases: list[dict[str, Any]],
    *,
    cases_sha: str,
    runtime_sha: str,
    approval_id: str,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        passed = index < 6
        results.append(
            {
                "id": case["id"],
                "passed": passed,
                "trace_found": True,
                "cache_hit": False,
                "observed_behavior": "answer" if passed else "escalate",
                "was_escalated": not passed,
                "failure_reasons": [] if passed else ["unexpected_escalation"],
                "semantic_recovery_attempted": index < 8,
                "semantic_recovery_status": "ok" if index < 7 else "failed",
            }
        )
    return {
        "target": recovery10.TARGET,
        "cases_total": 10,
        "cases_file_sha256": cases_sha,
        "results": results,
        "runtime_identity": {
            "expected_runtime_git_sha": runtime_sha,
            "verified_release_git_sha": runtime_sha,
            "matched_expected_runtime": True,
        },
        "cost_control": {
            "pricing_complete": True,
            "reservation": {
                "valid": True,
                "runtime_git_sha": runtime_sha,
                "manifest_sha256": cases_sha,
                "case_count": 10,
                "approved_cap_rub": 200.0,
                "high_cost_approval_id": approval_id,
            },
        },
        "llm_estimated_cost_rub": 7.5,
        "llm_budget_exceeded": False,
        "llm_budget_stopped": False,
        "llm_pricing_stopped": False,
        "latency_ms": {"p50": 100, "p95": 200},
        "eval_run_id": "ask-eval-safe",
        "run_started_at": "2026-08-14T00:00:00+00:00",
        "run_completed_at": "2026-08-14T00:01:00+00:00",
    }


def test_summarize_emits_only_safe_aggregate_and_go_gate(tmp_path: Path) -> None:
    prior_cases, prior_report = _prior_artifacts(tmp_path)
    cases_path = tmp_path / "cases.json"
    manifest_path = tmp_path / "manifest.json"
    recovery10.prepare(
        prior_cases_path=prior_cases,
        prior_report_path=prior_report,
        output_cases_path=cases_path,
        output_manifest_path=manifest_path,
    )
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases_sha = recovery10._file_sha256(cases_path)
    runtime_sha = "a" * 40
    approval_id = f"owner-chat-semantic10-{runtime_sha}-cap200"
    report_path = tmp_path / "report.json"
    _write(
        report_path,
        _candidate_report(
            cases,
            cases_sha=cases_sha,
            runtime_sha=runtime_sha,
            approval_id=approval_id,
        ),
    )
    output_path = tmp_path / "safe.json"

    safe = recovery10.summarize(
        manifest_path=manifest_path,
        cases_path=cases_path,
        report_path=report_path,
        output_path=output_path,
        expected_runtime_git_sha=runtime_sha,
        expected_approval_id=approval_id,
    )

    assert safe["diagnostic_gate"]["status"] == "GO"
    assert safe["counts"] == {
        "total": 10,
        "passed": 6,
        "no_operator": 6,
        "trace_found": 10,
        "cache_hits": 0,
        "semantic_recovery_attempted": 8,
        "semantic_recovery_succeeded": 7,
    }
    assert recovery10.show_safe(output_path) == safe
    encoded = output_path.read_text(encoding="utf-8").casefold()
    assert '"query"' not in encoded
    assert '"response"' not in encoded


def test_summarize_rejects_runtime_identity_mismatch(tmp_path: Path) -> None:
    prior_cases, prior_report = _prior_artifacts(tmp_path)
    cases_path = tmp_path / "cases.json"
    manifest_path = tmp_path / "manifest.json"
    recovery10.prepare(
        prior_cases_path=prior_cases,
        prior_report_path=prior_report,
        output_cases_path=cases_path,
        output_manifest_path=manifest_path,
    )
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases_sha = recovery10._file_sha256(cases_path)
    runtime_sha = "a" * 40
    approval_id = f"owner-chat-semantic10-{runtime_sha}-cap200"
    report = _candidate_report(
        cases,
        cases_sha=cases_sha,
        runtime_sha=runtime_sha,
        approval_id=approval_id,
    )
    report["runtime_identity"]["verified_release_git_sha"] = "b" * 40
    report_path = tmp_path / "report.json"
    _write(report_path, report)

    with pytest.raises(ValueError, match="runtime identity"):
        recovery10.summarize(
            manifest_path=manifest_path,
            cases_path=cases_path,
            report_path=report_path,
            output_path=tmp_path / "safe.json",
            expected_runtime_git_sha=runtime_sha,
            expected_approval_id=approval_id,
        )
