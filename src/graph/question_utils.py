from __future__ import annotations

from src.models import QueryAnalysis, Question

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
        ),
        "Как подать заявку или зарегистрироваться?",
    ),
    (("документ", "паспорт", "справк"), "Какие документы нужны?"),
    (("положен",), "Где найти положение мероприятия?"),
    (("трансфер", "автобус", "шаттл"), "Есть ли трансфер?"),
    (("питани", "еда", "корм"), "Есть ли питание?"),
    (("возраст", "лет", "14", "18", "35"), "Какие возрастные ограничения?"),
    (
        (
            "проезд",
            "оплат",
            "расход",
            "покрыва",
            "дорог",
            "билет",
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
        ("проживан", "жиль", "гостиниц", "отель", "отеле", "отеля"),
        "Какие условия проживания?",
    ),
    (("ноутбук", "снаряж", "вещ", "одежд", "взять с собой"), "Что нужно взять с собой?"),
    (
        ("отказ", "отказаться", "отозвать", "отменить участие"),
        "Как отказаться от участия или отозвать заявку?",
    ),
    (("отклон", "причин отклон"), "Почему отклонили заявку?"),
    (
        (
            "письмо-вызов",
            "письмо вызов",
            "письмо на регион",
            "письмо в регион",
            "письмо для региона",
            "приглашен",
            "подтверждение участия",
        ),
        "Как получить письмо-вызов или подтверждение участия?",
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
    (("дата", "даты", "срок", "заезд", "выезд"), "Какие даты и сроки?"),
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
            "не открывается",
            "не загружается",
            "не могу войти",
            "не получается войти",
            "не могу зайти",
            "авторизац",
            "баг",
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
        )

    return _base_questions(analysis, message)


def _base_questions(
    analysis: QueryAnalysis,
    message: str | None,
    *,
    extra_fallback_markers: tuple[tuple[tuple[str, ...], str], ...] = (),
) -> list[Question]:
    message = str(message or "").strip()
    if _has_feedback_context(message):
        return [
            Question(
                text=message,
                category=analysis.category or "\u0433\u0440\u0430\u043d\u0442\u044b",
                forum_normalized=analysis.forum_normalized,
            )
        ]

    if analysis.questions:
        filtered_questions = _filter_inferred_aspect_questions(
            analysis.questions,
            message,
            extra_markers=extra_fallback_markers,
            category=analysis.category,
        )
        if filtered_questions:
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
    for question in questions:
        question_marker_groups = _matched_marker_group_indexes(
            question.text,
            extra_markers=extra_markers,
            category=category,
        )
        if question_marker_groups and not question_marker_groups <= message_marker_groups:
            continue
        filtered.append(question)
    return filtered


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
        if any(marker in normalized for marker in markers):
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
) -> list[Question]:
    expanded: list[Question] = []
    seen: set[tuple[str, str | None, str | None, str | None]] = set()
    for question in questions:
        if question.forum_normalized in forums:
            _append_question(expanded, seen, question)
            continue
        for forum in forums:
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
    for markers, question in _fallback_marker_groups(extra_markers, category=category):
        if any(marker in normalized for marker in markers):
            if _should_skip_fallback_question(question, normalized, category=category):
                continue
            if question not in questions:
                questions.append(question)
    return questions


def _fallback_marker_groups(
    extra_markers: tuple[tuple[tuple[str, ...], str], ...],
    *,
    category: str | None,
) -> tuple[tuple[tuple[str, ...], str], ...]:
    category_markers = (
        GRANT_FALLBACK_QUESTION_MARKERS if category == "гранты" else ()
    )
    return (*extra_markers, *FALLBACK_QUESTION_MARKERS, *category_markers)


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
    if question == "Какие даты и сроки?":
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
        has_explicit_event_dates = any(
            marker in normalized_message
            for marker in ("дата", "даты", "заезд", "выезд")
        )
        return has_reporting_context and not has_explicit_event_dates

    if question == "Кто оплачивает проезд?":
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
        return "отклон" in normalized_message

    return False
