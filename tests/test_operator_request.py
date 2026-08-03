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


def test_operator_request_does_not_confuse_grant_support_with_human_support() -> None:
    text = "Можно ли получить сертификат участника команды, получившей грантовую поддержку?"

    assert is_operator_request(text) is False
    assert operator_review_reason(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Нужен оператор, сайт не работает.",
        "Соедини с оператором, бот не работает.",
        "Нужен оператор по обработке моей заявки.",
        "Хочу поговорить со службой поддержки.",
    ],
)
def test_direct_operator_request_wins_over_incidental_work_words(text: str) -> None:
    assert is_operator_request(text) is True
    assert operator_review_reason(text) == "operator_requested"


def test_operator_review_does_not_escalate_for_missing_forum_rules_document() -> None:
    text = "Территория смыслов Вышлите пожалуйста положение, в личном кабинете не отображается"

    assert operator_review_reason(text) is None


def test_operator_review_keeps_first_platform_issue_in_rag() -> None:
    text = "Не могу зарегистрироваться, в личном кабинете ошибка и кнопка не работает"

    assert operator_review_reason(text) is None


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
        (
            "Мне нужно повторно отправить подписанное письмо с печатью, "
            "проверьте мою заявку",
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
        "Как получить билет на несовершеннолетнего ребёнка?",
        "Как зарегистрировать ребёнка на День молодёжи?",
        "Можно ли исправить в заявке ответы в поле анкеты?",
        "Нажимаю перейти, но меню не меняется и непонятно, видны ли файлы.",
        "При регистрации в МАКС я случайно ввела неправильную почту и не получила билет.",
        "Когда будут объявлены даты следующей премии ШУМ?",
    ],
)
def test_operator_review_keeps_first_line_policy_questions_in_rag(text: str) -> None:
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


@pytest.mark.parametrize(
    "text",
    [
        "Проверьте, пожалуйста, почему пропала моя заявка на мероприятие.",
        "Как восстановить мой сертификат после участия?",
        "Моё удостоверение не отображается в личном кабинете.",
    ],
)
def test_operator_review_routes_failed_personal_workflow_objects(text: str) -> None:
    assert operator_review_reason(text) == "personal_status"


def test_operator_review_routes_registered_appeal_request() -> None:
    text = "Зарегистрируйте обращение и сообщите его регистрационный номер."

    assert operator_review_reason(text) == "operator_requested"


@pytest.mark.parametrize(
    "text",
    [
        "Как проверить свою заявку перед отправкой?",
        "Нужен ли регистрационный номер для подачи заявки?",
    ],
)
def test_operator_review_does_not_confuse_policy_with_personal_escalation(
    text: str,
) -> None:
    assert operator_review_reason(text) is None


def test_operator_review_routes_personal_application_stage_lookup() -> None:
    text = "На каком этапе отбора находится моя заявка?"

    assert operator_review_reason(text) == "personal_status"


@pytest.mark.parametrize(
    "text",
    [
        "Я подал заявку. На каком этапе её рассмотрение и когда ждать подтверждения?",
        "Я подавала заявку, но сейчас её нет в аккаунте.",
        "Мою заявку до сих пор не приняли. Сколько ещё займёт рассмотрение?",
    ],
)
def test_operator_review_routes_high_precision_own_application_lookup(
    text: str,
) -> None:
    assert operator_review_reason(text) == "personal_status"


def test_operator_review_keeps_general_status_explanation_in_rag() -> None:
    assert (
        operator_review_reason(
            "Заявка всё ещё на рассмотрении. Что означает этот статус?"
        )
        is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "Заявка на грант",
        "Грантовое соглашение",
        "Вопрос по грантовому конкурсу",
        "Что означает статус «Участие офлайн»?",
        "Где можно найти записи мероприятия?",
        "Вопрос по заявке на форум: какие документы нужны?",
        "Кто может быть зарегистрирован на форум?",
    ],
)
def test_operator_review_keeps_ambiguous_general_topics_in_rag(text: str) -> None:
    assert operator_review_reason(text) is None


def test_operator_review_still_routes_personal_offline_status_lookup() -> None:
    text = "У меня статус «Участие офлайн». Могу уже покупать билеты?"

    assert operator_review_reason(text) == "personal_status"


@pytest.mark.parametrize(
    "text",
    [
        "Я зарегистрирован на форум, но подтверждение не пришло.",
        "Зарегистрирована ли я на смену?",
    ],
)
def test_operator_review_still_routes_personal_registration_lookup(text: str) -> None:
    assert operator_review_reason(text) == "personal_status"


@pytest.mark.parametrize(
    "text",
    [
        "Как подать заявку на мероприятие?",
        "Можно ли исправить ответы в заявке до отправки?",
        "Где скачать сертификат участника?",
        "Когда публикуют результаты отбора?",
    ],
)
def test_operator_review_keeps_general_workflow_policy_in_rag(text: str) -> None:
    assert operator_review_reason(text) is None
