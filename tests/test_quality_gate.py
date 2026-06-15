from __future__ import annotations

import json
from pathlib import Path

from eval.check_quality_gate import (
    GateConfig,
    build_quality_gate_report,
    write_markdown,
    write_report,
)


def test_quality_gate_passes_core_metrics_and_warns_on_threshold_calibration() -> None:
    report = build_quality_gate_report(
        retrieval_metrics={
            "recall_at_5": 0.92,
            "cases_total": 50,
            "cases_scored": 50,
            "generated_smoke_cases": False,
        },
        ask_metrics={
            "pass_rate": 0.95,
            "expected_chunk_hit_rate": 0.93,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "low_confidence_expected_chunk_hit_rate": 0.05,
            "cases_total": 50,
            "generated_smoke_cases": False,
        },
        threshold_suggestions={
            "current_low_threshold": 0.4,
            "recommended_low_threshold": 0.05,
        },
        generation_metrics={
            "cases_total": 50,
            "pass_rate": 0.96,
            "source_context_rate": 0.98,
            "verifier_hallucination_rate": 0.0,
        },
    )

    assert report["passed"] is True
    assert report["failed_checks"] == 0
    assert report["warning_checks"] == 1
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["retrieval_recall_at_5"]["status"] == "pass"
    assert checks["generation_pass_rate"]["status"] == "pass"
    assert checks["rag_low_threshold_calibration"]["status"] == "warn"


def test_quality_gate_fails_missing_or_bad_core_metrics() -> None:
    report = build_quality_gate_report(
        retrieval_metrics=None,
        ask_metrics={
            "pass_rate": 0.5,
            "expected_chunk_hit_rate": 0.8,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "low_confidence_expected_chunk_hit_rate": 0.2,
        },
        config=GateConfig(min_ask_pass_rate=0.9, max_low_confidence_hit_rate=0.1),
    )

    assert report["passed"] is False
    failed = {item["name"] for item in report["checks"] if item["status"] == "fail"}
    assert "retrieval_metrics_present" in failed
    assert "ask_pass_rate" in failed
    assert "ask_expected_chunk_hit_rate" in failed
    assert "ask_low_confidence_expected_chunk_hit_rate" in failed


def test_quality_gate_checks_forum_smoke_summary_when_provided() -> None:
    report = build_quality_gate_report(
        retrieval_metrics={"recall_at_5": 1.0, "cases_scored": 1},
        ask_metrics={
            "pass_rate": 1.0,
            "expected_chunk_hit_rate": 1.0,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "low_confidence_expected_chunk_hit_rate": 0.0,
        },
        forum_metrics={
            "cases_total": 29,
            "forums_total": 29,
            "pass_rate": 1.0,
            "expected_chunk_hit_rate": 1.0,
            "problem_forums": [],
        },
        config=GateConfig(min_forums_total=29),
    )

    checks = {item["name"]: item for item in report["checks"]}
    assert report["passed"] is True
    assert checks["forum_smoke_pass_rate"]["status"] == "pass"
    assert checks["forum_smoke_expected_chunk_hit_rate"]["status"] == "pass"
    assert checks["forum_smoke_problem_forums"]["status"] == "pass"
    assert checks["forum_smoke_forums_total"]["status"] == "pass"


def test_quality_gate_fails_bad_generation_metrics() -> None:
    report = build_quality_gate_report(
        retrieval_metrics={"recall_at_5": 1.0, "cases_scored": 1},
        ask_metrics={
            "pass_rate": 1.0,
            "expected_chunk_hit_rate": 1.0,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "low_confidence_expected_chunk_hit_rate": 0.0,
        },
        generation_metrics={
            "pass_rate": 0.5,
            "source_context_rate": 0.4,
            "verifier_hallucination_rate": 0.2,
        },
    )

    failed = {item["name"] for item in report["checks"] if item["status"] == "fail"}
    assert "generation_pass_rate" in failed
    assert "generation_source_context_rate" in failed
    assert "generation_verifier_hallucination_rate" in failed


def test_quality_gate_fails_forum_smoke_problem_forums() -> None:
    report = build_quality_gate_report(
        retrieval_metrics={"recall_at_5": 1.0, "cases_scored": 1},
        ask_metrics={
            "pass_rate": 1.0,
            "expected_chunk_hit_rate": 1.0,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "low_confidence_expected_chunk_hit_rate": 0.0,
        },
        forum_metrics={
            "cases_total": 2,
            "forums_total": 2,
            "pass_rate": 0.5,
            "expected_chunk_hit_rate": 1.0,
            "problem_forums": [{"forum": "Утро"}],
        },
    )

    failed = {item["name"] for item in report["checks"] if item["status"] == "fail"}
    assert "forum_smoke_pass_rate" in failed
    assert "forum_smoke_problem_forums" in failed


def test_quality_gate_writes_json_and_markdown(tmp_path: Path) -> None:
    report = build_quality_gate_report(
        retrieval_metrics={"recall_at_5": 1.0, "cases_scored": 1},
        ask_metrics={
            "pass_rate": 1.0,
            "expected_chunk_hit_rate": 1.0,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "low_confidence_expected_chunk_hit_rate": 0.0,
        },
    )
    output = tmp_path / "quality_gate.json"
    markdown = tmp_path / "quality_gate.md"

    write_report(output, report)
    write_markdown(markdown, report)

    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True
    assert "Quality Gate Report" in markdown.read_text(encoding="utf-8")
