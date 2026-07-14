from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "data" / "forums_registry.json"
KB_SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "knowledge_base_seed.json"
NON_WORD_PATTERN = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)

# Yonote contains a few navigation headings in ``forum_normalized``. Treating them as
# event names would make ordinary phrases such as "платформы" over-filter retrieval.
GENERIC_SOURCE_LABELS = frozenset(
    {
        "конкурсные отборы",
        "мероприятия вне треков",
        "о росмолодежи",
        "платформы",
        "рабочей молодежи",
        "структура и направления",
    }
)


def detect_forum_from_text(text: str, registry_path: Path = REGISTRY_PATH) -> str | None:
    forums = detect_forums_from_text(text, registry_path)
    return forums[0] if forums else None


def detect_forums_from_text(text: str, registry_path: Path = REGISTRY_PATH) -> list[str]:
    normalized_text = f" {_normalize_for_match(text)} "
    if not normalized_text.strip():
        return []

    detected: list[str] = []
    seen: set[str] = set()
    matched_spans: list[tuple[int, int]] = []
    for alias, normalized_forum in _forum_aliases(registry_path):
        spans = [
            span
            for span in _alias_spans(normalized_text, alias)
            if not _span_is_contained(span, matched_spans)
        ]
        if not spans:
            continue
        matched_spans.extend(spans)
        if normalized_forum in seen:
            continue
        detected.append(normalized_forum)
        seen.add(normalized_forum)
    return detected


def canonicalize_forum_name(
    value: str | None,
    registry_path: Path = REGISTRY_PATH,
) -> str | None:
    """Return the canonical event name for registry and Yonote spelling variants."""

    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    normalized = _normalize_for_match(raw_value)
    return dict(_forum_aliases(registry_path)).get(normalized, raw_value)


def forum_filter_values(
    value: str | None,
    registry_path: Path = REGISTRY_PATH,
) -> tuple[str, ...]:
    """Return payload values equivalent to the canonical forum name.

    Qdrant keyword filters are exact. The KB can legitimately contain legacy XLSX
    spelling and a newer Yonote spelling, so retrieval must query both variants.
    """

    canonical = canonicalize_forum_name(value, registry_path)
    if not canonical:
        return ()
    values = _forum_source_values(registry_path).get(canonical, ())
    return values or (canonical,)


def forums_are_equivalent(
    left: str | None,
    right: str | None,
    registry_path: Path = REGISTRY_PATH,
) -> bool:
    left_canonical = canonicalize_forum_name(left, registry_path)
    right_canonical = canonicalize_forum_name(right, registry_path)
    return bool(left_canonical and left_canonical == right_canonical)


@lru_cache(maxsize=8)
def _forum_aliases(registry_path: Path) -> tuple[tuple[str, str], ...]:
    aliases: dict[str, str] = {}
    for item in _raw_registry(registry_path):
        normalized_forum = str(item.get("normalized") or item.get("name") or "").strip()
        if not normalized_forum:
            continue
        for raw_alias in _raw_aliases(item):
            alias = _normalize_for_match(raw_alias)
            if alias:
                aliases[alias] = normalized_forum

    if registry_path.resolve() == REGISTRY_PATH.resolve():
        for source_forum in _seed_forum_names(KB_SEED_PATH):
            alias = _normalize_for_match(source_forum)
            if not _is_distinct_source_label(alias):
                continue
            aliases.setdefault(alias, aliases.get(alias, source_forum))
    return tuple(sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True))


@lru_cache(maxsize=8)
def _forum_source_values(registry_path: Path) -> dict[str, tuple[str, ...]]:
    alias_map = dict(_forum_aliases(registry_path))
    values: dict[str, set[str]] = {}

    for item in _raw_registry(registry_path):
        canonical = str(item.get("normalized") or item.get("name") or "").strip()
        if not canonical:
            continue
        values.setdefault(canonical, set()).update(
            raw_alias.strip() for raw_alias in _raw_aliases(item) if raw_alias.strip()
        )

    if registry_path.resolve() == REGISTRY_PATH.resolve():
        for source_forum in _seed_forum_names(KB_SEED_PATH):
            normalized = _normalize_for_match(source_forum)
            if not _is_distinct_source_label(normalized):
                continue
            canonical = alias_map.get(normalized, source_forum)
            values.setdefault(canonical, set()).add(source_forum)

    return {
        canonical: tuple(sorted(raw_values | {canonical}, key=lambda item: (len(item), item)))
        for canonical, raw_values in values.items()
    }


@lru_cache(maxsize=8)
def _raw_registry(registry_path: Path) -> tuple[dict[str, Any], ...]:
    raw_registry = json.loads(registry_path.read_text(encoding="utf-8"))
    return tuple(item for item in raw_registry if isinstance(item, dict))


@lru_cache(maxsize=4)
def _seed_forum_names(seed_path: Path) -> tuple[str, ...]:
    if not seed_path.exists():
        return ()
    raw_seed = json.loads(seed_path.read_text(encoding="utf-8"))
    return tuple(
        sorted(
            {
                str(item.get("forum_normalized") or "").strip()
                for item in raw_seed
                if isinstance(item, dict)
                and str(item.get("status") or "published") == "published"
                and str(item.get("forum_normalized") or "").strip()
            }
        )
    )


def _is_distinct_source_label(normalized: str) -> bool:
    if not normalized or normalized in GENERIC_SOURCE_LABELS:
        return False
    return len(normalized) >= 6


def _raw_aliases(item: dict[str, Any]) -> list[str]:
    values = [str(item.get("normalized") or ""), str(item.get("name") or "")]
    values.extend(str(alias) for alias in item.get("aliases") or [])
    return values


def _alias_spans(normalized_text: str, alias: str) -> list[tuple[int, int]]:
    pattern = f" {alias} "
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = normalized_text.find(pattern, start)
        if index < 0:
            return spans
        alias_start = index + 1
        spans.append((alias_start, alias_start + len(alias)))
        start = index + len(pattern) - 1


def _span_is_contained(span: tuple[int, int], containers: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(
        container_start <= start and end <= container_end
        for container_start, container_end in containers
    )


def _normalize_for_match(text: str) -> str:
    normalized = str(text or "").casefold().replace("ё", "е").replace("ë", "е")
    normalized = NON_WORD_PATTERN.sub(" ", normalized)
    return " ".join(normalized.split())
