from __future__ import annotations

import pytest

from src.security import profanity


@pytest.mark.parametrize(
    "text",
    [
        "блять",
        "сука",
        "какого хуя",
        "пиздец",
        "заебали",
        "долбоёб",
        "мудак",
        "мудила",
        "говно",
        "гавно",
        "задолбали",
    ],
)
def test_profanity_detects_common_russian_forms(text: str) -> None:
    assert profanity.check(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Как подать заявку?",
        "У меня проблема с кабинетом",
        "Тебя добавили в чат?",
        "Сучок дерева упал на дорогу",
    ],
)
def test_profanity_avoids_regular_phrases(text: str) -> None:
    assert profanity.check(text) is False
