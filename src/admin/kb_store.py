from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.index_kb import validate_seed_items

VALID_STATUSES = {"draft", "published", "archived"}


def list_chunks(
    path: Path,
    *,
    status: str | None = None,
    category: str | None = None,
    forum: str | None = None,
    source_type: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    records = load_seed_records(path)
    filtered = [
        record
        for record in records
        if _matches(
            record,
            status=status,
            category=category,
            forum=forum,
            source_type=source_type,
            q=q,
        )
    ]
    return {
        "total": len(filtered),
        "limit": limit,
        "offset": offset,
        "items": [_compact_record(record) for record in filtered[offset : offset + limit]],
    }


def get_chunk(path: Path, chunk_id: str) -> dict[str, Any] | None:
    for record in load_seed_records(path):
        if str(record.get("chunk_id") or "") == chunk_id:
            return record
    return None


def update_chunk(
    path: Path,
    chunk_id: str,
    *,
    status: str | None = None,
    text_clean: str | None = None,
) -> dict[str, Any]:
    records = load_seed_records(path)
    target: dict[str, Any] | None = None
    for record in records:
        if str(record.get("chunk_id") or "") == chunk_id:
            target = record
            break
    if target is None:
        raise KeyError(chunk_id)

    if status is not None:
        normalized_status = status.strip()
        if normalized_status not in VALID_STATUSES:
            raise ValueError("status must be draft, published or archived")
        target["status"] = normalized_status
    if text_clean is not None:
        normalized_text = text_clean.strip()
        if not normalized_text:
            raise ValueError("text_clean must not be empty")
        target["text_clean"] = normalized_text

    validate_seed_items(records)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load_seed_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError("knowledge base seed must contain a JSON array")
    if not all(isinstance(item, dict) for item in payload):
        raise ValueError("knowledge base seed records must be JSON objects")
    return [dict(item) for item in payload]


def _matches(
    record: dict[str, Any],
    *,
    status: str | None,
    category: str | None,
    forum: str | None,
    source_type: str | None,
    q: str | None,
) -> bool:
    if status and str(record.get("status") or "published") != status:
        return False
    if category and str(record.get("category") or "") != category:
        return False
    if forum and str(record.get("forum_normalized") or "") != forum:
        return False
    if source_type and str(record.get("source_type") or "") != source_type:
        return False
    if q and q.casefold() not in _search_haystack(record):
        return False
    return True


def _compact_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": record.get("chunk_id"),
        "status": record.get("status", "published"),
        "category": record.get("category"),
        "forum_normalized": record.get("forum_normalized"),
        "topic": record.get("topic"),
        "intent_name": record.get("intent_name"),
        "source_type": record.get("source_type"),
        "text_preview": _preview(record.get("text_clean") or record.get("text") or ""),
    }


def _search_haystack(record: dict[str, Any]) -> str:
    values = [
        record.get("chunk_id"),
        record.get("text_clean"),
        record.get("text"),
        record.get("forum_normalized"),
        record.get("category"),
        record.get("topic"),
        record.get("intent_name"),
        record.get("source_type"),
    ]
    examples = record.get("intent_examples") or []
    values.extend(examples if isinstance(examples, list) else [])
    return " ".join(str(value or "") for value in values).casefold()


def _preview(text: str, *, limit: int = 180) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"
