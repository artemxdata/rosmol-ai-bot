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
        "Хочу понять, как получить жильё молодому специалисту",
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


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            "Статус заявки уже несколько дней на подписании, можно проверить?",
            "personal_status",
        ),
        (
            "Я подал заявку, но не понимаю, прошел ли, и не могу войти в кабинет",
            "personal_status",
        ),
        (
            "Как получить билет на мужа, если у него нет приложения МАКС?",
            "personal_status",
        ),
        (
            "Как получить аккредитацию для съёмки видео на Дне молодёжи в Тамбове?",
            "operator_requested",
        ),
        (
            "Почему задерживается выдача удостоверений по программе?",
            "operator_requested",
        ),
        (
            "Можно ли исправить в заявке ответы в поле анкеты?",
            "personal_status",
        ),
        (
            "Нажимаю перейти, но меню не меняется и непонятно, видны ли файлы.",
            "technical_issue",
        ),
        (
            "При регистрации в МАКС я случайно ввела неправильную почту и не получила билет.",
            "personal_status",
        ),
        (
            "Прошу направить копию иска и контакты юридического отдела.",
            "operator_requested",
        ),
        (
            "У меня горит статус 'Участие офлайн'. "
            "Это значит я ещё в рассмотрении или могу покупать билеты?",
            "personal_status",
        ),
        (
            "Почему я не прошла на форум, хотя видеовизитка рассматривалась?",
            "personal_status",
        ),
        (
            "Почему я не могу отменить заявку на сайте, "
            "если сайт указывает обратиться в службу поддержки?",
            "personal_status",
        ),
    ],
)
def test_operator_review_routes_blind_june_personal_and_staff_cases(
    text: str, reason: str
) -> None:
    assert operator_review_reason(text) == reason


@pytest.mark.parametrize(
    "text",
    [
        "Я получила билет на День молодёжи. Мужу и ребёнку нужны отдельные билеты?",
        "По одному билету можно пройти с мужем?",
        "Нужен ли ребёнку отдельный билет на фестиваль?",
    ],
)
def test_operator_review_keeps_general_family_ticket_policy_in_rag(text: str) -> None:
    assert operator_review_reason(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Мне третий раз никто не помог",
        "Я уже всё перепробовал",
        "Мне надоело, что меня гоняют по кругу",
    ],
)
def test_operator_review_escalates_repeated_support_failures(text: str) -> None:
    assert operator_review_reason(text) == "repeated_support_failure"
