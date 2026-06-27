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
from src.rag.embedder import Embedder, sparse_to_indices_values
from src.rag.errors import MLDependencyError
from src.rag.filter_keys import build_filter_key_payload


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
) -> None:
    if prune_stale and limit is not None:
        raise ValueError("--prune-stale cannot be used with --limit")
    if embedding_batch_size < 1:
        raise ValueError("--embedding-batch-size must be positive")

    settings = get_settings()
    client = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        if not await client.collection_exists(collection):
            raise RuntimeError(
                f"Qdrant collection '{collection}' does not exist. "
                "Run: python scripts/init_qdrant.py"
            )

        embedder = Embedder()
        raw_records = await asyncio.to_thread(path.read_text, encoding="utf-8")
        raw_items = json.loads(raw_records)
        records = validate_seed_items(raw_items)
        if limit is not None:
            records = records[:limit]

        started_at = perf_counter()
        points: list[models.PointStruct] = []
        indexed = 0
        for offset in range(0, len(records), embedding_batch_size):
            batch = records[offset : offset + embedding_batch_size]
            embedding_texts = [build_embedding_text(record) for record in batch]
            encoded_batch = await asyncio.to_thread(embedder.encode_batch, embedding_texts)

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
                await client.upsert(collection_name=collection, points=points)
                indexed += len(points)
                points.clear()

        if points:
            await client.upsert(collection_name=collection, points=points)
            indexed += len(points)

        pruned_stale = 0
        if prune_stale:
            pruned_stale = await prune_stale_points(
                client,
                collection,
                {record.chunk_id for record in records},
            )

        elapsed = perf_counter() - started_at
        limit_label = "all" if limit is None else str(limit)
        print(
            f"indexed={indexed} skipped=0 pruned_stale={pruned_stale} collection={collection} "
            f"limit={limit_label} elapsed_sec={elapsed:.2f}"
        )
    finally:
        await client.close()


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


def validate_only(path: Path) -> None:
    raw_items = _read_json(path)
    records = validate_seed_items(raw_items)
    print(f"valid_records={len(records)} path={path}")


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


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="data/knowledge_base_seed.json")
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.validate_only:
        validate_only(Path(args.path))
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
