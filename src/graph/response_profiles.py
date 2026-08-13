from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.models import QueryAnalysis
from src.response_contract import ResponseProfileName

DATE_VALUE_RE = re.compile(
    r"(?:"
    r"\b\d{1,2}\s*(?:(?:[-–—]\s*|по\s+)\d{1,2})?\s+"
    r"(?:январ[яе]|феврал[яе]|март[ае]?|апрел[яе]|ма[яе]|июн[яе]|"
    r"июл[яе]|август[ае]?|сентябр[яе]|октябр[яе]|ноябр[яе]|декабр[яе])"
    r"|\b(?:январ[яе]|феврал[яе]|март[ае]?|апрел[яе]|ма[яе]|июн[яе]|"
    r"июл[яе]|август[ае]?|сентябр[яе]|октябр[яе]|ноябр[яе]|декабр[яе])\b"
    r"|\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b"
    r"|\b20\d{2}\s+год"
    r")",
    flags=re.IGNORECASE,
)
_CONCRETE_EVENT_DATE_RE = re.compile(
    r"(?:"
    r"\b\d{1,2}\s+"
    r"(?:январ[яе]|феврал[яе]|март[ае]?|апрел[яе]|ма[яе]|июн[яе]|"
    r"июл[яе]|август[ае]?|сентябр[яе]|октябр[яе]|ноябр[яе]|декабр[яе])"
    r"|\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b"
    r")",
    flags=re.IGNORECASE,
)
_EVENT_DATE_RANGE_RE = re.compile(
    r"(?:"
    r"\b(?:с\s+)?\d{1,2}\s*(?:[-–—]|по)\s*\d{1,2}\s+"
    r"(?:январ[яе]|феврал[яе]|март[ае]?|апрел[яе]|ма[яе]|июн[яе]|"
    r"июл[яе]|август[ае]?|сентябр[яе]|октябр[яе]|ноябр[яе]|декабр[яе])"
    r"|\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\s*[-–—]\s*"
    r"\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b"
    r")",
    flags=re.IGNORECASE,
)

LEGACY_EVENT_DATE_TOPICS = frozenset(
    {
        "daty_nachala_meropriyatiya",
        "mesto_i_daty_provedeniya_meropriyatiya",
        "mesto_i_ploschadka_provedeniya",
        "vremya_nachala_i_raspisanie",
        "sut_festivalya_i_data",
    }
)

_EVENT_DATE_TEXT_MARKERS = (
    "даты проведения",
    "дата проведения",
    "период проведения",
    "дата форума",
    "даты форума",
    "дата мероприятия",
    "даты мероприятия",
    "дата смены",
    "даты смены",
    "даты:",
    "пройдет",
    "пройдёт",
    "проходит",
    "состоится",
    "начнется",
    "начнётся",
    "день заезда",
    "день открытия",
    "день закрытия",
    "день выезда",
    "полные дни",
)
_SHIFT_MARKERS = ("смена", "заезд", "выезд", "отъезд", "разъезд")
_EVENT_SUBJECT_MARKERS = (
    "форум",
    "фестиваль",
    "мероприят",
    "событи",
    "смена",
    "заезд",
    "выезд",
)
_APPLICATION_TOPIC_MARKERS = (
    "registraci",
    "registration",
    "dedlayn",
    "deadline",
    "podach",
    "zayav",
    "application",
    "otbor",
    "selection",
    "etap",
)
_APPLICATION_TEXT_MARKERS = (
    "регистрац",
    "подача заяв",
    "подачи заяв",
    "подачу заяв",
    "прием заяв",
    "приём заяв",
    "дедлайн",
    "отбор",
)

_TEMPORAL_TEXT_MARKERS = (
    "когда",
    "срок",
    "дедлайн",
    "до какого",
    "по какое",
    "дат",
    "период",
    "опублику",
    "объяв",
    "извест",
    "уведом",
)
_SELECTION_BINDING_MARKERS = (
    "результат",
    "статус заяв",
    "статусы заяв",
    "итог",
    "прошел отбор",
    "прошёл отбор",
    "прошла отбор",
    "отбор",
    "одобрен",
    "одобрена",
    "отклонен",
    "отклонён",
    "отклонена",
    "резерв",
)
_APPLICATION_BINDING_MARKERS = (
    "подача заяв",
    "подачи заяв",
    "подачу заяв",
    "подать заяв",
    "прием заяв",
    "приём заяв",
    "регистрац",
    "зарегистр",
    "дедлайн заяв",
    "срок заяв",
)
_APPLICATION_PROCESS_MARKERS = (
    "этап",
    "порядок",
    "процесс",
    "процедур",
)
_SELECTION_OUTCOME_MARKERS = (
    "результат",
    "статус",
    "итог",
    "одобрен",
    "отклон",
    "резерв",
    "победител",
    "прошел отбор",
    "прошёл отбор",
)
_EXPLICIT_EVENT_TIMING_MARKERS = (
    "дата проведения",
    "даты проведения",
    "период проведения",
    "срок проведения",
    "когда проходит",
    "когда он проходит",
    "когда пройдет",
    "когда он пройдет",
    "когда пройдёт",
    "когда он пройдёт",
    "когда проводится",
    "когда состоится",
    "когда начинается",
    "когда начнется",
    "когда начнётся",
    "когда сам форум",
    "дата форума",
    "даты форума",
    "дата мероприятия",
    "даты мероприятия",
    "дата смены",
    "даты смены",
    "начало форума",
    "окончание форума",
    "начало мероприятия",
    "окончание мероприятия",
)
_TECHNICAL_FAILURE_MARKERS = (
    "появляется ошибка",
    "возникает ошибка",
    "выдает ошиб",
    "выдаёт ошиб",
    "код ошибки",
    "ошибка на сайт",
    "ошибка в форм",
    "ошибка при",
    "не отправляется",
    "не отправилась",
    "не отправлено",
    "не сохраняется",
    "не сохранилась",
    "не сохранено",
    "не загружается",
    "не загрузилось",
    "не прикрепляется",
    "не прикрепилось",
    "не открывается",
    "не работает",
    "повторно не помогло",
    "снова не помогло",
)

_EXACT_WORD_PROFILE_MARKERS = frozenset({"еда", "овз"})
_STRICT_CROSS_ASPECT_PROFILES = frozenset(
    {
        ResponseProfileName.DATES,
        ResponseProfileName.APPLICATION,
        ResponseProfileName.DOCUMENTS,
        ResponseProfileName.SELECTION_STATUS,
        ResponseProfileName.TRAVEL,
    }
)

_PROFILE_MARKERS: dict[ResponseProfileName, tuple[str, ...]] = {
    ResponseProfileName.APPLICATION: (
        "подать заяв",
        "подача заяв",
        "подачи заяв",
        "подачу заяв",
        "заявку пода",
        "заявка пода",
        "заявки пода",
        "заявку нужно подать",
        "заявку можно подать",
        "прием заяв",
        "приём заяв",
        "регистрац",
        "зарегистр",
        "анкет",
        "отозвать заяв",
        "отменить заяв",
        "изменить заяв",
        "редактировать заяв",
    ),
    ResponseProfileName.ELIGIBILITY: (
        "возраст",
        "кто может",
        "условия участия",
        "требования к участник",
        "подхожу ли",
        "юридическое лицо",
    ),
    ResponseProfileName.DOCUMENTS: (
        "документ",
        "паспорт",
        "полис",
        "справк",
        "сертификат",
        "положение",
        "письмо-вызов",
        "письмо вызов",
    ),
    ResponseProfileName.SELECTION_STATUS: (
        "статус заяв",
        "статусы заяв",
        "результат",
        "на рассмотрении",
        "рассмотрение заяв",
        "рассматривается заяв",
        "рассматривают заяв",
        "прошел отбор",
        "прошёл отбор",
        "прошла отбор",
        "одобрен",
        "отклонен",
        "отклонён",
        "резерв",
        "отбор",
    ),
    ResponseProfileName.PROGRAM: (
        "программ",
        "расписан",
        "афиш",
        "кто выступ",
        "лекци",
        "дискус",
        "мастер-класс",
        "активност",
    ),
    ResponseProfileName.TRAVEL: (
        "проезд",
        "дорог",
        "трансфер",
        "маршрут",
        "вокзал",
        "аэропорт",
    ),
    ResponseProfileName.ACCOMMODATION: (
        "прожив",
        "размещен",
        "размещён",
        "общежит",
        "гостиниц",
        "палат",
    ),
    ResponseProfileName.FOOD: ("питан", "еда", "корм", "столов"),
    ResponseProfileName.ACCESSIBILITY: (
        "инвалид",
        "овз",
        "маломобиль",
        "сопровождающ",
    ),
    ResponseProfileName.GRANTS: (
        "грант",
        "смет",
        "отчет по проект",
        "отчёт по проект",
    ),
    ResponseProfileName.TECHNICAL: (
        "появляется ошибка",
        "возникает ошибка",
        "выдает ошиб",
        "выдаёт ошиб",
        "код ошибки",
        "ошибка на сайт",
        "ошибка в форм",
        "ошибка при",
        "не работает",
        "не могу войти",
        "техподдерж",
        "парол",
        "авторизац",
        "кеш",
        "cookie",
        "браузер",
        "инкогнито",
        "другое устройство",
    ),
}


def normalize_profile_text(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def infer_response_profile(
    analysis: QueryAnalysis,
    message: str,
) -> ResponseProfileName:
    """Infer the requested answer aspect, preferring explicit intent over question words."""

    category = normalize_profile_text(str(analysis.category or ""))
    primary = _infer_profile_from_evidence(
        normalize_profile_text(message),
        category="",
        is_technical=False,
    )
    if primary != ResponseProfileName.GENERIC:
        return primary

    fallback_evidence = normalize_profile_text(
        " ".join(
            [
                *analysis.topics,
                *(question.text for question in analysis.questions),
                *(question.topic or "" for question in analysis.questions),
            ]
        )
    )
    return _infer_profile_from_evidence(
        fallback_evidence,
        category=category,
        is_technical=analysis.is_technical,
    )


def _infer_profile_from_evidence(
    evidence: str,
    *,
    category: str,
    is_technical: bool,
) -> ResponseProfileName:
    if _has_application_edit_policy_intent(evidence):
        return ResponseProfileName.APPLICATION
    if (
        is_technical
        or category == "техподдержка"
        or has_explicit_technical_failure(evidence)
        or _has_profile_markers(
            evidence,
            ResponseProfileName.TECHNICAL,
        )
    ):
        return ResponseProfileName.TECHNICAL

    if _has_selection_status_intent(evidence):
        return ResponseProfileName.SELECTION_STATUS
    ticket_profile = resolve_ticket_response_profile(evidence)
    if ticket_profile is not None:
        return ticket_profile
    if _has_participation_withdrawal_intent(evidence):
        return ResponseProfileName.APPLICATION
    if _has_explicit_travel_focus(evidence):
        return ResponseProfileName.TRAVEL
    if _has_document_delivery_intent(evidence):
        return ResponseProfileName.DOCUMENTS
    if _has_application_process_intent(evidence):
        return ResponseProfileName.APPLICATION
    if _has_contextual_eligibility_intent(evidence):
        return ResponseProfileName.ELIGIBILITY
    if _has_arrival_logistics_focus(evidence):
        return ResponseProfileName.TRAVEL

    temporal_profile = bound_temporal_response_profile(evidence)
    if temporal_profile is not None:
        return temporal_profile

    # Explicit business aspects must win over a generic temporal word. For example,
    # "когда будут результаты?" is selection status, not event dates.
    ordered_profiles = (
        ResponseProfileName.SELECTION_STATUS,
        ResponseProfileName.DOCUMENTS,
        ResponseProfileName.ELIGIBILITY,
        ResponseProfileName.TRAVEL,
        ResponseProfileName.ACCOMMODATION,
        ResponseProfileName.FOOD,
        ResponseProfileName.ACCESSIBILITY,
        ResponseProfileName.APPLICATION,
        ResponseProfileName.PROGRAM,
    )
    for profile in ordered_profiles:
        if _has_profile_markers(evidence, profile):
            return profile

    if category == "гранты" or _has_profile_markers(
        evidence,
        ResponseProfileName.GRANTS,
    ):
        return ResponseProfileName.GRANTS
    if asks_event_dates(evidence):
        return ResponseProfileName.DATES
    return ResponseProfileName.GENERIC


def asks_event_dates(normalized_text: str) -> bool:
    text = normalize_profile_text(normalized_text)
    if not text:
        return False
    if bound_temporal_response_profile(text) is not None:
        return False
    if any(
        marker in text
        for profile in (
            ResponseProfileName.SELECTION_STATUS,
            ResponseProfileName.TRAVEL,
            ResponseProfileName.APPLICATION,
            ResponseProfileName.DOCUMENTS,
            ResponseProfileName.PROGRAM,
        )
        for marker in _PROFILE_MARKERS[profile]
    ):
        return False
    return any(
        marker in text
        for marker in (
            "когда",
            "дата",
            "даты",
            "срок проведения",
            "период проведения",
        )
    )


def _has_explicit_travel_focus(text: str) -> bool:
    normalized = normalize_profile_text(text)
    return any(marker in normalized for marker in ("трансфер", "шаттл"))


def _has_document_delivery_intent(text: str) -> bool:
    normalized = normalize_profile_text(text)
    has_letter = "письм" in normalized
    has_invitation = any(
        marker in normalized
        for marker in ("приглашение", "пригласительное", "письмо-вызов", "письмо вызов")
    )
    has_official_confirmation = (
        "официальн" in normalized and "подтвержд" in normalized
    )
    has_document_action = any(
        marker in normalized
        for marker in ("получ", "присл", "отправ", "нуж", "где", "когда")
    )
    return (has_invitation or has_official_confirmation) and (
        has_letter or has_document_action
    )


def _has_selection_status_intent(text: str) -> bool:
    normalized = normalize_profile_text(text)
    if "заяв" not in normalized:
        return False
    if any(
        marker in normalized
        for marker in (
            "на рассмотрении",
            "на каком этапе",
            "этап ее рассмотр",
            "сколько времени рассматри",
            "когда рассматрива",
            "сколько еще может занять рассмотр",
            "не рассмотрели",
            "до сих пор не приняли",
            "статус заявки",
            "статусы заявки",
        )
    ):
        return True
    submitted = any(
        marker in normalized
        for marker in (
            "подал заяв",
            "подала заяв",
            "подавал заяв",
            "подавала заяв",
            "отправил заяв",
            "отправила заяв",
        )
    )
    missing_notice = any(
        marker in normalized
        for marker in (
            "письмо не пришло",
            "письмо не приходит",
            "приглашение не пришло",
            "когда ждать подтвержд",
        )
    )
    return submitted and missing_notice


def resolve_ticket_response_profile(text: str) -> ResponseProfileName | None:
    """Resolve an admission ticket separately from a transport ticket.

    The bare word ``билет`` is deliberately insufficient: without a transport
    or event-admission anchor it must not silently move an answer to TRAVEL.
    """

    normalized = normalize_profile_text(text)
    if "билет" not in normalized:
        return None

    explicit_transport = any(
        marker in normalized
        for marker in (
            "проезд",
            "дорог",
            "поезд",
            "самолет",
            "самолёт",
            "авиа",
            "вокзал",
            "аэропорт",
            "туда и обратно",
            "железнодорож",
            "ж/д",
            "ржд",
            "перелет",
            "перелёт",
            "транспортн",
        )
    )
    transport_payment = any(
        marker in normalized
        for marker in (
            "оплат",
            "оплач",
            "компенс",
            "возмещ",
            "возмест",
            "стоимост",
        )
    ) and any(
        marker in normalized
        for marker in (
            "до форума",
            "до мероприятия",
            "до места проведения",
            "туда и обратно",
        )
    )
    if explicit_transport or transport_payment:
        return ResponseProfileName.TRAVEL

    admission_delivery = any(
        marker in normalized
        for marker in (
            "мои билеты",
            "входной билет",
            "электронный билет",
            "qr",
            "код вход",
            "код для вход",
            "билет не приш",
            "не пришел билет",
            "не пришёл билет",
            "не вижу билет",
            "найти билет",
            "получить билет",
            "восстановить билет",
            "повторно получить билет",
            "письм",
            "спам",
            "чат-бот",
            "чат бот",
            "max",
        )
    )
    event_subject = any(
        marker in normalized
        for marker in ("форум", "фестивал", "мероприят", "день молодежи")
    )
    if admission_delivery or event_subject:
        return ResponseProfileName.APPLICATION
    return None


def _has_event_admission_ticket_intent(text: str) -> bool:
    """Compatibility wrapper for existing internal callers."""

    return resolve_ticket_response_profile(text) == ResponseProfileName.APPLICATION


def _has_participation_withdrawal_intent(text: str) -> bool:
    normalized = normalize_profile_text(text)
    if bool(
        re.search(
            r"\b(?:отменить|отозвать|отказаться\s+от)\s+"
            r"(?:уже\s+)?(?:подтвержденн\w*\s+)?участи\w*",
            normalized,
        )
    ):
        return True

    cannot_attend = any(
        marker in normalized
        for marker in (
            "не могу поехать",
            "не смогу поехать",
            "не получается поехать",
            "не могу приехать",
            "не смогу приехать",
            "не получается приехать",
            "не могу посетить",
            "не смогу посетить",
        )
    )
    if not cannot_attend:
        return False
    has_arrival_timing = any(
        marker in normalized
        for marker in (
            "день заезда",
            "дата заезда",
            "время заезда",
            "раньше",
            "позже",
            "опозд",
            "в другой день",
        )
    )
    return not has_arrival_timing and any(
        marker in normalized
        for marker in ("что делать", "подтверд", "участи", "отказ", "отозв")
    )


def _has_application_process_intent(text: str) -> bool:
    normalized = normalize_profile_text(text)
    if "отбор" not in normalized:
        return False
    if any(marker in normalized for marker in _SELECTION_OUTCOME_MARKERS):
        return False
    return any(marker in normalized for marker in _APPLICATION_PROCESS_MARKERS)


def _has_contextual_eligibility_intent(text: str) -> bool:
    normalized = normalize_profile_text(text)
    has_permission_question = any(
        marker in normalized
        for marker in (
            "может ли",
            "могут ли",
            "можно ли",
            "кто может",
            "как могут участв",
        )
    )
    if "волонтер" in normalized and has_permission_question:
        return True
    if "команд" in normalized and any(
        marker in normalized
        for marker in ("участв", "возраст", "может", "можно")
    ):
        return True
    return bool(
        re.search(r"(?<!\d)\d{1,2}\s*(?:[-–—]\s*\d{1,2}\s*)?лет\b", normalized)
        and any(marker in normalized for marker in ("участв", "команд", "можно", "может"))
    )


def _has_arrival_logistics_focus(text: str) -> bool:
    normalized = normalize_profile_text(text)
    if any(
        marker in normalized
        for marker in ("когда", "дата заезда", "даты заезда", "день заезда когда")
    ):
        return False
    has_arrival_action = any(
        marker in normalized
        for marker in ("приехать", "поехать", "заезд", "выезд", "уехать")
    )
    has_logistics_constraint = any(
        marker in normalized
        for marker in (
            "не могу",
            "не смогу",
            "не получается",
            "раньше",
            "позже",
            "опозд",
            "в другой день",
        )
    )
    return has_arrival_action and has_logistics_constraint


def bound_temporal_response_profile(
    text: str,
) -> ResponseProfileName | None:
    """Bind a temporal phrase to its explicit business subject.

    A word such as ``когда`` or ``срок`` is not an event-date intent by itself:
    the requested fact may be a selection result or an application deadline.
    """

    normalized = normalize_profile_text(text)
    if not normalized or not any(
        marker in normalized for marker in _TEMPORAL_TEXT_MARKERS
    ):
        return None
    if _has_selection_status_intent(normalized):
        return ResponseProfileName.SELECTION_STATUS
    if _has_application_process_intent(normalized):
        return ResponseProfileName.APPLICATION
    if any(marker in normalized for marker in _SELECTION_BINDING_MARKERS):
        return ResponseProfileName.SELECTION_STATUS
    if any(marker in normalized for marker in _APPLICATION_BINDING_MARKERS):
        return ResponseProfileName.APPLICATION
    return None


def has_explicit_event_timing(text: str) -> bool:
    """Return true for an event/shift timing clause, not a nearby deadline."""

    normalized = normalize_profile_text(text)
    if any(marker in normalized for marker in _EXPLICIT_EVENT_TIMING_MARKERS):
        return True
    if re.search(
        r"\b(?:сроки?|даты?|когда)\s+(?:сам(?:ого|ому|ом)?\s+)?"
        r"(?:форум\w*|мероприяти\w*|фестивал\w*|событи\w*|смен\w*)\b",
        normalized,
        flags=re.UNICODE,
    ):
        return True
    return (
        any(marker in normalized for marker in _SHIFT_MARKERS)
        and any(
            marker in normalized
            for marker in ("когда", "дата", "даты", "период", "начина")
        )
        and not any(
            marker in normalized
            for marker in ("подача заяв", "прием заяв", "приём заяв", "дедлайн")
        )
    )


def should_suppress_event_date_question(text: str) -> bool:
    """Do not synthesize an event-date question from a non-event deadline clause."""

    return (
        bound_temporal_response_profile(text) is not None
        and not has_explicit_event_timing(text)
    )


def has_explicit_application_action(text: str) -> bool:
    """Detect an explicit request about submitting or registering an application."""

    normalized = normalize_profile_text(text)
    has_reporting_subject = any(
        marker in normalized
        for marker in (
            "отчет",
            "отчетност",
            "соглашен",
            "закрывающ",
            "контрольн",
        )
    )
    has_application_subject = any(
        marker in normalized for marker in ("заяв", "регистрац", "анкет")
    )
    if has_reporting_subject and not has_application_subject:
        return False
    return _has_application_edit_policy_intent(normalized) or any(
        marker in normalized
        for marker in (
            "как подать",
            "где подать",
            "когда подать",
            "до какого числа подать",
            "срок подачи",
            "дедлайн подачи",
            "как зарегистр",
            "где зарегистр",
            "срок регистрац",
            "дедлайн регистрац",
        )
    )


def _has_application_edit_policy_intent(text: str) -> bool:
    normalized = normalize_profile_text(text)
    if "заяв" not in normalized or not any(
        marker in normalized for marker in ("исправ", "измен", "редакт")
    ):
        return False
    if any(
        marker in normalized
        for marker in (
            "не работает",
            "не сохраня",
            "не отправ",
            "не откры",
            "не загруж",
            "не отображ",
            "кнопк",
            "появляется ошибка",
            "выдает ошиб",
            "выдаёт ошиб",
        )
    ):
        return False
    return any(
        marker in normalized
        for marker in ("до отправ", "ответ", "данн", "поле анкеты", "в анкете")
    )


def has_explicit_technical_failure(text: str) -> bool:
    """Detect an observable UI failure independently of the affected business noun."""

    normalized = normalize_profile_text(text)
    if _has_application_edit_policy_intent(normalized):
        return False
    if any(
        marker in normalized
        for marker in ("не получается", "не удается", "не удаётся")
    ) and any(
        marker in normalized
        for marker in (
            "войти",
            "авториз",
            "зарегистр",
            "отправить заяв",
            "подать заяв",
            "сохранить",
            "загрузить",
            "прикрепить",
            "открыть",
        )
    ):
        return True
    if any(marker in normalized for marker in _TECHNICAL_FAILURE_MARKERS):
        return True

    has_ui_context = any(
        marker in normalized
        for marker in (
            "форм",
            "поле",
            "кнопк",
            "кабинет",
            "аккаунт",
            "платформ",
            "фгаис",
            "ссылк",
            "проект",
            "заявк",
        )
    )
    if has_ui_context and any(
        marker in normalized
        for marker in (
            "не отображ",
            "не высвеч",
            "не появляется",
            "пропал",
            "пропала",
            "исчез",
            "недоступ",
            "неверная ссыл",
            "неправильная ссыл",
            "потерял доступ",
            "потеряла доступ",
            "нет доступа",
        )
    ):
        if "приглашен" not in normalized and "приглашён" not in normalized:
            return True

    return (
        bool(
            re.search(
                r"(?:не приходит\s+письм|письм[^.!?]{0,80}\s+не приход|"
                r"не пришл[ои]\s+письм)",
                normalized,
            )
        )
        and any(
            marker in normalized
            for marker in ("форм", "платформ", "фгаис", "подтвержд")
        )
    )


def chunk_has_event_date_evidence(
    text: str,
    metadata: Mapping[str, Any] | None,
) -> bool:
    """Return true only for event timing, not a registration deadline alone."""

    metadata = metadata or {}
    normalized_text = normalize_profile_text(text)
    topic = normalize_profile_text(str(metadata.get("topic") or ""))
    metadata_haystack = normalize_profile_text(
        " ".join(
            str(value or "")
            for value in (
                metadata.get("chunk_id"),
                metadata.get("intent_name"),
                metadata.get("topic"),
                metadata.get("source_category"),
            )
        )
    )
    if topic in LEGACY_EVENT_DATE_TOPICS or any(
        legacy_topic in metadata_haystack for legacy_topic in LEGACY_EVENT_DATE_TOPICS
    ):
        return True

    has_concrete_date = bool(_CONCRETE_EVENT_DATE_RE.search(normalized_text))
    has_date_range = bool(_EVENT_DATE_RANGE_RE.search(normalized_text))
    if not has_concrete_date:
        return False

    shift_context = any(
        marker in f"{metadata_haystack} {normalized_text}" for marker in _SHIFT_MARKERS
    )
    application_topic = any(
        marker in topic or marker in metadata_haystack
        for marker in _APPLICATION_TOPIC_MARKERS
    )
    event_subject = any(
        marker in normalized_text for marker in _EVENT_SUBJECT_MARKERS
    )

    # Registration, selection and deadline chunks often contain many dates and
    # generic verbs such as "проходит". They are not event-schedule evidence.
    # A mixed registration/shift chunk is accepted only when it contains an
    # actual shift range, not merely a deadline.
    if application_topic:
        return shift_context and has_date_range

    if has_date_range and (shift_context or event_subject):
        return True

    for part in re.split(r"[\n.!?]+", normalized_text):
        if not _CONCRETE_EVENT_DATE_RE.search(part):
            continue
        if not any(marker in part for marker in _EVENT_DATE_TEXT_MARKERS):
            continue
        if any(marker in part for marker in _APPLICATION_TEXT_MARKERS) and not any(
            marker in part for marker in _EVENT_SUBJECT_MARKERS
        ):
            continue
        return True
    return False


def response_has_cross_aspect_drift(
    expected: ResponseProfileName,
    response: str,
) -> bool:
    """Reject an answer that only contains evidence of a different explicit aspect."""

    if expected in {
        ResponseProfileName.DATES,
        ResponseProfileName.GENERIC,
        ResponseProfileName.GRANTS,
        ResponseProfileName.TECHNICAL,
    }:
        return False
    for clause in _response_profile_clauses(response):
        detected = detect_response_profiles(clause)
        if (
            expected == ResponseProfileName.ELIGIBILITY
            and ResponseProfileName.APPLICATION in detected
            and _is_adjectival_eligibility_registration(clause)
        ):
            detected.discard(ResponseProfileName.APPLICATION)
        relevant_detected = detected & _STRICT_CROSS_ASPECT_PROFILES
        if not relevant_detected or expected in relevant_detected:
            continue
        return True
    return False


def _is_adjectival_eligibility_registration(clause: str) -> bool:
    normalized = normalize_profile_text(clause)
    return bool(
        "юридическое лицо" in normalized
        and re.search(
            r"зарегистрированн\w*\s+на\s+территории",
            normalized,
        )
        and not any(
            marker in normalized
            for marker in (
                "заявк",
                "зарегистрироваться",
                "пройти регистрацию",
                "создать кабинет",
                "личный кабинет",
            )
        )
    )


def response_has_cross_aspect_drift_for_profiles(
    expected: set[ResponseProfileName],
    response: str,
) -> bool:
    relevant_expected = expected - {
        ResponseProfileName.GENERIC,
        ResponseProfileName.GRANTS,
        ResponseProfileName.TECHNICAL,
    }
    if not relevant_expected:
        return False
    clauses = _response_profile_clauses(response)
    detected_by_clause = [detect_response_profiles(clause) for clause in clauses]
    relevant_detected = set().union(*detected_by_clause) if detected_by_clause else set()
    if not relevant_expected.issubset(relevant_detected):
        return True
    if ResponseProfileName.GENERIC in expected:
        return False

    strict_expected = relevant_expected & _STRICT_CROSS_ASPECT_PROFILES
    return any(
        bool(
            (clause_profiles & _STRICT_CROSS_ASPECT_PROFILES)
            - strict_expected
        )
        and not bool(clause_profiles & relevant_expected)
        for clause_profiles in detected_by_clause
    )


def detect_response_profiles(response: str) -> set[ResponseProfileName]:
    normalized = normalize_profile_text(response)
    detected = {
        profile
        for profile in _PROFILE_MARKERS
        if _has_profile_markers(normalized, profile)
    }
    if chunk_has_event_date_evidence(response, {}):
        detected.add(ResponseProfileName.DATES)
    if _has_application_edit_policy_intent(normalized):
        detected.add(ResponseProfileName.APPLICATION)
        detected.discard(ResponseProfileName.TECHNICAL)
    ticket_profile = resolve_ticket_response_profile(normalized)
    if ticket_profile is not None:
        detected.add(ticket_profile)
    return detected


def _response_profile_clauses(response: str) -> list[str]:
    clauses = [
        clause.strip()
        for clause in re.split(
            r"\n+|;+\s*|(?<=[.!?])\s+"
            r"|,\s+(?=(?:а|но|при этом|дата|даты)\b)"
            r"|,\s+(?=(?:форум|мероприятие|смена|регистрация|заявка|"
            r"проезд|трансфер|письмо|результаты|отбор)\b)"
            r"|,?\s+и\s+(?=(?:форум|мероприятие|смена|регистрация|заявка|"
            r"проезд|трансфер|письмо|результаты)\b)",
            response,
            flags=re.IGNORECASE,
        )
        if clause.strip()
    ]
    return clauses or [response]


def _has_profile_markers(text: str, profile: ResponseProfileName) -> bool:
    return any(_contains_profile_marker(text, marker) for marker in _PROFILE_MARKERS[profile])


def _contains_profile_marker(text: str, marker: str) -> bool:
    if marker == "положение":
        # ``положение`` is a document, while ``местоположение`` is only a
        # search/filter field. A raw substring match makes a grounded
        # application answer look like an unrelated documents answer.
        return bool(
            re.search(
                r"(?<!\w)положени(?:е|я|ю|ем|и|й|ям|ями|ях)(?!\w)",
                text,
            )
        )
    if marker in _EXACT_WORD_PROFILE_MARKERS:
        return bool(re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", text))
    return marker in text
