from __future__ import annotations

from scripts.build_operator_qa_golden_set import (
    _sanitize_operator_text,
    balanced_records,
    classify_operator_behavior,
    deduplicate_records,
    golden_exclusion_reasons,
    is_low_signal_question,
)
from scripts.build_ticket_answer_bank import RegexOnlyPIIMasker


def test_classify_operator_behavior_separates_answer_clarify_and_escalate() -> None:
    assert (
        classify_operator_behavior("Как подать заявку?", "Заявку можно подать в профиле.")
        == "answer"
    )
    assert (
        classify_operator_behavior(
            "Не работает",
            "Уточните, пожалуйста, название мероприятия и текст ошибки.",
        )
        == "clarify"
    )
    assert (
        classify_operator_behavior(
            "Почему отказали?",
            "Мы передали ваше обращение профильным специалистам.",
        )
        == "escalate"
    )
    assert (
        classify_operator_behavior(
            "Пожалуйтесь на канал",
            "Росмолодёжь не уполномочена в решении вашего вопроса.",
        )
        == "scope_note"
    )


def test_clarification_after_substantive_answer_does_not_replace_answer() -> None:
    answer = (
        "Для участия нужно зарегистрироваться через чат-бот события и получить билет. "
        "Дети до 13 лет могут пройти с родителями. Уточните, пожалуйста, возраст ребёнка."
    )

    assert classify_operator_behavior("Как прийти с ребёнком?", answer) == "answer"


def test_low_signal_question_rejects_bot_commands_and_attachments() -> None:
    assert is_low_signal_question("тест") is True
    assert is_low_signal_question("/start") is True
    assert is_low_signal_question("image-123") is True
    assert is_low_signal_question("Как зарегистрироваться на форум?") is False


def test_golden_exclusions_reject_personal_and_temporarily_manual_answers() -> None:
    reasons = golden_exclusion_reasons(
        question="Почему не меняется статус?",
        answer="Мы проверили: по вашей заявке решение ещё не принято.",
        behavior="answer",
        category="платформа_фгаис",
    )

    assert "personal_or_manual_answer" in reasons

    temporal = golden_exclusion_reasons(
        question="Когда закончится регистрация?",
        answer="Регистрация завершится 16.07.2026.",
        behavior="answer",
        category="форумы",
    )

    assert "temporal_answer_requires_review" in temporal

    personal = golden_exclusion_reasons(
        question="Меня зовут Иван. Почему мою заявку отклонили?",
        answer="Причины отклонения можно посмотреть в личном кабинете.",
        behavior="answer",
        category="гранты",
    )

    assert "personal_status_question" in personal


def test_golden_exclusions_reject_answer_shaped_question() -> None:
    reasons = golden_exclusion_reasons(
        question=(
            "Благодарим вас за обращение. Чтобы найти нужные контакты, "
            "перейдите на страницу региона."
        ),
        answer="Контакты региональных органов опубликованы на платформе.",
        behavior="answer",
        category="навигация",
    )

    assert "answer_shaped_question" in reasons


def test_operator_sanitizer_removes_signature_name_and_ticket_code() -> None:
    text = (
        "Подскажите, билет: DM26-M3W24V точно сработает? "
        "-- Светлана Фролова"
    )

    sanitized = _sanitize_operator_text(text, RegexOnlyPIIMasker())

    assert "DM26-M3W24V" not in sanitized
    assert "Светлана" not in sanitized
    assert "Фролова" not in sanitized
    assert "[ID]" in sanitized


def test_golden_exclusions_reject_operator_join_placeholder() -> None:
    reasons = golden_exclusion_reasons(
        question="Когда поступят средства по гранту?",
        answer="Меня зовут [ИМЯ]. Присоединюсь к диалогу в ближайшее время.",
        behavior="answer",
        category="гранты",
    )

    assert "operator_join_placeholder" in reasons


def test_golden_exclusions_reject_personal_routing_and_contextless_followup() -> None:
    personal = golden_exclusion_reasons(
        question="Удостоверение так и не прислали, когда я его получу?",
        answer="Срок подготовки удостоверения зависит от программы.",
        behavior="answer",
        category="платформа_фгаис",
    )
    followup = golden_exclusion_reasons(
        question="Спасибо за совет, но я уже всё перепробовал",
        answer="Обращение нужно передать специалисту.",
        behavior="answer",
        category="техподдержка",
    )

    assert "routing_requires_operator" in personal
    assert "followup_without_context" in followup


def test_deduplicate_records_keeps_better_official_match() -> None:
    base = {
        "query": "Как подать заявку?",
        "reference_answer": "Ответ",
        "forum_normalized": None,
        "ticket_hash": "a",
    }
    records = [
        {**base, "official_overlap_score": 0.1},
        {**base, "ticket_hash": "b", "official_overlap_score": 0.8},
    ]

    assert deduplicate_records(records)[0]["ticket_hash"] == "b"


def test_balanced_records_round_robins_intent_groups() -> None:
    records = [
        {
            "category": "форумы",
            "topic": "регистрация_и_заявка",
            "forum_normalized": "Амур",
            "official_overlap_score": 0.9,
            "reference_answer": "A" * 100,
            "ticket_hash": "a1",
        },
        {
            "category": "форумы",
            "topic": "регистрация_и_заявка",
            "forum_normalized": "Амур",
            "official_overlap_score": 0.8,
            "reference_answer": "A" * 100,
            "ticket_hash": "a2",
        },
        {
            "category": "гранты",
            "topic": "грантовая_отчетность",
            "forum_normalized": None,
            "official_overlap_score": 0.7,
            "reference_answer": "B" * 100,
            "ticket_hash": "b1",
        },
    ]

    ordered = balanced_records(records)

    assert [item["ticket_hash"] for item in ordered[:2]] == ["b1", "a1"]
