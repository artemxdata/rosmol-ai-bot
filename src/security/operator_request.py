from __future__ import annotations

import re

LIVE_PERSON_RE = re.compile(
    r"\bжив(?:ой|ого|ым)?\s+человек(?:а|ом)?\b",
    re.IGNORECASE | re.UNICODE,
)

TARGET_MARKERS = ("оператор", "специалист", "сотрудник", "поддержк")
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
    "статус все тот же",
    "статус всё тот же",
    "мне ничего не приходило",
    "на почте ничего нет",
    "не могу подтвердить",
    "не получается подтвердить",
    "не пришел сертификат",
    "не пришёл сертификат",
    "сертификат не получил",
    "сертификат не получила",
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
    "премия шум",
    "премии шум",
    "маркет молодых",
    "жареным мороженым",
    "где можно найти записи",
    "организатор не подключился",
)


def is_operator_request(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().replace("ё", "е").split())
    if not normalized:
        return False
    if LIVE_PERSON_RE.search(normalized):
        return True
    if not any(marker in normalized for marker in TARGET_MARKERS):
        return False
    if any(marker in normalized for marker in EMPLOYMENT_MARKERS):
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
    if _is_technical_review_request(normalized):
        return "technical_issue"
    if any(marker in normalized for marker in OPERATOR_ONLY_MARKERS):
        return "operator_requested"
    return None


def _is_personal_status_request(normalized: str) -> bool:
    if normalized in {
        "статус заявки",
        "заявка на грант",
        "грантовое соглашение",
        "вопрос по грантовому конкурсу",
    }:
        return True
    if any(marker in normalized for marker in PERSONAL_STATUS_MARKERS):
        return True
    if re.search(r"\bзаявк[аи]\s*[№#]\s*\d+", normalized):
        return True
    if "вопрос по заявке" in normalized and len(normalized) < 120:
        return True
    return False


def _is_technical_review_request(normalized: str) -> bool:
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
