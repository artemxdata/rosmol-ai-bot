from __future__ import annotations

import json
from pathlib import Path

from eval.suggest_rag_thresholds import analyze_thresholds, write_markdown, write_report


def test_analyze_thresholds_recommends_low_threshold_from_hit_retention() -> None:
    report = analyze_thresholds(
        {
            "results": [
                {
                    "expected_chunk_ids": ["a"],
                    "expected_chunk_hit": True,
                    "max_reranker_score": 0.9,
                    "escalation_reason": None,
                },
                {
                    "expected_chunk_ids": ["b"],
                    "expected_chunk_hit": True,
                    "max_reranker_score": 0.05,
                    "escalation_reason": "low_confidence",
                },
                {
                    "expected_chunk_ids": ["c"],
                    "expected_chunk_hit": False,
                    "max_reranker_score": 0.03,
                    "escalation_reason": "low_confidence",
                },
            ]
        },
        current_low=0.4,
        current_high=0.7,
        target_hit_retention=1.0,
        candidates=[0.0, 0.03, 0.05, 0.1, 0.4, 0.7],
    )

    assert report["scored_cases"] == 3
    assert report["expected_chunk_hits"] == 2
    assert report["expected_chunk_misses"] == 1
    assert report["low_confidence_expected_chunk_hits"] == 1
    assert report["recommended_low_threshold"] == 0.05
    assert report["recommended_high_threshold"] == 0.7
    assert report["hit_score"] == {"min": 0.05, "p50": 0.05, "p90": 0.9, "max": 0.9}

    by_threshold = {row["threshold"]: row for row in report["threshold_candidates"]}
    assert by_threshold[0.05]["hit_retention_rate"] == 1.0
    assert by_threshold[0.05]["miss_rejection_rate"] == 1.0
    assert by_threshold[0.4]["hit_retention_rate"] == 0.5


def test_threshold_report_writes_json_and_markdown(tmp_path: Path) -> None:
    report = analyze_thresholds(
        {
            "results": [
                {
                    "expected_chunk_ids": ["a"],
                    "expected_chunk_hit": True,
                    "max_reranker_score": 0.2,
                }
            ]
        },
        candidates=[0.1, 0.2],
    )
    output = tmp_path / "thresholds.json"
    markdown = tmp_path / "thresholds.md"

    write_report(output, report)
    write_markdown(markdown, report)

    assert json.loads(output.read_text(encoding="utf-8"))["recommended_low_threshold"] == 0.2
    assert "RAG Threshold Suggestions" in markdown.read_text(encoding="utf-8")
