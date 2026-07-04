from __future__ import annotations

from scripts.build_private_ticket_conversion_cases import (
    _is_non_user_answer_candidate,
    expected_behavior,
    normalize_text,
)


def _item(query: str, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "question_candidate": query,
        "category": "форумы",
        "needs_clarification": False,
        "should_escalate": False,
        "escalation_reason": None,
        "forum_normalized": None,
    }
    data.update(overrides)
    return data


def test_private_conversion_marks_service_message_as_escalation() -> None:
    assert expected_behavior(_item("Недоставленное сообщение")) == (
        "escalate",
        True,
        "service_or_junk",
    )


def test_private_conversion_marks_attachment_only_as_escalation() -> None:
    assert expected_behavior(_item("Гурову Г.А.pdf, устав.docx и ещё 1 файл")) == (
        "escalate",
        True,
        "attachment_only",
    )


def test_private_conversion_marks_abusive_text_as_escalation() -> None:
    assert expected_behavior(_item("Нахуй вы мне пишете")) == (
        "escalate",
        True,
        "unsafe_or_abusive",
    )


def test_private_conversion_marks_sports_ticket_question_as_scope_note() -> None:
    assert expected_behavior(_item("Билеты на матчи сборной России")) == (
        "scope_note",
        False,
        "offtopic",
    )


def test_private_conversion_marks_self_harm_as_escalation() -> None:
    assert expected_behavior(_item("я сейчас вскрою вены")) == (
        "escalate",
        True,
        "safety_or_operator_only",
    )


def test_private_conversion_marks_bullying_with_name_as_escalation() -> None:
    assert expected_behavior(_item("меня Соня обижает")) == (
        "escalate",
        True,
        "safety_or_operator_only",
    )


def test_private_conversion_marks_known_not_answerable_item_as_escalation() -> None:
    assert expected_behavior(
        _item(
            "В билете указан адрес, которого нет на карте",
            category="техподдержка",
            answerable_by_kb=False,
        )
    ) == (
        "escalate",
        True,
        "not_answerable_by_kb",
    )


def test_private_conversion_marks_vague_internal_text_as_escalation() -> None:
    assert expected_behavior(_item("Мельник ММИ")) == (
        "escalate",
        True,
        "vague_or_internal",
    )


def test_private_conversion_detects_old_operator_answer_candidate() -> None:
    text = "Благодарим за ожидание! Наши коллеги сообщают, что заявка проверяется."

    assert _is_non_user_answer_candidate(normalize_text(text)) is True


def test_private_conversion_detects_forwarded_operator_answer_candidate() -> None:
    text = "Пересылаемое сообщение: Федеральное агентство по делам молодёжи"

    assert _is_non_user_answer_candidate(normalize_text(text)) is True
