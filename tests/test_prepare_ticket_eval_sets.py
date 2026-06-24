from __future__ import annotations

from scripts.prepare_ticket_eval_sets import (
    build_chunk_index,
    match_chunks,
    prepare_case,
    retrieval_case,
    select_balanced_cases,
)


def test_match_chunks_prefers_same_forum_and_answer_overlap() -> None:
    chunks = build_chunk_index(
        [
            {
                "chunk_id": "mashuk_registration",
                "text_clean": "Подать заявку на форум Машук можно через личный кабинет.",
                "category": "платформа_фгаис",
                "topic": "регистрация_и_заявка",
                "forum_normalized": "Машук",
                "status": "published",
            },
            {
                "chunk_id": "utro_registration",
                "text_clean": "Подать заявку на форум Утро можно через личный кабинет.",
                "category": "платформа_фгаис",
                "topic": "регистрация_и_заявка",
                "forum_normalized": "Утро",
                "status": "published",
            },
        ]
    )

    matches = match_chunks(
        {
            "query": "Как подать заявку на Машук?",
            "expected_answer": "Подать заявку на форум Машук можно через личный кабинет.",
            "category": "платформа_фгаис",
            "forum_normalized": "Машук",
        },
        chunks,
        top_matches=2,
    )

    assert matches[0]["chunk_id"] == "mashuk_registration"
    assert matches[0]["score"] > matches[1]["score"]


def test_prepare_case_marks_weak_chunk_labels_for_review() -> None:
    chunks = build_chunk_index(
        [
            {
                "chunk_id": "grant_return",
                "text_clean": "Для возврата грантовых средств напишите в поддержку.",
                "category": "гранты",
                "topic": "грантовая_отчетность",
                "status": "published",
            }
        ]
    )

    case = prepare_case(
        {
            "id": "ticket::1",
            "query": "Как вернуть грантовые средства?",
            "expected_answer": "Для возврата грантовых средств напишите в поддержку.",
            "expected_escalated": False,
            "expected_answer_contains": ["вернуть грантовые средства"],
            "category": "гранты",
            "topic": "грантовая_отчетность",
            "difficulty": "simple",
            "source_ticket_ids": ["1"],
        },
        chunks,
        min_chunk_score=0.1,
        top_matches=3,
    )

    assert case["expected_chunk_ids"] == ["grant_return"]
    assert case["expected_cited_chunk_ids"] == ["grant_return"]
    assert case["needs_review"] is False
    assert case["tags"][:4] == [
        "ticket_analysis",
        "category:гранты",
        "topic:грантовая_отчетность",
        "difficulty:simple",
    ]


def test_retrieval_case_contains_filters_and_expected_chunks() -> None:
    item = {
        "id": "case",
        "query": "Вопрос",
        "forum_normalized": "Машук",
        "category": "форумы",
        "topic": "регистрация",
        "expected_chunk_ids": ["chunk"],
        "candidate_chunk_matches": [
            {
                "chunk_id": "chunk",
                "forum_normalized": "Машук",
                "category": "форумы",
            }
        ],
        "source_ticket_ids": ["1"],
        "best_chunk_match_score": 0.5,
    }

    assert retrieval_case(item) == {
        "id": "case",
        "query": "Вопрос",
        "filters": {
            "forum_normalized": "Машук",
            "category": "форумы",
        },
        "expected_chunk_ids": ["chunk"],
        "source_ticket_ids": ["1"],
        "best_chunk_match_score": 0.5,
    }


def test_retrieval_case_uses_expected_chunk_metadata_without_ticket_forum_fallback() -> None:
    item = {
        "id": "case",
        "query": "Р’РѕРїСЂРѕСЃ",
        "forum_normalized": "РњР°С€СѓРє",
        "category": "РѕР±С‰РµРµ",
        "expected_chunk_ids": ["fallback"],
        "candidate_chunk_matches": [
            {
                "chunk_id": "fallback",
                "forum_normalized": None,
                "category": "РѕР±С‰РµРµ",
            }
        ],
    }

    assert retrieval_case(item)["filters"] == {"category": "РѕР±С‰РµРµ"}


def test_select_balanced_cases_round_robins_groups() -> None:
    cases = [
        {"id": "a", "category": "форумы", "difficulty": "simple", "expected_escalated": False},
        {"id": "b", "category": "форумы", "difficulty": "simple", "expected_escalated": False},
        {"id": "c", "category": "гранты", "difficulty": "complex", "expected_escalated": True},
    ]

    selected = select_balanced_cases(cases, 2)

    assert [item["id"] for item in selected] == ["c", "a"]
