from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from src.graph.query_normalization import (
    ACCOUNT_DATA_RECOVERY,
    PHYSICAL_GRANTS_OVERVIEW,
    bounded_query_intent,
)
from src.graph.response_profiles import (
    asks_event_dates,
    has_explicit_application_action,
    has_explicit_event_timing,
    has_explicit_technical_failure,
    should_suppress_event_date_question,
)
from src.kb.forum_registry import (
    canonicalize_forum_name,
    detect_forums_from_text,
    forum_filter_values,
)
from src.models import QueryAnalysis, Question
from src.response_contract import ResponseProfileName

FORUM_CLAUSE_NON_WORD_RE = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)
SHARED_AGE_RANGE_RE = re.compile(
    r"(?<!\d)(?:от\s+)?\d{1,2}\s*(?:[-–—]|\s+до\s+)\s*"
    r"\d{1,2}\s*(?:лет|года?)\b",
    re.IGNORECASE,
)
SHARED_EXPLICIT_AGE_RE = re.compile(
    r"\bмне\s+(\d{1,2})\s*(?:лет|года?|год)?\b",
    re.IGNORECASE,
)
SHARED_SHIFT_RE = re.compile(
    r"\b(?:(?:перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят)\w*"
    r"\s+смен\w*|\d{1,2}\s*[-–—]?\s*(?:я|й|ю|е|ая|ый|ую|ой)\s+смен\w*"
    r"|\d{1,2}\s+смен\w*|смен\w*\s*(?:№|#|номер)?\s*\d{1,2})\b",
    re.IGNORECASE,
)
SHARED_NAMED_SECTION_RE = re.compile(
    r"\b(?:смен(?:а|ы|у|е|ой)|профил(?:ь|я|ю|е)|трек(?:а|у|е)?|"
    r"программ(?:а|ы|у|е))\s+"
    r"(?:[«\"]([^»\"]{2,80})[»\"]|"
    r"([а-яёa-z][а-яёa-z0-9 -]{1,50}?))"
    r"(?=\s+(?:на\s+форум\w*|форум\w*|в\s+рамках|для\b|среди\b|"
    r"пройд\w*|проход\w*|состо\w*|и\s+(?:кто|что|как|где|когда|"
    r"будет|есть|можно|нужн\w*|какие|оплач\w*))|[?.!,;]|$)",
    re.IGNORECASE | re.UNICODE,
)
SHARED_AUDIENCE_RE = re.compile(
    r"\bдля\s+((?:настав|школьн|студент|педагог|учител|волонт|"
    r"очн|заочн|подрост|взросл)\w*)\b",
    re.IGNORECASE,
)

EVENT_CONSTRAINT_CLAUSE_RE = re.compile(
    r"[;.!?]+|,\s*(?=(?:а|но)\s+)|\s+(?:а|но)\s+|"
    r"\s+и\s+(?=(?:кто|что|как|где|когда|"
    r"какие|какая|какой|есть|будет|можно|нужн|оплач|\d{1,2}\s*[-–—]?\s*"
    r"(?:я|й|ю|е|ая|ый|ую|ой)?\s*смен))",
    re.IGNORECASE | re.UNICODE,
)
QUERY_PROVEN_REQUEST_HEAD = (
    r"(?:кто|что|как(?:ой|ая|ое|ие|ую|ого|их)?|где|куда|когда|зачем|почему|"
    r"сколько|можно\s+ли|нужно\s+ли|надо\s+ли|будет\s+ли|есть\s+ли|"
    r"нуж(?:ен|на|но|ны)\s+ли|стоит\s+ли|"
    r"подскаж\w*|расскаж\w*|объясн\w*|назов\w*|покаж\w*|"
    r"уточн\w*|перечисл\w*|помог\w*|скажи(?:те)?)"
)
QUERY_PROVEN_CLAUSE_RE = re.compile(
    rf"[;!?]+|[,:]\s+|\.(?=\s+)|"
    rf"\s+(?:и|а|но)\s+(?=(?:(?:заодно|отдельно|потом|еще)\s+)*"
    rf"{QUERY_PROVEN_REQUEST_HEAD}\b)",
    re.IGNORECASE | re.UNICODE,
)
QUERY_PROVEN_COORDINATION_RE = re.compile(
    r"\s+(?:и|а|но)\s+",
    re.IGNORECASE | re.UNICODE,
)
QUERY_PROVEN_REQUEST_SIGNAL_RE = re.compile(
    rf"\b{QUERY_PROVEN_REQUEST_HEAD}\b",
    re.IGNORECASE | re.UNICODE,
)
QUERY_PROVEN_ACTION_SIGNAL_RE = re.compile(
    r"(?:\b[а-яё]{3,}(?:ть|ться|ти|чь)\b|"
    r"^(?:не\s+)?[а-яё]{3,}(?:йте|ите|ай|яй|уй|ей|и)\b)",
    re.IGNORECASE | re.UNICODE,
)
QUERY_PROVEN_SUBJECT_PREDICATE_RE = re.compile(
    r"\b(?:есть|будет|нужен|нужна|нужно|нужны|можно|надо|стоит)\b",
    re.IGNORECASE | re.UNICODE,
)
QUERY_PROVEN_ANAPHORIC_FRAGMENT_RE = re.compile(
    r"^(?:сколько|како\w*)\s+(?:их|там|это|они|он|она)\b",
    re.IGNORECASE | re.UNICODE,
)
QUERY_PROVEN_QUOTED_SCOPE_RE = re.compile(
    r"«[^»\r\n]{2,200}»|„[^“\r\n]{2,200}“|\"[^\"\r\n]{2,200}\"",
    re.UNICODE,
)

_NAMED_SECTION_PREFIX_STOP_RE = re.compile(
    r"^(?:для|среди|на|и|в|во|по|при|от|до)(?:\s|$)",
    re.IGNORECASE | re.UNICODE,
)
_NAMED_SECTION_ORDINAL_RE = re.compile(
    r"(?:(?:перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят)\w*"
    r"|\d{1,2}\s*[-–—]?\s*(?:я|й|ю|е|ая|ый|ую|ой))",
    re.IGNORECASE | re.UNICODE,
)
_NAMED_SECTION_STOP_TOKENS = frozenset(
    {
        "форум",
        "форума",
        "форуме",
        "мероприятие",
        "мероприятия",
        "мероприятии",
        "смена",
        "смены",
        "программа",
        "программы",
        "участники",
        "участников",
        "студенты",
        "студентов",
        "очники",
        "очников",
    }
)

FALLBACK_QUESTION_MARKERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "регистрац",
            "зарегистр",
            "подать заяв",
            "подать проект",
            "заявк",
            "поучаств",
            "участв",
            "акци",
            "хочу попасть на",
            "как попасть на форум",
            "как туда попасть",
            "что нужно сделать чтобы попасть",
            "что нужно сделать, чтобы попасть",
            "как стать участником",
            "как стать участницей",
            "хочу на форум",
            "хочу на мероприятие",
            "вписаться в движ",
            "залететь на форум",
            "залететь на программу",
            "присоединиться к форуму",
        ),
        "Как подать заявку или зарегистрироваться?",
    ),
    (("документ", "паспорт", "справк"), "Какие документы нужны?"),
    (("положен",), "Где найти положение мероприятия?"),
    (("трансфер", "автобус", "шаттл"), "Есть ли трансфер?"),
    (("питани", "еда", "корм"), "Есть ли питание?"),
    (("возраст", "лет", "14", "18", "35"), "Какие возрастные ограничения?"),
    (
        ("ребен", "ребён", "дети", "детьми", "ребёнком", "ребенком"),
        "Можно ли прийти с ребёнком или детьми?",
    ),
    (
        (
            "проезд",
            "оплат",
            "расход",
            "покрыва",
            "дорог",
            "оплачив",
            "стоимост",
            "компенс",
            "возмест",
            "до мероприятия",
            "до форума",
            "до места проведения",
            "билет на поезд",
            "билет на самол",
            "авиабилет",
            "жд билет",
            "ж/д билет",
            "поезд",
            "самол",
            "транспортн",
            "чартер",
            "доезд",
            "доехать",
            "добраться",
            "ехать",
            "поехать",
            "поездк",
            "возмещ",
        ),
        "Кто оплачивает проезд?",
    ),
    (
        (
            "проживан",
            "жиль",
            "жить",
            "где жить",
            "гостиниц",
            "отель",
            "отеле",
            "отеля",
        ),
        "Какие условия проживания?",
    ),
    (("ноутбук", "снаряж", "вещ", "одежд", "взять с собой"), "Что нужно взять с собой?"),
    (
        (
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
            "не получается поехать",
            "не получается приехать",
            "подтвердил участие",
            "подтвердила участие",
        ),
        "Как отказаться от участия или отозвать заявку?",
    ),
    (
        ("отклон", "причин отклон", "завернул", "не прошел отбор", "не прошёл отбор"),
        "Почему отклонили заявку?",
    ),
    (
        (
            "письмо-вызов",
            "письмо вызов",
            "письмо на регион",
            "письмо в регион",
            "письмо для региона",
            "приглашен",
        ),
        "Как получить письмо-вызов?",
    ),
    (
        (
            "заезд и выезд",
            "заезда и выезда",
            "время заезда",
            "время выезда",
            "когда заезд",
            "когда выезд",
        ),
        "Когда заезд и выезд?",
    ),
    (
        (
            "дата",
            "даты",
            "срок",
            "заезд",
            "выезд",
            "когда проходит",
            "когда проводится",
            "когда смена",
            "период проведения",
        ),
        "Какие даты и сроки?",
    ),
    (
        (
            "место проведения",
            "где и когда проходит",
            "где и когда будет проходить",
            "где проходит",
            "где пройдет",
            "где пройдёт",
            "где будет проходить",
            "где проводится",
            "адрес площадки",
            "локац",
        ),
        "Где проходит мероприятие?",
    ),
    (("сертификат",), "Будет ли сертификат?"),
    (("чат", "куратор"), "Как попасть в чат мероприятия?"),
    (("результат", "отбор", "одобрен", "статус", "рассмотр"), "Когда будут результаты отбора?"),
    (
        (
            "оператор",
            "контакт",
            "связаться",
            "поддержк",
            "служба заботы",
        ),
        "Как связаться с оператором или поддержкой?",
    ),
    (
        (
            "техническ",
            "ошиб",
            "не работает",
            "не груз",
            "не открывается",
            "не загружается",
            "не могу войти",
            "не получается войти",
            "не могу зайти",
            "авторизац",
            "баг",
            "не отправляется",
            "не сохраняется",
            "не загружается",
            "не прикрепляется",
            "повторно не помогло",
            "снова не помогло",
        ),
        "Что делать при технической ошибке или проблеме доступа?",
    ),
    (
        (
            "вернуть грантов",
            "возврат грантов",
            "вернуть средства",
            "возврат средств",
            "вернуть деньги",
            "возврат денег",
            "вернуть денеж",
            "возврат денеж",
        ),
        "Как вернуть грантовые средства?",
    ),
    (
        ("отчет", "отчетност", "отчёт", "отчётност"),
        "Как оформить отчётность по гранту?",
    ),
    (
        (
            "не удается реализ",
            "не удаётся реализ",
            "не могу реализ",
            "не получается реализ",
            "сорвал",
        ),
        "Как вернуть грантовые средства?",
    ),
    (("id не", "id проф", "айди", "ид проф"), "Где найти ID профиля?"),
    (
        (
            "что такое росмолод",
            "кто такие росмолод",
            "чем занимается росмолод",
        ),
        "Что такое Росмолодёжь?",
    ),
    (
        ("до свид", "пока", "прощ", "всего добр", "хорошего дня"),
        "Прощание",
    ),
    (
        ("рекоменд", "посовет", "подбери", "подойдет", "подойдёт"),
        "Какие мероприятия могут подойти?",
    ),
)
GRANT_FALLBACK_QUESTION_MARKERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "внести измен",
            "изменить проект",
            "изменить смет",
            "поменять смет",
            "скорректировать проект",
            "редактировать проект",
        ),
        "Можно ли внести изменения в проект?",
    ),
    (
        (
            "отчет",
            "отчетност",
            "отчёт",
            "отчётност",
            "расход",
            "смет",
            "договор",
            "акт",
            "наклад",
            "закуп",
            "контрольн",
            "точк",
        ),
        "Как оформить отчётность по гранту?",
    ),
)
ADDITIONAL_FALLBACK_QUESTION_MARKERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "изменить заявку",
            "изменить заявк",
            "внести изменения в заявк",
            "поменять заявк",
        ),
        "Можно ли внести изменения в заявку?",
    ),
    (
        ("медпункт", "медицин", "здоров"),
        "Есть ли медицинская помощь?",
    ),
    (
        ("госуслуг", "есиа"),
        "Можно ли привязать Госуслуги к профилю?",
    ),
    (
        (
            "овз",
            "ограниченными возможн",
            "инвалид",
        ),
        "Можно ли участвовать с ОВЗ?",
    ),
    (
        ("иностран", "иностранц"),
        "Могут ли участвовать иностранные граждане?",
    ),
    (
        ("грантовый конкурс", "гранты", "грантов"),
        "Есть ли грантовый конкурс?",
    ),
    (
        ("цифровая неделя",),
        "Что такое цифровая неделя?",
    ),
    (
        ("подтверждени", "подтверд"),
        "Что с подтверждением участия?",
    ),
    (
        (
            "где посмотреть результ",
            "результат",
            "списки",
            "отбор",
        ),
        "Где посмотреть результаты отбора?",
    ),
    (
        (
            "в чем суть",
            "суть форум",
            "о форуме",
            "тематик",
        ),
        "В чём суть форума?",
    ),
    (
        (
            "программ",
            "артист",
            "расписан",
        ),
        "Где посмотреть программу и артистов?",
    ),
)
MULTI_FORUM_QUESTION_MARKERS: tuple[tuple[tuple[str, ...], str], ...] = ()


@dataclass(frozen=True)
class _CanonicalTopicAspect:
    name: str
    topic: str
    marker_groups: tuple[tuple[str, ...], ...]
    coverage_markers: tuple[str, ...]
    allow_single: bool = False


@dataclass(frozen=True)
class QueryProvenSourceAspect:
    """One current-request aspect and its published-source constraints."""

    key: str
    question: Question
    response_profile: ResponseProfileName
    source_topics: tuple[str, ...] = ()
    source_topic_pattern: str | None = None
    marker_groups: tuple[tuple[str, ...], ...] = ()
    coverage_markers: tuple[str, ...] = ()
    allow_single: bool = False
    structured_source: bool = False


@dataclass(frozen=True)
class QueryProvenTopicPlan:
    """Current-request aspect plan shared by retrieval, rerank and generation.

    ``questions`` is populated only when every explicit request clause maps
    one-to-one to an unambiguous canonical aspect.  ``incomplete`` marks a
    partially recognized request that must stay on the normal grounded path;
    consumers must never use a partial deterministic plan.
    """

    questions: tuple[Question, ...] = ()
    source_aspects: tuple[QueryProvenSourceAspect, ...] = ()
    clauses: tuple[str, ...] = ()
    unmapped_clauses: tuple[str, ...] = ()
    incomplete: bool = False


QUERY_PROVEN_TOPIC_ASPECTS: dict[str, tuple[_CanonicalTopicAspect, ...]] = {
    "platform": (
        _CanonicalTopicAspect(
            "registration",
            "registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
            (("регистрац", "зарегистр"),),
            ("регистрац", "зарегистр"),
        ),
        _CanonicalTopicAspect(
            "navigation",
            "poisk_i_navigaciya_po_meropriyatiyam",
            (("найти меропр", "по регион", "отфильтр", "фильтр"),),
            ("найти меропр", "поиск меропр", "по регион", "отфильтр", "фильтр"),
        ),
        _CanonicalTopicAspect(
            "recovery",
            "obedinenie_akkauntov",
            (
                ("потер", "перенес", "перенос"),
                ("профил", "почт", "данн"),
            ),
            ("потер", "перенос", "перенест", "объедин", "старого проф"),
        ),
        _CanonicalTopicAspect(
            "status",
            "statusy_zayavok",
            (("статус",), ("что значит", "одобрен", "отклонен")),
            ("статус", "одобрен", "отклон", "результат", "отбор"),
        ),
    ),
    "territory": (
        _CanonicalTopicAspect(
            "overview",
            "o_meropriyatii",
            (("что за", "в двух слов", "общий период"),),
            (
                "что за",
                "о меропр",
                "о форум",
                "общий период",
                "когда она идет",
                "период форум",
            ),
        ),
        _CanonicalTopicAspect(
            "shifts",
            "tematicheskie_smeny_foruma",
            (("какие там смен", "какие смен", "тематическ"),),
            ("смен", "тематическ"),
        ),
        _CanonicalTopicAspect(
            "pravda_dates",
            "daty_26_30_iyulya_2026_goda",
            (("правд",), ("дат", "период", "смен")),
            ("правд", "дат", "срок", "когда"),
        ),
    ),
    "grants": (
        _CanonicalTopicAspect(
            "nominations",
            "nominacii_grantovyh_konkursov",
            (("номинац", "направлен"),),
            ("номинац", "направлен", "тематик"),
        ),
        _CanonicalTopicAspect(
            "season_application",
            "poshagovyy_algoritm",
            (
                ("первого сезон", "первый сезон", "1 сезон"),
                ("шаг",),
                ("подач",),
            ),
            ("шаг", "подач", "заявк", "алгоритм"),
        ),
        _CanonicalTopicAspect(
            "physical_application",
            "obschaya_informaciya",
            (
                ("грант",),
                ("заявк",),
                ("подат", "подач"),
                ("поучаств", "участ"),
            ),
            ("заявк", "подач", "поучаств", "участ"),
            allow_single=True,
        ),
    ),
    "dobro": (
        _CanonicalTopicAspect(
            "cabinet",
            "registraciya_s_pomoschyu_sozdaniya_kabineta",
            (("создать кабинет", "создать аккаунт"),),
            ("кабинет", "аккаунт", "регистрац"),
        ),
        _CanonicalTopicAspect(
            "volunteer",
            "volonterskaya_pomosch",
            (("волонт",), ("заявк", "отфильтр", "фильтр")),
            ("волонт", "заявк", "мероприят", "фильтр"),
        ),
    ),
}
QUERY_PROVEN_UNSUPPORTED_MARKERS = {
    "platform": ("удал", "парол", "оператор", "технич", "ошибк"),
    "territory": (
        "заявк",
        "участник",
        "прожив",
        "питан",
        "дорог",
        "проезд",
        "компенс",
        "трансфер",
        "документ",
    ),
    "grants": ("соглашен", "отчет", "бюджет", "эксперт", "проверк"),
    "dobro": ("дата", "даты", "срок", "проезд", "прожив", "питан"),
}

SHIFT_ORDINAL_STEMS = (
    ("перв", 1),
    ("втор", 2),
    ("трет", 3),
    ("четверт", 4),
    ("пят", 5),
    ("шест", 6),
    ("седьм", 7),
    ("восьм", 8),
    ("девят", 9),
    ("десят", 10),
)


def query_proven_clause_matches_source_aspect(
    clause: str,
    aspect: QueryProvenSourceAspect,
) -> bool:
    """Return whether one current-request clause proves ``aspect``."""

    normalized = _normalize_query_proven_text(clause)
    key = aspect.key
    if aspect.marker_groups and _canonical_aspect_covers_text(
        _CanonicalTopicAspect(
            key,
            str(aspect.question.topic or ""),
            aspect.marker_groups,
            aspect.coverage_markers,
            aspect.allow_single,
        ),
        normalized,
    ):
        return True
    if key == "account_data_recovery":
        return bounded_query_intent(clause) == ACCOUNT_DATA_RECOVERY
    if key == "physical_grants_overview":
        return bounded_query_intent(clause) == PHYSICAL_GRANTS_OVERVIEW
    if key == "application_deadline":
        return _asks_application_deadline(normalized)
    if key == "event_eligibility":
        return _asks_event_eligibility(normalized)
    if key == "food_and_accommodation_payment":
        return _asks_food_and_accommodation_payment(normalized)
    if key == "travel_compensation":
        return _asks_travel_compensation(normalized)
    if key == "post_registration_messenger_ticket":
        return _asks_post_registration_messenger_ticket(normalized)
    if key == "grant_agreement_review":
        return _asks_grant_agreement_review(normalized) or bool(
            "сколько" in normalized
            and re.search(
                r"\bпроект\w*\s+грантов\w*\s+соглашени\w*\b",
                normalized,
            )
        )
    if key == "final_grant_report_review":
        return _asks_final_grant_report_review(normalized) or bool(
            "сколько" in normalized
            and re.search(
                r"\bитогов(?:ый|ого|ому|ым)\s+отч[её]т(?:а|у|ом)?\b",
                normalized,
            )
        )
    shift_match = re.fullmatch(r"shift_(\d+)_dates", key)
    return bool(
        shift_match
        and int(shift_match.group(1))
        in (
            _requested_shift_date_ordinals(normalized)
            | _elliptical_shift_date_ordinals(normalized)
        )
    )


def query_proven_clause_matches_source_aspects(
    clause: str,
    aspects: Sequence[QueryProvenSourceAspect],
) -> bool:
    """Match one clause, including a collective reference to requested shifts."""

    if any(
        query_proven_clause_matches_source_aspect(clause, aspect)
        for aspect in aspects
    ):
        return True
    normalized = _normalize_query_proven_text(clause)
    shift_aspects = [
        aspect for aspect in aspects if aspect.key.startswith("shift_")
    ]
    return bool(
        len(shift_aspects) > 1
        and any(marker in normalized for marker in ("разъезд", "отъезд"))
        and any(marker in normalized for marker in ("кажд", "обе", "всех"))
    )


def source_aspect_matches_topic(
    aspect: QueryProvenSourceAspect,
    topic: str | None,
) -> bool:
    """Match a published topic against query-proven source constraints."""

    normalized_topic = str(topic or "").strip().casefold()
    if not normalized_topic:
        return False
    if aspect.source_topics and normalized_topic in aspect.source_topics:
        return True
    return bool(
        aspect.source_topic_pattern
        and re.fullmatch(aspect.source_topic_pattern, normalized_topic)
    )


def query_proven_shift_ordinals(
    question_text: str | None,
    question_topic: str | None = None,
) -> set[int]:
    """Extract explicitly requested shift ordinals for planning and binding."""

    normalized = _normalize_query_proven_text(question_text)
    ordinals: set[int] = set()
    for stem, ordinal in SHIFT_ORDINAL_STEMS:
        if re.search(rf"\b{stem}\w*\s+смен", normalized):
            ordinals.add(ordinal)
        if "смен" in normalized and re.search(
            rf"\b(?:начин|заканч|старт|финиш|разъезд|отъезд)\w*"
            rf"[^.!?]{{0,32}}\b{stem}\w*\b",
            normalized,
        ):
            ordinals.add(ordinal)
    ordinals.update(
        int(value)
        for value in re.findall(
            r"\b(\d+)\s*(?:[-–]\s*)?(?:я|й|ая|ой)?\s+смен",
            normalized,
        )
    )
    topic_match = re.match(r"^(\d+)_smena_", str(question_topic or ""))
    if not ordinals and topic_match:
        ordinals.add(int(topic_match.group(1)))
    return ordinals


def _structured_query_proven_aspects(
    analysis: QueryAnalysis,
    text: str,
) -> list[QueryProvenSourceAspect]:
    normalized = _normalize_query_proven_text(text)
    aspects: list[QueryProvenSourceAspect] = []
    forum_scope = _query_proven_forum_scope(analysis, text)

    if (
        bounded_query_intent(
            text,
            forum_normalized=analysis.forum_normalized,
        )
        == ACCOUNT_DATA_RECOVERY
    ):
        aspects.append(
            _source_aspect(
                key="account_data_recovery",
                question_text=(
                    "Как восстановить данные после потери доступа к почте профиля?"
                ),
                category="платформа_фгаис",
                response_profile=ResponseProfileName.TECHNICAL,
                topics=("obedinenie_akkauntov",),
            )
        )

    if (
        bounded_query_intent(
            text,
            forum_normalized=analysis.forum_normalized,
        )
        == PHYSICAL_GRANTS_OVERVIEW
    ):
        aspects.append(
            _source_aspect(
                key="physical_grants_overview",
                question_text="Каковы цель и участники конкурса?",
                category="гранты",
                response_profile=ResponseProfileName.GRANTS,
                forum="Гранты для физических лиц",
                topics=("obschaya_informaciya",),
            )
        )

    if forum_scope:
        if _asks_application_deadline(normalized):
            aspects.append(
                _source_aspect(
                    key="application_deadline",
                    question_text="До какого срока можно подать заявку?",
                    category="форумы",
                    response_profile=ResponseProfileName.APPLICATION,
                    forum=forum_scope,
                    topics=("registraciya", "forum"),
                )
            )
        if _asks_event_eligibility(normalized):
            aspects.append(
                _source_aspect(
                    key="event_eligibility",
                    question_text="Кто может участвовать?",
                    category="форумы",
                    response_profile=ResponseProfileName.ELIGIBILITY,
                    forum=forum_scope,
                    topics=("uchastniki",),
                )
            )
        if _asks_food_and_accommodation_payment(normalized):
            aspects.append(
                _source_aspect(
                    key="food_and_accommodation_payment",
                    question_text="Кто оплачивает проживание и питание?",
                    category="форумы",
                    response_profile=ResponseProfileName.ACCOMMODATION,
                    forum=forum_scope,
                    topics=("pitanie_i_prozhivanie",),
                )
            )
        if _asks_travel_compensation(normalized):
            aspects.append(
                _source_aspect(
                    key="travel_compensation",
                    question_text="Могут ли компенсировать проезд?",
                    category="форумы",
                    response_profile=ResponseProfileName.TRAVEL,
                    forum=forum_scope,
                    topics=("kompensaciya",),
                )
            )
        for ordinal in sorted(_requested_shift_date_ordinals(normalized)):
            aspects.append(
                _source_aspect(
                    key=f"shift_{ordinal}_dates",
                    question_text=(
                        f"Какие даты у {ordinal}-й смены и когда её разъезд?"
                    ),
                    category="форумы",
                    response_profile=ResponseProfileName.DATES,
                    forum=forum_scope,
                    topic_pattern=rf"{ordinal}_smena_\d+_\d+_avgusta",
                )
            )
        if _asks_post_registration_messenger_ticket(normalized):
            aspects.append(
                _source_aspect(
                    key="post_registration_messenger_ticket",
                    question_text="Как получить билет после регистрации через МАХ?",
                    category="форумы",
                    response_profile=ResponseProfileName.APPLICATION,
                    forum=forum_scope,
                    topic_pattern=(
                        r"sposob_1_cherez_chat_bot_v_mah_.+_youthday_bot"
                    ),
                )
            )

    if _asks_grant_agreement_review(normalized):
        aspects.append(
            _source_aspect(
                key="grant_agreement_review",
                question_text="Сколько проверяют проект грантового соглашения?",
                category="гранты",
                response_profile=ResponseProfileName.GRANTS,
                forum="Гранты для физических лиц",
                topics=("proverka_proekta_grantovogo_soglasheniya",),
            )
        )
    if _asks_final_grant_report_review(normalized):
        aspects.append(
            _source_aspect(
                key="final_grant_report_review",
                question_text="Сколько проверяют итоговый отчёт?",
                category="гранты",
                response_profile=ResponseProfileName.GRANTS,
                forum="Гранты для физических лиц",
                topics=("proverka_otcheta",),
            )
        )
    return aspects


def _source_aspect(
    *,
    key: str,
    question_text: str,
    category: str,
    response_profile: ResponseProfileName,
    forum: str | None = None,
    topics: tuple[str, ...] = (),
    topic_pattern: str | None = None,
) -> QueryProvenSourceAspect:
    return QueryProvenSourceAspect(
        key=key,
        question=Question(
            text=question_text,
            category=category,
            forum_normalized=forum,
            topic=topics[0] if topics else None,
        ),
        response_profile=response_profile,
        source_topics=topics,
        source_topic_pattern=topic_pattern,
        allow_single=True,
        structured_source=True,
    )


def _query_proven_forum_scope(
    analysis: QueryAnalysis,
    text: str,
) -> str | None:
    explicit = canonicalize_forum_name(analysis.forum_normalized)
    if explicit:
        return explicit
    detected = detect_forums_from_text(text)
    return detected[0] if len(detected) == 1 else None


def _asks_application_deadline(normalized: str) -> bool:
    return any(marker in normalized for marker in ("заяв", "подач", "регистрац")) and any(
        marker in normalized for marker in ("до какого", "крайн", "срок", "дедлайн")
    )


def _asks_event_eligibility(normalized: str) -> bool:
    return any(
        marker in normalized for marker in ("кто", "возраст", "участник", "претендент")
    ) and any(
        marker in normalized for marker in ("участв", "участник", "претенд", "подход")
    )


def _asks_food_and_accommodation_payment(normalized: str) -> bool:
    return bool(
        "прожив" in normalized
        and any(marker in normalized for marker in ("питан", "ед"))
        and any(
            marker in normalized
            for marker in ("оплач", "платит", "за счет", "за счёт")
        )
    )


def _asks_travel_compensation(normalized: str) -> bool:
    return "компенс" in normalized and any(
        marker in normalized for marker in ("проезд", "дорог", "билет")
    )


def _requested_shift_date_ordinals(normalized: str) -> set[int]:
    if "смен" not in normalized or not any(
        marker in normalized
        for marker in ("дат", "период", "когда", "календар", "разъезд", "отъезд")
    ):
        return set()
    ordinals = query_proven_shift_ordinals(normalized)
    for stem, ordinal in SHIFT_ORDINAL_STEMS:
        if re.search(rf"\b{stem}\w*\b[^.!?]{{0,32}}\bсмен", normalized):
            ordinals.add(ordinal)
    return ordinals


def _elliptical_shift_date_ordinals(normalized: str) -> set[int]:
    """Resolve `когда заканчивается вторая` only inside a proven shift plan."""

    if not any(
        marker in normalized
        for marker in ("когда", "начин", "заканч", "старт", "финиш", "разъезд", "отъезд")
    ):
        return set()
    return {
        ordinal
        for stem, ordinal in SHIFT_ORDINAL_STEMS
        if re.search(rf"\b{stem}\w*\b", normalized)
    }


def _asks_post_registration_messenger_ticket(normalized: str) -> bool:
    return bool(
        "билет" in normalized
        and "после регистрац" in normalized
        and re.search(r"\b(?:мах|max)\b", normalized)
    )


def _asks_grant_agreement_review(normalized: str) -> bool:
    return bool(
        "проект грантового соглашения" in normalized
        and "провер" in normalized
        and any(marker in normalized for marker in ("сколько", "срок", "кто"))
    )


def _asks_final_grant_report_review(normalized: str) -> bool:
    return bool(
        re.search(r"\bитогов\w*\s+отчет\w*\b", normalized)
        and "провер" in normalized
        and any(marker in normalized for marker in ("сколько", "срок"))
    )


def _normalize_query_proven_text(value: str | None) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def build_effective_questions(analysis: QueryAnalysis, message: str | None) -> list[Question]:
    detected_forums = _detected_forums(analysis)
    if detected_forums:
        base_questions = _base_questions(
            analysis,
            message,
            extra_fallback_markers=MULTI_FORUM_QUESTION_MARKERS,
        )
        return _expand_questions_for_forums(
            base_questions,
            detected_forums,
            default_category=analysis.category,
            message=message,
        )

    return _preserve_shared_event_constraints(
        _base_questions(analysis, message),
        analysis,
        message,
    )


def build_query_proven_topic_plan(
    analysis: QueryAnalysis | None,
    message: str | None,
) -> QueryProvenTopicPlan:
    """Build one complete current-request plan for every RAG core stage."""

    text = str(message or "").strip()
    normalized = _normalize_query_proven_text(text)
    if analysis is None or not normalized:
        return QueryProvenTopicPlan()

    structured_aspects = _structured_query_proven_aspects(analysis, text)
    scope = _query_proven_scope(analysis, normalized)
    canonical_aspects = (
        [
            aspect
            for aspect in QUERY_PROVEN_TOPIC_ASPECTS[scope]
            if _canonical_aspect_matches(normalized, aspect.marker_groups)
        ]
        if scope is not None
        else []
    )
    effective = build_effective_questions(analysis, text)
    source_aspects = _merge_query_proven_source_aspects(
        structured_aspects,
        _canonical_query_proven_source_aspects(
            analysis,
            canonical_aspects,
            effective,
            text,
        ),
    )
    if not source_aspects:
        return QueryProvenTopicPlan()

    def aspect_matcher(clause: str) -> bool:
        return query_proven_clause_matches_source_aspects(clause, source_aspects)
    clauses = tuple(
        split_explicit_request_clauses(
            analysis,
            normalized,
            aspect_matcher=aspect_matcher,
        )
    )
    unmapped_clauses = tuple(
        unmapped_explicit_request_clauses(
            analysis,
            normalized,
            aspect_matcher=aspect_matcher,
            questions=effective,
        )
    )
    canonical_unsupported = bool(
        scope is not None
        and canonical_aspects
        and any(
            marker in normalized
            for marker in QUERY_PROVEN_UNSUPPORTED_MARKERS[scope]
        )
    )
    incomplete = bool(
        canonical_unsupported
        or unmapped_clauses
        or _has_unsupported_structured_source_aspect(normalized, source_aspects)
        or _structured_shift_plan_drops_age_scope(
            normalized,
            effective,
            source_aspects,
        )
        or not _source_aspect_clauses_fully_covered(clauses, source_aspects)
        or (
            canonical_aspects
            and not structured_aspects
            and not _effective_questions_fully_covered(
                effective,
                canonical_aspects,
            )
        )
    )
    if incomplete:
        return QueryProvenTopicPlan(
            source_aspects=tuple(source_aspects),
            clauses=clauses,
            unmapped_clauses=unmapped_clauses,
            incomplete=True,
        )

    if len(canonical_aspects) == 1 and canonical_aspects[0].allow_single:
        forum = str(analysis.forum_normalized or "").strip()
        if forum and not _is_general_grant_scope(forum):
            return QueryProvenTopicPlan(
                source_aspects=tuple(source_aspects),
                clauses=clauses,
            )
    actionable = bool(
        structured_aspects
        or len(canonical_aspects) > 1
        or any(aspect.allow_single for aspect in canonical_aspects)
    )
    if not actionable:
        return QueryProvenTopicPlan(
            source_aspects=tuple(source_aspects),
            clauses=clauses,
        )
    return QueryProvenTopicPlan(
        questions=tuple(aspect.question for aspect in source_aspects),
        source_aspects=tuple(source_aspects),
        clauses=clauses,
    )


def _canonical_query_proven_source_aspects(
    analysis: QueryAnalysis,
    aspects: list[_CanonicalTopicAspect],
    effective: list[Question],
    fallback: str,
) -> list[QueryProvenSourceAspect]:
    return [
        QueryProvenSourceAspect(
            key=aspect.name,
            question=Question(
                text=_canonical_aspect_question_text(aspect, effective, fallback),
                topic=aspect.topic,
                category=analysis.category,
                forum_normalized=analysis.forum_normalized,
            ),
            response_profile=analysis.response_profile,
            source_topics=(aspect.topic,),
            marker_groups=aspect.marker_groups,
            coverage_markers=aspect.coverage_markers,
            allow_single=aspect.allow_single,
        )
        for aspect in aspects
    ]


def _merge_query_proven_source_aspects(
    preferred: list[QueryProvenSourceAspect],
    fallback: list[QueryProvenSourceAspect],
) -> list[QueryProvenSourceAspect]:
    merged: list[QueryProvenSourceAspect] = []
    seen: set[tuple[str, str, tuple[str, ...], str]] = set()
    for aspect in [*preferred, *fallback]:
        question = aspect.question
        key = (
            str(question.category or "").strip(),
            str(question.forum_normalized or "").strip(),
            aspect.source_topics,
            str(aspect.source_topic_pattern or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(aspect)
    return merged


def _source_aspect_clauses_fully_covered(
    clauses: Sequence[str],
    aspects: Sequence[QueryProvenSourceAspect],
) -> bool:
    if not clauses or not aspects:
        return False
    covered: set[str] = set()
    for clause in clauses:
        matches = [
            aspect
            for aspect in aspects
            if query_proven_clause_matches_source_aspect(clause, aspect)
        ]
        normalized = _normalize_query_proven_text(clause)
        collective_shift_clause = bool(
            not matches
            and len([aspect for aspect in aspects if aspect.key.startswith("shift_")])
            > 1
            and any(marker in normalized for marker in ("разъезд", "отъезд"))
            and any(marker in normalized for marker in ("кажд", "обе", "всех"))
        )
        if collective_shift_clause:
            covered.update(
                aspect.key for aspect in aspects if aspect.key.startswith("shift_")
            )
            continue
        if len(matches) > 1 and all(
            aspect.key.startswith("shift_") for aspect in matches
        ):
            covered.update(aspect.key for aspect in matches)
            continue
        if len(matches) != 1:
            return False
        covered.add(matches[0].key)
    return covered == {aspect.key for aspect in aspects}


def _has_unsupported_structured_source_aspect(
    normalized: str,
    aspects: Sequence[QueryProvenSourceAspect],
) -> bool:
    keys = {aspect.key for aspect in aspects}
    if not any(aspect.structured_source for aspect in aspects):
        return False
    if "программ" in normalized:
        return True
    if any(
        marker in normalized
        for marker in (
            "документ",
            "какие вещи",
            "список вещей",
            "что взять",
            "взять с собой",
            "трансфер",
            "шаттл",
            "автобус",
            "медпункт",
            "медицин",
            "здоров",
            "ребен",
            "ребён",
            "детьми",
            "парков",
            "место проведения",
            "адрес",
        )
    ):
        return True
    if any(
        marker in normalized
        for marker in (
            "изменить заявку",
            "внести изменения в заявк",
            "поменять заявк",
            "где смотреть статус",
            "где посмотреть статус",
            "где отслеживать статус",
        )
    ):
        return True
    if any(marker in normalized for marker in ("результат", "списки", "отбор")):
        return True
    if any(marker in normalized for marker in ("регистрац", "зарегистр")) and not keys.intersection(
        {
            "account_data_recovery",
            "application_deadline",
            "post_registration_messenger_ticket",
            "registration",
        }
    ):
        return True
    if (
        "условия проживан" in normalized
        or (
            "проживан" in normalized
            and not any(marker in normalized for marker in ("оплат", "стоимост"))
        )
    ) and "food_and_accommodation_payment" not in keys:
        return True
    if (
        any(marker in normalized for marker in ("проезд", "дорог", "билет"))
        and any(
            marker in normalized
            for marker in ("оплач", "платит", "за счет", "за счёт", "компенс")
        )
        and "travel_compensation" not in keys
    ):
        return True
    event_date_request = asks_event_dates(normalized) or bool(
        ("дата" in normalized or "даты" in normalized) and "срок" in normalized
    )
    if event_date_request and not (
        "application_deadline" in keys
        or any(key.startswith("shift_") for key in keys)
    ):
        return True
    return False


def _structured_shift_plan_drops_age_scope(
    normalized_request: str,
    questions: Sequence[Question],
    aspects: Sequence[QueryProvenSourceAspect],
) -> bool:
    """Keep audience-conditioned calendars on the generic grounded path."""

    if not any(aspect.key.startswith("shift_") for aspect in aspects):
        return False
    texts = [normalized_request, *(str(question.text or "") for question in questions)]
    return any(
        "возрастн" in _normalize_query_proven_text(text)
        or SHARED_AGE_RANGE_RE.search(text) is not None
        or SHARED_EXPLICIT_AGE_RE.search(text) is not None
        for text in texts
    )


def _effective_questions_fully_covered(
    questions: list[Question],
    aspects: list[_CanonicalTopicAspect],
) -> bool:
    """Reject deterministic plans that would silently drop a known extra aspect."""

    for question in questions:
        normalized = " ".join(question.text.casefold().replace("ё", "е").split())
        if not normalized:
            continue
        if any(
            _canonical_aspect_matches(normalized, aspect.marker_groups)
            or any(marker in normalized for marker in aspect.coverage_markers)
            for aspect in aspects
        ):
            continue
        return False
    return True


def _request_clauses_fully_covered(
    clauses: Sequence[str],
    aspects: list[_CanonicalTopicAspect],
) -> bool:
    """Require every standalone current-request clause to prove one aspect."""

    if not clauses:
        return False

    covered_aspects: set[str] = set()
    for clause in clauses:
        matching_aspects = [
            aspect
            for aspect in aspects
            if _canonical_aspect_covers_text(aspect, clause)
        ]
        if len(matching_aspects) != 1:
            return False
        covered_aspects.add(matching_aspects[0].name)
    return covered_aspects == {aspect.name for aspect in aspects}


def split_explicit_request_clauses(
    analysis: QueryAnalysis,
    message: str | None,
    *,
    aspect_matcher: Callable[[str], bool] | None = None,
) -> list[str]:
    """Split only the current user request into independently asked clauses.

    Retrieval and generation share this parser. ``aspect_matcher`` may mark a
    coordinated fragment as a known standalone aspect, but it cannot make an
    otherwise unknown clause disappear: every non-context fragment is returned
    to the caller for an explicit one-to-one completeness check.
    """

    normalized_message = " ".join(
        str(message or "").casefold().replace("ё", "е").split()
    )
    if not normalized_message:
        return []
    protected_message, protected_scopes = _protect_request_clause_scopes(
        normalized_message,
        analysis=analysis,
    )
    matches_aspect = aspect_matcher or (lambda _clause: False)
    leading_context = _query_proven_leading_context(
        normalized_message,
        analysis=analysis,
    )
    primary = [
        _strip_clause_lead(clause)
        for clause in QUERY_PROVEN_CLAUSE_RE.split(protected_message)
        if _strip_clause_lead(clause)
    ]
    primary = _attach_conditional_request_fragments(primary)
    primary = _attach_request_context_to_following_clause(
        primary,
        analysis=analysis,
    )
    primary = _attach_bare_request_heads(primary)
    clauses: list[str] = []
    for clause in primary:
        candidates = _query_proven_coordination_clauses(
            clause,
            aspect_matcher=matches_aspect,
        )
        for candidate in candidates:
            candidate = _restore_request_clause_scopes(
                candidate,
                protected_scopes,
            )
            if matches_aspect(candidate):
                clauses.append(candidate)
                continue
            if QUERY_PROVEN_ANAPHORIC_FRAGMENT_RE.match(candidate):
                continue
            if candidate == leading_context or _is_safe_non_request_fragment(candidate):
                continue
            clauses.append(candidate)
    return clauses


def _protect_request_clause_scopes(
    message: str,
    *,
    analysis: QueryAnalysis,
) -> tuple[str, dict[str, str]]:
    """Keep entity names and quoted values atomic during clause splitting."""

    protected = message
    values = {match.group(0) for match in QUERY_PROVEN_QUOTED_SCOPE_RE.finditer(message)}
    forum = str(analysis.forum_normalized or analysis.forum or "").strip()
    if forum:
        values.update(forum_filter_values(forum))
        values.add(forum)
    normalized_values = {
        " ".join(str(value).casefold().replace("ё", "е").split())
        for value in values
        if len(str(value).strip()) >= 2
    }
    restore: dict[str, str] = {}
    for index, value in enumerate(
        sorted(normalized_values, key=lambda item: (-len(item), item))
    ):
        if value not in protected:
            continue
        placeholder = f"\ue000{index}\ue001"
        protected = protected.replace(value, placeholder)
        restore[placeholder] = value
    return protected, restore


def _restore_request_clause_scopes(
    clause: str,
    restore: dict[str, str],
) -> str:
    restored = clause
    for placeholder, value in restore.items():
        restored = restored.replace(placeholder, value)
    return restored


def unmapped_explicit_request_clauses(
    analysis: QueryAnalysis,
    message: str | None,
    *,
    aspect_matcher: Callable[[str], bool],
    questions: Sequence[Question] = (),
    source_matcher: Callable[[str], bool] | None = None,
) -> list[str]:
    """Return compound-request clauses not proven by an aspect or source.

    This is the single completeness boundary shared by retrieval and
    generation. A deterministic fast path must not run when any independently
    requested clause is missing. For a simple one-clause request, the normal
    topic/source response contract remains authoritative.
    """

    clauses = split_explicit_request_clauses(
        analysis,
        message,
        aspect_matcher=aspect_matcher,
    )
    if len(clauses) < 2:
        return []
    question_covered = _clauses_have_distinct_question_coverage(
        clauses,
        questions,
    )
    return [
        clause
        for index, clause in enumerate(clauses)
        if not aspect_matcher(clause)
        and index not in question_covered
        and not (source_matcher is not None and source_matcher(clause))
    ]


def _clauses_have_distinct_question_coverage(
    clauses: list[str],
    questions: Sequence[Question],
) -> set[int]:
    if len(questions) < len(clauses):
        return set()
    ignored = {
        "а", "будет", "будут", "где", "да", "до", "и", "как", "какие",
        "какой", "когда", "кто", "ли", "либо", "можно", "на", "по",
        "сколько", "что",
    }

    def semantic_tokens(text: str) -> set[str]:
        return {
            token[:6] if len(token) > 6 else token
            for token in re.findall(r"[0-9a-zа-яё]+", text.casefold())
            if token not in ignored
        }

    question_tokens = [semantic_tokens(str(question.text or "")) for question in questions]
    candidates = [
        [
            index
            for index, tokens in enumerate(question_tokens)
            if tokens and semantic_tokens(clause) & tokens
        ]
        for clause in clauses
    ]
    if any(not indexes for indexes in candidates):
        return set()
    ordered = sorted(range(len(clauses)), key=lambda index: len(candidates[index]))

    def assign(position: int, used: set[int]) -> bool:
        if position == len(ordered):
            return True
        clause_index = ordered[position]
        for question_index in candidates[clause_index]:
            if question_index in used:
                continue
            used.add(question_index)
            if assign(position + 1, used):
                return True
            used.remove(question_index)
        return False

    return set(range(len(clauses))) if assign(0, set()) else set()


def _attach_request_context_to_following_clause(
    clauses: list[str],
    *,
    analysis: QueryAnalysis,
) -> list[str]:
    """Keep a symptom or user goal with the question that gives it meaning."""

    attached: list[str] = []
    pending_context: list[str] = []
    for index, clause in enumerate(clauses):
        has_later_clause = index + 1 < len(clauses)
        if has_later_clause and _is_context_for_following_request(
            clause,
            following_clause=clauses[index + 1],
            analysis=analysis,
        ):
            pending_context.append(clause)
            continue
        if pending_context:
            clause = ". ".join([*pending_context, clause])
            pending_context.clear()
        attached.append(clause)
    attached.extend(pending_context)
    return attached


def _attach_conditional_request_fragments(clauses: list[str]) -> list[str]:
    """Keep a request head with its conditional premise and consequence."""

    attached: list[str] = []
    for clause in clauses:
        normalized = _normalize_query_proven_text(clause)
        conditional = re.match(r"^(?:если|пока)\b", normalized) is not None
        if (
            attached
            and re.match(r"^когда\b", normalized)
            and re.search(
                r"\b(?:что\s+делать|как\s+быть|как\s+поступить)\b",
                _normalize_query_proven_text(attached[-1]),
            )
        ):
            conditional = True
        consequence = bool(
            attached
            and conditional is False
            and re.match(r"^(?:теперь|тогда)\b", normalized)
        )
        if attached and (conditional or consequence):
            attached[-1] = f"{attached[-1]}, {clause}"
            continue
        attached.append(clause)
    return attached


def _attach_bare_request_heads(clauses: list[str]) -> list[str]:
    attached: list[str] = []
    index = 0
    while index < len(clauses):
        clause = clauses[index]
        if _is_bare_request_head(clause) and index + 1 < len(clauses):
            attached.append(f"{clause} и {clauses[index + 1]}")
            index += 2
            continue
        attached.append(clause)
        index += 1
    return attached


def _is_context_for_following_request(
    fragment: str,
    *,
    following_clause: str,
    analysis: QueryAnalysis,
) -> bool:
    if (
        analysis.category == "техподдержка"
        and re.search(
            r"\b(?:не\s+(?:груз\w*|работ\w*|откры\w*|запуска\w*|получа\w*)|"
            r"ошибк\w*|проблем\w*)\b",
            fragment,
            flags=re.IGNORECASE | re.UNICODE,
        )
        and re.search(
            r"^(?:что\s+(?:мне\s+)?делать|как\s+(?:быть|поступить|это\s+исправить))\b",
            following_clause,
            flags=re.IGNORECASE | re.UNICODE,
        )
    ):
        return True
    if QUERY_PROVEN_REQUEST_SIGNAL_RE.search(fragment):
        return False
    if QUERY_PROVEN_SUBJECT_PREDICATE_RE.search(fragment):
        return False
    if re.match(
        r"^(?:я\b|у\s+меня\b|мне\b|мы\b|хочу\b|пытаюсь\b|планирую\b|"
        r"нужно\b|не\s+могу\b|не\s+получается\b)",
        fragment,
        flags=re.IGNORECASE | re.UNICODE,
    ):
        return True
    return not _fragment_has_request_predicate(fragment)


def _query_proven_coordination_clauses(
    clause: str,
    *,
    aspect_matcher: Callable[[str], bool],
) -> list[str]:
    parts = [
        _strip_clause_lead(part)
        for part in QUERY_PROVEN_COORDINATION_RE.split(clause)
        if _strip_clause_lead(part)
    ]
    if not parts:
        return []

    grouped = [parts[0]]
    for part in parts[1:]:
        if _is_incomplete_coordinated_temporal_head(grouped[-1], part):
            grouped[-1] = f"{grouped[-1]} и {part}"
            continue
        if _is_standalone_coordination_fragment(
            part,
            aspect_matcher=aspect_matcher,
        ):
            grouped.append(part)
        else:
            grouped[-1] = f"{grouped[-1]} и {part}"
    index = 0
    while index + 1 < len(grouped):
        if _is_bare_request_head(grouped[index]):
            grouped[index + 1] = f"{grouped[index]} и {grouped[index + 1]}"
            del grouped[index]
            continue
        index += 1
    return grouped


def _is_incomplete_coordinated_temporal_head(
    previous: str,
    current: str,
) -> bool:
    """Keep `когда старт и финиш у первой смены` as one request."""

    normalized_previous = _normalize_query_proven_text(previous)
    normalized_current = _normalize_query_proven_text(current)
    return bool(
        re.search(r"\bкогда\s+(?:старт|начал\w*)\s*$", normalized_previous)
        and re.match(r"^(?:финиш|конец|оконч\w*)\b", normalized_current)
        and query_proven_shift_ordinals(normalized_current)
    )


def _is_bare_request_head(fragment: str) -> bool:
    return re.fullmatch(
        r"(?:кто|что|где|куда|когда|зачем|почему|как|сколько|какой|какая|какие)",
        fragment.strip(),
        flags=re.IGNORECASE | re.UNICODE,
    ) is not None


def _is_standalone_coordination_fragment(
    fragment: str,
    *,
    aspect_matcher: Callable[[str], bool],
) -> bool:
    return bool(
        aspect_matcher(fragment)
        or QUERY_PROVEN_REQUEST_SIGNAL_RE.search(fragment)
        or QUERY_PROVEN_ACTION_SIGNAL_RE.search(fragment)
        or QUERY_PROVEN_SUBJECT_PREDICATE_RE.search(fragment)
    )


def _query_proven_leading_context(
    normalized_message: str,
    *,
    analysis: QueryAnalysis,
) -> str:
    match = re.match(r"^(?P<prefix>[^:;!?]{1,160}):\s+", normalized_message)
    if match is None:
        return ""
    prefix = _strip_clause_lead(match.group("prefix"))
    if not prefix:
        return ""
    suffix = normalized_message[match.end():]
    if "?" not in prefix and _fragment_has_request_predicate(suffix):
        return prefix
    if _fragment_has_request_predicate(prefix):
        return ""
    if re.match(
        r"^(?:без|для|по|на|в|во|у|при|от|до|с|со|об|о|про)\b",
        prefix,
        flags=re.IGNORECASE | re.UNICODE,
    ):
        return prefix
    return prefix if _is_scope_entity_fragment(prefix, analysis) else ""


def _fragment_has_request_predicate(fragment: str) -> bool:
    return bool(
        QUERY_PROVEN_REQUEST_SIGNAL_RE.search(fragment)
        or QUERY_PROVEN_ACTION_SIGNAL_RE.search(fragment)
        or QUERY_PROVEN_SUBJECT_PREDICATE_RE.search(fragment)
    )


def _is_scope_entity_fragment(fragment: str, analysis: QueryAnalysis) -> bool:
    normalized = _normalize_for_forum_clause(fragment)
    if not normalized or len(normalized.split()) > 6:
        return False
    category = str(analysis.category or "").strip()
    if category == "платформа_фгаис":
        return any(marker in normalized for marker in ("фгаис", "myrosmol"))
    if category == "гранты":
        return "грант" in normalized
    forum = str(analysis.forum_normalized or "").strip()
    if not forum:
        return False
    aliases = [
        _normalize_for_forum_clause(alias)
        for alias in forum_filter_values(forum)
    ]
    return any(alias and alias in normalized for alias in aliases)


def _is_safe_non_request_fragment(fragment: str) -> bool:
    tokens = set(re.findall(r"[0-9a-zа-яё]+", fragment, flags=re.IGNORECASE))
    return bool(tokens) and tokens <= {
        "пожалуйста",
        "коротко",
        "кратко",
        "отдельно",
        "сначала",
        "потом",
        "заодно",
    }


def _aspects_covering_text(
    aspects: list[_CanonicalTopicAspect],
    normalized_text: str,
) -> list[_CanonicalTopicAspect]:
    return [
        aspect
        for aspect in aspects
        if _canonical_aspect_covers_text(aspect, normalized_text)
    ]


def _canonical_aspect_covers_text(
    aspect: _CanonicalTopicAspect,
    normalized_text: str,
) -> bool:
    return _canonical_aspect_matches(normalized_text, aspect.marker_groups) or any(
        marker in normalized_text for marker in aspect.coverage_markers
    )


def _strip_clause_lead(value: str) -> str:
    clause = value.strip(" \t\r\n.,;:!?—–-")
    return re.sub(
        r"^(?:(?:и|а|но|заодно|отдельно|потом|еще)\s+)+",
        "",
        clause,
        flags=re.IGNORECASE | re.UNICODE,
    ).strip()


def _query_proven_scope(analysis: QueryAnalysis, normalized: str) -> str | None:
    category = str(analysis.category or "").strip()
    forum = str(analysis.forum_normalized or "").strip()
    normalized_forum = " ".join(forum.casefold().replace("ё", "е").split())
    if category == "платформа_фгаис" and not forum and any(
        marker in normalized
        for marker in ("фгаис", "myrosmol", "молодежь россии")
    ):
        return "platform"
    if (
        category == "форумы"
        and normalized_forum == "территория смыслов"
        and "территор" in normalized
    ):
        return "territory"
    if (
        category == "форумы"
        and normalized_forum == "добро.рф"
        and "добро.рф" in normalized
    ):
        return "dobro"
    if category == "гранты" and (
        "грант" in normalized
        or (
            "номинац" in normalized
            and any(marker in normalized for marker in ("сезон", "конкурс"))
        )
    ):
        return "grants"
    return None


def _canonical_aspect_matches(
    normalized: str,
    marker_groups: tuple[tuple[str, ...], ...],
) -> bool:
    return all(
        any(marker in normalized for marker in alternatives)
        for alternatives in marker_groups
    )


def _canonical_aspect_question_text(
    aspect: _CanonicalTopicAspect,
    questions: list[Question],
    fallback: str,
) -> str:
    for question in questions:
        normalized = question.text.casefold().replace("ё", "е")
        if _canonical_aspect_matches(normalized, aspect.marker_groups):
            return question.text
    return fallback


def _is_general_grant_scope(value: str) -> bool:
    normalized = value.casefold().replace("ё", "е")
    return "грант" in normalized and "физичес" in normalized


def _preserve_shared_event_constraints(
    questions: list[Question],
    analysis: QueryAnalysis,
    message: str | None,
) -> list[Question]:
    """Keep qualifiers that deterministic aspect decomposition would otherwise lose."""

    text = str(message or "").strip()
    if not questions or not text:
        return questions
    constraint_clauses = _event_constraint_clauses(
        text,
        analysis.forum_normalized,
    )
    if not constraint_clauses:
        return questions

    # More than one age or section in the same clause has no deterministic
    # pairing.  Keep the original questions so retrieval cannot receive a
    # fabricated all-to-all combination.
    if any(ambiguous for _clause, _constraints, ambiguous in constraint_clauses):
        return questions

    scoped: list[Question] = []
    for question in questions:
        if not _question_accepts_event_constraints(question):
            scoped.append(question)
            continue

        direct = [
            constraints
            for clause, constraints, _ambiguous in constraint_clauses
            if _clause_matches_question_aspect(clause, question)
        ]
        direct = _unique_constraint_groups(direct)
        is_application = _question_is_application(question)

        if direct:
            selected_groups = direct
            if len(constraint_clauses) > 1:
                orphan_groups = [
                    constraints
                    for clause, constraints, _ambiguous in constraint_clauses
                    if _has_section_constraint(constraints)
                    and not any(
                        _clause_matches_question_aspect(clause, candidate)
                        for candidate in questions
                    )
                ]
                selected_groups = _unique_constraint_groups(
                    [*selected_groups, *orphan_groups]
                )
        elif is_application:
            # A shift/age from a neighbouring aspect must not silently narrow
            # an application question.
            selected_groups = []
        elif len(constraint_clauses) == 1:
            selected_groups = [constraint_clauses[0][1]]
        else:
            # Explicitly paired shift/age branches are safe to expand.  An
            # aspect mentioned outside those clauses applies to every branch;
            # constraints inside different aspect clauses stay local above.
            selected_groups = _unique_constraint_groups(
                [
                    constraints
                    for _clause, constraints, _ambiguous in constraint_clauses
                    if _has_section_constraint(constraints)
                ]
            )

        if not selected_groups:
            scoped.append(question)
            continue

        appended = False
        for constraints in selected_groups:
            if _question_already_expresses_constraint(question, constraints):
                if not appended:
                    scoped.append(question)
                    appended = True
                continue
            if _question_is_age_only(question) and not _has_section_constraint(constraints):
                if not appended:
                    scoped.append(question)
                    appended = True
                continue
            scoped.append(_question_with_constraints(question, constraints))
            appended = True
    return scoped


def named_section_entities(
    message: str,
    forum_normalized: str | None = None,
) -> list[str]:
    """Extract meaningful named shifts/tracks without audience/preposition noise."""

    normalized_forum = str(forum_normalized or "").casefold().replace("ё", "е")
    entities: list[str] = []
    seen: set[str] = set()

    for match in SHARED_NAMED_SECTION_RE.finditer(str(message or "")):
        raw_entity = next((group for group in match.groups() if group), "")
        entity = " ".join(raw_entity.strip(" \t\r\n.,;!?–—-").split())
        normalized_entity = entity.casefold().replace("ё", "е")
        if not entity or normalized_entity in seen:
            continue
        if normalized_entity.startswith(("форум", "мероприят")):
            continue
        if _NAMED_SECTION_PREFIX_STOP_RE.match(normalized_entity):
            continue
        if _NAMED_SECTION_ORDINAL_RE.fullmatch(normalized_entity):
            continue
        entity_tokens = set(re.findall(r"[0-9a-zа-я]+", normalized_entity))
        if entity_tokens and entity_tokens <= _NAMED_SECTION_STOP_TOKENS:
            continue
        if normalized_forum and (
            normalized_entity in normalized_forum
            or normalized_forum in normalized_entity
        ):
            continue
        entities.append(entity)
        seen.add(normalized_entity)
    return entities


def _shared_event_constraints(
    message: str,
    forum_normalized: str | None,
) -> list[str]:
    constraints: list[str] = []

    def append(value: str) -> None:
        cleaned = " ".join(value.strip(" \t\r\n.,;!?–—-").split())
        normalized = cleaned.casefold().replace("ё", "е")
        if not cleaned or normalized in {
            item.casefold().replace("ё", "е") for item in constraints
        }:
            return
        constraints.append(cleaned)

    for match in SHARED_SHIFT_RE.finditer(message):
        append(match.group(0))
    for entity in named_section_entities(message, forum_normalized):
        append(f"смена «{entity}»")
    for match in SHARED_AGE_RANGE_RE.finditer(message):
        append(match.group(0))
    for match in SHARED_EXPLICIT_AGE_RE.finditer(message):
        append(f"возраст {int(match.group(1))} лет")
    normalized_message = message.casefold().replace("ё", "е")
    for marker in (
        "несовершеннолетний",
        "несовершеннолетняя",
        "совершеннолетний",
        "совершеннолетняя",
        "подросток",
        "взрослый",
    ):
        if marker in normalized_message:
            append(marker)
    for match in SHARED_AUDIENCE_RE.finditer(message):
        append(f"для {match.group(1)}")
    return constraints


def _event_constraint_clauses(
    message: str,
    forum_normalized: str | None,
) -> list[tuple[str, list[str], bool]]:
    clauses = [
        clause.strip()
        for clause in EVENT_CONSTRAINT_CLAUSE_RE.split(message)
        if clause.strip()
    ]
    result: list[tuple[str, list[str], bool]] = []
    for clause in clauses or [message]:
        constraints = _shared_event_constraints(clause, forum_normalized)
        if not constraints:
            continue
        age_count = sum(_is_age_constraint(value) for value in constraints)
        section_count = sum(_is_section_constraint(value) for value in constraints)
        result.append((clause, constraints, age_count > 1 or section_count > 1))
    return result


def _unique_constraint_groups(groups: list[list[str]]) -> list[list[str]]:
    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for group in groups:
        key = tuple(value.casefold().replace("ё", "е") for value in group)
        if not key or key in seen:
            continue
        unique.append(group)
        seen.add(key)
    return unique


def _clause_matches_question_aspect(clause: str, question: Question) -> bool:
    normalized_clause = clause.casefold().replace("ё", "е")
    normalized_question = question.text.casefold().replace("ё", "е")
    topic = str(question.topic or "").casefold()
    if asks_event_dates(normalized_question) or "daty" in topic:
        return has_explicit_event_timing(normalized_clause)
    if _question_is_application(question):
        return has_explicit_application_action(normalized_clause)
    aspect_markers: tuple[str, ...] = ()
    if any(marker in f"{normalized_question} {topic}" for marker in ("документ", "вещ")):
        aspect_markers = ("документ", "паспорт", "справк", "вещ", "взять")
    elif any(marker in f"{normalized_question} {topic}" for marker in ("программ", "расписан")):
        aspect_markers = ("программ", "расписан", "афиш", "активност")
    elif _question_is_age_only(question):
        aspect_markers = ("возраст", "лет", "участв", "подхож", "допуска")
    elif "положен" in f"{normalized_question} {topic}":
        aspect_markers = ("положен",)
    return bool(aspect_markers) and any(
        marker in normalized_clause for marker in aspect_markers
    )


def _question_is_application(question: Question) -> bool:
    normalized = f"{question.text} {question.topic or ''}".casefold().replace("ё", "е")
    return any(marker in normalized for marker in ("заяв", "регистрац", "podach", "registr"))


def _question_is_age_only(question: Question) -> bool:
    normalized = f"{question.text} {question.topic or ''}".casefold().replace("ё", "е")
    return any(marker in normalized for marker in ("возраст", "vozrast"))


def _is_age_constraint(value: str) -> bool:
    normalized = value.casefold().replace("ё", "е")
    return "возраст" in normalized or bool(re.search(r"\b\d{1,2}\b.*\bлет\b", normalized))


def _is_section_constraint(value: str) -> bool:
    normalized = value.casefold().replace("ё", "е")
    return "смен" in normalized and not normalized.startswith(("для ", "среди "))


def _has_section_constraint(constraints: list[str]) -> bool:
    return any(_is_section_constraint(value) for value in constraints)


def _question_already_expresses_constraint(
    question: Question,
    constraints: list[str],
) -> bool:
    normalized_question = question.text.casefold().replace("ё", "е")
    return all(
        _constraint_is_already_bound(normalized_question, value)
        for value in constraints
    )


def _question_with_constraints(
    question: Question,
    constraints: list[str],
) -> Question:
    normalized_question = question.text.casefold().replace("ё", "е")
    missing = [
        value
        for value in constraints
        if not _constraint_is_already_bound(normalized_question, value)
    ]
    if not missing:
        return question
    return question.model_copy(
        update={
            "text": f"{question.text.rstrip()} Условия запроса: {'; '.join(missing)}."
        }
    )


def _constraint_is_already_bound(normalized_question: str, value: str) -> bool:
    normalized_value = value.casefold().replace("ё", "е")
    if normalized_value in normalized_question:
        return True
    existing = _shared_event_constraints(normalized_question, None)
    if _is_age_constraint(value):
        return any(_is_age_constraint(item) for item in existing)
    if _is_section_constraint(value):
        return any(_is_section_constraint(item) for item in existing)
    return False


def _question_accepts_event_constraints(question: Question) -> bool:
    normalized = question.text.casefold().replace("ё", "е")
    topic = str(question.topic or "").casefold()
    if asks_event_dates(normalized):
        return True
    return any(
        marker in f"{normalized} {topic}"
        for marker in (
            "документ",
            "вещ",
            "программ",
            "расписан",
            "участв",
            "возраст",
            "заяв",
            "регистрац",
            "положен",
            "сертификат",
            "daty",
            "smena",
        )
    )


def _base_questions(
    analysis: QueryAnalysis,
    message: str | None,
    *,
    extra_fallback_markers: tuple[tuple[tuple[str, ...], str], ...] = (),
) -> list[Question]:
    message = str(message or "").strip()
    if _has_combined_event_place_date_request(message):
        return _combined_event_place_date_questions(
            analysis,
            message,
            extra_fallback_markers=extra_fallback_markers,
        )
    if _has_feedback_context(message):
        return [
            Question(
                text=message,
                category=analysis.category or "\u0433\u0440\u0430\u043d\u0442\u044b",
                forum_normalized=analysis.forum_normalized,
            )
        ]
    event_ticket_topic = _event_ticket_lookup_topic(message)
    if not analysis.questions and event_ticket_topic:
        questions = [
            Question(
                text=message,
                topic=event_ticket_topic,
                category=analysis.category,
                forum_normalized=analysis.forum_normalized,
            )
        ]
        for text in _fallback_questions_from_message(
            message,
            extra_markers=extra_fallback_markers,
            category=analysis.category,
        ):
            questions.append(
                Question(
                    text=text,
                    category=analysis.category,
                    forum_normalized=analysis.forum_normalized,
                )
            )
        return questions

    if analysis.questions:
        filtered_questions = _filter_inferred_aspect_questions(
            analysis.questions,
            message,
            extra_markers=extra_fallback_markers,
            category=analysis.category,
        )
        if filtered_questions:
            detected = _fallback_questions_from_message(
                message,
                extra_markers=extra_fallback_markers,
                category=analysis.category,
            )
            _append_missing_fallback_questions(
                filtered_questions,
                detected,
                category=analysis.category,
                forum_normalized=analysis.forum_normalized,
            )
            return filtered_questions
        detected = _fallback_questions_from_message(
            message,
            extra_markers=extra_fallback_markers,
            category=analysis.category,
        )
        if detected:
            return [
                Question(
                    text=text,
                    category=analysis.category,
                    forum_normalized=analysis.forum_normalized,
                )
                for text in detected
            ]
        return analysis.questions

    if not message:
        return []

    detected = _fallback_questions_from_message(
        message,
        extra_markers=extra_fallback_markers,
        category=analysis.category,
    )
    if detected:
        return [
            Question(
                text=text,
                category=analysis.category,
                forum_normalized=analysis.forum_normalized,
            )
            for text in detected
        ]

    return [
        Question(
            text=message,
            category=analysis.category,
            forum_normalized=analysis.forum_normalized,
        )
    ]


def _combined_event_place_date_questions(
    analysis: QueryAnalysis,
    message: str,
    *,
    extra_fallback_markers: tuple[tuple[tuple[str, ...], str], ...],
) -> list[Question]:
    """Keep place/date as one aspect without dropping other explicit questions."""

    combined = Question(
        text="Где и когда проходит мероприятие?",
        topic="opisanie",
        category=analysis.category,
        forum_normalized=analysis.forum_normalized,
    )
    candidates = _filter_inferred_aspect_questions(
        analysis.questions,
        message,
        extra_markers=extra_fallback_markers,
        category=analysis.category,
    )
    detected = _fallback_questions_from_message(
        message,
        extra_markers=extra_fallback_markers,
        category=analysis.category,
    )
    _append_missing_fallback_questions(
        candidates,
        detected,
        category=analysis.category,
        forum_normalized=analysis.forum_normalized,
    )

    result = [combined]
    seen = {combined.text.casefold().replace("ё", "е")}
    for question in candidates:
        if _is_place_or_date_question(question):
            continue
        key = question.text.casefold().replace("ё", "е")
        if key in seen:
            continue
        result.append(question)
        seen.add(key)
    return result


def _is_place_or_date_question(question: Question) -> bool:
    topic = str(question.topic or "").casefold()
    if topic in {
        "opisanie",
        "daty_nachala_meropriyatiya",
        "mesto_i_ploschadka_provedeniya",
    }:
        return True
    normalized = question.text.casefold().replace("ё", "е")
    return normalized in {
        "где и когда проходит мероприятие?",
        "где проходит мероприятие?",
        "какие даты и сроки?",
    }


def _event_ticket_lookup_topic(message: str) -> str | None:
    clauses = re.split(
        r"[,;.!?]+|\s+(?:и|а)\s+",
        str(message or ""),
        flags=re.IGNORECASE,
    )
    topics = {
        topic
        for clause in clauses
        if (topic := _event_ticket_topic_for_clause(clause)) is not None
    }
    if "bilet_ne_prishel_povtornoe_poluchenie" in topics:
        return "bilet_ne_prishel_povtornoe_poluchenie"
    if "poluchenie_i_naznachenie_bileta" in topics:
        return "poluchenie_i_naznachenie_bileta"
    return None


def _event_ticket_topic_for_clause(clause: str) -> str | None:
    normalized = str(clause or "").casefold().replace("ё", "е").strip()
    if not re.search(r"(?<![\w])билет[а-я]*", normalized, flags=re.UNICODE):
        return None
    if not any(
        marker in normalized
        for marker in (
            "где",
            "найти",
            "посмотреть",
            "получить",
            "не приш",
            "не вижу",
            "потер",
        )
    ):
        return None
    if any(
        marker in normalized
        for marker in (
            "проезд",
            "дорог",
            "поезд",
            "самолет",
            "авиа",
            "ж/д",
            "транспорт",
            "до форума",
            "до мероприятия",
            "до места проведения",
            "оплат",
            "стоимост",
            "сколько стоит",
            "компенс",
            "возмещ",
            "возмест",
        )
    ):
        return None
    if re.search(
        r"(?<![\w])(?:ребен|дет|несовершеннолет|муж|жен|супруг)[а-я]*(?![\w])",
        normalized,
        flags=re.UNICODE,
    ) or "другого человек" in normalized:
        return None
    if any(
        marker in normalized
        for marker in (
            "не приш",
            "не вижу",
            "не могу найти",
            "не получается найти",
            "потер",
        )
    ):
        return "bilet_ne_prishel_povtornoe_poluchenie"
    return "poluchenie_i_naznachenie_bileta"


def _has_combined_event_place_date_request(message: str) -> bool:
    normalized = str(message or "").casefold().replace("ё", "е")
    return any(
        marker in normalized
        for marker in (
            "где и когда проходит",
            "где и когда будет проходить",
            "когда и где проходит",
            "когда и где будет проходить",
        )
    )


def _has_feedback_context(message: str) -> bool:
    normalized = str(message or "").casefold().replace("\u0451", "\u0435")
    if "\u043e\u0431\u0440\u0430\u0442\u043d" not in normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "\u0437\u0430\u044f\u0432\u043a",
            "\u043f\u0440\u043e\u0435\u043a\u0442",
            "\u0433\u0440\u0430\u043d\u0442",
            "\u044d\u043a\u0441\u043f\u0435\u0440\u0442",
            "\u043e\u0446\u0435\u043d\u043a",
            "\u043a\u0443\u0440\u0430\u0442\u043e\u0440",
            "\u0431\u0430\u043b\u043b",
            "\u043e\u0441\u0442\u0430\u0432",
            "\u043f\u043e\u0434\u0435\u043b\u0438\u0442",
            "\u0432\u043f\u0435\u0447\u0430\u0442\u043b",
        )
    )


def _filter_inferred_aspect_questions(
    questions: list[Question],
    message: str,
    *,
    extra_markers: tuple[tuple[tuple[str, ...], str], ...] = (),
    category: str | None = None,
) -> list[Question]:
    message_marker_groups = _matched_marker_group_indexes(
        message,
        extra_markers=extra_markers,
        category=category,
    )
    if not message_marker_groups:
        return questions

    filtered: list[Question] = []
    normalized_message = message.casefold().replace("ё", "е")
    for question in questions:
        if _should_drop_grant_return_for_travel_reimbursement(
            question,
            questions,
            normalized_message,
            category=category,
        ):
            continue
        if _should_skip_fallback_question(
            question.text,
            normalized_message,
            category=category,
        ):
            continue
        question_marker_groups = _matched_marker_group_indexes(
            question.text,
            extra_markers=extra_markers,
            category=category,
        )
        if question_marker_groups and not question_marker_groups <= message_marker_groups:
            if not question.topic or not question_marker_groups & message_marker_groups:
                continue
        filtered.append(question)
    return filtered


def _should_drop_grant_return_for_travel_reimbursement(
    question: Question,
    questions: list[Question],
    normalized_message: str,
    *,
    category: str | None,
) -> bool:
    if category == "гранты":
        return False
    if question.topic != "vernut_denezhnye_sredstva":
        return False
    if any(marker in normalized_message for marker in ("грант", "грантов")):
        return False
    has_travel_question = any(
        candidate is not question and candidate.topic == "oplata_proezda"
        for candidate in questions
    )
    if not has_travel_question:
        return False
    return any(
        marker in normalized_message
        for marker in (
            "проезд",
            "поездк",
            "дорог",
            "билет",
            "трансфер",
            "чартер",
            "доезд",
            "доехать",
            "добраться",
            "самолет",
            "самолёт",
        )
    )


def _append_missing_fallback_questions(
    questions: list[Question],
    detected_texts: list[str],
    *,
    category: str | None,
    forum_normalized: str | None,
) -> None:
    seen = {question.text.casefold().replace("ё", "е") for question in questions}
    seen_aspects = {_fallback_question_aspect_key(question.text) for question in questions}
    for text in detected_texts:
        key = text.casefold().replace("ё", "е")
        aspect_key = _fallback_question_aspect_key(text)
        if key in seen or aspect_key in seen_aspects:
            continue
        detected_marker_groups = _matched_marker_group_indexes(text, category=category)
        if detected_marker_groups and any(
            detected_marker_groups
            & _matched_marker_group_indexes(question.text, category=category)
            for question in questions
        ):
            continue
        questions.append(
            Question(
                text=text,
                category=category,
                forum_normalized=forum_normalized,
            )
        )
        seen.add(key)
        seen_aspects.add(aspect_key)


def _matched_marker_group_indexes(
    text: str,
    *,
    extra_markers: tuple[tuple[tuple[str, ...], str], ...] = (),
    category: str | None = None,
) -> set[int]:
    normalized = text.casefold().replace("ё", "е")
    groups: set[int] = set()
    markers_to_scan = _fallback_marker_groups(extra_markers, category=category)
    for index, (markers, _question) in enumerate(markers_to_scan):
        if _has_any_marker(normalized, markers):
            groups.add(index)
    return groups


def _detected_forums(analysis: QueryAnalysis) -> list[str]:
    raw_forums = analysis.extracted_params.get("detected_forums")
    if not isinstance(raw_forums, list):
        return []

    detected: list[str] = []
    seen: set[str] = set()
    for item in raw_forums:
        forum = str(item or "").strip()
        if not forum or forum in seen:
            continue
        detected.append(forum)
        seen.add(forum)
    return detected if len(detected) > 1 else []


def _expand_questions_for_forums(
    questions: list[Question],
    forums: list[str],
    *,
    default_category: str | None,
    message: str | None,
) -> list[Question]:
    expanded: list[Question] = []
    seen: set[tuple[str, str | None, str | None, str | None]] = set()
    clause_marker_groups = _forum_clause_marker_groups(
        message or "",
        forums,
        category=default_category,
    )
    for question in questions:
        if question.forum_normalized in forums:
            _append_question(expanded, seen, question)
            continue
        question_groups = _matched_marker_group_indexes(
            question.text,
            category=question.category or default_category,
        )
        scoped_forums = [
            forum
            for forum in forums
            if question_groups and question_groups & clause_marker_groups.get(forum, set())
        ]
        for forum in scoped_forums or forums:
            _append_question(
                expanded,
                seen,
                Question(
                    text=_question_text_for_forum(forum, question.text),
                    topic=question.topic,
                    category=question.category or default_category or "форумы",
                    forum_normalized=forum,
                ),
            )
    return expanded


def _forum_clause_marker_groups(
    message: str,
    forums: list[str],
    *,
    category: str | None,
) -> dict[str, set[int]]:
    normalized = _normalize_for_forum_clause(message)
    if not normalized or _is_multi_forum_comparison(normalized):
        return {}

    occurrences = _forum_clause_occurrences(normalized, forums)
    if len(occurrences) < 2:
        return {}

    scoped: dict[str, set[int]] = {}
    for index, (start, forum) in enumerate(occurrences):
        end = occurrences[index + 1][0] if index + 1 < len(occurrences) else len(normalized)
        clause = normalized[start:end]
        groups = _matched_marker_group_indexes(clause, category=category)
        if groups:
            scoped.setdefault(forum, set()).update(groups)
    return scoped


def _forum_clause_occurrences(
    normalized_message: str,
    forums: list[str],
) -> list[tuple[int, str]]:
    padded_message = f" {normalized_message} "
    candidates: list[tuple[int, int, str]] = []
    for forum in forums:
        for raw_alias in forum_filter_values(forum):
            alias = _normalize_for_forum_clause(raw_alias)
            if not alias:
                continue
            pattern = f" {alias} "
            start = 0
            while True:
                index = padded_message.find(pattern, start)
                if index < 0:
                    break
                # ``index`` points at the padding/boundary space, which has the
                # same offset as the alias start in the unpadded normalized text.
                candidates.append((index, index + len(alias), forum))
                start = index + len(pattern) - 1

    # The registry can contain both a long canonical name and a shorter alias at
    # the same location.  Prefer the longest match so one textual mention creates
    # exactly one clause boundary.
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    selected: list[tuple[int, int, str]] = []
    for start, end, forum in candidates:
        if any(
            selected_start <= start and end <= selected_end
            for selected_start, selected_end, _ in selected
        ):
            continue
        selected.append((start, end, forum))
    return [(start, forum) for start, _end, forum in selected]


def _normalize_for_forum_clause(value: str) -> str:
    normalized = str(value or "").casefold().replace("ё", "е").replace("ë", "е")
    return " ".join(FORUM_CLAUSE_NON_WORD_RE.sub(" ", normalized).split())


def _is_multi_forum_comparison(normalized_message: str) -> bool:
    return any(
        marker in normalized_message
        for marker in (
            "чем отлич",
            "сравни",
            "разниц",
            "у какого",
            "у кого",
            "в обоих",
            "для обоих",
        )
    )


def _append_question(
    questions: list[Question],
    seen: set[tuple[str, str | None, str | None, str | None]],
    question: Question,
) -> None:
    key = (
        question.text.casefold().replace("ё", "е"),
        question.topic,
        question.category,
        question.forum_normalized,
    )
    if key in seen:
        return
    seen.add(key)
    questions.append(question)


def _question_text_for_forum(forum: str, text: str) -> str:
    normalized_forum = forum.casefold().replace("ё", "е")
    normalized_text = text.casefold().replace("ё", "е")
    if normalized_forum in normalized_text:
        return text
    return f"{forum}: {text}"


def _fallback_questions_from_message(
    message: str,
    *,
    extra_markers: tuple[tuple[tuple[str, ...], str], ...] = (),
    category: str | None = None,
) -> list[str]:
    normalized = message.casefold().replace("ё", "е")
    questions: list[str] = []
    seen_aspects: set[str] = set()
    for markers, question in _fallback_marker_groups(extra_markers, category=category):
        if _has_any_marker(normalized, markers):
            if _should_skip_fallback_question(question, normalized, category=category):
                continue
            aspect_key = _fallback_question_aspect_key(question)
            if question not in questions and aspect_key not in seen_aspects:
                questions.append(question)
                seen_aspects.add(aspect_key)
    return questions


def _has_any_marker(normalized: str, markers: tuple[str, ...]) -> bool:
    for marker in markers:
        if marker == "когда смена":
            if re.search(
                r"\bкогда\s+(?:(?:(?:перв|втор|трет|четверт|пят|шест|седьм|"
                r"восьм|девят|десят)\w*|\d{1,2}\s*[-–—]?\s*"
                r"(?:я|й|ю|е|ая|ый|ую|ой)?)\s+)?смен\w*\b",
                normalized,
                flags=re.UNICODE,
            ):
                return True
            continue
        if marker == "лет":
            if re.search(
                r"(?<![\w])лет(?![\w])|(?<![\w])\d{1,3}-летн[а-я]*(?![\w])",
                normalized,
                flags=re.UNICODE,
            ):
                return True
            continue
        if marker in normalized:
            return True
    return False


def _fallback_question_aspect_key(question: str) -> str:
    normalized = question.casefold().replace("ё", "е")
    if asks_event_dates(normalized):
        return "event_dates"
    if "результат" in normalized and "отбор" in normalized:
        return "selection_results"
    return normalized


def _fallback_marker_groups(
    extra_markers: tuple[tuple[tuple[str, ...], str], ...],
    *,
    category: str | None,
) -> tuple[tuple[tuple[str, ...], str], ...]:
    category_markers = (
        GRANT_FALLBACK_QUESTION_MARKERS if category == "гранты" else ()
    )
    return (
        *extra_markers,
        *FALLBACK_QUESTION_MARKERS,
        *ADDITIONAL_FALLBACK_QUESTION_MARKERS,
        *category_markers,
    )


def _should_skip_fallback_question(
    question: str,
    normalized_message: str,
    *,
    category: str | None,
) -> bool:
    has_reporting_context = any(
        marker in normalized_message
        for marker in ("отчет", "отчетност", "отчёт", "отчётност")
    )
    normalized_question = question.casefold().replace("ё", "е")
    if (
        question == "Какие документы нужны?"
        or "документ" in normalized_question
        and "нуж" in normalized_question
    ):
        if "возмещ" in normalized_message:
            return True
        if category == "форумы":
            return False
        return _has_personal_document_context(
            normalized_message
        ) and not _has_event_document_context(normalized_message)
    if question == "Какие возрастные ограничения?":
        has_explicit_age_question = any(
            marker in normalized_message
            for marker in (
                "возраст",
                "огранич",
                "подхож",
                "допуска",
                "могу участв",
                "могу ли участв",
                "можно участв",
                "можно ли участв",
            )
        )
        if asks_event_dates(normalized_message) and not has_explicit_age_question:
            return True
    if question == "Какие даты и сроки?":
        if should_suppress_event_date_question(normalized_message):
            return True
        if "когда добав" in normalized_message and "чат" in normalized_message:
            return True
        if _has_selection_result_context(normalized_message):
            return True
        has_arrival_departure_context = any(
            marker in normalized_message
            for marker in (
                "заезд и выезд",
                "заезда и выезда",
                "время заезда",
                "время выезда",
                "когда заезд",
                "когда выезд",
            )
        )
        if has_arrival_departure_context:
            return True
        if _has_personal_date_context(normalized_message) and not _has_event_date_context(
            normalized_message
        ):
            return True
        has_explicit_event_dates = any(
            marker in normalized_message
            for marker in ("дата", "даты", "заезд", "выезд")
        )
        return has_reporting_context and not has_explicit_event_dates

    if question == "Что с подтверждением участия?":
        return _has_decline_participation_context(normalized_message)

    if (
        "результат" in normalized_question
        or "отбор" in normalized_question
    ) and has_explicit_technical_failure(normalized_message):
        has_explicit_outcome_request = any(
            marker in normalized_message
            for marker in (
                "результат",
                "одобрен",
                "отклонен",
                "отклонён",
                "резерв",
                "прошел отбор",
                "прошёл отбор",
            )
        )
        if not has_explicit_outcome_request:
            return True

    if question == "Кто оплачивает проезд?":
        if "возмещ" in normalized_message:
            return True
        has_travel_context = any(
            marker in normalized_message
            for marker in (
                "проезд",
                "дорог",
                "билет",
                "чартер",
                "доезд",
                "доехать",
                "добраться",
                "поездк",
            )
        )
        if not has_travel_context:
            return True
        if _has_decline_participation_context(normalized_message):
            has_explicit_travel_cost_context = any(
                marker in normalized_message
                for marker in (
                    "проезд",
                    "дорог",
                    "билет",
                    "чартер",
                    "доезд",
                    "добраться",
                    "оплат",
                    "стоимост",
                    "возмещ",
                )
            )
            if not has_explicit_travel_cost_context:
                return True
        return category == "гранты" and "расход" in normalized_message and not has_travel_context

    if question == "Как подать заявку или зарегистрироваться?":
        if (
            has_explicit_technical_failure(normalized_message)
            and not has_explicit_application_action(normalized_message)
        ):
            return True
        if _has_decline_participation_context(
            normalized_message
        ) and not _has_explicit_application_context(normalized_message):
            return True
        return "отклон" in normalized_message

    if question == "Есть ли грантовый конкурс?":
        return category == "гранты"

    return False

def _has_decline_participation_context(normalized_message: str) -> bool:
    return any(
        marker in normalized_message
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
            "не получается поехать",
            "не получается приехать",
            "не выйдет поехать",
            "не выйдет приехать",
            "потом отказаться",
            "подтвердил участие",
            "подтвердила участие",
        )
    )


def _has_explicit_application_context(normalized_message: str) -> bool:
    return any(
        marker in normalized_message
        for marker in (
            "подать заяв",
            "подача заяв",
            "зарегистр",
            "регистрац",
            "как участвовать",
            "как принять участие",
        )
    )


def _has_personal_document_context(normalized_message: str) -> bool:
    return any(
        marker in normalized_message
        for marker in ("[документ]", "[snils]", "снилс", "паспорт")
    )


def _has_event_document_context(normalized_message: str) -> bool:
    return any(
        marker in normalized_message
        for marker in (
            "какие документы",
            "документы нужны",
            "что взять",
            "список вещ",
            "памятк",
        )
    )


def _has_selection_result_context(normalized_message: str) -> bool:
    has_selection_context = any(
        marker in normalized_message
        for marker in ("отбор", "конкурс", "результат", "резерв", "список", "списки")
    )
    has_timing_context = any(
        marker in normalized_message
        for marker in ("срок", "когда", "оповещ", "известн", "результат")
    )
    return has_selection_context and has_timing_context


def _has_personal_date_context(normalized_message: str) -> bool:
    return "дата рождения" in normalized_message or "[дата]" in normalized_message


def _has_event_date_context(normalized_message: str) -> bool:
    return any(
        marker in normalized_message
        for marker in (
            "дата форум",
            "даты форум",
            "дата меропр",
            "даты меропр",
            "когда проходит",
            "когда начинается",
            "сроки регистрац",
            "срок приема",
            "срок приёма",
            "заезд",
            "выезд",
        )
    )
