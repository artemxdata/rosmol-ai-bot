from __future__ import annotations

import re

from src.graph.response_profiles import (
    asks_event_dates,
    has_explicit_application_action,
    has_explicit_event_timing,
    has_explicit_technical_failure,
    should_suppress_event_date_question,
)
from src.kb.forum_registry import forum_filter_values
from src.models import QueryAnalysis, Question

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
