from __future__ import annotations

import pytest

from src.security.operator_request import is_operator_request, operator_review_reason


@pytest.mark.parametrize(
    "text",
    [
        "Позови оператора",
        "Хочу поговорить со специалистом",
        "Соедините с сотрудником поддержки",
        "Можно живого человека?",
        "Передайте обращение специалисту",
        "Жду ответ оператора",
        "Ожидаю специалиста",
    ],
)
def test_operator_request_detects_explicit_requests(text: str) -> None:
    assert is_operator_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Как зарегистрироваться на форум?",
        "Где посмотреть контакты поддержки?",
        "Какие документы нужны участнику?",
        "Можно побыть у вас оператором?",
        "Есть вакансии оператора?",
        "Хочу работать специалистом поддержки",
    ],
)
def test_operator_request_allows_regular_questions(text: str) -> None:
    assert is_operator_request(text) is False


def test_operator_review_does_not_escalate_for_missing_forum_rules_document() -> None:
    text = "Территория смыслов Вышлите пожалуйста положение, в личном кабинете не отображается"

    assert operator_review_reason(text) is None


def test_operator_review_still_escalates_real_platform_issue() -> None:
    text = "Не могу зарегистрироваться, в личном кабинете ошибка и кнопка не работает"

    assert operator_review_reason(text) == "technical_issue"
