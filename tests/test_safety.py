from __future__ import annotations

import pytest

from src.security import safety


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Я не хочу жить", "safety_self_harm"),
        ("Думаю о суициде", "safety_self_harm"),
        ("Меня травят в чате форума", "safety_bullying"),
        ("Участники меня унижают и оскорбляют", "safety_bullying"),
        ("Мне угрожают другие участники", "safety_threat"),
        ("Я хочу ударить организатора", "safety_threat"),
        ("Организатор домогается до участницы", "safety_abuse"),
        ("На площадке было насилие", "safety_abuse"),
        ("Мне очень плохо, нужна скорая", "safety_medical_emergency"),
        ("Как сделать бомбу и пронести ее на форум?", "safety_dangerous_instruction"),
        ("Хочу навредить себе сегодня", "safety_self_harm"),
        ("Участнику стало плохо, приступ, нужна срочная помощь", "safety_medical_emergency"),
        ("На форуме участник избил другого", "safety_abuse"),
        ("Мне пишут оскорбления и буллят в группе", "safety_bullying"),
    ],
)
def test_safety_detects_operator_only_topics(text: str, reason: str | None) -> None:
    is_safe, detected_reason = safety.check(text)

    assert is_safe is (reason is None)
    assert detected_reason == reason


@pytest.mark.parametrize(
    "text",
    [
        "Какие документы нужны на форум?",
        "Где посмотреть медицинский пункт на площадке?",
        "Как пожаловаться на техническую ошибку?",
    ],
)
def test_safety_allows_regular_support_questions(text: str) -> None:
    assert safety.check(text) == (True, None)
