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
        "пошёл нахуй",
        "ty mudak",
        "idi nahuy",
        "blyat",
        "иди нахуй",
        "хули ты не отвечаешь",
        "ты охуел",
        "сучара",
        "мразь",
        "тварь",
        "урод",
        "чмо",
        "шлюха",
        "гондон",
        "пидор",
        "иди в жопу",
        "засранец",
        "заткнись",
        "сдохни",
        "х у й",
        "п-и-з-д-е-ц",
        "xуй",
        "fuck you",
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
        "Это сообщение тебе",
        "Страхует участников",
        "Поездка в Сухум",
        "Урожай собрали",
        "Мастер-класс: аквагрим и блеск-тату",
        "Сукно для национального костюма",
        "Суккуленты на выставке",
    ],
)
def test_profanity_avoids_regular_phrases(text: str) -> None:
    assert profanity.check(text) is False
