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
_RUSSIAN_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_WORD_DATE = (
    r"(?P<day>\d{1,2})\s+"
    rf"(?P<month>{'|'.join(_RUSSIAN_MONTHS)})\s+"
    r"(?P<year>\d{4})"
)
_WORD_DATE_TIME = (
    r"(?:\s*г(?:ода)?\.?)?"
    r"(?:\s*\(?\s*(?:включительно\s*,?\s*)?"
    r"(?:(?:до|в)\s*)?(?P<time>\d{1,2}:\d{2})\s*(?:мск)?\s*\)?)?"
)
_REGISTRATION_DEADLINE_PATTERNS = (
    re.compile(
        rf"(?:регистрац\w*|при[её]м\w*\s+заяв\w*|подач\w*\s+заяв\w*|"
        rf"подать\s+заяв\w*).{{0,600}}?\bс\s+\d{{1,2}}[./-]\d{{1,2}}"
        rf"(?:[./-]\d{{4}})?\s+по\s+"
        rf"(?P<time>\d{{1,2}}:\d{{2}})\s*(?:мск)?\s+{_DATE}",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"(?:регистрац\w*|при[её]м\w*\s+заяв\w*|подач\w*\s+заяв\w*|"
        rf"подать\s+заяв\w*).{{0,600}}?\bс\s+\d{{1,2}}\s+"
        rf"(?:{'|'.join(_RUSSIAN_MONTHS)})\s+по\s+"
        rf"(?P<time>\d{{1,2}}:\d{{2}})\s*(?:мск)?\s+{_WORD_DATE}"
        r"(?:\s*г(?:ода)?\.?)?",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"(?:регистрац\w*|при[её]м\w*\s+заяв\w*|подач\w*\s+заяв\w*|"
        rf"подать\s+заяв\w*).{{0,600}}?\b(?:до|по)\s+{_DATE}{_TIME}",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"окончани\w*\s+(?:(?:при[её]ма|подачи)\s+заяв\w*|регистрац\w*)"
        rf"[^\d]{{0,80}}{_DATE}{_TIME}",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"(?:регистрац\w*|при[её]м\w*\s+заяв\w*|подач\w*\s+заяв\w*|"
        rf"подать\s+заяв\w*).{{0,600}}?\b(?:до|по)\s+{_WORD_DATE}{_WORD_DATE_TIME}",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"окончани\w*\s+(?:(?:при[её]ма|подачи)\s+заяв\w*|регистрац\w*)"
        rf"[^\d]{{0,80}}{_WORD_DATE}{_WORD_DATE_TIME}",
        flags=re.IGNORECASE,
    ),
)
_REGISTRATION_DEADLINE_BRIDGE_RE = re.compile(
    r"\b(?:регистрац\w*|при[её]м\w*\s+заяв\w*|подач\w*\s+заяв\w*|"
    r"подать\s+заяв\w*|заявк\w*|фгаис|myrosmol|ссылк\w*|форм\w*|"
    r"срок\w*|дедлайн\w*|она|е[её])\b",
    flags=re.IGNORECASE,
)
_NON_REGISTRATION_DEADLINE_CLAUSE_RE = re.compile(
    r"\b(?:результат\w*|итог\w*|списк\w*|отбор\w*|"
    r"дат\w*\s+проведен\w*|заверш\w*\s+(?:форум\w*|мероприят\w*))\b",
    flags=re.IGNORECASE,
)
_REGISTRATION_QUERY_RE = re.compile(
    r"\b(?:регистрац\w*|зарегистрир\w*|подат\w*\s+заяв\w*|подач\w*\s+заяв\w*|"
    r"записа(?:ться|ться)|вписа(?:ться|ться)|как\s+(?:мне\s+)?попасть|"
    r"хоч\w*\s+попасть|стать\s+участник\w*|как\s+участвова\w*)\b",
    flags=re.IGNORECASE,
)
_REGISTRATION_DEADLINE_QUERY_RE = re.compile(
    r"(?:\b(?:до\s+какого(?:\s+числа)?|крайн\w*\s+срок|дедлайн)\b"
    r"[^.!?]{0,80}\b(?:заяв\w*|регистрац\w*)\b|"
    r"\b(?:заяв\w*|регистрац\w*)\b[^.!?]{0,80}"
    r"\b(?:до\s+какого(?:\s+числа)?|крайн\w*\s+срок|дедлайн)\b)",
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
_AS_OF_DATE_QUERY_RE = re.compile(
    rf"(?:по\s+состоянию\s+на|к)\s+{_WORD_DATE}",
    flags=re.IGNORECASE,
)
_EVENT_STATE_QUERY_RE = re.compile(
    r"\b(?:заверш\w*|продолжа\w*|проход\w*|ид[её]т|шла)\b",
    flags=re.IGNORECASE,
)
_EVENT_SAME_MONTH_RANGE_RE = re.compile(
    rf"(?<!\d)(?P<start>\d{{1,2}})\s*[-–—]\s*(?P<end>\d{{1,2}})\s+"
    rf"(?P<month>{'|'.join(_RUSSIAN_MONTHS)})(?:\s+(?P<year>\d{{4}}))?",
    flags=re.IGNORECASE,
)
_NAMED_SHIFT_QUERY_RE = re.compile(
    r"\bсмен\w*\s+«(?P<name>[^»\r\n]{1,80})»",
    flags=re.IGNORECASE,
)
_SEED_DEADLINE_CACHE: dict[
    str,
    tuple[int, int, date, dict[str, tuple[RegistrationDeadline, Chunk]]],
] = {}


@dataclass(frozen=True)
class RegistrationDeadline:
    closes_at: datetime
    explicit_time: bool


@dataclass(frozen=True)
class EventDateRange:
    starts_on: date
    ends_on: date


def as_of_event_fact(
    *,
    message: str,
    analysis: QueryAnalysis | None,
    chunks: Iterable[Chunk],
) -> tuple[str, Chunk] | None:
    """Answer an explicit event-state-as-of question from one exact Yonote range."""

    if analysis is None or not analysis.forum_normalized:
        return None
    if _EVENT_STATE_QUERY_RE.search(message) is None:
        return None
    as_of_match = _AS_OF_DATE_QUERY_RE.search(message)
    if as_of_match is None:
        return None
    as_of = _parse_word_date(
        as_of_match.group("day"),
        as_of_match.group("month"),
        as_of_match.group("year"),
    )
    if as_of is None:
        return None

    forum = analysis.forum_normalized.casefold()
    candidates: list[tuple[EventDateRange, Chunk]] = []
    for chunk in chunks:
        metadata = chunk.metadata
        if (
            str(metadata.get("status") or "") != "published"
            or str(metadata.get("source_type") or "") != "yonote"
            or str(metadata.get("source") or "") != "yonote_api"
            or str(metadata.get("version") or "") != "yonote-api-v1"
            or str(metadata.get("forum_normalized") or "").casefold() != forum
        ):
            continue
        event_range = _event_date_range_from_chunk(chunk)
        if event_range is not None:
            candidates.append((event_range, chunk))
    if len(candidates) != 1:
        return None

    event_range, source_chunk = candidates[0]
    subject = _event_subject_from_query(message)
    range_label = (
        f"с {_format_russian_date(event_range.starts_on)} "
        f"по {_format_russian_date(event_range.ends_on)}"
    )
    as_of_label = _format_russian_date(as_of)
    if as_of > event_range.ends_on:
        state = (
            f"Смена завершилась к {as_of_label}. "
            f"{subject} проходила {range_label}."
        )
    elif event_range.starts_on <= as_of <= event_range.ends_on:
        state = (
            f"На {as_of_label} смена продолжалась. "
            f"{subject} проходит {range_label}."
        )
    else:
        state = (
            f"На {as_of_label} смена ещё не началась. "
            f"{subject} проходит {range_label}."
        )
    return state, source_chunk


def _event_subject_from_query(message: str) -> str:
    if re.search(r"\bперв\w*\s+смен\w*\b", message, flags=re.IGNORECASE):
        return "Первая смена"
    if re.search(r"\bвтор\w*\s+смен\w*\b", message, flags=re.IGNORECASE):
        return "Вторая смена"
    named = _NAMED_SHIFT_QUERY_RE.search(message)
    if named is not None:
        return f"Смена «{named.group('name').strip()}»"
    return "Смена"


def _event_date_range_from_chunk(chunk: Chunk) -> EventDateRange | None:
    normalized = " ".join(chunk.text.replace("\xa0", " ").split()).casefold()
    matches = list(_EVENT_SAME_MONTH_RANGE_RE.finditer(normalized))
    if not matches:
        return None
    candidate = matches[0]
    month = _RUSSIAN_MONTHS.get(candidate.group("month").casefold())
    if month is None:
        return None
    year = candidate.group("year")
    if year is None:
        metadata_years = set(
            re.findall(
                r"(?<!\d)(20\d{2})(?!\d)",
                " ".join(
                    [
                        str(chunk.metadata.get("source_file") or ""),
                        str(chunk.metadata.get("intent_name") or ""),
                        *(
                            str(value)
                            for value in (
                                chunk.metadata.get("source_heading_path") or []
                            )
                        ),
                    ]
                ),
            )
        )
        if len(metadata_years) != 1:
            return None
        year = next(iter(metadata_years))
    try:
        starts_on = date(int(year), month, int(candidate.group("start")))
        ends_on = date(int(year), month, int(candidate.group("end")))
    except ValueError:
        return None
    if starts_on >= ends_on:
        return None
    return EventDateRange(starts_on=starts_on, ends_on=ends_on)


def extract_registration_deadline(text: str) -> RegistrationDeadline | None:
    """Extract a registration closing instant, not an event or selection date."""
    normalized = " ".join(str(text or "").replace("\xa0", " ").split())
    candidates: list[RegistrationDeadline] = []
    for pattern in _REGISTRATION_DEADLINE_PATTERNS:
        for match in pattern.finditer(normalized):
            if not _registration_deadline_match_is_scoped(match.group(0)):
                continue
            groups = match.groupdict()
            parsed_date = (
                _parse_date(groups["date"])
                if groups.get("date")
                else _parse_word_date(
                    groups.get("day"),
                    groups.get("month"),
                    groups.get("year"),
                )
            )
            if parsed_date is None:
                continue
            parsed_time = _parse_time(groups.get("time"))
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
    return _latest_deadline(candidates)


def _registration_deadline_match_is_scoped(matched_text: str) -> bool:
    """Do not bind a later event/result date to an earlier registration mention."""

    if _NON_REGISTRATION_DEADLINE_CLAUSE_RE.search(matched_text) is not None:
        return False
    clauses = [
        clause.strip()
        for clause in re.split(r"(?<=[.!?;])\s+", matched_text)
        if clause.strip()
    ]
    if len(clauses) <= 1:
        return True
    final_clause = clauses[-1]
    return (
        _REGISTRATION_DEADLINE_BRIDGE_RE.search(final_clause) is not None
    )


def registration_deadline_iso(text: str) -> str | None:
    deadline = extract_registration_deadline(text)
    return deadline.closes_at.isoformat() if deadline else None


def moscow_today() -> date:
    return datetime.now(MOSCOW_TZ).date()


def is_published_active_record(
    record: dict[str, Any],
    *,
    as_of: date | None = None,
) -> bool:
    if str(record.get("status") or "published").strip() != "published":
        return False
    active_on = as_of or moscow_today()
    valid_from = _metadata_date(record.get("valid_from"))
    valid_to = _metadata_date(record.get("valid_to"))
    return not (
        (valid_from is not None and valid_from > active_on)
        or (valid_to is not None and valid_to < active_on)
    )


def is_registration_query(text: str, topics: Iterable[str] = ()) -> bool:
    query = str(text or "")
    if _REGISTRATION_QUERY_RE.search(query) or _REGISTRATION_DEADLINE_QUERY_RE.search(
        query
    ):
        return True
    if _NON_REGISTRATION_QUERY_RE.search(query):
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
    fact = expired_registration_fact(
        message=message,
        analysis=analysis,
        chunks=chunks,
        now=now,
        seed_path=seed_path,
    )
    return fact[0] if fact else None


def expired_registration_fact(
    *,
    message: str,
    analysis: QueryAnalysis | None,
    chunks: Iterable[Chunk],
    now: datetime | None = None,
    seed_path: str | Path | None = None,
) -> tuple[str, Chunk] | None:
    topics = analysis.topics if analysis else ()
    if not is_registration_query(message, topics):
        return None
    if _FOREIGN_PARTICIPANT_RE.search(message):
        # International registration can use a separate form and a different deadline.
        return None

    forum = analysis.forum_normalized if analysis else None
    if not forum:
        return None
    current = now or datetime.now(MOSCOW_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MOSCOW_TZ)
    else:
        current = current.astimezone(MOSCOW_TZ)
    deadline_candidates: list[tuple[RegistrationDeadline, Chunk]] = []
    matching_chunks: list[Chunk] = []
    for chunk in chunks:
        if not is_published_active_record(chunk.metadata, as_of=current.date()):
            continue
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

    seed_candidate = _seed_registration_deadline(
        forum,
        seed_path,
        as_of=current.date(),
    )
    if seed_candidate is not None:
        deadline_candidates.append(seed_candidate)

    if not deadline_candidates:
        return None

    yonote_candidates = [
        item
        for item in deadline_candidates
        if str(item[1].metadata.get("source_type") or "").strip() == "yonote"
    ]
    if yonote_candidates:
        deadline_candidates = yonote_candidates
    # If trusted sources disagree, declaring closure after the latest deadline is safer.
    deadline, source_chunk = _latest_deadline_candidate(deadline_candidates)
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
    registration_channel = (
        "\nРегистрация во ФГАИС завершена."
        if re.search(
            r"\bрегистрац\w*\s+во\s+фгаис\b",
            source_chunk.text,
            flags=re.IGNORECASE,
        )
        else ""
    )
    return (
        (
            f"Регистрация {subject} закрыта: приём заявок завершился "
            f"{date_label}{time_label}.\n"
            f"{registration_channel.lstrip()}"
            f"{' ' if registration_channel else ''}"
            "Новую заявку сейчас подать нельзя. Следи за обновлениями в карточке "
            "мероприятия на платформе ФГАИС «Молодёжь России»."
        ),
        source_chunk,
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
    *,
    as_of: date,
) -> tuple[RegistrationDeadline, Chunk] | None:
    if not forum or not seed_path:
        return None
    deadlines = _load_seed_deadlines(Path(seed_path), as_of=as_of)
    return deadlines.get(forum.casefold())


def _load_seed_deadlines(
    path: Path,
    *,
    as_of: date,
) -> dict[str, tuple[RegistrationDeadline, Chunk]]:
    try:
        stat = path.stat()
    except OSError:
        return {}
    cache_key = str(path.resolve())
    cached = _SEED_DEADLINE_CACHE.get(cache_key)
    signature = (stat.st_mtime_ns, stat.st_size)
    if cached and cached[:3] == (*signature, as_of):
        return cached[3]

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, list):
        return {}

    grouped: dict[str, list[tuple[RegistrationDeadline, Chunk]]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        if not is_published_active_record(item, as_of=as_of):
            continue
        if str(item.get("source_type") or "").strip() != "yonote":
            continue
        forum = str(item.get("forum_normalized") or "").strip()
        if not forum:
            continue
        deadline = _deadline_from_record(item)
        chunk_id = str(item.get("chunk_id") or "").strip()
        if deadline is not None and chunk_id:
            grouped.setdefault(forum.casefold(), []).append(
                (
                    deadline,
                    Chunk(
                        chunk_id=chunk_id,
                        text=str(item.get("text_clean") or item.get("text_raw") or ""),
                        metadata=dict(item),
                    ),
                )
            )

    deadlines = {
        forum: _latest_deadline_candidate(items)
        for forum, items in grouped.items()
        if items
    }
    _SEED_DEADLINE_CACHE[cache_key] = (*signature, as_of, deadlines)
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


def _parse_word_date(
    day: str | None,
    month: str | None,
    year: str | None,
) -> date | None:
    if not day or not month or not year:
        return None
    month_number = _RUSSIAN_MONTHS.get(month.casefold())
    if month_number is None:
        return None
    try:
        return date(int(year), month_number, int(day))
    except ValueError:
        return None


def _metadata_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
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


def _latest_deadline_candidate(
    candidates: Iterable[tuple[RegistrationDeadline, Chunk]],
) -> tuple[RegistrationDeadline, Chunk]:
    return max(
        candidates,
        key=lambda item: (
            item[0].closes_at.date(),
            item[0].explicit_time,
            item[0].closes_at.time(),
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
