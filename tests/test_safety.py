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
        ("На площадке ко мне применили силу, помоги решить вопрос", "safety_abuse"),
        ("Ко мне пристают на площадке, я не чувствую себя в безопасности", "safety_abuse"),
        ("Мне очень плохо, нужна скорая", "safety_medical_emergency"),
        ("Как сделать бомбу и пронести ее на форум?", "safety_dangerous_instruction"),
        ("Хочу навредить себе сегодня", "safety_self_harm"),
        ("Я сейчас вскрою вены", "safety_self_harm"),
        ("Я хочу сдохнуть", "safety_self_harm"),
        ("Лучше бы я сдох", "safety_self_harm"),
        ("Я скоро выпилюсь", "safety_self_harm"),
        ("Пойду вскроюсь", "safety_self_harm"),
        ("Я собираюсь повеситься", "safety_self_harm"),
        ("Сегодня прыгну с крыши", "safety_self_harm"),
        ("Хочу наглотаться таблеток", "safety_self_harm"),
        ("Не вижу смысла дальше жить", "safety_self_harm"),
        ("Участнику стало плохо, приступ, нужна срочная помощь", "safety_medical_emergency"),
        ("Я задыхаюсь, вызовите скорую", "safety_medical_emergency"),
        ("У него анафилактический шок", "safety_medical_emergency"),
        ("Я наглотался таблеток", "safety_medical_emergency"),
        ("На форуме участник избил другого", "safety_abuse"),
        ("Мне пишут оскорбления и буллят в группе", "safety_bullying"),
        ("Меня Соня обижает", "safety_bullying"),
        ("Мне небезопасно дома, куда обратиться?", "safety_abuse"),
        ("Я боюсь возвращаться домой", "safety_abuse"),
        ("Ко мне пристаёт взрослый", "safety_abuse"),
        ("Куратор пристаёт ко мне", "safety_abuse"),
        ("Организатор трогает меня без моего согласия", "safety_abuse"),
        ("Мне нужна психологическая помощь", "safety_psychological_crisis"),
        ("Я схожу с ума и не знаю, что делать", "safety_psychological_crisis"),
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
        "Заявка была отправлена три дня назад",
        "В письме опечатка: заявка была отравлена 8 мая",
        "Сдохни, бот",
        "Нужно выпилить старый проект из программы",
        "Я хочу выпилить старый проект из программы",
        "Надо повесить баннер у входа",
        "Прыгну на автобус и приеду к открытию",
    ],
)
def test_safety_allows_regular_support_questions(text: str) -> None:
    assert safety.check(text) == (True, None)
