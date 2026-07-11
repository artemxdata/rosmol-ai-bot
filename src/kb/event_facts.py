from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from src.models import Chunk, QueryAnalysis

_PLACE_DATE_QUERY_RE = re.compile(
    r"\b(?:где\s+и\s+когда|когда\s+и\s+где)\s+(?:будет\s+)?проход\w*",
    flags=re.IGNORECASE,
)
_PLACE_DATE_SECTION_RE = re.compile(
    r"Дата\s+и\s+место\s+проведения\s*:\s*(?P<value>.+?)"
    r"(?=\s+Участники\s*:|\s+Общее\s+количество|\s+Где\s+пройд[её]т|$)",
    flags=re.IGNORECASE | re.DOTALL,
)
_EVENT_DATE_RE = re.compile(
    r"(?P<value>с\s+\d{1,2}\s+по\s+\d{1,2}\s+[а-яё]+\s+\d{4}\s+года)",
    flags=re.IGNORECASE,
)
_EVENT_PLACE_RE = re.compile(
    r"(?:форум|мероприятие|событие)\s+будет\s+проходить\s+(?P<value>[^.]+)",
    flags=re.IGNORECASE,
)
_FOREIGN_REGISTRATION_QUERY_RE = re.compile(
    r"\b(?:иностран\w*|foreign|друг\w*\s+гражданств\w*)\b",
    flags=re.IGNORECASE,
)
_REGISTRATION_QUERY_RE = re.compile(
    r"\b(?:регистрац\w*|зарегистрир\w*|подат\w*\s+заяв\w*)\b",
    flags=re.IGNORECASE,
)
_FOREIGN_REGISTRATION_LINK_RE = re.compile(
    r"регистрац\w*\s+для\s+иностран\w*\s+участник\w*"
    r"[^\n.]{0,120}?https?://[^\s)]+",
    flags=re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s)]+", flags=re.IGNORECASE)
_SEED_RECORD_CACHE: dict[str, tuple[int, int, list[dict[str, Any]]]] = {}


def concise_event_place_date_response(
    *,
    message: str,
    analysis: QueryAnalysis | None,
    chunks: Iterable[Chunk],
) -> str | None:
    if not _PLACE_DATE_QUERY_RE.search(str(message or "")):
        return None

    forum = analysis.forum_normalized if analysis else None
    candidates = [
        chunk
        for chunk in chunks
        if not forum
        or not chunk.metadata.get("forum_normalized")
        or str(chunk.metadata.get("forum_normalized")).casefold() == forum.casefold()
    ]
    candidates.sort(
        key=lambda chunk: (
            str(chunk.metadata.get("source_type") or "") != "yonote",
            -float(getattr(chunk, "reranker_score", 0.0) or 0.0),
        )
    )
    for chunk in candidates:
        section = _PLACE_DATE_SECTION_RE.search(" ".join(chunk.text.split()))
        if section is None:
            continue
        value = section.group("value").strip()
        date_match = _EVENT_DATE_RE.search(value)
        place_match = _EVENT_PLACE_RE.search(value)
        if date_match and place_match:
            subject = f"Форум «{forum}»" if forum else "Мероприятие"
            return (
                f"{subject} пройдёт {date_match.group('value')} "
                f"{place_match.group('value').strip()}."
            )
        if value:
            return value
    return None


def foreign_registration_response(
    *,
    message: str,
    analysis: QueryAnalysis | None,
    chunks: Iterable[Chunk],
    seed_path: str | Path | None = None,
) -> str | None:
    if not (
        _FOREIGN_REGISTRATION_QUERY_RE.search(str(message or ""))
        and _REGISTRATION_QUERY_RE.search(str(message or ""))
    ):
        return None

    forum = analysis.forum_normalized if analysis else None
    texts = [chunk.text for chunk in chunks if _same_forum(chunk.metadata, forum)]
    if seed_path:
        texts.extend(
            str(record.get("text_clean") or record.get("text_raw") or "")
            for record in _load_seed_records(Path(seed_path))
            if str(record.get("source_type") or "") == "yonote"
            and _same_forum(record, forum)
        )
    for text in texts:
        match = _FOREIGN_REGISTRATION_LINK_RE.search(" ".join(text.split()))
        if match is None:
            continue
        url_match = _URL_RE.search(match.group(0))
        if url_match is None:
            continue
        subject = f"на форум «{forum}»" if forum else "на мероприятие"
        return (
            f"Для иностранных участников регистрация {subject} доступна отдельно: "
            f"{url_match.group(0).rstrip('.,')}"
        )
    return None


def _same_forum(metadata: dict[str, Any], forum: str | None) -> bool:
    if not forum:
        return True
    item_forum = str(metadata.get("forum_normalized") or "").strip()
    return not item_forum or item_forum.casefold() == forum.casefold()


def _load_seed_records(path: Path) -> list[dict[str, Any]]:
    try:
        stat = path.stat()
    except OSError:
        return []
    cache_key = str(path.resolve())
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _SEED_RECORD_CACHE.get(cache_key)
    if cached and cached[:2] == signature:
        return cached[2]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    records = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    _SEED_RECORD_CACHE[cache_key] = (*signature, records)
    return records
