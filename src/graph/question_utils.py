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
)


def build_effective_questions(analysis: QueryAnalysis, message: str | None) -> list[Question]:
    if analysis.questions:
        return analysis.questions

    message = str(message or "").strip()
    if not message:
        return []

    detected = _fallback_questions_from_message(message)
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


def _fallback_questions_from_message(message: str) -> list[str]:
    normalized = message.casefold().replace("ё", "е")
    questions: list[str] = []
    for markers, question in FALLBACK_QUESTION_MARKERS:
        if any(marker in normalized for marker in markers):
            questions.append(question)
    return questions
