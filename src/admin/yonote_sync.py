from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from scripts.build_yonote_kb_seed import merge_records
from scripts.index_kb import validate_seed_items
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
    validate_seed_items(merged_records)
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
    validate_seed_items(merged_records)
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

    if not base_url:
        raise YonoteSyncConfigError("YONOTE_BASE_URL is not configured")
    if not api_token:
        raise YonoteSyncConfigError("YONOTE_API_TOKEN is not configured")

    with YonoteClient(
        base_url=base_url,
        api_token=api_token,
        timeout_seconds=timeout_seconds,
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
        "category_counts": _count_field(fresh_yonote_records, "category"),
        "forum_counts": _count_field(fresh_yonote_records, "forum_normalized"),
        "message": _human_message(
            applied=applied,
            changed=len(changed_ids),
            added=len(added_ids),
            removed=len(removed_ids),
        ),
    }


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
