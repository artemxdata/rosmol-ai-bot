from __future__ import annotations

from src.models import QueryAnalysis, Question

FALLBACK_QUESTION_MARKERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("документ", "паспорт", "справк"), "Какие документы нужны?"),
    (("трансфер", "автобус", "шаттл"), "Есть ли трансфер?"),
    (("питани", "еда", "корм"), "Есть ли питание?"),
    (("возраст", "лет", "14", "18", "35"), "Какие возрастные ограничения?"),
    (("проезд", "дорог", "билет", "чартер"), "Кто оплачивает проезд?"),
    (("проживан", "жиль", "гостиниц", "отел"), "Какие условия проживания?"),
    (("сертификат",), "Будет ли сертификат?"),
    (("чат", "куратор"), "Как попасть в чат мероприятия?"),
    (("результат", "отбор", "одобрен", "статус", "рассмотр"), "Когда будут результаты отбора?"),
)
MULTI_FORUM_QUESTION_MARKERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("регистрац", "подать заяв", "заявк"), "Как подать заявку или зарегистрироваться?"),
)


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
    if analysis.questions:
        return analysis.questions

    message = str(message or "").strip()
    if not message:
        return []

    detected = _fallback_questions_from_message(message, extra_markers=extra_fallback_markers)
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
) -> list[str]:
    normalized = message.casefold().replace("ё", "е")
    questions: list[str] = []
    for markers, question in (*extra_markers, *FALLBACK_QUESTION_MARKERS):
        if any(marker in normalized for marker in markers):
            questions.append(question)
    return questions
