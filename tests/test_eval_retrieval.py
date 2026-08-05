from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.run_retrieval import (
    _normalize_case,
    build_seed_smoke_cases,
    compute_recall,
    compute_recall_at_k,
    rank_summary,
    run_eval,
    run_private_yonote_candidate_audit,
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


def test_compute_recall_supports_cutoffs() -> None:
    results = [
        {"expected_chunk_ids": ["c"], "retrieved_chunk_ids": ["a", "b", "c"]},
        {"expected_chunk_ids": ["d"], "retrieved_chunk_ids": ["d", "e", "f"]},
    ]

    assert compute_recall(results, cutoff=1) == 0.5
    assert compute_recall(results, cutoff=2) == 0.5
    assert compute_recall(results, cutoff=3) == 1.0
    assert compute_recall(results) == 1.0


def test_rank_summary_counts_expected_positions() -> None:
    results = [
        {"expected_chunk_ids": ["c"], "retrieved_chunk_ids": ["a", "b", "c"], "expected_rank": 3},
        {"expected_chunk_ids": ["d"], "retrieved_chunk_ids": ["d", "e"], "expected_rank": 1},
        {"expected_chunk_ids": ["x"], "retrieved_chunk_ids": ["y"], "expected_rank": None},
    ]

    summary = rank_summary(results)

    assert summary["hits"] == 2
    assert summary["misses"] == 1
    assert summary["mrr"] == 0.666667
    assert summary["avg_expected_rank"] == 2.0
    assert summary["expected_rank_histogram"] == {"1": 1, "3": 1}


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


def test_build_seed_smoke_cases_ignores_fallback_source_category_prefix() -> None:
    cases = build_seed_smoke_cases(
        [
            {
                "chunk_id": "fallback_1",
                "status": "published",
                "category": "навигация",
                "source_category": "fallback",
                "intent_examples": ["id not visible"],
            }
        ]
    )

    assert cases[0]["query"] == "id not visible"


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
    result = json.loads(output.read_text(encoding="utf-8"))["results"][0]
    assert result["hit"] is True
    assert result["expected_rank"] == 1
    assert metrics["mrr"] == 1.0
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


def test_private_candidate_audit_is_user_only_and_yonote_only(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    source = private_root / "tickets.jsonl"
    output = private_root / "candidate_audit.json"
    kb_seed = tmp_path / "kb.json"
    source.write_text(
        json.dumps(
            {
                "ticket_id": "raw-ticket-42",
                "user_turns": ["Как зарегистрироваться?", "Именно на форум Машук"],
                "bot_turns": ["SECRET BOT ANSWER"],
                "closed_without_operator": True,
                "category": "label-must-be-ignored",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    kb_seed.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "yonote_published",
                    "status": "published",
                    "source_type": "yonote",
                    "text_clean": "Регистрация на форум Машук открыта.",
                },
                {
                    "chunk_id": "xlsx_published",
                    "status": "published",
                    "source_type": "xlsx",
                    "text_clean": "Регистрация на форум Машук открыта.",
                },
                {
                    "chunk_id": "yonote_archived",
                    "status": "archived",
                    "source_type": "yonote",
                    "text_clean": "Регистрация на форум Машук открыта.",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = run_private_yonote_candidate_audit(
        source,
        output,
        kb_seed_path=kb_seed,
        top_k=5,
        private_root=private_root,
    )

    assert report["schema_version"] == "private-yonote-candidate-audit-v1"
    assert report["cases_total"] == 1
    assert report["user_turns_total"] == 2
    assert report["multi_turn_cases"] == 1
    assert report["qrels_status"] == "not_available"
    assert report["metrics_status"] == "unscored"
    assert report["source_filter"] == {"status": "published", "source_type": "yonote"}
    result = report["results"][0]
    assert result["ticket_id_hash"] != "raw-ticket-42"
    assert len(result["ticket_id_hash"]) == 24
    assert len(result["turns"]) == 2
    assert result["turns"][0]["query_sha256"] != result["turns"][1]["query_sha256"]
    assert {
        candidate["chunk_id"]
        for turn in result["turns"]
        for candidate in turn["candidates"]
    } == {"yonote_published"}
    assert [item["chunk_id"] for item in result["union_candidates"]] == [
        "yonote_published"
    ]
    serialized = output.read_text(encoding="utf-8")
    assert "raw-ticket-42" not in serialized
    assert "SECRET BOT ANSWER" not in serialized
    assert "label-must-be-ignored" not in serialized
    assert "Как зарегистрироваться?" not in serialized


def test_private_candidate_audit_ignores_bot_and_conversion_fields(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    kb_seed = tmp_path / "kb.json"
    kb_seed.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "yonote",
                    "status": "published",
                    "source_type": "yonote",
                    "text_clean": "Как подать заявку на форум.",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reports = []
    for index, ignored in enumerate(("first", "second"), start=1):
        source = private_root / f"tickets-{index}.jsonl"
        source.write_text(
            json.dumps(
                {
                    "ticket_id": "same-ticket",
                    "user_turns": ["Как подать заявку?"],
                    "bot_turns": [ignored],
                    "closed_without_operator": index == 1,
                    "counted_in_conversion": index != 1,
                    "topic": ignored,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        reports.append(
            run_private_yonote_candidate_audit(
                source,
                private_root / f"audit-{index}.json",
                kb_seed_path=kb_seed,
                top_k=5,
                private_root=private_root,
            )
        )

    assert reports[0]["results"] == reports[1]["results"]


def test_private_candidate_audit_rejects_malformed_turns(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    source = private_root / "tickets.jsonl"
    output = private_root / "audit.json"
    kb_seed = tmp_path / "kb.json"
    source.write_text(
        json.dumps({"ticket_id": "ticket", "user_turns": ["ok", 42]}) + "\n",
        encoding="utf-8",
    )
    kb_seed.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="user_turns must contain non-empty strings"):
        run_private_yonote_candidate_audit(
            source,
            output,
            kb_seed_path=kb_seed,
            top_k=5,
            private_root=private_root,
        )


def test_private_candidate_audit_requires_private_paths(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    source = tmp_path / "outside.jsonl"
    output = private_root / "audit.json"
    kb_seed = tmp_path / "kb.json"
    source.write_text(
        json.dumps({"ticket_id": "ticket", "user_turns": ["question"]}) + "\n",
        encoding="utf-8",
    )
    kb_seed.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="must stay under data/private"):
        run_private_yonote_candidate_audit(
            source,
            output,
            kb_seed_path=kb_seed,
            top_k=5,
            private_root=private_root,
        )
