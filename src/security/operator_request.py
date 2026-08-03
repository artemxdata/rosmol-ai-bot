from __future__ import annotations

import re

LIVE_PERSON_RE = re.compile(
    r"\bжив(?:ой|ого|ым)?\s+человек(?:а|ом)?\b",
    re.IGNORECASE | re.UNICODE,
)

TARGET_MARKERS = ("оператор", "специалист", "сотрудник")
EMPLOYMENT_MARKERS = (
    "ваканс",
    "работ",
    "трудоустр",
    "устроиться",
    "стать",
    "побыть",
    "резюме",
    "карьер",
)
ACTION_MARKERS = (
    "хочу",
    "нужен",
    "нужна",
    "нужно",
    "можно",
    "перевед",
    "соедин",
    "свяж",
    "поговор",
    "позов",
    "передай",
    "передайте",
    "позови",
    "позовите",
    "жду",
    "ожида",
)
PERSONAL_STATUS_MARKERS = (
    "смена статуса",
    "статус не измен",
    "завис статус",
    "зависла заявка",
    "зависла на статусе",
    "на подписании",
    "статус все тот же",
    "статус всё тот же",
    "действительно ли я зарегистр",
    "проверить статус участия",
    "уточнить статус участия",
    "могу уже покупать билеты",
    "убрать подтверждение",
    "снять подтверждение",
    "отменить подтверждение",
    "не могу отменить заявку",
    "не могу отменить заяв",
    "отменить заявку на сайте",
    "мне ничего не приходило",
    "на почте ничего нет",
    "не было рассмотрено",
    "письмо с подписью",
    "подписанное письмо",
    "письмо с печатью",
    "подписью и печатью",
    "не могу подтвердить",
    "не получается подтвердить",
    "не понимаю прошел ли",
    "не понимаю прошёл ли",
    "прошел ли я",
    "прошёл ли я",
    "не пришел сертификат",
    "не пришёл сертификат",
    "сертификат не получил",
    "сертификат не получила",
    "удостоверение не получил",
    "удостоверение не получила",
    "удостоверение так и не",
    "так и не прислали",
)
TECHNICAL_REVIEW_MARKERS = (
    "скриншот",
    "скриншоты",
    "поправьте",
    "исправьте",
    "тех.проблема",
    "техническая проблема",
    "не могу выгрузить",
    "не могла выгрузить",
    "не отображ",
    "не отобраз",
    "меню не меняется",
    "файлы не видны",
    "видны ли файлы",
    "неправильную почту",
    "приложение не позволяет",
    "заявка подана",
)
OPERATOR_ONLY_MARKERS = (
    "требуется ли в вашу команду",
    "требуется ли в вашу команду дизайнер",
    "в вашу команду дизайнер",
    "набор кураторов",
    "брендбук",
    "удостоверение по программе",
    "паспорт молодости",
    "паспорт молодоости",
    "маркет молодых",
    "жареным мороженым",
    "организатор не подключился",
    "выдача удостоверений",
    "удостоверения задерж",
    "удостоверений задерж",
    "юридический отдел",
    "копию иска",
    "подали в суд",
    "судебный иск",
)
PERSONAL_WORKFLOW_OBJECT_MARKERS = (
    "моя заявк",
    "мою заявк",
    "моей заявк",
    "свою заявк",
    "мой сертификат",
    "моего сертификат",
    "мое удостовер",
    "моё удостовер",
    "моего удостовер",
)
PERSONAL_WORKFLOW_FAILURE_MARKERS = (
    "пропал",
    "пропала",
    "исчез",
    "не отображ",
    "не вид",
    "не приш",
    "не получил",
    "не получила",
    "восстанов",
)
REGISTERED_APPEAL_MARKERS = (
    "зарегистрировать обращение",
    "зарегистрируйте обращение",
    "зарегистрировать входящ",
    "зарегистрируйте входящ",
    "регистрационный номер обращения",
    "номер обращения",
    "номер входящего",
    "входящий номер",
    "присвоить номер",
    "присвойте номер",
)
REPEATED_SUPPORT_FAILURE_MARKERS = (
    "третий раз никто не помог",
    "никто не помог",
    "все перепробовал",
    "всё перепробовал",
    "все бесполезно, ничего не работает",
    "всё бесполезно, ничего не работает",
    "гоняют по кругу",
)


def is_operator_request(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().replace("ё", "е").split())
    if not normalized:
        return False
    if LIVE_PERSON_RE.search(normalized):
        return True
    if any(
        marker in normalized
        for marker in (
            "молодому специалисту",
            "молодого специалиста",
            "молодой специалист",
        )
    ) and not any(marker in normalized for marker in ("оператор", "поддержк", "сотрудник")):
        return False
    has_direct_target = any(marker in normalized for marker in TARGET_MARKERS)
    has_support_target = any(
        marker in normalized
        for marker in (
            "служба поддерж",
            "службой поддерж",
            "техподдерж",
            "сотрудник поддерж",
            "оператор поддерж",
        )
    )
    if not has_direct_target and not has_support_target:
        return False
    if _is_operator_employment_request(normalized):
        return False
    return any(marker in normalized for marker in ACTION_MARKERS)


def operator_review_reason(text: str) -> str | None:
    """Return a fail-safe escalation reason for messages that need a human check."""

    normalized = " ".join(str(text or "").casefold().replace("ё", "е").split())
    if not normalized:
        return None
    if is_operator_request(normalized):
        return "operator_requested"
    if _is_personal_status_request(normalized):
        return "personal_status"
    if _is_personal_workflow_failure(normalized):
        return "personal_status"
    if _is_registered_appeal_request(normalized):
        return "operator_requested"
    # Yonote first-line policy requires troubleshooting guidance before escalation.
    # Repeated failures are handled below; a first technical report stays in RAG.
    if _is_press_accreditation_request(normalized):
        return "operator_requested"
    if _is_personal_ticket_request(normalized):
        return "personal_status"
    if any(marker in normalized for marker in REPEATED_SUPPORT_FAILURE_MARKERS):
        return "repeated_support_failure"
    if any(marker in normalized for marker in OPERATOR_ONLY_MARKERS):
        return "operator_requested"
    return None


def _is_personal_status_request(normalized: str) -> bool:
    if normalized in {
        "статус заявки",
    }:
        return True
    if any(marker in normalized for marker in PERSONAL_STATUS_MARKERS):
        return True
    if "я подал заяв" in normalized and any(
        marker in normalized for marker in ("прошел ли", "прошёл ли")
    ):
        return True
    if any(
        marker in normalized
        for marker in (
            "я зарегистрирован на",
            "я зарегистрирована на",
            "зарегистрирован ли я",
            "зарегистрирована ли я",
        )
    ):
        return True
    if (
        "статус" in normalized
        and not _is_general_status_explanation(normalized)
        and any(
            marker in normalized
            for marker in ("участие офлайн", "в рассмотр", "покупать билет")
        )
    ):
        return True
    if "заяв" in normalized and "службу поддержки" in normalized and any(
        marker in normalized for marker in ("отмен", "отозв")
    ):
        return True
    if any(marker in normalized for marker in ("не прошла", "не прошел", "не прошёл")) and any(
        marker in normalized for marker in ("форум", "отбор", "видеовизит")
    ) and any(marker in normalized for marker in ("почему", "рассматрив")):
        return True
    if re.search(r"\bзаявк[аи]\s*[№#]\s*\d+", normalized):
        return True
    if any(
        marker in normalized
        for marker in ("моя заявк", "мою заявк", "моей заявк", "свою заявк")
    ) and any(
        marker in normalized
        for marker in ("на каком этапе", "где сейчас", "что с заяв", "находится")
    ):
        return True
    submitted_own_application = any(
        marker in normalized
        for marker in (
            "я подал заяв",
            "я подала заяв",
            "я подавал заяв",
            "я подавала заяв",
            "я отправил заяв",
            "я отправила заяв",
        )
    )
    if submitted_own_application and any(
        marker in normalized
        for marker in (
            "на каком этапе",
            "этап рассмотр",
            "когда ждать подтвержд",
            "ее нет в аккаунте",
            "ее нет в кабинете",
            "пропала из аккаунта",
            "пропала из кабинета",
        )
    ):
        return True
    if any(
        marker in normalized
        for marker in ("моя заявк", "мою заявк", "моей заявк")
    ) and any(
        marker in normalized
        for marker in ("не приняли", "не принята", "до сих пор не принят")
    ) and any(
        marker in normalized
        for marker in (
            "сколько еще",
            "сколько ждать",
            "займет рассмотр",
            "займёт рассмотр",
        )
    ):
        return True
    return False


def _is_general_status_explanation(normalized: str) -> bool:
    if not any(
        marker in normalized
        for marker in (
            "что означает",
            "что значит",
            "как понимать",
            "объясните статус",
            "расшифруйте статус",
        )
    ):
        return False
    return not any(
        marker in normalized
        for marker in (
            "у меня",
            "мой статус",
            "моя заявк",
            "мою заявк",
            "моей заявк",
            "я подал",
            "я подала",
            "могу покупать",
            "могу уже",
        )
    )


def _is_operator_employment_request(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "работать оператор",
            "работа оператор",
            "вакансия оператор",
            "вакансии оператор",
            "устроиться оператор",
            "стать оператором",
            "побыть оператор",
            "побыть у вас оператор",
            "работать специалистом поддерж",
            "вакансия специалиста поддерж",
            "отправить резюме",
        )
    )


def _is_personal_workflow_failure(normalized: str) -> bool:
    """Route only an individual failed object, not a general how-to question."""

    has_personal_object = any(
        marker in normalized for marker in PERSONAL_WORKFLOW_OBJECT_MARKERS
    )
    if not has_personal_object:
        return False
    return any(marker in normalized for marker in PERSONAL_WORKFLOW_FAILURE_MARKERS)


def _is_registered_appeal_request(normalized: str) -> bool:
    if not any(marker in normalized for marker in REGISTERED_APPEAL_MARKERS):
        return False
    return any(
        marker in normalized
        for marker in (
            "зарегистр",
            "присво",
            "сообщ",
            "предостав",
            "дать",
            "нужен",
        )
    )


def _is_technical_review_request(normalized: str) -> bool:
    if "положен" in normalized and any(
        marker in normalized for marker in ("не отображ", "личном кабинете")
    ):
        return False
    if any(marker in normalized for marker in TECHNICAL_REVIEW_MARKERS):
        return True
    if "ошибка" in normalized and any(
        marker in normalized
        for marker in ("публикац", "карточ", "сайт", "фгаис", "личном кабинете")
    ):
        return True
    if "не могу зарегистрироваться" in normalized:
        return True
    return False


def _is_press_accreditation_request(normalized: str) -> bool:
    if "аккредитац" not in normalized:
        return False
    return any(
        marker in normalized
        for marker in ("съем", "съём", "сми", "пресс", "видео", "фото")
    )


def _is_personal_ticket_request(normalized: str) -> bool:
    if "билет" not in normalized:
        return False

    if any(
        marker in normalized
        for marker in (
            "переоформ",
            "перенести билет",
            "перенесите билет",
            "привязать билет",
            "отвязать билет",
            "восстановить билет",
            "заменить билет",
            "изменить билет",
            "удалить билет",
        )
    ):
        return True

    related_person = any(
        marker in normalized
        for marker in (
            "муж",
            "жена",
            "супруг",
            "супруга",
            "ребен",
            "ребён",
            "дет",
            "другой человек",
        )
    )
    child_policy_question = any(
        marker in normalized for marker in ("ребен", "ребён", "дет")
    ) and any(
        marker in normalized
        for marker in (
            "как получить билет",
            "как зарегистрировать",
            "нужен ли билет",
            "нужен ли отдельный билет",
        )
    )
    if child_policy_question and not any(
        marker in normalized
        for marker in ("не получил", "не получила", "не пришел", "не пришёл", "переоформ")
    ):
        return False
    personal_action = any(
        marker in normalized
        for marker in (
            "получить билет на",
            "оформить билет на",
            "сдать билет",
            "на мою почту",
            "на мою электронную почту",
            "вместо него",
            "вместо нее",
            "вместо неё",
        )
    )
    if related_person and personal_action:
        return True

    missing_app = any(
        marker in normalized for marker in ("нет приложения max", "нет приложения макс")
    )
    return missing_app and any(
        marker in normalized for marker in ("получ", "оформ", "отправ", "перенес")
    )
