from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from scripts.build_yonote_kb_seed import merge_records
from scripts.index_kb import (
    load_forum_registry,
    validate_seed_items,
    validate_semantic_seed_items,
)
from scripts.sync_yonote_kb import (
    YonoteClient,
    build_records_from_api_documents,
    load_yonote_documents,
    selected_collection_names,
)

COMPARE_FIELDS = (
    "text_clean",
    "status",
    "category",
    "forum_normalized",
    "topic",
    "intent_name",
    "source_url",
    "source_document_updated_at",
)


class YonoteSyncConfigError(RuntimeError):
    pass


def preview_sync(
    seed_path: Path,
    settings: Any,
    *,
    limit_documents: int | None = None,
) -> dict[str, Any]:
    current_records = _load_seed_records(seed_path)
    documents, fresh_yonote_records = _load_fresh_yonote_records(
        settings,
        limit_documents=limit_documents,
    )
    merged_records = merge_records(
        current_records,
        fresh_yonote_records,
        replace_existing_yonote=True,
    )
    _validate_merged_seed(seed_path, merged_records)
    return _build_sync_report(
        current_records=current_records,
        fresh_yonote_records=fresh_yonote_records,
        merged_records=merged_records,
        documents_count=len(documents),
        applied=False,
        seed_path=seed_path,
    )


def apply_sync(
    seed_path: Path,
    settings: Any,
    *,
    limit_documents: int | None = None,
) -> dict[str, Any]:
    current_records = _load_seed_records(seed_path)
    documents, fresh_yonote_records = _load_fresh_yonote_records(
        settings,
        limit_documents=limit_documents,
    )
    merged_records = merge_records(
        current_records,
        fresh_yonote_records,
        replace_existing_yonote=True,
    )
    _validate_merged_seed(seed_path, merged_records)
    _write_seed_records(seed_path, merged_records)
    return _build_sync_report(
        current_records=current_records,
        fresh_yonote_records=fresh_yonote_records,
        merged_records=merged_records,
        documents_count=len(documents),
        applied=True,
        seed_path=seed_path,
    )


def _load_fresh_yonote_records(
    settings: Any,
    *,
    limit_documents: int | None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    base_url = str(getattr(settings, "yonote_base_url", "") or "").strip()
    api_token = str(getattr(settings, "yonote_api_token", "") or "").strip()
    timeout_seconds = float(getattr(settings, "yonote_request_timeout_seconds", 30.0))
    max_retries = int(getattr(settings, "yonote_max_retries", 2))
    min_request_interval_seconds = float(
        getattr(settings, "yonote_min_request_interval_seconds", 0.15)
    )

    if not base_url:
        raise YonoteSyncConfigError("YONOTE_BASE_URL is not configured")
    if not api_token:
        raise YonoteSyncConfigError("YONOTE_API_TOKEN is not configured")

    with YonoteClient(
        base_url=base_url,
        api_token=api_token,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        min_request_interval_seconds=min_request_interval_seconds,
    ) as client:
        documents = load_yonote_documents(
            client,
            selected_collection_names(),
            limit_documents=limit_documents,
        )

    records = build_records_from_api_documents(
        documents,
        base_url=base_url,
        extraction_date=date.today(),
    )
    validate_seed_items(records)
    return documents, records


def _validate_merged_seed(seed_path: Path, records: list[dict[str, Any]]) -> None:
    validate_seed_items(records)
    validate_semantic_seed_items(
        records,
        forum_registry=load_forum_registry(seed_path),
    )


def _build_sync_report(
    *,
    current_records: list[dict[str, Any]],
    fresh_yonote_records: list[dict[str, Any]],
    merged_records: list[dict[str, Any]],
    documents_count: int,
    applied: bool,
    seed_path: Path,
) -> dict[str, Any]:
    old_yonote_records = [
        record
        for record in current_records
        if str(record.get("source_type") or "") == "yonote"
    ]
    old_by_id = {str(record["chunk_id"]): record for record in old_yonote_records}
    fresh_by_id = {str(record["chunk_id"]): record for record in fresh_yonote_records}

    old_ids = set(old_by_id)
    fresh_ids = set(fresh_by_id)
    added_ids = sorted(fresh_ids - old_ids)
    removed_ids = sorted(old_ids - fresh_ids)
    changed_ids = sorted(
        chunk_id
        for chunk_id in old_ids & fresh_ids
        if _record_changed(old_by_id[chunk_id], fresh_by_id[chunk_id])
    )
    unchanged_count = len((old_ids & fresh_ids) - set(changed_ids))

    added_items = [_record_summary(fresh_by_id[chunk_id]) for chunk_id in added_ids]
    removed_items = [_record_summary(old_by_id[chunk_id]) for chunk_id in removed_ids]
    changed_items = [
        _changed_record_summary(old_by_id[chunk_id], fresh_by_id[chunk_id])
        for chunk_id in changed_ids
    ]

    return {
        "ok": True,
        "applied": applied,
        "seed_path": str(seed_path),
        "index_required": applied,
        "documents": documents_count,
        "current_records": len(current_records),
        "current_yonote_records": len(old_yonote_records),
        "fresh_yonote_records": len(fresh_yonote_records),
        "merged_records": len(merged_records),
        "added": len(added_ids),
        "changed": len(changed_ids),
        "removed": len(removed_ids),
        "unchanged": unchanged_count,
        "added_sample": added_ids[:10],
        "changed_sample": changed_ids[:10],
        "removed_sample": removed_ids[:10],
        "added_items": added_items,
        "changed_items": changed_items,
        "removed_items": removed_items,
        "category_counts": _count_field(fresh_yonote_records, "category"),
        "forum_counts": _count_field(fresh_yonote_records, "forum_normalized"),
        "collection_counts": _count_field(
            fresh_yonote_records,
            "source_collection_name",
        ),
        "message": _human_message(
            applied=applied,
            changed=len(changed_ids),
            added=len(added_ids),
            removed=len(removed_ids),
        ),
    }


def _record_summary(record: dict[str, Any]) -> dict[str, Any]:
    heading_path = record.get("source_heading_path")
    heading = ""
    if isinstance(heading_path, list):
        heading = " / ".join(str(item).strip() for item in heading_path if str(item).strip())

    title = str(record.get("intent_name") or "").strip()
    if not title and isinstance(heading_path, list) and heading_path:
        title = str(heading_path[-1] or "").strip()
    if not title:
        title = str(record.get("topic") or record.get("chunk_id") or "Без названия").strip()

    return {
        "chunk_id": str(record.get("chunk_id") or ""),
        "title": title,
        "heading": heading,
        "collection": str(record.get("source_collection_name") or "").strip(),
        "forum": str(record.get("forum_normalized") or "").strip(),
        "category": str(record.get("category") or "").strip(),
        "source_url": str(record.get("source_url") or "").strip(),
        "updated_at": str(
            record.get("source_document_updated_at") or record.get("updated_at") or ""
        ).strip(),
        "text_preview": _text_preview(record.get("text_clean") or record.get("text_raw") or ""),
    }


def _changed_record_summary(
    old: dict[str, Any],
    fresh: dict[str, Any],
) -> dict[str, Any]:
    summary = _record_summary(fresh)
    summary["changed_fields"] = [
        field
        for field in COMPARE_FIELDS
        if _normalize_compare(old.get(field)) != _normalize_compare(fresh.get(field))
    ]
    summary["before_text"] = _text_preview(
        old.get("text_clean") or old.get("text_raw") or ""
    )
    summary["after_text"] = _text_preview(
        fresh.get("text_clean") or fresh.get("text_raw") or ""
    )
    return summary


def _text_preview(value: Any, *, limit: int = 360) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _record_changed(old: dict[str, Any], fresh: dict[str, Any]) -> bool:
    return any(
        _normalize_compare(old.get(field)) != _normalize_compare(fresh.get(field))
        for field in COMPARE_FIELDS
    )


def _normalize_compare(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value or "").strip()


def _count_field(records: list[dict[str, Any]], field: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for record in records:
        value = str(record.get(field) or "unknown").strip() or "unknown"
        counter[value] += 1
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:30])


def _human_message(*, applied: bool, changed: int, added: int, removed: int) -> str:
    action = "Yonote applied to KB seed" if applied else "Yonote preview loaded"
    return f"{action}: added={added}, changed={changed}, removed={removed}"


def _load_seed_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("knowledge_base_seed.json must contain a JSON array of objects")
    return [dict(item) for item in payload]


def _write_seed_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)
