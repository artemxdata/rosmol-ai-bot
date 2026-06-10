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


class KBSeedRecord(BaseModel):
    chunk_id: str
    text_clean: str | None = None
    text: str | None = None
    forum_normalized: str | None = None
    category: str | None = None
    topic: str | None = None
    status: str = Field(default="published")

    model_config = {"extra": "allow"}

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in {"draft", "published", "archived"}:
            raise ValueError("status must be draft, published or archived")
        return value

    @property
    def content(self) -> str:
        return (self.text_clean or self.text or "").strip()


async def index_kb(path: Path, collection: str) -> None:
    settings = get_settings()
    client = AsyncQdrantClient(url=settings.qdrant_url)
    if not await client.collection_exists(collection):
        raise RuntimeError(
            f"Qdrant collection '{collection}' does not exist. "
            "Run: python scripts/init_qdrant.py"
        )

    embedder = Embedder()
    raw_records = await asyncio.to_thread(path.read_text, encoding="utf-8")
    raw_items = json.loads(raw_records)
    if not isinstance(raw_items, list):
        raise ValueError("knowledge_base_seed.json must contain a JSON array")

    started_at = perf_counter()
    points: list[models.PointStruct] = []
    indexed = 0
    skipped = 0
    for idx, raw_record in enumerate(raw_items):
        try:
            record = KBSeedRecord.model_validate(raw_record)
        except ValidationError as exc:
            skipped += 1
            print(f"skip index={idx} reason=validation_error details={exc.errors()}")
            continue

        text = record.content
        if not text:
            skipped += 1
            print(f"skip chunk_id={record.chunk_id} reason=empty_text")
            continue

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
    print(f"indexed={indexed} skipped={skipped} collection={collection} elapsed_sec={elapsed:.2f}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="data/knowledge_base_seed.json")
    parser.add_argument("--collection", default="knowledge_base")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(index_kb(Path(args.path), args.collection))
