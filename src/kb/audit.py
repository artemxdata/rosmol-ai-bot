from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

REQUIRED_METADATA = ("category", "topic", "source_type", "source_file")


def audit_seed_records(
    records: list[dict[str, Any]],
    *,
    forum_registry: list[dict[str, Any]] | None = None,
    min_forum_chunks: int = 0,
    min_forum_topics: int = 0,
) -> dict[str, Any]:
    findings = [
        *_find_trailing_export_quotes(records),
        *_find_template_artifacts(records),
        *_find_missing_metadata(records),
        *_find_grant_records_with_forum(records),
        *_find_duplicate_texts(records),
        *_find_forum_coverage_findings(
            records,
            forum_registry=forum_registry,
            min_forum_chunks=min_forum_chunks,
            min_forum_topics=min_forum_topics,
        ),
    ]
    errors = sum(1 for finding in findings if finding["severity"] == "error")
    warnings = sum(1 for finding in findings if finding["severity"] == "warning")
    return {
        "records_total": len(records),
        "errors": errors,
        "warnings": warnings,
        "summary": _summarize_records(records),
        "findings": findings,
    }


def _summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    category_counts: Counter[str] = Counter()
    forum_counts: Counter[str] = Counter()
    source_type_counts: Counter[str] = Counter()
    source_file_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    generic_records = 0
    char_counts: list[int] = []

    for record in records:
        category_counts[_field_or_missing(record, "category")] += 1
        source_type_counts[_field_or_missing(record, "source_type")] += 1
        source_file_counts[_field_or_missing(record, "source_file")] += 1
        status_counts[_field_or_missing(record, "status")] += 1

        forum = str(record.get("forum_normalized") or record.get("forum") or "").strip()
        if forum:
            forum_counts[forum] += 1
        else:
            generic_records += 1

        char_counts.append(_record_char_count(record))

    return {
        "category_counts": dict(category_counts.most_common()),
        "status_counts": dict(status_counts.most_common()),
        "source_type_counts": dict(source_type_counts.most_common()),
        "source_file_counts_top": dict(source_file_counts.most_common(20)),
        "forum_counts_top": dict(forum_counts.most_common(20)),
        "forums_total": len(forum_counts),
        "generic_records_count": generic_records,
        "char_count": _char_count_summary(char_counts),
    }


def _find_forum_coverage_findings(
    records: list[dict[str, Any]],
    *,
    forum_registry: list[dict[str, Any]] | None,
    min_forum_chunks: int,
    min_forum_topics: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    stats = _forum_coverage_stats(records)
    registry_forums = _registry_forums(forum_registry or [])

    if registry_forums:
        missing = sorted(registry_forums - set(stats))
        if missing:
            findings.append(
                {
                    "code": "registry_forum_without_published_chunks",
                    "severity": "warning",
                    "message": "forum is present in registry but has no published KB chunks",
                    "count": len(missing),
                    "forums": missing,
                }
            )

        extra = sorted(set(stats) - registry_forums)
        if extra:
            findings.append(
                {
                    "code": "forum_not_in_registry",
                    "severity": "warning",
                    "message": "published KB chunks use a forum missing from forums_registry",
                    "count": len(extra),
                    "forums": extra,
                }
            )

    if min_forum_chunks > 0:
        low_chunks = [
            {"forum": forum, "chunks": data["chunks"]}
            for forum, data in sorted(stats.items())
            if data["chunks"] < min_forum_chunks
        ]
        if low_chunks:
            findings.append(
                {
                    "code": "low_forum_chunk_coverage",
                    "severity": "warning",
                    "message": "forum has fewer published chunks than the configured threshold",
                    "threshold": min_forum_chunks,
                    "count": len(low_chunks),
                    "forums": low_chunks[:50],
                }
            )

    if min_forum_topics > 0:
        low_topics = [
            {"forum": forum, "topics": len(data["topics"])}
            for forum, data in sorted(stats.items())
            if len(data["topics"]) < min_forum_topics
        ]
        if low_topics:
            findings.append(
                {
                    "code": "low_forum_topic_coverage",
                    "severity": "warning",
                    "message": "forum has fewer published topics than the configured threshold",
                    "threshold": min_forum_topics,
                    "count": len(low_topics),
                    "forums": low_topics[:50],
                }
            )

    return findings


def _forum_coverage_stats(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for record in records:
        if str(record.get("status") or "published") != "published":
            continue
        forum = str(record.get("forum_normalized") or record.get("forum") or "").strip()
        if not forum:
            continue
        data = stats.setdefault(forum, {"chunks": 0, "topics": set()})
        data["chunks"] += 1
        topic = str(record.get("topic") or "").strip()
        if topic:
            data["topics"].add(topic)
    return stats


def _registry_forums(forum_registry: list[dict[str, Any]]) -> set[str]:
    forums: set[str] = set()
    for item in forum_registry:
        if not isinstance(item, dict):
            continue
        forum = str(item.get("normalized") or item.get("name") or "").strip()
        if forum:
            forums.add(forum)
    return forums


def _find_trailing_export_quotes(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunk_ids = [
        str(record.get("chunk_id"))
        for record in records
        if str(record.get("text_clean") or "").endswith("'")
    ]
    if not chunk_ids:
        return []
    return [
        {
            "code": "trailing_export_quote",
            "severity": "error",
            "message": "text_clean ends with an ASCII export quote artifact",
            "count": len(chunk_ids),
            "chunk_ids": chunk_ids[:50],
        }
    ]


def _find_template_artifacts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pattern = re.compile(r"({{.*?}}|{%.*?%}|\|random)", flags=re.DOTALL)
    chunk_ids = [
        str(record.get("chunk_id"))
        for record in records
        if pattern.search(str(record.get("text_clean") or ""))
    ]
    if not chunk_ids:
        return []
    return [
        {
            "code": "template_artifact",
            "severity": "warning",
            "message": "text_clean contains a template expression that should be reviewed",
            "count": len(chunk_ids),
            "chunk_ids": chunk_ids[:50],
        }
    ]


def _find_missing_metadata(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for field in REQUIRED_METADATA:
        chunk_ids = [str(record.get("chunk_id")) for record in records if not record.get(field)]
        if chunk_ids:
            findings.append(
                {
                    "code": f"missing_{field}",
                    "severity": "error",
                    "message": f"required metadata field is missing: {field}",
                    "count": len(chunk_ids),
                    "chunk_ids": chunk_ids[:50],
                }
            )
    return findings


def _find_grant_records_with_forum(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunk_ids = [
        str(record.get("chunk_id"))
        for record in records
        if record.get("category") == "гранты" and record.get("forum_normalized")
    ]
    if not chunk_ids:
        return []
    return [
        {
            "code": "grant_record_has_forum",
            "severity": "warning",
            "message": "grant-category record has forum_normalized; review taxonomy",
            "count": len(chunk_ids),
            "chunk_ids": chunk_ids[:50],
        }
    ]


def _find_duplicate_texts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for record in records:
        text = _normalize_text(str(record.get("text_clean") or record.get("text") or ""))
        if text:
            groups[text].append(str(record.get("chunk_id")))

    duplicates = [chunk_ids for chunk_ids in groups.values() if len(chunk_ids) > 1]
    if not duplicates:
        return []
    duplicate_count = sum(len(chunk_ids) for chunk_ids in duplicates)
    return [
        {
            "code": "duplicate_text",
            "severity": "warning",
            "message": "multiple chunks have identical normalized text",
            "count": duplicate_count,
            "groups": duplicates[:20],
        }
    ]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip()


def _field_or_missing(record: dict[str, Any], field: str) -> str:
    value = str(record.get(field) or "").strip()
    return value or "missing"


def _record_char_count(record: dict[str, Any]) -> int:
    explicit = record.get("char_count")
    if isinstance(explicit, int) and explicit >= 0:
        return explicit
    return len(str(record.get("text_clean") or record.get("text") or ""))


def _char_count_summary(values: list[int]) -> dict[str, int | float | None]:
    if not values:
        return {"min": None, "p50": None, "p95": None, "max": None, "avg": None}
    sorted_values = sorted(values)
    return {
        "min": sorted_values[0],
        "p50": _percentile(sorted_values, 0.50),
        "p95": _percentile(sorted_values, 0.95),
        "max": sorted_values[-1],
        "avg": round(sum(sorted_values) / len(sorted_values), 2),
    }


def _percentile(sorted_values: list[int], percentile: float) -> int:
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = round((len(sorted_values) - 1) * percentile)
    return sorted_values[index]
