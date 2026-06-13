from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.run_retrieval import (
    _normalize_case,
    build_seed_smoke_cases,
    compute_recall_at_k,
    run_eval,
)


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


def test_build_seed_smoke_cases_uses_intent_examples() -> None:
    cases = build_seed_smoke_cases(
        [
            {
                "chunk_id": "chunk_1",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Машук",
                "intent_examples": ["кто платит за дорогу"],
            },
            {
                "chunk_id": "chunk_2",
                "status": "archived",
                "intent_examples": ["старый вопрос"],
            },
        ]
    )

    assert cases == [
        {
            "id": "seed_smoke::chunk_1",
            "query": "Машук кто платит за дорогу",
            "filters": {"category": "форумы", "forum_normalized": "Машук"},
            "expected_chunk_ids": ["chunk_1"],
        }
    ]


def test_build_seed_smoke_cases_balances_categories_and_forums() -> None:
    records = [
        {
            "chunk_id": f"grant_{index}",
            "status": "published",
            "category": "grants",
            "intent_name": f"Grant topic {index}",
        }
        for index in range(1, 7)
    ] + [
        {
            "chunk_id": f"forum_a_{index}",
            "status": "published",
            "category": "forums",
            "forum_normalized": "Forum A",
            "intent_name": f"Forum topic {index}",
        }
        for index in range(1, 7)
    ]

    cases = build_seed_smoke_cases(records, max_cases=6)

    category_counts = {
        category: sum(case["filters"].get("category") == category for case in cases)
        for category in ("forums", "grants")
    }
    forum_a_count = sum(case["filters"].get("forum_normalized") == "Forum A" for case in cases)
    assert category_counts == {"forums": 3, "grants": 3}
    assert forum_a_count == 3


@pytest.mark.asyncio
async def test_run_eval_with_lexical_backend(tmp_path: Path) -> None:
    golden = tmp_path / "golden.json"
    output = tmp_path / "metrics.json"
    markdown = tmp_path / "metrics.md"
    kb_seed = tmp_path / "kb.json"
    kb_seed.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "travel",
                    "status": "published",
                    "text_clean": "Проезд до форума оплачивает участник.",
                    "category": "форумы",
                    "forum_normalized": "Машук",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    golden.write_text(
        json.dumps(
            [
                {
                    "id": "travel",
                    "query": "кто оплачивает проезд",
                    "category": "форумы",
                    "forum_normalized": "Машук",
                    "expected_chunks": ["travel"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    metrics = await run_eval(
        golden,
        output,
        top_k=5,
        backend="lexical",
        kb_seed_path=kb_seed,
        markdown_path=markdown,
    )

    assert metrics["backend"] == "lexical"
    assert metrics["recall_at_5"] == 1.0
    assert json.loads(output.read_text(encoding="utf-8"))["results"][0]["hit"] is True
    assert "Retrieval Eval Report" in markdown.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_eval_can_generate_lexical_smoke_cases(tmp_path: Path) -> None:
    golden = tmp_path / "golden.json"
    output = tmp_path / "metrics.json"
    kb_seed = tmp_path / "kb.json"
    golden.write_text("[]", encoding="utf-8")
    kb_seed.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "chat",
                    "status": "published",
                    "text_clean": "После отбора тебя добавят в организационный чат.",
                    "category": "форумы",
                    "forum_normalized": "Российский Север",
                    "intent_name": "Добавление в чат",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    metrics = await run_eval(
        golden,
        output,
        top_k=5,
        backend="lexical",
        kb_seed_path=kb_seed,
        auto_smoke_cases=True,
    )

    assert metrics["generated_smoke_cases"] is True
    assert metrics["cases_total"] == 1
    assert metrics["recall_at_5"] == 1.0
