from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import analyze_balanced50_runtime as analysis

RUNTIME_SHA = "a" * 40


def _result(ordinal: int) -> dict[str, object]:
    group = "typical" if ordinal <= 25 else "atypical"
    escalated = ordinal % 5 == 0
    passed = ordinal % 4 != 0
    return {
        "id": f"case-{ordinal:02d}",
        "query": f"PRIVATE_QUERY_SENTINEL_{ordinal}",
        "response": f"PRIVATE_RESPONSE_SENTINEL_{ordinal}",
        "tags": ["pilot50:v5", f"type:{group}"],
        "http_success": True,
        "trace_found": True,
        "cache_hit": False,
        "passed": passed,
        "failure_reasons": [] if passed else ["answer_contains_mismatch"],
        "observed_behavior": "escalate" if escalated else "answer",
        "was_escalated": escalated,
        "escalation_reason": "low_confidence" if escalated else None,
        "generator_model": "source_chunk" if ordinal % 2 else "GigaChat-2-Max",
        "generate_retry_reasons": ["coverage"] if ordinal % 7 == 0 else [],
        "expected_or_equivalent_chunk_hit": ordinal % 6 != 0,
        "expected_cited_or_equivalent_chunk_hit": ordinal % 8 != 0,
        "latency_ms": 1000 + ordinal,
    }


def _report() -> dict[str, object]:
    return {
        "cases_total": 50,
        "cases_passed": 38,
        "results": [_result(ordinal) for ordinal in range(1, 51)],
        "trace_coverage_rate": 1.0,
        "cache_hit_rate": 0.0,
        "llm_estimated_cost_rub": 12.5,
        "llm_budget_rub": 200.0,
        "llm_budget_exceeded": False,
        "llm_budget_stopped": False,
        "llm_pricing_stopped": False,
        "runtime_identity": {
            "required": False,
            "status": "observed_unbound",
            "expected_runtime_git_sha": None,
            "preflight_release_git_sha": RUNTIME_SHA,
            "postflight_release_git_sha": RUNTIME_SHA,
            "verified_release_git_sha": RUNTIME_SHA,
            "matched_expected_runtime": None,
        },
    }


def test_builds_text_free_global_typical_and_atypical_summary() -> None:
    summary = analysis.build_global_analysis(
        _report(),
        expected_runtime_sha=RUNTIME_SHA,
        report_sha256="b" * 64,
    )

    assert summary["status"] == "OK"
    assert summary["dataset_id"] == "pilot50_balanced_v5"
    assert summary["classification"] == "exposed_calibration_regression"
    assert summary["human_product_verdict"] is False
    assert summary["execution"] == {
        "cases_total": 50,
        "trace_coverage": 1.0,
        "cache_hits": 0,
        "llm_cost_rub": 12.5,
        "llm_cost_cap_rub": 200,
    }
    assert summary["outcomes"]["typical"]["total"] == 25
    assert summary["outcomes"]["atypical"]["total"] == 25
    assert summary["outcomes"]["overall"]["total"] == 50
    assert summary["failure_reason_counts"] == {"answer_contains_mismatch": 12}
    assert summary["escalation_reason_counts"] == {"low_confidence": 10}
    assert summary["generator_model_counts"] == {
        "GigaChat-2-Max": 25,
        "source_chunk": 25,
    }
    serialized = json.dumps(summary, ensure_ascii=False)
    assert "PRIVATE_QUERY_SENTINEL" not in serialized
    assert "PRIVATE_RESPONSE_SENTINEL" not in serialized
    assert "request_id" not in serialized


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value.update(cases_total=49), "cases_total_invalid"),
        (
            lambda value: value["results"][0].update(tags=["type:atypical"]),
            "group_counts_invalid",
        ),
        (
            lambda value: value["results"][0].update(trace_found=False),
            "trace_coverage_incomplete",
        ),
        (
            lambda value: value["results"][0].update(cache_hit=True),
            "cache_bypass_invalid",
        ),
        (
            lambda value: value["runtime_identity"].update(
                postflight_release_git_sha="c" * 40
            ),
            "runtime_postflight_sha_mismatch",
        ),
        (
            lambda value: value.update(llm_budget_stopped=True),
            "llm_budget_stopped",
        ),
    ],
)
def test_fails_closed_on_incomplete_or_unbound_report(mutation, reason: str) -> None:
    report = copy.deepcopy(_report())
    mutation(report)

    with pytest.raises(analysis.AnalysisError, match=reason):
        analysis.build_global_analysis(
            report,
            expected_runtime_sha=RUNTIME_SHA,
            report_sha256="b" * 64,
        )


def test_cli_writes_idempotent_safe_summary(tmp_path: Path, capsys) -> None:
    report_path = tmp_path / "report.json"
    output_path = tmp_path / "summary.json"
    report_path.write_text(json.dumps(_report()), encoding="utf-8")
    arguments = [
        "--report",
        str(report_path),
        "--output",
        str(output_path),
        "--expected-runtime-git-sha",
        RUNTIME_SHA,
    ]

    assert analysis.main(arguments) == 0
    first = output_path.read_bytes()
    assert analysis.main(arguments) == 0
    assert output_path.read_bytes() == first
    stdout = capsys.readouterr().out
    assert stdout.count("balanced50_global_analysis=OK") == 2
    assert "PRIVATE_QUERY_SENTINEL" not in stdout
    assert "PRIVATE_RESPONSE_SENTINEL" not in stdout


def test_cli_rejects_conflicting_existing_summary(tmp_path: Path, capsys) -> None:
    report_path = tmp_path / "report.json"
    output_path = tmp_path / "summary.json"
    report_path.write_text(json.dumps(_report()), encoding="utf-8")
    output_path.write_text("{}\n", encoding="utf-8")

    result = analysis.main(
        [
            "--report",
            str(report_path),
            "--output",
            str(output_path),
            "--expected-runtime-git-sha",
            RUNTIME_SHA,
        ]
    )

    assert result == 2
    assert capsys.readouterr().out == (
        "balanced50_global_analysis=FAIL reason=validation_failed\n"
    )
