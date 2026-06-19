from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "data" / "forums_registry.json"
NON_WORD_PATTERN = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)


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


@lru_cache(maxsize=8)
def _forum_aliases(registry_path: Path) -> tuple[tuple[str, str], ...]:
    raw_registry = json.loads(registry_path.read_text(encoding="utf-8"))
    aliases: dict[str, str] = {}
    for item in raw_registry:
        normalized_forum = str(item.get("normalized") or item.get("name") or "").strip()
        if not normalized_forum:
            continue
        for raw_alias in _raw_aliases(item):
            alias = _normalize_for_match(raw_alias)
            if alias:
                aliases[alias] = normalized_forum
    return tuple(sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True))


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
    normalized = str(text or "").casefold().replace("ё", "е")
    normalized = NON_WORD_PATTERN.sub(" ", normalized)
    return " ".join(normalized.split())
