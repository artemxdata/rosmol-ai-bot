from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field, ValidationError, field_validator
from qdrant_client import AsyncQdrantClient, models

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import get_settings
from src.rag.embedder import Embedder, sparse_to_indices_values
from src.rag.errors import MLDependencyError


class KBSeedRecord(BaseModel):
    chunk_id: str
    text_clean: str | None = None
    text: str | None = None
    forum_normalized: str | None = None
    category: str | None = None
    topic: str | None = None
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


async def index_kb(path: Path, collection: str, limit: int | None = None) -> None:
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
        for record in records:
            text = record.content
            dense, sparse = await asyncio.to_thread(embedder.encode, text)
            indices, values = sparse_to_indices_values(sparse)
            payload = {
                **record.model_dump(),
                "text": text,
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

        elapsed = perf_counter() - started_at
        limit_label = "all" if limit is None else str(limit)
        print(
            f"indexed={indexed} skipped=0 collection={collection} "
            f"limit={limit_label} elapsed_sec={elapsed:.2f}"
        )
    finally:
        await client.close()


def validate_only(path: Path) -> None:
    raw_items = _read_json(path)
    records = validate_seed_items(raw_items)
    print(f"valid_records={len(records)} path={path}")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="data/knowledge_base_seed.json")
    parser.add_argument("--collection", default="knowledge_base")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Index only the first N valid records. Useful for ML smoke tests.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.validate_only:
        validate_only(Path(args.path))
    else:
        try:
            asyncio.run(index_kb(Path(args.path), args.collection, args.limit))
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
