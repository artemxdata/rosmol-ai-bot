from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.compare_full_operator_audits import compare_audits


def _audit(direct: float, *, missing: int = 0) -> dict:
    quality = {
        "cases": 10,
        "direct_answers": int(direct * 10),
        "direct_conversion": direct,
        "contained_without_operator": 9,
        "containment_rate": 0.9,
        "escalated": 1,
        "escalation_rate": 0.1,
        "source_grounded_direct_answers": int(direct * 10),
        "source_grounded_direct_answer_rate": direct,
        "strict_grounded_answers": int(direct * 10),
        "strict_grounded_answer_rate": direct,
    }
    return {
        "coverage": {
            "source_rows_total": 11,
            "runnable_rows": 10,
            "empty_queries_excluded": 1,
            "missing_results": missing,
        },
        "quality": quality,
        "golden_eligible_quality": quality,
        "context_limited_quality": quality,
        "category_metrics": [{"category": "форумы", **quality}],
        "latency_ms": {"p50": 100, "p95": 500},
        "cost": {"llm_estimated_cost_rub": 1.0},
    }


def test_compare_audits_builds_aggregate_report(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    post_fix = tmp_path / "post.json"
    output = tmp_path / "comparison.json"
    scenario = tmp_path / "context.json"
    dialog_scenario = tmp_path / "dialog.json"
    baseline.write_text(json.dumps(_audit(0.5)), encoding="utf-8")
    post_fix.write_text(json.dumps(_audit(0.7)), encoding="utf-8")
    scenario.write_text(
        json.dumps(
            {
                "cases_total": 20,
                "observed_behavior_counts": {"answer": 19, "escalate": 1},
                "http_success_rate": 1.0,
                "trace_coverage_rate": 1.0,
                "latency_ms": {"p50": 100, "p95": 400},
                "llm_estimated_cost_rub": 0.5,
            }
        ),
        encoding="utf-8",
    )
    dialog_scenario.write_text(
        json.dumps(
            {
                "conversations_total": 3,
                "conversations_executed": 3,
                "conversation_pass_rate": 1.0,
                "turns_total": 15,
                "turn_pass_rate": 1.0,
                "http_success_rate": 1.0,
                "trace_coverage_rate": 1.0,
                "llm_estimated_cost_rub": 1.25,
            }
        ),
        encoding="utf-8",
    )

    report = compare_audits(
        baseline,
        post_fix,
        output,
        context_scenario_path=scenario,
        dialog_scenario_paths=[dialog_scenario],
    )

    assert report["overall"]["delta"]["direct_conversion"] == pytest.approx(0.2)
    assert report["category_comparison"][0]["direct_conversion_delta"] == pytest.approx(
        0.2
    )
    assert output.exists()
    assert output.with_suffix(".md").exists()
    assert report["explicit_channel_context_scenario"]["direct_conversion"] == 0.95
    assert report["multi_turn_scenarios"][0]["turns_total"] == 15
    assert "Сценарий с контекстом канала HDE" in output.with_suffix(".md").read_text(
        encoding="utf-8"
    )
    assert "Многошаговые диалоги" in output.with_suffix(".md").read_text(
        encoding="utf-8"
    )


def test_compare_audits_rejects_incomplete_post_fix(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    post_fix = tmp_path / "post.json"
    baseline.write_text(json.dumps(_audit(0.5)), encoding="utf-8")
    post_fix.write_text(json.dumps(_audit(0.7, missing=1)), encoding="utf-8")

    with pytest.raises(ValueError, match="incomplete"):
        compare_audits(baseline, post_fix, tmp_path / "comparison.json")
