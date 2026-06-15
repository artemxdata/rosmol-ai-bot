from __future__ import annotations

from scripts.analyze_ticket_dataset import (
    ForumAlias,
    build_golden_candidates,
    build_reranker_pairs,
    classify_category,
    classify_escalation,
    classify_topic,
    detect_forum,
    is_low_signal_title,
    mask_pii,
    normalize_ticket,
    split_message_segments,
)


def test_mask_pii_masks_contacts_and_urls() -> None:
    masked, pii_types = mask_pii(
        "Пишите на test@example.com или звоните 8(999)123-45-67, ссылка https://x.test/a"
    )

    assert "[EMAIL]" in masked
    assert "[ТЕЛЕФОН]" in masked
    assert "[URL]" in masked
    assert pii_types == ["email", "phone", "url"]


def test_split_message_segments_uses_dialog_separator() -> None:
    assert split_message_segments("Привет --- Ответ оператора --- Спасибо") == [
        "Привет",
        "Ответ оператора",
        "Спасибо",
    ]


def test_golden_candidates_are_balanced_across_groups() -> None:
    records = [
        golden_record("complex-1", category="forums", difficulty="complex", forum="Forum A"),
        golden_record("complex-2", category="forums", difficulty="complex", forum="Forum A"),
        golden_record("simple-1", category="grants", difficulty="simple", forum=""),
        golden_record(
            "escalate-1",
            category="support",
            difficulty="complex",
            should_escalate=True,
            escalation_reason="technical_issue",
        ),
    ]

    golden = build_golden_candidates(records, max_items=3)

    selected_ids = [item["source_ticket_ids"][0] for item in golden]

    assert len(selected_ids) == 3
    assert len({"complex-1", "complex-2"} & set(selected_ids)) == 1
    assert "simple-1" in selected_ids
    assert "escalate-1" in selected_ids


def test_golden_candidates_skip_unanswerable_non_escalation_records() -> None:
    records = [
        golden_record("answerable"),
        {
            **golden_record("weak"),
            "answer_candidate": "",
            "answerable_by_kb": False,
            "should_escalate": False,
        },
    ]

    golden = build_golden_candidates(records, max_items=10)

    assert [item["source_ticket_ids"][0] for item in golden] == ["answerable"]


def golden_record(
    ticket_id: str,
    *,
    category: str = "forums",
    difficulty: str = "simple",
    forum: str = "Forum A",
    should_escalate: bool = False,
    escalation_reason: str | None = None,
) -> dict[str, object]:
    return {
        "ticket_hash": ticket_id,
        "ticket_id": ticket_id,
        "question_candidate": f"How to solve ticket {ticket_id}?",
        "answer_candidate": "Use the official knowledge-base answer for this exact ticket.",
        "answerable_by_kb": not should_escalate,
        "should_escalate": should_escalate,
        "escalation_reason": escalation_reason,
        "forum_normalized": forum,
        "category": category,
        "topic": "registration",
        "intent": f"{category}.registration",
        "difficulty": difficulty,
        "quality_notes": "",
    }


def test_low_signal_title_is_not_used_as_question_candidate() -> None:
    record = normalize_ticket(
        {
            "id": "1",
            "title": "Личное сообщение VKontakte",
            "messages": (
                "Здравствуйте, где посмотреть статус заявки на форум Машук? --- "
                "Статус заявки отображается в личном кабинете."
            ),
            "status": "closed",
            "typical_atypical": "Типовой",
        },
        [ForumAlias(normalized="Машук", aliases=("Машук",))],
    )

    assert is_low_signal_title("Личное сообщение VKontakte") is True
    assert record["question_candidate"] == (
        "Здравствуйте, где посмотреть статус заявки на форум Машук?"
    )


def test_classification_detects_forum_registration_and_technical_escalation() -> None:
    text = "Не могу войти в профиль, кнопка подачи заявки на форум Машук не работает"

    assert classify_category(text) == "техподдержка"
    assert classify_topic(text) == "доступ_и_техническая_ошибка"
    assert classify_escalation(text, {"status": "closed"}) == "technical_issue"
    assert (
        detect_forum(
            text,
            [ForumAlias(normalized="Машук", aliases=("Машук",))],
        )
        == "Машук"
    )


def test_normalize_ticket_builds_masked_private_record() -> None:
    record = normalize_ticket(
        {
            "id": "1",
            "unique_id": "u1",
            "department": "MAX Бот",
            "status": "closed",
            "title": "Как подать заявку на форум Машук?",
            "messages": (
                "Как подать заявку? --- Подать заявку можно в личном кабинете "
                "Росмолодёжи после авторизации и выбора нужного мероприятия."
            ),
            "typical_atypical": "Типовой",
        },
        [ForumAlias(normalized="Машук", aliases=("Машук",))],
    )

    assert record["ticket_id"] == "1"
    assert record["category"] == "платформа_фгаис"
    assert record["forum_normalized"] == "Машук"
    assert record["answerable_by_kb"] is True
    assert record["should_escalate"] is False


def test_golden_candidates_and_reranker_pairs_are_built() -> None:
    records = [
        {
            "ticket_hash": "a",
            "ticket_id": "1",
            "question_candidate": "Как подать заявку на Машук?",
            "answer_candidate": "Подать заявку можно через личный кабинет Росмолодёжи.",
            "answerable_by_kb": True,
            "should_escalate": False,
            "escalation_reason": None,
            "forum_normalized": "Машук",
            "category": "платформа_фгаис",
            "topic": "регистрация_и_заявка",
            "intent": "платформа_фгаис.регистрация_и_заявка",
            "difficulty": "simple",
            "quality_notes": "",
        },
        {
            "ticket_hash": "b",
            "ticket_id": "2",
            "question_candidate": "Как подать заявку на Утро?",
            "answer_candidate": "Для форума Утро заявка также подается через личный кабинет.",
            "answerable_by_kb": True,
            "should_escalate": False,
            "escalation_reason": None,
            "forum_normalized": "Утро",
            "category": "платформа_фгаис",
            "topic": "регистрация_и_заявка",
            "intent": "платформа_фгаис.регистрация_и_заявка",
            "difficulty": "simple",
            "quality_notes": "",
        },
    ]

    golden = build_golden_candidates(records, max_items=10)
    pairs = build_reranker_pairs(golden, records, max_pairs=10)

    assert len(golden) == 2
    assert pairs[0]["query"] == "Как подать заявку на Машук?"
    assert pairs[0]["hard_negative_texts"] == [
        "Для форума Утро заявка также подается через личный кабинет."
    ]
