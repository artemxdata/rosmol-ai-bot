from __future__ import annotations

from eval.ask_cases import (
    build_seed_ask_cases,
    seed_smoke_query,
    select_balanced_records,
    summarize_cases,
)


def _record(
    chunk_id: str,
    category: str,
    *,
    forum: str | None = None,
    example: str | None = None,
) -> dict:
    return {
        "chunk_id": chunk_id,
        "status": "published",
        "category": category,
        "forum_normalized": forum,
        "intent_name": f"Intent {chunk_id}",
        "intent_examples": [example or f"question {chunk_id}"],
        "text_clean": f"Answer {chunk_id}",
    }


def test_select_balanced_records_round_robins_categories() -> None:
    records = [
        _record("forum_1", "форумы", forum="Машук"),
        _record("forum_2", "форумы", forum="Машук"),
        _record("forum_3", "форумы", forum="Машук"),
        _record("grant_1", "гранты"),
        _record("grant_2", "гранты"),
        _record("tech_1", "техподдержка"),
    ]

    selected = select_balanced_records(records, max_cases=4, per_forum_limit=1)

    assert [record["chunk_id"] for record in selected] == [
        "forum_1",
        "grant_1",
        "tech_1",
        "grant_2",
    ]


def test_build_seed_ask_cases_adds_expected_chunk_and_tags() -> None:
    cases = build_seed_ask_cases([_record("grant_1", "гранты")], user_prefix="local")

    assert cases == [
        {
            "id": "seed_balanced::grant_1",
            "query": "question grant_1",
            "user_id": "local-1",
            "channel": "api",
            "expected_chunk_ids": ["grant_1"],
            "expected_answer_contains": [],
            "expected_escalated": None,
            "expected_escalation_reason": None,
            "expected_generator_model": None,
            "tags": ["seed_balanced", "category:гранты"],
        }
    ]


def test_seed_smoke_query_ignores_fallback_source_category_prefix() -> None:
    record = _record("fallback_1", "навигация", example="id not visible")
    record["source_category"] = "fallback"

    assert seed_smoke_query(record) == "id not visible"


def test_summarize_cases_counts_categories_and_forums() -> None:
    cases = build_seed_ask_cases(
        [
            _record("forum_1", "форумы", forum="Машук"),
            _record("grant_1", "гранты"),
        ]
    )

    summary = summarize_cases(cases)

    assert summary["cases_total"] == 2
    assert summary["category_counts"] == {"форумы": 1, "гранты": 1}
    assert summary["forum_counts_top"] == {"Машук": 1}
