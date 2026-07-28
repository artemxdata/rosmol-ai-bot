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
    "прием заяв",
    "приём заяв",
    "дедлайн",
    "отбор",
)

_PROFILE_MARKERS: dict[ResponseProfileName, tuple[str, ...]] = {
    ResponseProfileName.APPLICATION: (
        "подать заяв",
        "подача заяв",
        "заявку пода",
        "заявка пода",
        "заявки пода",
        "прием заяв",
        "приём заяв",
        "регистрац",
        "зарегистр",
    ),
    ResponseProfileName.ELIGIBILITY: (
        "возраст",
        "кто может",
        "условия участия",
        "требования к участник",
        "подхожу ли",
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
        "результат",
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
    ),
    ResponseProfileName.TRAVEL: (
        "проезд",
        "дорог",
        "трансфер",
        "маршрут",
        "билет",
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
        "доступн",
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
        "ошиб",
        "не работает",
        "не могу войти",
        "не получается",
        "техподдерж",
        "парол",
        "авторизац",
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
    if (
        is_technical
        or category == "техподдержка"
        or _has_profile_markers(
            evidence,
            ResponseProfileName.TECHNICAL,
        )
    ):
        return ResponseProfileName.TECHNICAL

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
    detected = detect_response_profiles(response)
    return bool(detected) and expected not in detected


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
    detected = detect_response_profiles(response)
    return bool(detected) and not relevant_expected.issubset(detected)


def detect_response_profiles(response: str) -> set[ResponseProfileName]:
    normalized = normalize_profile_text(response)
    detected = {
        profile
        for profile in _PROFILE_MARKERS
        if _has_profile_markers(normalized, profile)
    }
    if chunk_has_event_date_evidence(response, {}):
        detected.add(ResponseProfileName.DATES)
    return detected


def _has_profile_markers(text: str, profile: ResponseProfileName) -> bool:
    return any(marker in text for marker in _PROFILE_MARKERS[profile])
