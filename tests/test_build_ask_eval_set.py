from __future__ import annotations

import json
from pathlib import Path

from eval.build_ask_eval_set import build_eval_set, parse_source_type_limits


def test_build_eval_set_writes_balanced_cases(tmp_path: Path) -> None:
    kb_seed = tmp_path / "kb.json"
    output = tmp_path / "ask_cases.json"
    kb_seed.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "forum_1",
                    "status": "published",
                    "category": "форумы",
                    "forum_normalized": "Машук",
                    "intent_examples": ["как подать заявку"],
                    "text_clean": "Ответ",
                },
                {
                    "chunk_id": "grant_1",
                    "status": "published",
                    "category": "гранты",
                    "intent_examples": ["как получить грант"],
                    "text_clean": "Ответ",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = build_eval_set(
        kb_seed_path=kb_seed,
        output_path=output,
        max_cases=10,
        per_category_limit=None,
        per_forum_limit=3,
    )

    cases = json.loads(output.read_text(encoding="utf-8"))
    assert summary["cases_total"] == 2
    assert {case["expected_chunk_ids"][0] for case in cases} == {"forum_1", "grant_1"}


def test_parse_source_type_limits() -> None:
    assert parse_source_type_limits("ticket_answer_bank=100,xlsx=45,docx=15") == {
        "ticket_answer_bank": 100,
        "xlsx": 45,
        "docx": 15,
    }
    assert parse_source_type_limits("") is None


def test_build_eval_set_can_balance_source_types_and_require_citations(
    tmp_path: Path,
) -> None:
    kb_seed = tmp_path / "kb.json"
    output = tmp_path / "ask_cases.json"
    kb_seed.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "ticket_1",
                    "status": "published",
                    "category": "forums",
                    "source_type": "ticket_answer_bank",
                    "intent_examples": ["ticket question 1"],
                    "text_clean": "Ticket answer 1",
                },
                {
                    "chunk_id": "ticket_2",
                    "status": "published",
                    "category": "grants",
                    "source_type": "ticket_answer_bank",
                    "intent_examples": ["ticket question 2"],
                    "text_clean": "Ticket answer 2",
                },
                {
                    "chunk_id": "xlsx_1",
                    "status": "published",
                    "category": "forums",
                    "source_type": "xlsx",
                    "intent_examples": ["xlsx question 1"],
                    "text_clean": "XLSX answer 1",
                },
                {
                    "chunk_id": "docx_1",
                    "status": "published",
                    "category": "forums",
                    "source_type": "docx",
                    "intent_examples": ["docx question 1"],
                    "text_clean": "DOCX answer 1",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = build_eval_set(
        kb_seed_path=kb_seed,
        output_path=output,
        max_cases=4,
        per_category_limit=None,
        per_forum_limit=3,
        source_type_limits={"ticket_answer_bank": 2, "xlsx": 1, "docx": 1},
        require_cited_chunks=True,
    )

    cases = json.loads(output.read_text(encoding="utf-8"))

    assert summary["source_type_counts"] == {
        "ticket_answer_bank": 2,
        "xlsx": 1,
        "docx": 1,
    }
    assert all(case["expected_cited_chunk_ids"] == case["expected_chunk_ids"] for case in cases)
