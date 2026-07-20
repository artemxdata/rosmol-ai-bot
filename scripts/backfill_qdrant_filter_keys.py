from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import AsyncQdrantClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.index_kb import _read_json, validate_seed_items
from src.config import get_settings
from src.rag.filter_keys import build_filter_key_payload


async def backfill_filter_keys(
    path: Path,
    collection: str,
    batch_size: int = 64,
) -> None:
    settings = get_settings()
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
    )
    try:
        raw_items = await asyncio.to_thread(_read_json, path)
        records = validate_seed_items(raw_items)
        updated = 0
        skipped = 0
        batch: list[tuple[str, dict[str, str]]] = []
        for record in records:
            payload = build_filter_key_payload(record.model_dump())
            if not payload:
                skipped += 1
                continue
            batch.append((str(uuid5(NAMESPACE_URL, record.chunk_id)), payload))
            if len(batch) >= batch_size:
                await _set_payload_batch(client, collection, batch)
                updated += len(batch)
                batch.clear()

        if batch:
            await _set_payload_batch(client, collection, batch)
            updated += len(batch)

        print(f"filter_keys_updated={updated} skipped={skipped} collection={collection}")
    finally:
        await client.close()


async def _set_payload_batch(
    client: AsyncQdrantClient,
    collection: str,
    batch: list[tuple[str, dict[str, str]]],
) -> None:
    for point_id, payload in batch:
        await client.set_payload(
            collection_name=collection,
            payload=payload,
            points=[point_id],
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="data/knowledge_base_seed.json")
    parser.add_argument("--collection", default="knowledge_base")
    parser.add_argument("--batch-size", type=int, default=64)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        backfill_filter_keys(
            path=Path(args.path),
            collection=args.collection,
            batch_size=args.batch_size,
        )
    )


if __name__ == "__main__":
    main()
