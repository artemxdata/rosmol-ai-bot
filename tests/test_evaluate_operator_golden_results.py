from __future__ import annotations

from scripts.evaluate_operator_golden_results import (
    answer_alignment,
    fact_coverage_score,
    review_priority,
    score_case,
    token_overlap,
)


def test_token_overlap_detects_paraphrased_shared_content() -> None:
    _precision, _recall, f1 = token_overlap(
        "Подать заявку можно в личном кабинете на платформе.",
        "Заявку подай через личный кабинет платформы.",
    )

    assert f1 > 0.5


def test_fact_coverage_matches_urls_and_numbers() -> None:
    coverage, matched = fact_coverage_score(
        ["https://example.ru/path", "35"],
        "Участвовать можно до 35 лет. Подробнее: https://example.ru/path.",
    )

    assert coverage == 1.0
    assert matched == ["https://example.ru/path", "35"]


def test_answer_alignment_requires_answer_and_content_signal() -> None:
    assert (
        answer_alignment(
            answered_without_operator=True,
            token_f1=0.25,
            fact_coverage=0.0,
            facts_count=0,
        )
        is True
    )
    assert (
        answer_alignment(
            answered_without_operator=False,
            token_f1=0.9,
            fact_coverage=1.0,
            facts_count=2,
        )
        is False
    )


def test_score_case_marks_escalation_as_conversion_gap() -> None:
    row = score_case(
        {
            "id": "case-1",
            "query": "Как подать заявку?",
            "reference_answer": "Заявку можно подать через личный кабинет.",
            "reference_facts": [],
            "expected_behavior": "answer",
        },
        {
            "response": "Передаю обращение специалисту.",
            "observed_behavior": "escalate",
            "was_escalated": True,
            "escalation_reason": "retrieval_failed",
        },
    )

    assert row["answered_without_operator"] is False
    assert row["gap_candidate"] is True
    assert row["review_priority"] == "P0_conversion"


def test_score_case_accepts_eval_runner_cited_source_ids() -> None:
    row = score_case(
        {
            "id": "case-source",
            "query": "Как подать заявку?",
            "reference_answer": "Заявку можно подать в карточке мероприятия.",
            "reference_facts": [],
            "expected_behavior": "answer",
        },
        {
            "response": "Заявку можно подать в карточке мероприятия.",
            "observed_behavior": "answer",
            "was_escalated": False,
            "cited_source_ids": ["chunk-1"],
        },
    )

    assert row["cited_sources"] == ["chunk-1"]
    assert row["source_grounded"] is True
    assert row["review_priority"] == "P3_pass"


def test_review_priority_separates_source_and_content_failures() -> None:
    assert (
        review_priority(
            answered_without_operator=True,
            source_grounded=False,
            content_aligned=True,
        )
        == "P1_no_source"
    )
    assert (
        review_priority(
            answered_without_operator=True,
            source_grounded=True,
            content_aligned=False,
        )
        == "P1_content_mismatch"
    )
