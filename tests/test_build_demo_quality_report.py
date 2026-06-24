from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.build_demo_quality_report import build_demo_quality_report


def test_build_demo_quality_report_writes_summary_and_examples(tmp_path: Path) -> None:
    metrics = tmp_path / "ask_eval.json"
    output = tmp_path / "demo_quality.md"
    metrics.write_text(
        json.dumps(
            {
                "cases_total": 3,
                "pass_rate": 2 / 3,
                "http_success_rate": 1.0,
                "expected_cited_or_equivalent_chunk_hit_rate": 0.95,
                "escalation_rate": 1 / 3,
                "cache_hit_rate": 0.1,
                "llm_estimated_cost_rub": 1.23,
                "llm_budget_rub": 5.0,
                "generator_model_counts": {
                    "source_chunk": 1,
                    "GigaChat/GigaChat-2-Max": 1,
                },
                "escalation_reason_counts": {"missing_source_citations": 1},
                "failure_reason_counts": {"expected_chunk_not_cited": 1},
                "results": [
                    {
                        "id": "simple",
                        "query": "Как зарегистрироваться?",
                        "response": "Через ФГАИС.",
                        "passed": True,
                        "generator_model": "source_chunk",
                        "was_escalated": False,
                        "cited_source_ids": ["registration"],
                        "cited_source_types": ["xlsx"],
                    },
                    {
                        "id": "complex",
                        "tags": ["complex"],
                        "query": "Я еду на форум, хочу понять проезд, проживание и документы.",
                        "response": "Проезд отдельно. Проживание отдельно.",
                        "passed": True,
                        "generator_model": "GigaChat/GigaChat-2-Max",
                        "was_escalated": False,
                        "cited_source_ids": ["travel", "housing"],
                        "cited_source_types": ["xlsx", "docx"],
                    },
                    {
                        "id": "failed",
                        "query": "Нет источников",
                        "response": "Передаю специалисту.",
                        "passed": False,
                        "generator_model": "source_only",
                        "was_escalated": True,
                        "escalation_reason": "missing_source_citations",
                        "failure_reasons": ["expected_chunk_not_cited"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = build_demo_quality_report(metrics, output, max_examples=2)
    markdown = output.read_text(encoding="utf-8")

    assert report["source_coverage_rate"] == 0.95
    assert report["examples"][0]["id"] == "complex"
    assert "# Demo Quality Report" in markdown
    assert "Pass rate: `66.7%`" in markdown
    assert "GigaChat/GigaChat-2-Max" in markdown
    assert "travel, housing" in markdown
    assert "missing_source_citations" in markdown


def test_build_demo_quality_report_rejects_zero_examples(tmp_path: Path) -> None:
    metrics = tmp_path / "ask_eval.json"
    metrics.write_text(json.dumps({"results": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="max_examples"):
        build_demo_quality_report(metrics, tmp_path / "demo.md", max_examples=0)
