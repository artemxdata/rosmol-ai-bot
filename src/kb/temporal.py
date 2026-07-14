from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from src.models import Chunk, QueryAnalysis

MOSCOW_TZ = timezone(timedelta(hours=3), name="MSK")

_DATE = r"(?P<date>\d{1,2}[./-]\d{1,2}[./-]\d{4})"
_TIME = r"(?:\s*(?:г(?:ода)?\.?\s*)?(?:в\s*)?(?P<time>\d{1,2}:\d{2}))?"
_REGISTRATION_DEADLINE_PATTERNS = (
    re.compile(
        rf"(?:регистрац\w*|при[её]м\w*\s+заяв\w*|подач\w*\s+заяв\w*|"
        rf"подать\s+заяв\w*).{{0,600}}?\bдо\s+{_DATE}{_TIME}",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"окончани\w*\s+(?:(?:при[её]ма|подачи)\s+заяв\w*|регистрац\w*)"
        rf"[^\d]{{0,80}}{_DATE}{_TIME}",
        flags=re.IGNORECASE,
    ),
)
_REGISTRATION_QUERY_RE = re.compile(
    r"\b(?:регистрац\w*|зарегистрир\w*|подат\w*\s+заяв\w*|подач\w*\s+заяв\w*|"
    r"записа(?:ться|ться)|вписа(?:ться|ться)|как\s+(?:мне\s+)?попасть|"
    r"хоч\w*\s+попасть|стать\s+участник\w*|как\s+участвова\w*)\b",
    flags=re.IGNORECASE,
)
_NON_REGISTRATION_QUERY_RE = re.compile(
    r"\b(?:где\s+проход\w*|когда\s+проход\w*|дат\w*\s+проведен\w*|программ\w*|"
    r"проезд\w*|трансфер\w*|прожив\w*|где\s+жить|питан\w*|документ\w*|"
    r"возраст\w*|результат\w*\s+отбор\w*)\b",
    flags=re.IGNORECASE,
)
_REGISTRATION_TOPIC_MARKERS = (
    "registr",
    "zaregistr",
    "podacha_zayav",
    "podachi_zayav",
    "kak_popast",
    "stat_uchastnik",
)
_FOREIGN_PARTICIPANT_RE = re.compile(
    r"\b(?:иностран\w*|foreign|друг\w*\s+гражданств\w*)\b",
    flags=re.IGNORECASE,
)
_SEED_DEADLINE_CACHE: dict[
    str,
    tuple[int, int, dict[str, RegistrationDeadline]],
] = {}


@dataclass(frozen=True)
class RegistrationDeadline:
    closes_at: datetime
    explicit_time: bool


def extract_registration_deadline(text: str) -> RegistrationDeadline | None:
    """Extract a registration closing instant, not an event or selection date."""
    normalized = " ".join(str(text or "").replace("\xa0", " ").split())
    candidates: list[RegistrationDeadline] = []
    for pattern in _REGISTRATION_DEADLINE_PATTERNS:
        for match in pattern.finditer(normalized):
            parsed_date = _parse_date(match.group("date"))
            if parsed_date is None:
                continue
            parsed_time = _parse_time(match.groupdict().get("time"))
            explicit_time = parsed_time is not None
            closes_at = datetime.combine(
                parsed_date,
                parsed_time or time(23, 59, 59),
                tzinfo=MOSCOW_TZ,
            )
            candidates.append(
                RegistrationDeadline(closes_at=closes_at, explicit_time=explicit_time)
            )
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.closes_at)


def registration_deadline_iso(text: str) -> str | None:
    deadline = extract_registration_deadline(text)
    return deadline.closes_at.isoformat() if deadline else None


def is_registration_query(text: str, topics: Iterable[str] = ()) -> bool:
    if _REGISTRATION_QUERY_RE.search(str(text or "")):
        return True
    if _NON_REGISTRATION_QUERY_RE.search(str(text or "")):
        return False
    normalized_topics = " ".join(str(topic).casefold() for topic in topics if topic)
    return any(marker in normalized_topics for marker in _REGISTRATION_TOPIC_MARKERS)


def expired_registration_response(
    *,
    message: str,
    analysis: QueryAnalysis | None,
    chunks: Iterable[Chunk],
    now: datetime | None = None,
    seed_path: str | Path | None = None,
) -> str | None:
    topics = analysis.topics if analysis else ()
    if not is_registration_query(message, topics):
        return None
    if _FOREIGN_PARTICIPANT_RE.search(message):
        # International registration can use a separate form and a different deadline.
        return None

    forum = analysis.forum_normalized if analysis else None
    deadline_candidates: list[tuple[RegistrationDeadline, Chunk | None]] = []
    matching_chunks: list[Chunk] = []
    for chunk in chunks:
        chunk_forum = str(chunk.metadata.get("forum_normalized") or "").strip()
        if forum and chunk_forum and chunk_forum.casefold() != forum.casefold():
            continue
        if not _is_trusted_temporal_chunk(chunk, forum):
            continue
        deadline = _deadline_from_chunk(chunk)
        if deadline is None:
            continue
        deadline_candidates.append((deadline, chunk))
        matching_chunks.append(chunk)

    seed_deadline = _seed_registration_deadline(forum, seed_path)
    if seed_deadline is not None:
        deadline_candidates.append((seed_deadline, None))

    if not deadline_candidates:
        return None

    yonote_candidates = [
        item
        for item in deadline_candidates
        if item[1] is None
        or str(item[1].metadata.get("source_type") or "").strip() == "yonote"
    ]
    if yonote_candidates:
        deadline_candidates = yonote_candidates
    # If trusted sources disagree, declaring closure after the latest deadline is safer.
    deadline = _latest_deadline(item[0] for item in deadline_candidates)
    current = now or datetime.now(MOSCOW_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MOSCOW_TZ)
    else:
        current = current.astimezone(MOSCOW_TZ)
    if current <= deadline.closes_at:
        return None

    if not forum:
        forum = next(
            (
                str(chunk.metadata.get("forum_normalized") or "").strip()
                for chunk in matching_chunks
                if chunk.metadata.get("forum_normalized")
            ),
            "",
        )
    subject = f"на форум «{forum}»" if forum else "на мероприятие"
    date_label = _format_russian_date(deadline.closes_at.date())
    time_label = (
        f" в {deadline.closes_at:%H:%M} (мск)" if deadline.explicit_time else ""
    )
    return (
        f"Регистрация {subject} закрыта: приём заявок завершился "
        f"{date_label}{time_label}.\n"
        "Новую заявку сейчас подать нельзя. Следи за обновлениями в карточке "
        "мероприятия на платформе ФГАИС «Молодёжь России»."
    )


def _deadline_from_chunk(chunk: Chunk) -> RegistrationDeadline | None:
    raw = chunk.metadata.get("registration_deadline")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=MOSCOW_TZ)
            else:
                parsed = parsed.astimezone(MOSCOW_TZ)
            explicit_time = parsed.time() != time(23, 59, 59)
            return RegistrationDeadline(parsed, explicit_time)
    return extract_registration_deadline(chunk.text)


def _is_trusted_temporal_chunk(chunk: Chunk, forum: str | None) -> bool:
    source_type = str(chunk.metadata.get("source_type") or "").strip()
    if source_type == "yonote":
        return True
    if source_type not in {"xlsx", "docx"}:
        return True
    if not forum:
        return False
    # Legacy spreadsheets contained copied registration templates under other forums.
    # Require the event name in the text before using such a deadline as a hard cutoff.
    return forum.casefold() in chunk.text.casefold()


def _seed_registration_deadline(
    forum: str | None,
    seed_path: str | Path | None,
) -> RegistrationDeadline | None:
    if not forum or not seed_path:
        return None
    deadlines = _load_seed_deadlines(Path(seed_path))
    return deadlines.get(forum.casefold())


def _load_seed_deadlines(path: Path) -> dict[str, RegistrationDeadline]:
    try:
        stat = path.stat()
    except OSError:
        return {}
    cache_key = str(path.resolve())
    cached = _SEED_DEADLINE_CACHE.get(cache_key)
    signature = (stat.st_mtime_ns, stat.st_size)
    if cached and cached[:2] == signature:
        return cached[2]

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, list):
        return {}

    grouped: dict[str, list[RegistrationDeadline]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("source_type") or "").strip() != "yonote":
            continue
        forum = str(item.get("forum_normalized") or "").strip()
        if not forum:
            continue
        deadline = _deadline_from_record(item)
        if deadline is not None:
            grouped.setdefault(forum.casefold(), []).append(deadline)

    deadlines = {
        forum: _latest_deadline(items)
        for forum, items in grouped.items()
        if items
    }
    _SEED_DEADLINE_CACHE[cache_key] = (*signature, deadlines)
    return deadlines


def _deadline_from_record(record: dict[str, Any]) -> RegistrationDeadline | None:
    metadata_deadline = record.get("registration_deadline")
    if metadata_deadline:
        try:
            parsed = datetime.fromisoformat(str(metadata_deadline))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=MOSCOW_TZ)
            else:
                parsed = parsed.astimezone(MOSCOW_TZ)
            return RegistrationDeadline(
                closes_at=parsed,
                explicit_time=parsed.time() != time(23, 59, 59),
            )
    return extract_registration_deadline(
        str(record.get("text_clean") or record.get("text_raw") or "")
    )


def _parse_date(value: str) -> date | None:
    normalized = value.replace("/", ".").replace("-", ".")
    try:
        return datetime.strptime(normalized, "%d.%m.%Y").date()
    except ValueError:
        return None


def _latest_deadline(deadlines: Iterable[RegistrationDeadline]) -> RegistrationDeadline:
    # An explicit time is authoritative over an inferred end-of-day time on the same date.
    return max(
        deadlines,
        key=lambda item: (
            item.closes_at.date(),
            item.explicit_time,
            item.closes_at.time(),
        ),
    )


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


def _format_russian_date(value: date) -> str:
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    return f"{value.day} {months[value.month - 1]} {value.year} года"
