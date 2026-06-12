from __future__ import annotations

from hashlib import sha1
from typing import Any

CATEGORY_KEY_ALIASES = {
    "форумы": "forums",
    "гранты": "grants",
    "техподдержка": "tech_support",
    "технические проблемы": "tech_support",
    "платформа_фгаис": "platform_fgais",
    "навигация": "navigation",
    "общее": "general",
    "модерация": "moderation",
}


def category_filter_key(value: Any) -> str:
    normalized = _normalize_text(value)
    return CATEGORY_KEY_ALIASES.get(normalized) or stable_text_filter_key(normalized)


def stable_text_filter_key(value: Any) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        return ""
    return "h_" + sha1(normalized.encode("utf-8")).hexdigest()[:16]


def build_filter_key_payload(record: dict[str, Any]) -> dict[str, str]:
    payload: dict[str, str] = {}
    category = record.get("category")
    if category:
        payload["category_key"] = category_filter_key(category)
    forum = record.get("forum_normalized")
    if forum:
        payload["forum_key"] = stable_text_filter_key(forum)
    topic = record.get("topic")
    if topic:
        payload["topic_key"] = stable_text_filter_key(topic)
    return payload


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().casefold()
