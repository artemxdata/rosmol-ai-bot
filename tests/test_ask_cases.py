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
    source_type: str | None = None,
) -> dict:
    record = {
        "chunk_id": chunk_id,
        "status": "published",
        "category": category,
        "forum_normalized": forum,
        "intent_name": f"Intent {chunk_id}",
        "intent_examples": [example or f"question {chunk_id}"],
        "text_clean": f"Answer {chunk_id}",
    }
    if source_type:
        record["source_type"] = source_type
    return record


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


def test_select_balanced_records_respects_source_type_limits() -> None:
    records = [
        _record("ticket_1", "forums", source_type="ticket_answer_bank"),
        _record("ticket_2", "grants", source_type="ticket_answer_bank"),
        _record("ticket_3", "platform", source_type="ticket_answer_bank"),
        _record("xlsx_1", "forums", source_type="xlsx"),
        _record("xlsx_2", "grants", source_type="xlsx"),
        _record("docx_1", "forums", source_type="docx"),
    ]

    selected = select_balanced_records(
        records,
        max_cases=5,
        source_type_limits={"ticket_answer_bank": 3, "xlsx": 1, "docx": 1},
    )

    assert [record["chunk_id"] for record in selected] == [
        "ticket_1",
        "ticket_2",
        "ticket_3",
        "xlsx_1",
        "docx_1",
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


def test_build_seed_ask_cases_can_require_expected_citation() -> None:
    cases = build_seed_ask_cases(
        [_record("ticket_1", "grants", source_type="ticket_answer_bank")],
        require_cited_chunks=True,
    )

    assert cases[0]["expected_cited_chunk_ids"] == ["ticket_1"]
    assert "source_type:ticket_answer_bank" in cases[0]["tags"]


def test_seed_smoke_query_ignores_fallback_source_category_prefix() -> None:
    record = _record("fallback_1", "навигация", example="id not visible")
    record["source_category"] = "fallback"

    assert seed_smoke_query(record) == "id not visible"


def test_seed_smoke_query_uses_forum_prefix_only_for_forum_category() -> None:
    forum_record = _record(
        "forum_1",
        "форумы",
        forum="Машук",
        example="какие документы нужны",
    )
    grant_record = _record(
        "grant_1",
        "гранты",
        forum="Гранты для физических лиц",
        example="где подать проект на грант",
    )
    grant_record["source_category"] = "Машук"

    assert seed_smoke_query(forum_record) == "Машук какие документы нужны"
    assert seed_smoke_query(grant_record) == "где подать проект на грант"


def test_seed_smoke_query_prefers_generic_category_example() -> None:
    record = _record(
        "grant_1",
        "гранты",
        forum="Гранты для физических лиц",
        example="есть ли гранты на смене физическая культура и спорт",
    )
    record["intent_examples"].extend(
        [
            "где подать проект на грант",
            "есть ли росмолодёжь гранты на бирюсе",
        ]
    )

    assert seed_smoke_query(record) == "где подать проект на грант"


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
