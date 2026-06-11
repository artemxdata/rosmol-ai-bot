from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

REQUIRED_METADATA = ("category", "topic", "source_type", "source_file")


def audit_seed_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    findings = [
        *_find_trailing_export_quotes(records),
        *_find_template_artifacts(records),
        *_find_missing_metadata(records),
        *_find_grant_records_with_forum(records),
        *_find_duplicate_texts(records),
    ]
    errors = sum(1 for finding in findings if finding["severity"] == "error")
    warnings = sum(1 for finding in findings if finding["severity"] == "warning")
    return {
        "records_total": len(records),
        "errors": errors,
        "warnings": warnings,
        "findings": findings,
    }


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
