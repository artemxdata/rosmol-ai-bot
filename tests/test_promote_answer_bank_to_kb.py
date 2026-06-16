from __future__ import annotations

import json
from pathlib import Path

from scripts.index_kb import validate_seed_items
from scripts.promote_answer_bank_to_kb import (
    build_draft_chunk,
    is_safe_answer_text,
    promote_answer_bank,
    select_promotable,
)


def _candidate(**overrides):
    data = {
        "id": "ticket_answer_bank::001",
        "review_status": "approved",
        "source": "sanitized_hde_ticket_answer_bank",
        "query": "Как зарегистрироваться на форум?",
        "answer": "Подать заявку можно через личный кабинет на платформе.",
        "category": "форумы",
        "topic": "регистрация_и_заявка",
        "forum_normalized": "Машук",
        "difficulty": "medium",
        "intent": "форумы.регистрация_и_заявка",
        "tags": ["answer_bank_candidate"],
        "quality_score": 12,
        "quality_reasons": ["answer_length_good"],
        "candidate_chunk_ids": ["existing_chunk"],
        "source_ticket_ids": ["must_not_leak"],
        "reference_answer": "must_not_leak",
    }
    data.update(overrides)
    return data


def test_select_promotable_uses_approved_by_default() -> None:
    selected = select_promotable(
        [
            _candidate(id="approved", review_status="approved"),
            _candidate(id="candidate", review_status="candidate"),
        ],
        include_candidates=False,
    )

    assert [item["id"] for item in selected] == ["approved"]
    assert "source_ticket_ids" not in selected[0]
    assert "reference_answer" not in selected[0]


def test_select_promotable_can_include_candidates_for_private_review() -> None:
    selected = select_promotable(
        [_candidate(review_status="candidate")],
        include_candidates=True,
    )

    assert selected[0]["review_status"] == "candidate"


def test_select_promotable_rejects_unsafe_answer_text() -> None:
    selected = select_promotable(
        [
            _candidate(id="safe"),
            _candidate(id="unsafe_query", query="Не пришло письмо на [EMAIL]"),
            _candidate(
                id="unsafe_thread_query",
                query="Служба Заботы Росмолодёжи: 2025 г., 10:12. Как подать заявку?",
            ),
            _candidate(id="unsafe_email", answer="Напишите нам на [EMAIL]"),
            _candidate(id="unsafe_first_person", answer="Мне пришло письмо, помогите"),
            _candidate(id="unsafe_private_request", answer="Прошу оказать содействие"),
            _candidate(
                id="unsafe_id",
                answer="ID: 825b4722-4c56-4c6e-b343-d05fc0b8df52",
            ),
            _candidate(
                id="unsafe_question_mark",
                answer="Не успела подать заявку, можете пожалуйста открыть?",
            ),
            _candidate(id="unsafe_latin_noise", answer="Julia Sushkova commented " * 5),
            _candidate(
                id="unsafe_thread_artifact",
                answer="Служба Заботы Росмолодёжи: 2025 г., 10:12. Запрос отправлял по гранту",
            ),
            _candidate(
                id="unsafe_name_prefix",
                answer="Анастасия, поскольку аккаунт верифицирован через ЕСИА, измените данные.",
            ),
            _candidate(id="unsafe_fragment_start", answer="(не черновик, именно проект)"),
            _candidate(id="unsafe_lowercase_start", answer="и не получила обратную связь"),
        ],
        include_candidates=True,
    )

    assert [item["id"] for item in selected] == ["safe"]


def test_is_safe_answer_text_rejects_private_ticket_artifacts() -> None:
    assert is_safe_answer_text("Заявку можно подать через личный кабинет") is True
    assert is_safe_answer_text("Укажите ID [ID]") is False
    assert is_safe_answer_text("Мне пришло письмо, подскажите что делать") is False


def test_build_draft_chunk_outputs_valid_kb_seed_record() -> None:
    record = build_draft_chunk(
        _candidate(),
        index=1,
        existing_chunk_ids=set(),
    )

    validate_seed_items([record])
    assert record["chunk_id"] == "ticket_answer_bank_001"
    assert record["status"] == "draft"
    assert record["text_clean"] == "Подать заявку можно через личный кабинет на платформе."
    assert record["intent_examples"] == ["Как зарегистрироваться на форум?"]
    assert record["answer_bank_candidate_chunk_ids"] == ["existing_chunk"]
    assert "source_ticket_ids" not in record
    assert "reference_answer" not in record


def test_build_draft_chunk_can_publish_for_private_sandbox() -> None:
    record = build_draft_chunk(
        _candidate(),
        index=1,
        existing_chunk_ids=set(),
        publish_for_sandbox=True,
    )

    validate_seed_items([record])
    assert record["status"] == "published"
    assert record["sandbox_published"] is True


def test_build_draft_chunk_makes_unique_chunk_id_against_base_kb() -> None:
    record = build_draft_chunk(
        _candidate(),
        index=1,
        existing_chunk_ids={"ticket_answer_bank_001"},
    )

    assert record["chunk_id"] == "ticket_answer_bank_001_2"


def test_promote_answer_bank_writes_draft_and_merged_outputs(tmp_path: Path) -> None:
    answer_bank = tmp_path / "answer_bank.json"
    base_kb = tmp_path / "kb.json"
    output = tmp_path / "drafts.json"
    merged = tmp_path / "merged.json"
    answer_bank.write_text(
        json.dumps(
            [
                _candidate(review_status="candidate"),
                _candidate(id="approved", review_status="approved"),
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    base_kb.write_text(
        json.dumps(
            [{"chunk_id": "base", "text_clean": "Базовый ответ.", "status": "published"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = promote_answer_bank(
        answer_bank,
        output,
        base_kb_path=base_kb,
        merged_output_path=merged,
        include_candidates=True,
    )

    drafts = json.loads(output.read_text(encoding="utf-8"))
    merged_records = json.loads(merged.read_text(encoding="utf-8"))
    validate_seed_items(drafts)
    validate_seed_items(merged_records)
    assert summary["promoted_chunks"] == 2
    assert len(drafts) == 2
    assert len(merged_records) == 3
    assert (tmp_path / "drafts.md").exists()
