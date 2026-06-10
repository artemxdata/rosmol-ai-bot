from __future__ import annotations

from eval.run_retrieval import _normalize_case, compute_recall_at_k


def test_compute_recall_at_k_counts_cases_with_expected_chunks() -> None:
    results = [
        {"expected_chunk_ids": ["a"], "retrieved_chunk_ids": ["a", "b"]},
        {"expected_chunk_ids": ["c"], "retrieved_chunk_ids": ["d"]},
        {"expected_chunk_ids": [], "retrieved_chunk_ids": ["x"]},
    ]

    assert compute_recall_at_k(results) == 0.5


def test_compute_recall_at_k_returns_none_without_scored_cases() -> None:
    assert compute_recall_at_k([{"expected_chunk_ids": [], "retrieved_chunk_ids": ["a"]}]) is None


def test_normalize_case_accepts_common_golden_fields() -> None:
    case = _normalize_case(
        {
            "case_id": "mashuk-travel",
            "question": "Кто оплачивает проезд на Машук?",
            "expected_chunks": "chunk_1",
            "forum_normalized": "Машук",
            "category": "форумы",
        }
    )

    assert case == {
        "id": "mashuk-travel",
        "query": "Кто оплачивает проезд на Машук?",
        "filters": {"forum_normalized": "Машук", "category": "форумы"},
        "expected_chunk_ids": ["chunk_1"],
    }
