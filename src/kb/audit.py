from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

REQUIRED_METADATA = ("category", "topic", "source_type", "source_file")

# These KMOЦ overview sections intentionally enumerate related forums. They are not
# event-answer chunks copied under the wrong metadata, so the high-confidence conflict
# gate must not reject them. Nested parent/subevent names are handled generically below.
FORUM_TEXT_CROSS_REFERENCE_ALLOWLIST = frozenset(
    {
        "xlsx_category_r0662_o_meropriyatii",
        "xlsx_category_r0666_oplata_proezda",
        "yonote_api_1azaurjxgj_s0003_programmy_i_meropriyatiya",
        "yonote_api_1azaurjxgj_s0007_svyaz_s_platformoy",
        "yonote_api_gbj3t9ecv2_s0001_opisanie",
        "yonote_api_ojnqfaxmmm_s0004_obrazovatelnye_meropriyatiya",
        "yonote_api_ojnqfaxmmm_s0005_infrastruktura",
        "yonote_api_s25v8pw7fx_s0005_kruglogodichnyy_cikl_programm_centra_vklyuchaet",
        "yonote_api_s25v8pw7fx_s0006_populyarizaciya_nauki",
        "yonote_api_s25v8pw7fx_s0014_programma_postsoprovozhdeniya_polyus",
        "yonote_api_s25v8pw7fx_s0020_prodvizhenie_oflayn_organizaciya_lokalnyh_aktivnostey_v_regi",
        "yonote_api_s25v8pw7fx_s0021_vserossiyskiy_forum_molodyh_uchenyh_polyus",
        "yonote_api_s25v8pw7fx_s0032_partnerstva_i_predstavitelstvo",
        "yonote_api_s25v8pw7fx_s0034_plany_centra_na_2025_god",
    }
)


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
        *_find_private_source_references(records),
        *_find_short_published_texts(records),
        *_find_offtopic_records_with_context(records),
        *_find_grant_records_with_forum(records),
        *_find_duplicate_texts(records),
        *semantic_integrity_findings(records, forum_registry=forum_registry),
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


def semantic_integrity_findings(
    records: list[dict[str, Any]],
    *,
    forum_registry: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return blocking semantic defects that are unsafe to publish or index."""

    return [
        *_find_forum_text_conflicts(records, forum_registry or []),
        *_find_malformed_links(records),
        *_find_suspicious_link_domains(records),
        *_find_unresolved_social_link_placeholders(records),
    ]


def _find_forum_text_conflicts(
    records: list[dict[str, Any]],
    forum_registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    alias_to_canonical: dict[str, str] = {}
    detectable_names: dict[str, set[str]] = defaultdict(set)
    for item in forum_registry:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("normalized") or item.get("name") or "").strip()
        canonical_key = _normalize_for_match(canonical)
        if not canonical_key:
            continue
        for value in [canonical, str(item.get("name") or ""), *(item.get("aliases") or [])]:
            normalized = _normalize_for_match(str(value))
            if normalized:
                alias_to_canonical[normalized] = canonical
        for value in [canonical, str(item.get("name") or ""), *(item.get("aliases") or [])]:
            normalized = _normalize_for_match(value)
            if normalized:
                detectable_names[canonical].add(normalized)

    conflicts: list[dict[str, str]] = []
    for record in records:
        if str(record.get("status") or "published") != "published":
            continue
        chunk_id = str(record.get("chunk_id") or "")
        if chunk_id in FORUM_TEXT_CROSS_REFERENCE_ALLOWLIST:
            continue
        forum = str(record.get("forum_normalized") or record.get("forum") or "").strip()
        if not forum:
            continue
        record_forum = alias_to_canonical.get(_normalize_for_match(forum), forum)
        text = _normalize_for_match(str(record.get("text_clean") or record.get("text") or ""))
        for mentioned_forum, names in detectable_names.items():
            if mentioned_forum == record_forum:
                continue
            if _event_names_are_nested(record_forum, mentioned_forum):
                continue
            if any(_explicit_event_reference(text, name) for name in names):
                conflicts.append(
                    {
                        "chunk_id": chunk_id,
                        "record_forum": forum,
                        "mentioned_forum": mentioned_forum,
                    }
                )

    if not conflicts:
        return []
    return [
        {
            "code": "forum_text_conflict",
            "severity": "error",
            "message": "published chunk metadata conflicts with an explicitly named event",
            "count": len(conflicts),
            "records": conflicts[:50],
        }
    ]


def _explicit_event_reference(text: str, event_name: str) -> bool:
    return bool(
        re.search(
            rf"\b(?:форум\w*|фестивал\w*|мероприят\w*|слет\w*)\s+"
            rf"{re.escape(event_name)}(?:\b|$)",
            text,
        )
    )


def _event_names_are_nested(left: str, right: str) -> bool:
    left_normalized = f" {_normalize_for_match(left)} "
    right_normalized = f" {_normalize_for_match(right)} "
    return left_normalized in right_normalized or right_normalized in left_normalized


def _find_malformed_links(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    malformed: list[dict[str, str]] = []
    for record in records:
        if str(record.get("status") or "published") != "published":
            continue
        chunk_id = str(record.get("chunk_id") or "")
        text = str(record.get("text_clean") or record.get("text") or "")
        if any(
            pattern.search(text)
            for pattern in (
                re.compile(r"\[[^\]]+\]\(https?://[^)]*[.,;:!?]\)"),
                re.compile(r"\[https?://[^\]\n]+\|[^\]\n]+\]"),
                re.compile(r"https?://[^\s<>()]*['\"]\w"),
            )
        ):
            malformed.append({"chunk_id": chunk_id, "location": "text"})
        for link in record.get("links") or []:
            value = str(link).strip()
            if re.search(r"[\s<>\[\]()|'\"]", value):
                malformed.append({"chunk_id": chunk_id, "location": "links"})
                break

    if not malformed:
        return []
    return [
        {
            "code": "malformed_link",
            "severity": "error",
            "message": "published chunk contains malformed Markdown or link metadata",
            "count": len(malformed),
            "records": malformed[:50],
        }
    ]


def _find_suspicious_link_domains(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunk_ids: list[str] = []
    typo_pattern = re.compile(r"\bevents\.myrosmol\.rru\b", flags=re.IGNORECASE)
    for record in records:
        if str(record.get("status") or "published") != "published":
            continue
        values = [
            str(record.get("text_clean") or record.get("text") or ""),
            *(str(link) for link in record.get("links") or []),
        ]
        if any(typo_pattern.search(value) for value in values):
            chunk_ids.append(str(record.get("chunk_id") or ""))
    if not chunk_ids:
        return []
    return [
        {
            "code": "suspicious_link_domain",
            "severity": "error",
            "message": "published chunk contains a known malformed service domain",
            "count": len(chunk_ids),
            "chunk_ids": chunk_ids[:50],
        }
    ]


def _find_unresolved_social_link_placeholders(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chunk_ids = [
        str(record.get("chunk_id") or "")
        for record in records
        if str(record.get("status") or "published") == "published"
        and re.search(
            r"\bVK\s+TG\b",
            str(record.get("text_clean") or record.get("text") or ""),
            flags=re.IGNORECASE,
        )
    ]
    if not chunk_ids:
        return []
    return [
        {
            "code": "unresolved_social_link_placeholder",
            "severity": "error",
            "message": "published chunk exposes unresolved VK/TG link labels as an answer",
            "count": len(chunk_ids),
            "chunk_ids": chunk_ids[:50],
        }
    ]


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
        if "грант" in forum.casefold():
            continue
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


def _find_private_source_references(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    private_markers = ("data/private", "data\\private", "tickets", "hde")
    chunk_ids = [
        str(record.get("chunk_id"))
        for record in records
        if any(
            marker in str(record.get(field) or "").casefold().replace("\\", "/")
            for field in ("source_file", "source_path", "source")
            for marker in private_markers
        )
    ]
    if not chunk_ids:
        return []
    return [
        {
            "code": "private_source_reference",
            "severity": "error",
            "message": "KB record references private ticket/HDE source material",
            "count": len(chunk_ids),
            "chunk_ids": chunk_ids[:50],
        }
    ]


def _find_short_published_texts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunk_ids = [
        str(record.get("chunk_id"))
        for record in records
        if str(record.get("status") or "published") == "published"
        and str(record.get("category") or "") != "навигация"
        and 0 < _record_char_count(record) < 10
    ]
    if not chunk_ids:
        return []
    return [
        {
            "code": "short_published_text",
            "severity": "warning",
            "message": "published non-navigation chunk is very short and may be weak for RAG",
            "count": len(chunk_ids),
            "chunk_ids": chunk_ids[:50],
        }
    ]


def _find_offtopic_records_with_context(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunk_ids = [
        str(record.get("chunk_id"))
        for record in records
        if str(record.get("topic") or "") == "offtop_ne_po_rosmolodezhi"
        and (record.get("forum") or record.get("forum_normalized"))
    ]
    if not chunk_ids:
        return []
    return [
        {
            "code": "offtopic_record_has_context",
            "severity": "warning",
            "message": "off-topic fallback record should not carry forum context",
            "count": len(chunk_ids),
            "chunk_ids": chunk_ids[:50],
        }
    ]


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


def _normalize_for_match(value: str) -> str:
    normalized = value.casefold().replace("ё", "е")
    normalized = re.sub(r"[^0-9a-zа-я]+", " ", normalized)
    return " ".join(normalized.split())


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
