from __future__ import annotations

from src.kb.forum_registry import detect_forums_from_text
from src.models import QueryAnalysis, Question, Session

MAX_CONTEXT_TURNS = 5
NON_FORUM_CONTEXT_CATEGORIES = frozenset(
    {
        "гранты",
        "платформа_фгаис",
        "техподдержка",
    }
)

FOLLOWUP_FORUM_MARKERS: tuple[str, ...] = (
    "а что",
    "а если",
    "а где",
    "а когда",
    "а какие",
    "а программа",
    "а билет",
    "а проезд",
    "а питание",
    "а проживание",
    "а документы",
    "а регистрац",
    "билет",
    "проезд",
    "питани",
    "прожив",
    "трансфер",
    "программ",
    "расписан",
    "документ",
    "справк",
    "сертификат",
    "муж",
    "жен",
    "ребен",
    "ребён",
    "дет",
    "сопровожд",
    "если я",
    "что делать",
    "теперь",
    "заявк",
    "участ",
    "поехать",
    "приехать",
    "посетить",
    "подтверд",
    "отказ",
    "отказаться",
    "отозвать",
    "отменить",
    "не могу",
    "не смогу",
    "не получается",
    "на этом мероприятии",
    "на этом форуме",
)
FOLLOWUP_TOPIC_MARKERS: tuple[str, ...] = (
    "а куда",
    "куда именно",
    "куда писать",
    "куда отправ",
    "на какую почту",
    "какая почта",
    "где именно",
    "как именно",
    "а сколько",
    "сколько времени",
    "а срок",
)
GRANT_RETURN_QUESTION = Question(
    text="Как вернуть грантовые средства?",
    topic="vernut_denezhnye_sredstva",
    category="гранты",
)


def apply_session_context(
    analysis: QueryAnalysis,
    message: str,
    session: Session | None,
) -> QueryAnalysis:
    if analysis.is_offtopic:
        return analysis

    if _is_grant_return_followup(message, session):
        questions = list(analysis.questions or [])
        if not any(question.topic == GRANT_RETURN_QUESTION.topic for question in questions):
            questions.insert(0, GRANT_RETURN_QUESTION)
        return analysis.model_copy(
            update={
                "category": "гранты",
                "questions": questions,
                "topics": _merge_topics(analysis.topics, [GRANT_RETURN_QUESTION.topic or ""]),
            }
        )

    if _has_explicit_non_forum_context(analysis):
        return analysis

    if _is_topic_followup(message):
        previous_category = last_category_from_session(session)
        if (
            previous_category in NON_FORUM_CONTEXT_CATEGORIES
            and analysis.category in {None, "общее", "навигация"}
        ):
            return analysis.model_copy(update={"category": previous_category})

    if _is_forum_followup(message) or _is_topic_followup(message):
        if analysis.forum_normalized:
            if analysis.category:
                return analysis
            return analysis.model_copy(update={"category": "форумы"})

        forum = last_forum_from_session(session)
        if forum:
            category = analysis.category
            if category in {None, "общее", "навигация"}:
                category = "форумы"
            clears_forum_clarification = (
                analysis.needs_clarification
                and analysis.clarification_question is not None
                and "о каком форуме" in analysis.clarification_question.casefold()
            )
            return analysis.model_copy(
                update={
                    "forum": forum,
                    "forum_normalized": forum,
                    "category": category,
                    "needs_clarification": (
                        False if clears_forum_clarification else analysis.needs_clarification
                    ),
                    "clarification_question": (
                        None
                        if clears_forum_clarification
                        else analysis.clarification_question
                    ),
                }
            )

    return analysis


def build_contextual_message(
    message: str,
    session: Session | None,
    analysis: QueryAnalysis | None,
) -> str:
    text = str(message or "").strip()
    if not text:
        return text

    if _has_explicit_non_forum_context(analysis):
        if _is_grant_return_followup(text, session):
            return f"{GRANT_RETURN_QUESTION.text} {text}"
        if _is_topic_followup(text):
            previous_user = context_anchor_from_session(session)
            if previous_user:
                return f"{previous_user}. Уточнение пользователя: {text}"
        return text

    forum = getattr(analysis, "forum_normalized", None) or last_forum_from_session(session)
    is_contextual_followup = _is_forum_followup(text) or _is_topic_followup(text)
    if not forum or _mentions_forum(text, forum) or not is_contextual_followup:
        if _is_grant_return_followup(text, session):
            return f"{GRANT_RETURN_QUESTION.text} {text}"
        if _is_topic_followup(text):
            previous_user = context_anchor_from_session(session)
            if previous_user:
                return f"{previous_user}. Уточнение пользователя: {text}"
        return text
    previous_user = context_anchor_from_session(session)
    if previous_user and (
        _is_topic_followup(text) or _needs_previous_topic_context(text)
    ):
        return f"{forum}: {previous_user}. {text}"
    return f"{forum}: {text}"


def _has_explicit_non_forum_context(analysis: QueryAnalysis | None) -> bool:
    if analysis is None or analysis.forum_normalized:
        return False
    return analysis.category in NON_FORUM_CONTEXT_CATEGORIES


def is_context_dependent_followup(message: str, session: Session | None) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if detect_forums_from_text(text):
        return False
    if _is_forum_followup(text) and last_forum_from_session(session):
        return True
    if _is_topic_followup(text) and last_user_message_from_session(session):
        return True
    return _is_grant_return_followup(text, session)


def last_forum_from_session(session: Session | None) -> str | None:
    if session is None:
        return None

    forum = str(session.forum_context or "").strip()
    if forum:
        return forum

    for message in reversed((session.last_messages or [])[-MAX_CONTEXT_TURNS:]):
        for field in ("user", "bot"):
            detected = detect_forums_from_text(str(message.get(field) or ""))
            if detected:
                return detected[-1]

    return None


def last_user_message_from_session(session: Session | None) -> str | None:
    if session is None:
        return None
    for message in reversed((session.last_messages or [])[-MAX_CONTEXT_TURNS:]):
        text = str(message.get("user") or "").strip()
        if text:
            return text
    return None


def last_category_from_session(session: Session | None) -> str | None:
    if session is None:
        return None
    category = session.extracted_entities.get("last_category")
    normalized = str(category or "").strip()
    return normalized or None


def context_anchor_from_session(session: Session | None) -> str | None:
    if session is None:
        return None
    fallback: str | None = None
    for message in reversed((session.last_messages or [])[-MAX_CONTEXT_TURNS:]):
        text = str(message.get("user") or "").strip()
        if not text:
            continue
        fallback = fallback or text
        if not _is_topic_followup(text):
            return text
    return fallback


def _is_forum_followup(message: str) -> bool:
    normalized = _normalize(message)
    if not normalized:
        return False
    return any(marker in normalized for marker in FOLLOWUP_FORUM_MARKERS)


def _is_topic_followup(message: str) -> bool:
    normalized = _normalize(message)
    if not normalized:
        return False
    return any(marker in normalized for marker in FOLLOWUP_TOPIC_MARKERS)


def _needs_previous_topic_context(message: str) -> bool:
    normalized = _normalize(message)
    if not normalized:
        return False
    has_explicit_topic = any(
        marker in normalized
        for marker in (
            "заявк",
            "проезд",
            "питани",
            "трансфер",
            "прожив",
            "документ",
            "мед",
            "овз",
            "чат",
            "грант",
            "сертификат",
            "письмо",
            "программа",
            "результат",
            "отказ",
            "отказаться",
            "не могу поехать",
            "не смогу поехать",
        )
    )
    return not has_explicit_topic and any(
        marker in normalized
        for marker in ("такие же", "условия", "а если", "для семьи", "с семь")
    )


def _is_grant_return_followup(message: str, session: Session | None) -> bool:
    return _is_topic_followup(message) and _last_grant_return_from_session(session)


def _last_grant_return_from_session(session: Session | None) -> bool:
    if session is None:
        return False

    for message in reversed((session.last_messages or [])[-MAX_CONTEXT_TURNS:]):
        text = _normalize(f"{message.get('user') or ''} {message.get('bot') or ''}")
        if _has_grant_return_context(text):
            return True

    last_topics = session.extracted_entities.get("last_topics")
    if isinstance(last_topics, list):
        return "vernut_denezhnye_sredstva" in {str(topic) for topic in last_topics}
    return False


def _has_grant_return_context(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "вернуть грантов",
            "возврат грантов",
            "грантовые средства",
            "вернуть денежные средства",
            "reportgrant",
        )
    )


def _merge_topics(existing: list[str], additions: list[str]) -> list[str]:
    topics: list[str] = []
    for topic in [*existing, *additions]:
        if topic and topic not in topics:
            topics.append(topic)
    return topics


def _mentions_forum(message: str, forum: str) -> bool:
    normalized_message = _normalize(message)
    normalized_forum = _normalize(forum)
    return bool(normalized_forum and normalized_forum in normalized_message)


def _normalize(text: str) -> str:
    return str(text or "").casefold().replace("ё", "е")
