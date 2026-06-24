from __future__ import annotations

import json
from pathlib import Path

from eval.audit_equivalent_chunks import (
    audit_equivalent_chunks,
    build_cases_with_equivalents,
    rescore_metrics_with_equivalents,
    write_markdown,
)


def test_audit_equivalent_chunks_marks_identical_text_as_auto_equivalent(
    tmp_path: Path,
) -> None:
    cases = tmp_path / "cases.json"
    metrics = tmp_path / "metrics.json"
    kb = tmp_path / "kb.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "case-1",
                    "query": "Where is the status?",
                    "expected_chunk_ids": ["expected"],
                    "expected_cited_chunk_ids": ["expected"],
                }
            ]
        ),
        encoding="utf-8",
    )
    metrics.write_text(
        json.dumps(
            {
                "target": "http://test/ask",
                "cases_path": str(cases),
                "results": [
                    {
                        "id": "case-1",
                        "failure_reasons": ["expected_chunk_not_cited"],
                        "cited_source_ids": ["neighbor"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    kb.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "expected",
                    "text_clean": "The status is available in the profile.",
                    "category": "platform",
                    "topic": "status",
                },
                {
                    "chunk_id": "neighbor",
                    "text_clean": "The status is available in the profile.",
                    "category": "platform",
                    "topic": "status",
                },
            ]
        ),
        encoding="utf-8",
    )

    report = audit_equivalent_chunks(
        cases_path=cases,
        metrics_path=metrics,
        kb_seed_path=kb,
    )

    assert report["auto_equivalent_pairs"] == 1
    assert report["rows"][0]["decision"] == "auto_equivalent"
    assert report["rows"][0]["exact_text_match"] is True


def test_build_cases_with_equivalents_adds_auto_pairs() -> None:
    cases = [
        {
            "id": "case-1",
            "query": "Where is the status?",
            "expected_chunk_ids": ["expected"],
            "expected_cited_chunk_ids": ["expected"],
        }
    ]
    report = {
        "rows": [
            {
                "case_id": "case-1",
                "expected_id": "expected",
                "cited_id": "neighbor",
                "decision": "auto_equivalent",
            }
        ]
    }

    enhanced = build_cases_with_equivalents(cases, report)

    assert enhanced[0]["equivalent_chunk_ids"] == {"expected": ["neighbor"]}


def test_rescore_metrics_with_equivalents_turns_equivalent_citation_into_pass() -> None:
    enhanced_cases = [
        {
            "id": "case-1",
            "query": "Where is the status?",
            "expected_chunk_ids": ["expected"],
            "expected_cited_chunk_ids": ["expected"],
            "equivalent_chunk_ids": {"expected": ["neighbor"]},
        }
    ]
    metrics = {
        "target": "http://test/ask",
        "cases_path": "cases.json",
        "results": [
            {
                "id": "case-1",
                "http_status": 200,
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Status answer.",
                "latency_ms": 10,
                "error": None,
                "observed_chunk_ids": ["expected", "neighbor"],
                "cited_source_ids": ["neighbor"],
                "was_escalated": False,
                "generator_model": "source_chunk",
                "cache_hit": False,
                "max_reranker_score": 0.9,
                "trace_total_latency_ms": 9,
            }
        ],
    }

    rescored = rescore_metrics_with_equivalents(
        enhanced_cases=enhanced_cases,
        metrics=metrics,
    )

    assert rescored["cases_passed"] == 1
    assert rescored["pass_rate"] == 1.0
    assert rescored["expected_cited_chunk_hit_rate"] == 0.0
    assert rescored["expected_cited_or_equivalent_chunk_hit_rate"] == 1.0


def test_write_markdown_does_not_include_raw_text(tmp_path: Path) -> None:
    output = tmp_path / "report.md"
    write_markdown(
        {
            "cases_path": "cases.json",
            "metrics_path": "metrics.json",
            "kb_seed_path": "kb.json",
            "candidate_pairs": 1,
            "auto_equivalent_pairs": 1,
            "needs_review_pairs": 0,
            "decision_counts": {"auto_equivalent": 1},
            "rows": [
                {
                    "decision": "auto_equivalent",
                    "case_id": "case-1",
                    "expected_id": "expected",
                    "cited_id": "neighbor",
                    "text_similarity": 1.0,
                    "token_jaccard": 1.0,
                    "same_topic": True,
                    "same_forum": True,
                }
            ],
        },
        output,
    )

    text = output.read_text(encoding="utf-8")
    assert "case-1" in text
    assert "The status is available" not in text
