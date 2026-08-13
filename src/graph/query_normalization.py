from __future__ import annotations

import re

GOSUSLUGI_SHORT_RE = re.compile(r"(?<![0-9a-zа-яё])гу(?![0-9a-zа-яё])", re.IGNORECASE)
PERSONAL_CABINET_SHORT_RE = re.compile(
    r"(?<![0-9a-zа-яё])лк(?![0-9a-zа-яё])",
    re.IGNORECASE,
)
GENERIC_PLATFORM_REGISTRATION = "generic_platform_registration"
PLATFORM_EVENT_NAVIGATION = "platform_event_navigation"
ACCOUNT_DATA_RECOVERY = "account_data_recovery"
INACTIVE_PLATFORM_APPLICATION_BUTTON = "inactive_platform_application_button"
GRANT_DIRECTIONS = "grant_directions"
PHYSICAL_GRANTS_OVERVIEW = "physical_grants_overview"
FORUM_DISCOVERY = "forum_discovery"

BOUNDED_QUERY_INTENT_HINTS = {
    GENERIC_PLATFORM_REGISTRATION: "регистрация аккаунта ФГАИС auth register",
    PLATFORM_EVENT_NAVIGATION: (
        "поиск и навигация по мероприятиям фильтры в личном кабинете"
    ),
    ACCOUNT_DATA_RECOVERY: "объединение аккаунтов перенос данных старая почта",
    INACTIVE_PLATFORM_APPLICATION_BUTTON: (
        "ФГАИС неактивная кнопка подать заявку недостающие данные период подачи"
    ),
    GRANT_DIRECTIONS: "номинации тематики проектов грантовых конкурсов",
    PHYSICAL_GRANTS_OVERVIEW: "гранты для физических лиц общая информация",
    FORUM_DISCOVERY: "официальный каталог форумов и мероприятий Форумной дирекции",
}


def expand_query_aliases(text: str) -> str:
    expanded = str(text or "")
    expanded = GOSUSLUGI_SHORT_RE.sub("госуслуги", expanded)
    expanded = PERSONAL_CABINET_SHORT_RE.sub("личный кабинет", expanded)
    normalized = expanded.casefold().replace("ё", "е")
    aliases: list[str] = []
    if "парол" in normalized and any(
        marker in normalized for marker in ("восстанов", "забыл", "сброс", "помен")
    ):
        aliases.append("восстановить пароль восстановление пароля личный кабинет")
    if "рекоменд" in normalized and "студент" in normalized:
        aliases.append("рекомендации студенты студенческие сообщества")
    if "грант" in normalized and "подать" in normalized and "заявк" in normalized:
        grant_application_alias = "гранты для физических лиц подать заявку на участие"
        if grant_application_alias not in normalized:
            aliases.append(grant_application_alias)
    if "письмо" in normalized and any(marker in normalized for marker in ("вызов", "регион")):
        aliases.append("письмо-вызов письмо вызов официальное подтверждение участия")
    if _has_decline_participation_context(normalized):
        aliases.append(
            "отказ от участия отказаться от участия отозвать заявку отменить участие "
            "отмена заявки не смогу приехать не смогу посетить мероприятие"
        )
    if any(
        marker in normalized
        for marker in ("завернул", "не прошел отбор", "не прошёл отбор")
    ):
        aliases.append("почему отклонили заявку причина отклонения результаты отбора")
    if aliases:
        expanded = f"{expanded} {' '.join(aliases)}"
    return expanded


def bounded_query_intent(
    text: str,
    *,
    forum_normalized: str | None = None,
) -> str | None:
    """Return a query-proven generic entity/intent that is safe to scope strictly."""
    normalized = normalize_query_text(text)
    if not normalized:
        return None
    raw_matches = [
        intent
        for intent, predicate in (
            (GENERIC_PLATFORM_REGISTRATION, _asks_generic_platform_registration),
            (PLATFORM_EVENT_NAVIGATION, _asks_platform_event_navigation),
            (ACCOUNT_DATA_RECOVERY, _asks_account_data_recovery),
            (
                INACTIVE_PLATFORM_APPLICATION_BUTTON,
                _asks_inactive_platform_application_button,
            ),
            (GRANT_DIRECTIONS, _asks_grant_directions),
            (PHYSICAL_GRANTS_OVERVIEW, _asks_physical_grants_overview),
            (FORUM_DISCOVERY, _asks_forum_discovery),
        )
        if predicate(normalized)
    ]
    # Strict entity scoping is safe only for a single proven intent. A combined
    # question must keep the broader candidate set so every requested aspect
    # can be grounded independently.
    if len(raw_matches) != 1:
        return None
    intent = raw_matches[0]
    has_named_event_scope = bool(str(forum_normalized or "").strip())
    if intent in {
        GENERIC_PLATFORM_REGISTRATION,
        PLATFORM_EVENT_NAVIGATION,
        ACCOUNT_DATA_RECOVERY,
        INACTIVE_PLATFORM_APPLICATION_BUTTON,
        FORUM_DISCOVERY,
    }:
        return None if has_named_event_scope else intent
    if intent == GRANT_DIRECTIONS:
        return intent if _is_generic_grant_scope(forum_normalized) else None
    if intent == PHYSICAL_GRANTS_OVERVIEW:
        return intent if _is_physical_grants_scope(forum_normalized) else None
    return None


def bounded_query_intent_hint(
    text: str,
    *,
    forum_normalized: str | None = None,
) -> str:
    intent = bounded_query_intent(text, forum_normalized=forum_normalized)
    return BOUNDED_QUERY_INTENT_HINTS.get(intent, "")


def normalize_query_text(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def _is_generic_grant_scope(forum_normalized: str | None) -> bool:
    forum = normalize_query_text(str(forum_normalized or "")).strip(" .")
    return not forum or forum in {
        "гранты",
        "росмолодежь гранты",
        "росмолодежь.гранты",
    }


def _is_physical_grants_scope(forum_normalized: str | None) -> bool:
    forum = normalize_query_text(str(forum_normalized or "")).strip(" .")
    return not forum or forum in {
        "гранты для физических лиц",
        "росмолодежь гранты для физических лиц",
        "росмолодежь.гранты для физических лиц",
    }


def _asks_generic_platform_registration(normalized: str) -> bool:
    has_platform = any(
        marker in normalized
        for marker in ("фгаис", "молодежь россии", "myrosmol")
    )
    event_bound = any(
        marker in normalized
        for marker in ("на форум", "на мероприят", "на событ", "в форуме")
    )
    return (
        has_platform
        and not event_bound
        and any(marker in normalized for marker in ("регистрац", "зарегистр"))
    )


def _asks_platform_event_navigation(normalized: str) -> bool:
    has_platform = any(
        marker in normalized
        for marker in ("фгаис", "молодежь россии", "myrosmol")
    )
    return (
        has_platform
        and any(marker in normalized for marker in ("мероприят", "событ"))
        and any(
            marker in normalized
            for marker in ("где", "найти", "поиск", "доступн", "фильтр")
        )
    )


def _asks_account_data_recovery(normalized: str) -> bool:
    return (
        any(marker in normalized for marker in ("фгаис", "профил", "аккаунт"))
        and "почт" in normalized
        and any(marker in normalized for marker in ("потер", "нет доступ", "стар"))
        and any(marker in normalized for marker in ("данн", "профил", "аккаунт"))
    )


def _asks_inactive_platform_application_button(normalized: str) -> bool:
    return (
        any(marker in normalized for marker in ("фгаис", "myrosmol", "молодежь россии"))
        and "кнопк" in normalized
        and any(marker in normalized for marker in ("неактив", "не актив", "не работает"))
        and "заявк" in normalized
        and any(marker in normalized for marker in ("подать", "подач"))
    )


def _asks_grant_directions(normalized: str) -> bool:
    asks_directions = "грант" in normalized and any(
        marker in normalized for marker in ("направлен", "номинац", "тематик")
    )
    asks_application_details = "заявк" in normalized and any(
        marker in normalized
        for marker in (
            "заполн",
            "оформ",
            "подат",
            "подач",
            "шаблон",
            "скач",
        )
    )
    return asks_directions and not asks_application_details


def _asks_physical_grants_overview(normalized: str) -> bool:
    return (
        "грант" in normalized
        and any(marker in normalized for marker in ("физических лиц", "физлиц"))
        and any(marker in normalized for marker in ("что такое", "что такие", "общая информац"))
    )


def _asks_forum_discovery(normalized: str) -> bool:
    generic_forum_reference = any(
        marker in normalized for marker in ("форумы", "форумов", "форумам")
    )
    return generic_forum_reference and any(
        marker in normalized
        for marker in ("сейчас есть", "вообще сейчас", "какие есть", "доступн", "где найти")
    )


def _has_decline_participation_context(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "отказ",
            "отказаться",
            "отозвать",
            "отменить участие",
            "отмена заявки",
            "не могу поехать",
            "не смогу поехать",
            "не могу приехать",
            "не смогу приехать",
            "не могу посетить",
            "не смогу посетить",
            "подтвердил участие",
            "подтвердила участие",
        )
    )
