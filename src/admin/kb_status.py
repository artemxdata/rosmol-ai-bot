from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient

from scripts.index_kb import KBSeedRecord, build_qdrant_payload
from src.admin.kb_store import load_validated_seed_snapshot


async def build_runtime_kb_status(
    qdrant: AsyncQdrantClient,
    *,
    seed_path: Path,
    knowledge_collection: str,
    response_cache_collection: str = "response_cache",
    scroll_limit: int = 256,
) -> dict[str, Any]:
    if scroll_limit < 1:
        raise ValueError("scroll_limit must be positive")

    validation, seed_records = load_validated_seed_snapshot(seed_path)
    published_seed_records = [
        record
        for record in seed_records
        if str(record.get("status") or "published").strip() == "published"
    ]
    expected_payloads = [
        build_qdrant_payload(KBSeedRecord.model_validate(record))
        for record in published_seed_records
    ]
    qdrant_records = await _load_qdrant_payloads(
        qdrant,
        collection=knowledge_collection,
        scroll_limit=scroll_limit,
    )
    expected_by_id = _records_by_id(expected_payloads)
    actual_by_id = _records_by_id(qdrant_records)
    expected_ids = set(expected_by_id)
    actual_ids = set(actual_by_id)
    invalid_or_duplicate_points = len(qdrant_records) - len(actual_by_id)
    missing_ids = sorted(expected_ids - actual_ids)
    stale_ids = sorted(actual_ids - expected_ids)
    changed_ids = sorted(
        chunk_id
        for chunk_id in expected_ids & actual_ids
        if _canonical_record(expected_by_id[chunk_id])
        != _canonical_record(actual_by_id[chunk_id])
    )
    snapshot_payload_match = (
        not missing_ids
        and not stale_ids
        and not changed_ids
        and invalid_or_duplicate_points == 0
        and len(qdrant_records) == len(expected_payloads)
    )

    cache_count = 0
    if await qdrant.collection_exists(response_cache_collection):
        cache_result = await qdrant.count(
            collection_name=response_cache_collection,
            exact=True,
        )
        cache_count = int(cache_result.count)

    post_scan_seed_sha256 = _current_seed_sha256(seed_path)
    seed_changed_during_scan = (
        post_scan_seed_sha256 is None
        or post_scan_seed_sha256 != validation["seed_sha256"]
    )
    exact_payload_match = snapshot_payload_match and not seed_changed_during_scan
    failure_reasons = (
        ["seed_changed_during_scan"] if seed_changed_during_scan else []
    )
    if not snapshot_payload_match:
        failure_reasons.append("qdrant_payload_mismatch")

    return {
        "ok": exact_payload_match,
        "status": "GO" if exact_payload_match else "STOP",
        "failure_reasons": failure_reasons,
        "seed": {
            "path": str(seed_path),
            "sha256": validation["seed_sha256"],
            "post_scan_sha256": post_scan_seed_sha256,
            "changed_during_scan": seed_changed_during_scan,
            "records": validation["valid_records"],
            "published": len(expected_payloads),
            "payload_fingerprint_sha256": _records_fingerprint(expected_payloads),
        },
        "qdrant": {
            "collection": knowledge_collection,
            "points": len(qdrant_records),
            "payload_fingerprint_sha256": _records_fingerprint(qdrant_records),
            "exact_payload_match": exact_payload_match,
            "snapshot_payload_match": snapshot_payload_match,
            "missing": len(missing_ids),
            "stale": len(stale_ids),
            "changed": len(changed_ids),
            "invalid_or_duplicate_points": invalid_or_duplicate_points,
            "missing_sample": missing_ids[:10],
            "stale_sample": stale_ids[:10],
            "changed_sample": changed_ids[:10],
        },
        "response_cache": {
            "collection": response_cache_collection,
            "points": cache_count,
        },
        "limitations": (
            "Payload and source binding are compared exactly; vector values are not "
            "recomputed by this read-only check."
        ),
    }


def _current_seed_sha256(seed_path: Path) -> str | None:
    try:
        return sha256(seed_path.read_bytes()).hexdigest()
    except OSError:
        return None


async def _load_qdrant_payloads(
    qdrant: AsyncQdrantClient,
    *,
    collection: str,
    scroll_limit: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    next_page_offset: Any = None
    while True:
        points, next_page_offset = await qdrant.scroll(
            collection_name=collection,
            with_payload=True,
            with_vectors=False,
            limit=scroll_limit,
            offset=next_page_offset,
        )
        for point in points:
            payload = getattr(point, "payload", None)
            records.append(dict(payload) if isinstance(payload, dict) else {})
        if next_page_offset is None:
            return records


def _records_by_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        chunk_id = str(record.get("chunk_id") or "").strip()
        if chunk_id:
            result[chunk_id] = record
    return result


def _records_fingerprint(records: list[dict[str, Any]]) -> str:
    canonical = sorted(
        (_canonical_record(record) for record in records),
        key=lambda rendered: (
            str(rendered.get("chunk_id") or ""),
            json.dumps(rendered, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )
    rendered = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256(rendered).hexdigest()


def _canonical_record(record: dict[str, Any]) -> dict[str, Any]:
    return json.loads(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
