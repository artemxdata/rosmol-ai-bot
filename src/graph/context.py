from __future__ import annotations

from src.kb.forum_registry import detect_forums_from_text
from src.models import QueryAnalysis, Session

MAX_CONTEXT_TURNS = 5

FOLLOWUP_FORUM_MARKERS: tuple[str, ...] = (
    "а что",
    "а если",
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
)


def apply_session_context(
    analysis: QueryAnalysis,
    message: str,
    session: Session | None,
) -> QueryAnalysis:
    if analysis.is_offtopic:
        return analysis
    if not _is_forum_followup(message):
        return analysis
    if analysis.forum_normalized:
        if analysis.category:
            return analysis
        return analysis.model_copy(update={"category": "форумы"})

    forum = last_forum_from_session(session)
    if not forum:
        return analysis

    category = analysis.category
    if category in {None, "общее", "навигация"}:
        category = "форумы"
    return analysis.model_copy(
        update={
            "forum": forum,
            "forum_normalized": forum,
            "category": category,
        }
    )


def build_contextual_message(
    message: str,
    session: Session | None,
    analysis: QueryAnalysis | None,
) -> str:
    text = str(message or "").strip()
    if not text:
        return text

    forum = getattr(analysis, "forum_normalized", None) or last_forum_from_session(session)
    if not forum or _mentions_forum(text, forum) or not _is_forum_followup(text):
        return text
    return f"{forum}: {text}"


def is_context_dependent_followup(message: str, session: Session | None) -> bool:
    text = str(message or "").strip()
    if not text or not _is_forum_followup(text):
        return False
    if detect_forums_from_text(text):
        return False
    return bool(last_forum_from_session(session))


def last_forum_from_session(session: Session | None) -> str | None:
    if session is None:
        return None

    for message in reversed((session.last_messages or [])[-MAX_CONTEXT_TURNS:]):
        for field in ("user", "bot"):
            detected = detect_forums_from_text(str(message.get(field) or ""))
            if detected:
                return detected[-1]

    forum = str(session.forum_context or "").strip()
    return forum or None


def _is_forum_followup(message: str) -> bool:
    normalized = _normalize(message)
    if not normalized:
        return False
    return any(marker in normalized for marker in FOLLOWUP_FORUM_MARKERS)


def _mentions_forum(message: str, forum: str) -> bool:
    normalized_message = _normalize(message)
    normalized_forum = _normalize(forum)
    return bool(normalized_forum and normalized_forum in normalized_message)


def _normalize(text: str) -> str:
    return str(text or "").casefold().replace("ё", "е")
