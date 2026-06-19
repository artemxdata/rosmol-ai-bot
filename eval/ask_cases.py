from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

IGNORED_SOURCE_CATEGORY_PREFIXES = {"fallback", "fallback_condition"}
CATEGORY_QUERY_CONFLICT_MARKERS = (
    " смен",
    "смене",
    "смены",
    "сменах",
    "бирюс",
    "форум",
    "мероприят",
)


def build_seed_ask_cases(
    records: list[dict[str, Any]],
    max_cases: int = 50,
    user_prefix: str = "ask-eval",
    per_category_limit: int | None = None,
    per_forum_limit: int = 3,
) -> list[dict[str, Any]]:
    selected = select_balanced_records(
        records,
        max_cases=max_cases,
        per_category_limit=per_category_limit,
        per_forum_limit=per_forum_limit,
    )
    return [
        _case_from_record(record, index, user_prefix)
        for index, record in enumerate(selected, 1)
    ]


def select_balanced_records(
    records: list[dict[str, Any]],
    max_cases: int,
    per_category_limit: int | None = None,
    per_forum_limit: int = 3,
) -> list[dict[str, Any]]:
    buckets: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for record in records:
        if not _is_eligible(record):
            continue
        category = str(record.get("category") or "unknown")
        buckets[category].append(record)

    category_order = sorted(buckets, key=lambda key: (-len(buckets[key]), key))
    category_counts: Counter[str] = Counter()
    forum_counts: Counter[tuple[str, str]] = Counter()
    selected: list[dict[str, Any]] = []

    while len(selected) < max_cases and any(buckets.values()):
        progressed = False
        for category in category_order:
            if len(selected) >= max_cases:
                break
            if per_category_limit is not None and category_counts[category] >= per_category_limit:
                buckets[category].clear()
                continue

            record = _pop_next_allowed_record(
                buckets[category],
                category=category,
                forum_counts=forum_counts,
                per_forum_limit=per_forum_limit,
            )
            if record is None:
                continue

            selected.append(record)
            category_counts[category] += 1
            forum = str(record.get("forum_normalized") or "")
            if forum:
                forum_counts[(category, forum)] += 1
            progressed = True

        if not progressed:
            break

    return selected


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    category_counts: Counter[str] = Counter()
    forum_counts: Counter[str] = Counter()
    tag_counts: Counter[str] = Counter()
    for case in cases:
        for tag in case.get("tags", []):
            tag_counts[tag] += 1
            if tag.startswith("category:"):
                category_counts[tag.removeprefix("category:")] += 1
            elif tag.startswith("forum:"):
                forum_counts[tag.removeprefix("forum:")] += 1

    return {
        "cases_total": len(cases),
        "category_counts": dict(category_counts),
        "forum_counts_top": dict(forum_counts.most_common(20)),
        "tag_counts_top": dict(tag_counts.most_common(20)),
    }


def write_cases(path: Path, cases: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")


def _case_from_record(record: dict[str, Any], index: int, user_prefix: str) -> dict[str, Any]:
    chunk_id = str(record["chunk_id"])
    tags = ["seed_balanced", f"category:{record.get('category') or 'unknown'}"]
    if record.get("forum_normalized"):
        tags.append(f"forum:{record['forum_normalized']}")
    if record.get("topic"):
        tags.append(f"topic:{record['topic']}")

    return {
        "id": f"seed_balanced::{chunk_id}",
        "query": seed_smoke_query(record),
        "user_id": f"{user_prefix}-{index}",
        "channel": "api",
        "expected_chunk_ids": [chunk_id],
        "expected_answer_contains": [],
        "expected_escalated": None,
        "expected_escalation_reason": None,
        "expected_generator_model": None,
        "tags": tags,
    }


def _pop_next_allowed_record(
    bucket: deque[dict[str, Any]],
    *,
    category: str,
    forum_counts: Counter[tuple[str, str]],
    per_forum_limit: int,
) -> dict[str, Any] | None:
    deferred: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    while bucket:
        record = bucket.popleft()
        forum = str(record.get("forum_normalized") or "")
        if forum and forum_counts[(category, forum)] >= per_forum_limit:
            deferred.append(record)
            continue
        selected = record
        break

    bucket.extendleft(reversed(deferred))
    return selected


def _is_eligible(record: dict[str, Any]) -> bool:
    return record.get("status") == "published" and bool(seed_smoke_query(record))


def seed_smoke_query(record: dict[str, Any]) -> str:
    examples = record.get("intent_examples") or []
    if examples:
        example = _select_seed_example(record, examples)
        prefix = _seed_query_prefix(record)
        return " ".join(part for part in [str(prefix), example] if part).strip()
    intent = record.get("intent_name")
    if intent:
        prefix = _seed_query_prefix(record)
        return " ".join(part for part in [str(prefix), str(intent)] if part).strip()
    return str(record.get("text_clean") or "")[:160]


def _select_seed_example(record: dict[str, Any], examples: list[Any]) -> str:
    cleaned = [str(example).strip() for example in examples if str(example).strip()]
    if not cleaned:
        return ""

    if _clean_optional(record.get("category")).casefold() == "форумы":
        return cleaned[0]

    for example in cleaned:
        if not _has_category_query_conflict(example):
            return example
    return cleaned[0]


def _has_category_query_conflict(example: str) -> bool:
    normalized = f" {example.casefold()} "
    return any(marker in normalized for marker in CATEGORY_QUERY_CONFLICT_MARKERS)


def _seed_query_prefix(record: dict[str, Any]) -> str:
    category = _clean_optional(record.get("category")).casefold()
    forum = _clean_optional(record.get("forum_normalized"))
    if forum and (not category or category == "форумы"):
        return forum

    if category and category != "форумы":
        return ""

    source_category = _clean_optional(record.get("source_category"))
    if source_category.casefold() in IGNORED_SOURCE_CATEGORY_PREFIXES:
        return ""
    return source_category


def _clean_optional(value: object) -> str:
    return str(value or "").strip()
