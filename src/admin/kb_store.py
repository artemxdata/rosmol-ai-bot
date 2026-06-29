from __future__ import annotations

import json
from collections import Counter
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


def validate_seed(path: Path) -> dict[str, Any]:
    records = load_seed_records(path)
    validate_seed_items(records)
    return {
        "ok": True,
        "path": str(path),
        "valid_records": len(records),
        "status_counts": _count_field(records, "status", default="published"),
        "category_counts": _count_field(records, "category"),
        "forum_counts": _count_field(records, "forum_normalized"),
        "source_type_counts": _count_field(records, "source_type"),
    }


def find_related_eval_cases(
    cases_dir: Path,
    chunk_id: str,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    if not cases_dir.exists():
        return {"chunk_id": chunk_id, "total": 0, "limit": limit, "items": []}

    matches: list[dict[str, Any]] = []
    for path in sorted(cases_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        for case in _iter_eval_cases(payload):
            if chunk_id not in _case_chunk_ids(case):
                continue
            matches.append(_compact_eval_case(case, source_file=path.name))

    return {
        "chunk_id": chunk_id,
        "total": len(matches),
        "limit": limit,
        "items": matches[:limit],
    }


def load_quality_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("quality report must contain a JSON object")
    return payload


def build_quality_check(
    seed_path: Path,
    *,
    report_path: Path,
) -> dict[str, Any]:
    validation = validate_seed(seed_path)
    report_exists = report_path.exists()
    report = load_quality_report(report_path) if report_exists else None
    return {
        "validation": validation,
        "latest_eval_report": report,
        "latest_eval_report_exists": report_exists,
    }


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


def _count_field(
    records: list[dict[str, Any]],
    field: str,
    *,
    default: str = "unknown",
) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for record in records:
        value = str(record.get(field) or default).strip() or default
        counter[value] += 1
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _iter_eval_cases(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []

    cases: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        turns = item.get("turns")
        if isinstance(turns, list):
            for turn in turns:
                if isinstance(turn, dict):
                    merged = dict(turn)
                    merged["conversation_id"] = item.get("id")
                    cases.append(merged)
            continue
        cases.append(item)
    return cases


def _case_chunk_ids(case: dict[str, Any]) -> set[str]:
    chunk_ids: set[str] = set()
    for key in ("expected_chunk_ids", "expected_cited_chunk_ids"):
        values = case.get(key)
        if isinstance(values, list):
            chunk_ids.update(str(value) for value in values if value)

    equivalent = case.get("equivalent_chunk_ids")
    if isinstance(equivalent, dict):
        chunk_ids.update(str(key) for key in equivalent if key)
        for values in equivalent.values():
            if isinstance(values, list):
                chunk_ids.update(str(value) for value in values if value)
            elif values:
                chunk_ids.add(str(values))
    return chunk_ids


def _compact_eval_case(case: dict[str, Any], *, source_file: str) -> dict[str, Any]:
    return {
        "id": case.get("id"),
        "conversation_id": case.get("conversation_id"),
        "source_file": source_file,
        "query": case.get("query"),
        "expected_behavior": case.get("expected_behavior"),
        "expected_chunk_ids": case.get("expected_chunk_ids") or [],
        "expected_cited_chunk_ids": case.get("expected_cited_chunk_ids") or [],
        "tags": case.get("tags") or [],
    }


def _preview(text: str, *, limit: int = 180) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"
