from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field, ValidationError, field_validator
from qdrant_client import AsyncQdrantClient, models

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import get_settings
from src.kb.audit import semantic_integrity_findings
from src.rag.embedder import Embedder, sparse_to_indices_values
from src.rag.errors import MLDependencyError
from src.rag.filter_keys import build_filter_key_payload

DEFAULT_FORUM_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "forums_registry.json"
)


class KBSeedRecord(BaseModel):
    chunk_id: str
    text_clean: str | None = None
    text: str | None = None
    forum_normalized: str | None = None
    category: str | None = None
    topic: str | None = None
    intent_name: str | None = None
    intent_examples: list[str] | None = None
    source_category: str | None = None
    status: str = Field(default="published")

    model_config = {"extra": "allow"}

    @field_validator("chunk_id")
    @classmethod
    def validate_chunk_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("chunk_id must not be empty")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in {"draft", "published", "archived"}:
            raise ValueError("status must be draft, published or archived")
        return value

    @property
    def content(self) -> str:
        return (self.text_clean or self.text or "").strip()


def validate_seed_items(raw_items: object) -> list[KBSeedRecord]:
    if not isinstance(raw_items, list):
        raise ValueError("knowledge_base_seed.json must contain a JSON array")

    records: list[KBSeedRecord] = []
    seen_chunk_ids: set[str] = set()
    errors: list[str] = []
    for idx, raw_record in enumerate(raw_items):
        try:
            record = KBSeedRecord.model_validate(raw_record)
        except ValidationError as exc:
            errors.append(f"index={idx} reason=validation_error details={exc.errors()}")
            continue

        if record.chunk_id in seen_chunk_ids:
            errors.append(f"index={idx} chunk_id={record.chunk_id} reason=duplicate_chunk_id")
            continue
        seen_chunk_ids.add(record.chunk_id)

        if not record.content:
            errors.append(f"index={idx} chunk_id={record.chunk_id} reason=empty_text")
            continue

        records.append(record)

    if errors:
        joined_errors = "\n".join(errors)
        raise ValueError(f"Invalid knowledge_base_seed.json:\n{joined_errors}")
    return records


def validate_semantic_seed_items(
    raw_items: object,
    *,
    forum_registry: list[dict[str, Any]] | None = None,
) -> None:
    if not isinstance(raw_items, list):
        raise ValueError("knowledge_base_seed.json must contain a JSON array")
    records = [item for item in raw_items if isinstance(item, dict)]
    findings = semantic_integrity_findings(records, forum_registry=forum_registry)
    errors = [finding for finding in findings if finding.get("severity") == "error"]
    if errors:
        details = ", ".join(
            f"{finding.get('code')}={finding.get('count')}" for finding in errors
        )
        raise ValueError(f"Invalid semantic KB integrity: {details}")


def build_embedding_text(record: KBSeedRecord) -> str:
    metadata_parts = []
    for label, value in (
        ("Интент", record.intent_name),
        ("Тема", record.topic),
        ("Форум", record.forum_normalized),
        ("Категория", record.category),
        ("Раздел", record.source_category),
    ):
        if value:
            metadata_parts.append(f"{label}: {value.strip()}")

    examples = [
        example.strip()
        for example in record.intent_examples or []
        if isinstance(example, str) and example.strip()
    ]

    parts = []
    if examples:
        parts.append("Примеры вопросов пользователей:\n" + "\n".join(examples[:30]))
    if metadata_parts:
        parts.append("Метаданные:\n" + "\n".join(metadata_parts))
    parts.append("Ответ:\n" + record.content)
    return "\n\n".join(parts)


async def index_kb(
    path: Path,
    collection: str,
    limit: int | None = None,
    *,
    embedding_batch_size: int = 16,
    prune_stale: bool = False,
    only_missing: bool = False,
    forum_registry_path: Path | None = None,
) -> None:
    if prune_stale and limit is not None:
        raise ValueError("--prune-stale cannot be used with --limit")
    if embedding_batch_size < 1:
        raise ValueError("--embedding-batch-size must be positive")

    settings = get_settings()
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
    )
    try:
        if not await client.collection_exists(collection):
            raise RuntimeError(
                f"Qdrant collection '{collection}' does not exist. "
                "Run: python scripts/init_qdrant.py"
            )

        raw_records = await asyncio.to_thread(path.read_text, encoding="utf-8")
        raw_items = json.loads(raw_records)
        records = validate_seed_items(raw_items)
        validate_semantic_seed_items(
            raw_items,
            forum_registry=load_forum_registry(path, forum_registry_path),
        )
        existing_chunk_ids: set[str] = set()
        if only_missing:
            existing_chunk_ids = await collect_existing_chunk_ids(client, collection)
        records, skipped_existing, allowed_chunk_ids = select_records_for_indexing(
            records,
            existing_chunk_ids=existing_chunk_ids,
            only_missing=only_missing,
        )
        if limit is not None:
            records = records[:limit]
        if only_missing:
            print(
                f"only_missing_skip existing={skipped_existing} remaining={len(records)} "
                f"collection={collection}",
                flush=True,
            )

        started_at = perf_counter()
        total_records = len(records)
        limit_label = "all" if limit is None else str(limit)
        print(
            f"index_start total={total_records} collection={collection} "
            f"limit={limit_label} embedding_batch_size={embedding_batch_size}",
            flush=True,
        )
        points: list[models.PointStruct] = []
        indexed = 0
        embedder = Embedder() if records else None
        for offset in range(0, len(records), embedding_batch_size):
            if embedder is None:
                raise RuntimeError("embedder is not initialized")
            batch = records[offset : offset + embedding_batch_size]
            batch_number = offset // embedding_batch_size + 1
            print(
                f"embedding_batch_start batch={batch_number} "
                f"offset={offset} size={len(batch)} total={total_records}",
                flush=True,
            )
            embedding_texts = [build_embedding_text(record) for record in batch]
            encoded_batch = await asyncio.to_thread(embedder.encode_batch, embedding_texts)
            print(
                f"embedding_batch_done batch={batch_number} "
                f"processed={offset + len(batch)}/{total_records} "
                f"elapsed_sec={perf_counter() - started_at:.2f}",
                flush=True,
            )

            for record, embedding_text, (dense, sparse) in zip(
                batch,
                embedding_texts,
                encoded_batch,
                strict=True,
            ):
                text = record.content
                indices, values = sparse_to_indices_values(sparse)
                payload = {
                    **record.model_dump(),
                    **build_filter_key_payload(record.model_dump()),
                    "text": text,
                    "embedding_text": embedding_text,
                    "status": record.status,
                }
                points.append(
                    models.PointStruct(
                        id=str(uuid5(NAMESPACE_URL, record.chunk_id)),
                        vector={
                            "dense": dense.tolist(),
                            "sparse": models.SparseVector(indices=indices, values=values),
                        },
                        payload=payload,
                    )
                )

            if len(points) >= 64:
                upsert_count = len(points)
                await client.upsert(collection_name=collection, points=points)
                indexed += upsert_count
                points.clear()
                print(
                    f"upsert_progress indexed={indexed}/{total_records} "
                    f"collection={collection} elapsed_sec={perf_counter() - started_at:.2f}",
                    flush=True,
                )

        if points:
            upsert_count = len(points)
            await client.upsert(collection_name=collection, points=points)
            indexed += upsert_count
            print(
                f"upsert_progress indexed={indexed}/{total_records} "
                f"collection={collection} elapsed_sec={perf_counter() - started_at:.2f}",
                flush=True,
            )

        pruned_stale = 0
        if prune_stale:
            pruned_stale = await prune_stale_points(
                client,
                collection,
                allowed_chunk_ids,
            )

        cleared_response_cache = 0
        if indexed or pruned_stale:
            cleared_response_cache = await clear_response_cache(client)

        elapsed = perf_counter() - started_at
        print(
            f"indexed={indexed} skipped={skipped_existing} "
            f"pruned_stale={pruned_stale} collection={collection} "
            f"response_cache_cleared={cleared_response_cache} "
            f"limit={limit_label} elapsed_sec={elapsed:.2f}",
            flush=True,
        )
    finally:
        await client.close()


def select_records_for_indexing(
    records: list[KBSeedRecord],
    *,
    existing_chunk_ids: set[str],
    only_missing: bool,
) -> tuple[list[KBSeedRecord], int, set[str]]:
    published_records = [record for record in records if record.status == "published"]
    allowed_chunk_ids = {record.chunk_id for record in published_records}
    if not only_missing:
        return published_records, 0, allowed_chunk_ids

    selected = [
        record
        for record in published_records
        if record.chunk_id not in existing_chunk_ids
    ]
    return selected, len(published_records) - len(selected), allowed_chunk_ids


async def collect_existing_chunk_ids(
    client: AsyncQdrantClient,
    collection: str,
    *,
    scroll_limit: int = 512,
) -> set[str]:
    chunk_ids: set[str] = set()
    next_page_offset: Any = None

    while True:
        points, next_page_offset = await client.scroll(
            collection_name=collection,
            with_payload=["chunk_id"],
            with_vectors=False,
            limit=scroll_limit,
            offset=next_page_offset,
        )
        for point in points:
            payload = getattr(point, "payload", None) or {}
            chunk_id = payload.get("chunk_id")
            if isinstance(chunk_id, str) and chunk_id:
                chunk_ids.add(chunk_id)

        if next_page_offset is None:
            break

    return chunk_ids


async def prune_stale_points(
    client: AsyncQdrantClient,
    collection: str,
    allowed_chunk_ids: set[str],
    *,
    scroll_limit: int = 256,
    delete_batch_size: int = 256,
) -> int:
    stale_point_ids: list[Any] = []
    next_page_offset: Any = None

    while True:
        points, next_page_offset = await client.scroll(
            collection_name=collection,
            with_payload=["chunk_id"],
            with_vectors=False,
            limit=scroll_limit,
            offset=next_page_offset,
        )
        for point in points:
            payload = getattr(point, "payload", None) or {}
            chunk_id = payload.get("chunk_id")
            if not isinstance(chunk_id, str) or chunk_id not in allowed_chunk_ids:
                stale_point_ids.append(point.id)

        if next_page_offset is None:
            break

    for offset in range(0, len(stale_point_ids), delete_batch_size):
        batch = stale_point_ids[offset : offset + delete_batch_size]
        await client.delete(
            collection_name=collection,
            points_selector=models.PointIdsList(points=batch),
        )

    return len(stale_point_ids)


async def clear_response_cache(
    client: AsyncQdrantClient,
    *,
    collection: str = "response_cache",
    scroll_limit: int = 512,
    delete_batch_size: int = 512,
) -> int:
    if not await client.collection_exists(collection):
        return 0

    point_ids: list[Any] = []
    next_page_offset: Any = None
    while True:
        points, next_page_offset = await client.scroll(
            collection_name=collection,
            with_payload=False,
            with_vectors=False,
            limit=scroll_limit,
            offset=next_page_offset,
        )
        point_ids.extend(point.id for point in points)
        if next_page_offset is None:
            break

    for offset in range(0, len(point_ids), delete_batch_size):
        await client.delete(
            collection_name=collection,
            points_selector=models.PointIdsList(
                points=point_ids[offset : offset + delete_batch_size]
            ),
            wait=True,
        )
    return len(point_ids)


def validate_only(path: Path, forum_registry_path: Path | None = None) -> None:
    raw_items = _read_json(path)
    records = validate_seed_items(raw_items)
    validate_semantic_seed_items(
        raw_items,
        forum_registry=load_forum_registry(path, forum_registry_path),
    )
    published_records = sum(record.status == "published" for record in records)
    print(
        f"valid_records={len(records)} published_records={published_records} path={path}"
    )


def validate_quality_gate(path: Path) -> dict:
    if not path.exists():
        raise ValueError(f"Quality gate report does not exist: {path}")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"Quality gate report must contain a JSON object: {path}")
    if payload.get("passed") is not True:
        failed_checks = payload.get("failed_checks")
        raise ValueError(
            "Quality gate did not pass"
            + (f": failed_checks={failed_checks}" if failed_checks is not None else "")
        )
    return payload


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def load_forum_registry(
    seed_path: Path,
    forum_registry_path: Path | None = None,
) -> list[dict[str, Any]]:
    if forum_registry_path is not None:
        registry_path = forum_registry_path
        if not registry_path.is_file():
            raise ValueError(f"forums registry does not exist: {registry_path}")
    else:
        sibling_path = seed_path.with_name("forums_registry.json")
        candidates = (sibling_path, DEFAULT_FORUM_REGISTRY_PATH)
        registry_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if registry_path is None:
            checked = ", ".join(str(candidate) for candidate in candidates)
            raise ValueError(f"forums registry does not exist; checked: {checked}")

    payload = _read_json(registry_path)
    if not isinstance(payload, list):
        raise ValueError(f"forums registry must contain a JSON array: {registry_path}")
    return [item for item in payload if isinstance(item, dict)]


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="data/knowledge_base_seed.json")
    parser.add_argument(
        "--forums-registry",
        type=Path,
        default=None,
        help=(
            "Forum registry used by semantic validation. Defaults to forums_registry.json "
            "next to the seed, then the versioned project registry."
        ),
    )
    parser.add_argument("--collection", default=settings.qdrant_knowledge_collection)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--quality-gate",
        default="",
        help="Path to quality_gate.json. Checked when --require-quality-gate is set.",
    )
    parser.add_argument(
        "--require-quality-gate",
        action="store_true",
        help="Refuse indexing unless the quality gate report exists and passed=true.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Index only the first N valid records. Useful for ML smoke tests.",
    )
    parser.add_argument(
        "--embedding-batch-size",
        type=int,
        default=16,
        help="Number of chunks encoded in one bge-m3 call during indexing.",
    )
    parser.add_argument(
        "--prune-stale",
        action="store_true",
        help=(
            "After indexing, delete Qdrant points whose chunk_id is not present in the "
            "current seed. Refuses to run with --limit."
        ),
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Index only seed records whose chunk_id is not already present in Qdrant.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.validate_only:
        validate_only(Path(args.path), args.forums_registry)
    else:
        if args.require_quality_gate:
            quality_gate_path = (
                Path(args.quality_gate)
                if args.quality_gate
                else Path("reports/quality_suite/quality_gate.json")
            )
            validate_quality_gate(quality_gate_path)
        try:
            asyncio.run(
                index_kb(
                    Path(args.path),
                    args.collection,
                    args.limit,
                    embedding_batch_size=args.embedding_batch_size,
                    prune_stale=args.prune_stale,
                    only_missing=args.only_missing,
                    forum_registry_path=args.forums_registry,
                )
            )
        except MLDependencyError as exc:
            print(
                "ML dependencies are required for indexing. "
                "Use docker-compose.ml.yml or install .[ml]. "
                f"Details: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
