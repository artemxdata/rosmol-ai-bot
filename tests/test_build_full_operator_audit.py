from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_full_operator_audit import (
    build_full_operator_audit,
    is_context_limited,
    merge_results,
)


def test_merge_results_uses_last_result_and_counts_duplicates(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(
        json.dumps({"results": [{"id": "case-1", "response": "old"}]}),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({"results": [{"id": "case-1", "response": "new"}]}),
        encoding="utf-8",
    )

    merged, duplicates = merge_results([first, second])

    assert duplicates == 1
    assert merged["case-1"]["response"] == "new"


def test_context_limited_detects_context_free_clarification() -> None:
    assert is_context_limited(
        {
            "query": "Где мой билет?",
            "forum_normalized": None,
            "category": "другое",
            "candidate_chunk_ids": [],
            "golden_exclusion_reasons": ["unsupported_category"],
        }
    )
    assert not is_context_limited(
        {
            "query": "Где мой билет на День молодёжи?",
            "forum_normalized": "День молодёжи",
            "category": "форумы",
            "candidate_chunk_ids": ["chunk-1"],
            "golden_exclusion_reasons": [],
        }
    )


def test_build_full_operator_audit_separates_conversion_and_containment(
    tmp_path: Path,
) -> None:
    cases = tmp_path / "cases.json"
    evaluation = tmp_path / "eval.json"
    empty = tmp_path / "empty.json"
    output = tmp_path / "audit.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "answer",
                    "query": "Как подать заявку на форум?",
                    "expected_behavior": "answer",
                    "golden_eligible": True,
                    "reference_facts": ["35"],
                    "department": "VK",
                },
                {
                    "id": "clarify",
                    "query": "Где билет?",
                    "expected_behavior": "answer",
                    "golden_eligible": False,
                    "category": "другое",
                    "department": "MAX Бот",
                    "golden_exclusion_reasons": ["unsupported_category"],
                },
                {
                    "id": "escalate",
                    "query": "Позови оператора",
                    "expected_behavior": "escalate",
                    "golden_eligible": False,
                    "department": "HDE",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    evaluation.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "id": "answer",
                        "response": "Участвовать можно до 35 лет.",
                        "observed_behavior": "answer",
                        "cited_source_ids": ["chunk-1"],
                    },
                    {
                        "id": "clarify",
                        "response": "Уточни событие.",
                        "observed_behavior": "clarify",
                    },
                    {
                        "id": "escalate",
                        "response": "Передаю специалисту.",
                        "observed_behavior": "escalate",
                        "was_escalated": True,
                        "escalation_reason": "operator_requested",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    empty.write_text("[{}]", encoding="utf-8")

    report = build_full_operator_audit(
        cases,
        [evaluation],
        output,
        empty_cases_path=empty,
    )

    assert report["coverage"]["source_rows_total"] == 4
    assert report["quality"]["direct_conversion"] == pytest.approx(1 / 3)
    assert report["quality"]["containment_rate"] == pytest.approx(2 / 3)
    assert report["golden_eligible_quality"]["strict_grounded_answers"] == 1
    assert report["context_limited_quality"]["cases"] == 1
    assert report["context_limited_by_department"][0]["department"] == "MAX Бот"
    assert output.exists()
    assert output.with_suffix(".md").exists()
