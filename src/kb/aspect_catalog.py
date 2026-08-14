from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.kb.fact_extractor import (
    KnowledgeAspect,
    infer_query_aspects,
    infer_source_aspects,
    semantic_fact_tokens,
    source_answer_signal_score,
    source_scope_constraints_match,
)
from src.kb.forum_registry import forums_are_equivalent

KB_SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "knowledge_base_seed.json"


@dataclass(frozen=True)
class SourceAspectEntry:
    topic: str
    category: str
    forum_normalized: str
    aspects: frozenset[KnowledgeAspect]
    source_text: str
    context_text: str
    metadata: dict[str, Any]


def topic_candidates_for_request(
    question_text: str,
    *,
    category: str | None = None,
    forum_normalized: str | None = None,
    seed_path: Path = KB_SEED_PATH,
    limit: int = 24,
) -> tuple[str, ...]:
    """Resolve mutable Yonote topics through the stable aspect ontology.

    This is a metadata-recall helper, not a source of answer facts.  Every
    returned topic belongs to a published Yonote row in the frozen runtime seed;
    the normal Qdrant filters and downstream source binding remain authoritative.
    """

    if limit <= 0:
        return ()
    requested = infer_query_aspects(question_text)
    if not requested:
        return ()

    return _topic_candidates(
        question_text,
        requested=requested,
        category=category,
        forum_normalized=forum_normalized,
        seed_path=seed_path,
        limit=limit,
    )


def topic_candidates_for_aspect(
    aspect: KnowledgeAspect,
    question_text: str,
    *,
    category: str | None = None,
    forum_normalized: str | None = None,
    seed_path: Path = KB_SEED_PATH,
    limit: int = 12,
) -> tuple[str, ...]:
    return _topic_candidates(
        question_text,
        requested=frozenset({aspect}),
        category=category,
        forum_normalized=forum_normalized,
        seed_path=seed_path,
        limit=limit,
    )


def _topic_candidates(
    question_text: str,
    *,
    requested: frozenset[KnowledgeAspect],
    category: str | None,
    forum_normalized: str | None,
    seed_path: Path,
    limit: int,
) -> tuple[str, ...]:
    if limit <= 0 or not requested:
        return ()
    expected_category = str(category or "").strip()
    expected_forum = str(forum_normalized or "").strip()
    query_tokens = _catalog_tokens(question_text)
    ranked: list[tuple[int, int, int, int, int, str]] = []
    for entry in _source_aspect_catalog(seed_path.resolve()):
        if expected_category and entry.category != expected_category:
            continue
        if expected_forum and not forums_are_equivalent(
            entry.forum_normalized,
            expected_forum,
        ) and not _explicit_grant_season_scope(
            question_text,
            expected_category,
            entry.forum_normalized,
        ):
            continue
        if not requested & entry.aspects:
            continue
        if not _entry_constraints_match(question_text, entry):
            continue
        exact_overlap = len(requested & entry.aspects)
        lexical_overlap = len(query_tokens & _entry_tokens(entry))
        named_context = _named_context_score(question_text, entry)
        answer_signal = sum(
            source_answer_signal_score(
                question_text,
                aspect,
                entry.metadata,
                entry.source_text,
            )
            for aspect in requested & entry.aspects
        )
        # Prefer a source that covers only the requested facet. Combined source
        # cards remain eligible but do not displace a precise heading.
        extra_aspects = len(entry.aspects - requested)
        ranked.append(
            (
                -exact_overlap,
                -answer_signal,
                -named_context,
                -lexical_overlap,
                extra_aspects,
                entry.topic,
            )
        )

    topics: list[str] = []
    seen: set[str] = set()
    for _overlap, _signal, _context, _lexical, _extra, topic in sorted(ranked):
        if topic in seen:
            continue
        topics.append(topic)
        seen.add(topic)
        if len(topics) >= limit:
            break
    return tuple(topics)


def _entry_constraints_match(
    question_text: str,
    entry: SourceAspectEntry,
) -> bool:
    # Keep e.g. first and second shifts or grant seasons separate while each
    # requested aspect is resolved independently.
    return source_scope_constraints_match(
        question_text,
        entry.metadata,
    )


def _catalog_tokens(text: str) -> set[str]:
    return set(semantic_fact_tokens(text)) - {
        "какие",
        "какой",
        "когда",
        "можн",
        "нужн",
        "форум",
    }


def _explicit_grant_season_scope(
    question_text: str,
    category: str,
    source_forum: str,
) -> bool:
    normalized_query = str(question_text or "").casefold().replace("ё", "е")
    normalized_forum = str(source_forum or "").casefold().replace("ё", "е")
    return bool(
        category == "гранты"
        and "сезон" in normalized_query
        and "сезон" in normalized_forum
        and "грант" in normalized_forum
    )


def _entry_tokens(entry: SourceAspectEntry) -> set[str]:
    metadata = entry.metadata
    heading = metadata.get("source_heading_path") or []
    heading_text = (
        " ".join(str(item) for item in heading)
        if isinstance(heading, (list, tuple))
        else str(heading or "")
    )
    return _catalog_tokens(
        " ".join(
            (
                entry.topic.replace("_", " "),
                str(metadata.get("intent_name") or ""),
                heading_text,
                entry.context_text,
            )
        )
    )


def _named_context_score(question_text: str, entry: SourceAspectEntry) -> int:
    phrases = [
        " ".join(value.split()).casefold().replace("ё", "е")
        for value in re.findall(r"[«„\"]([^»“\"]{2,96})[»“\"]", question_text)
    ]
    if not phrases:
        return 0
    context = " ".join(
        (
            entry.context_text,
            str(entry.metadata.get("intent_name") or ""),
            _heading_text(entry.metadata),
        )
    ).casefold().replace("ё", "е")
    return sum(1 for phrase in phrases if phrase in context)


def _heading_text(metadata: dict[str, Any]) -> str:
    heading = metadata.get("source_heading_path") or []
    if isinstance(heading, (list, tuple)):
        return " ".join(str(item) for item in heading)
    return str(heading or "")


@lru_cache(maxsize=4)
def _source_aspect_catalog(seed_path: Path) -> tuple[SourceAspectEntry, ...]:
    try:
        rows = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()

    entries: list[SourceAspectEntry] = []
    previous_by_document: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip().casefold() != "published":
            continue
        if str(row.get("source_type") or "").strip().casefold() != "yonote":
            continue
        topic = str(row.get("topic") or "").strip()
        if not topic:
            continue
        document_key = _document_key(str(row.get("chunk_id") or ""))
        previous = previous_by_document.get(document_key)
        source_text = str(row.get("text_clean") or row.get("text") or "")
        aspects = infer_source_aspects(row, source_text)
        if not aspects:
            previous_by_document[document_key] = row
            continue
        context_text = (
            _row_heading_context(previous)
            if previous is not None and KnowledgeAspect.DATES in aspects
            else ""
        )
        entries.append(
            SourceAspectEntry(
                topic=topic,
                category=str(row.get("category") or "").strip(),
                forum_normalized=str(row.get("forum_normalized") or "").strip(),
                aspects=aspects,
                source_text=source_text,
                context_text=context_text,
                metadata=row,
            )
        )
        previous_by_document[document_key] = row
    return tuple(entries)


def _document_key(chunk_id: str) -> str:
    return re.sub(r"_s\d{4}(?:_.*)?$", "", chunk_id)


def _row_heading_context(row: dict[str, Any]) -> str:
    heading = row.get("source_heading_path") or []
    heading_text = (
        " ".join(str(value) for value in heading)
        if isinstance(heading, (list, tuple))
        else str(heading or "")
    )
    return " ".join(
        (
            str(row.get("topic") or "").replace("_", " "),
            str(row.get("intent_name") or ""),
            heading_text,
        )
    )
