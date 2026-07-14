from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from qdrant_client import AsyncQdrantClient, models

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import get_settings


async def ensure_collections(
    recreate: bool = False,
    knowledge_collection: str | None = None,
) -> None:
    settings = get_settings()
    knowledge_collection = knowledge_collection or settings.qdrant_knowledge_collection
    client = AsyncQdrantClient(url=settings.qdrant_url)

    if recreate and await client.collection_exists(knowledge_collection):
        await client.delete_collection(knowledge_collection)
    if recreate and await client.collection_exists("response_cache"):
        await client.delete_collection("response_cache")

    if not await client.collection_exists(knowledge_collection):
        await client.create_collection(
            collection_name=knowledge_collection,
            vectors_config={
                "dense": models.VectorParams(size=1024, distance=models.Distance.COSINE),
            },
            sparse_vectors_config={
                "sparse": models.SparseVectorParams(
                    index=models.SparseIndexParams(on_disk=False),
                ),
            },
        )

    for field in (
        "forum_normalized",
        "category",
        "topic",
        "forum_key",
        "category_key",
        "topic_key",
        "status",
    ):
        await create_payload_index_if_missing(
            client,
            collection_name=knowledge_collection,
            field_name=field,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )

    if not await client.collection_exists("response_cache"):
        await client.create_collection(
            collection_name="response_cache",
            vectors_config={
                "dense": models.VectorParams(size=1024, distance=models.Distance.COSINE),
            },
        )
    for field, schema in (
        ("forum_normalized", models.PayloadSchemaType.KEYWORD),
        ("scope_key", models.PayloadSchemaType.KEYWORD),
        ("cache_schema_version", models.PayloadSchemaType.INTEGER),
        ("cached_at", models.PayloadSchemaType.DATETIME),
    ):
        await create_payload_index_if_missing(
            client,
            collection_name="response_cache",
            field_name=field,
            field_schema=schema,
        )

    collections = await client.get_collections()
    print([collection.name for collection in collections.collections])


async def create_payload_index_if_missing(
    client: AsyncQdrantClient,
    collection_name: str,
    field_name: str,
    field_schema: models.PayloadSchemaType,
) -> None:
    try:
        await client.create_payload_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema=field_schema,
        )
    except Exception as exc:
        message = str(exc).lower()
        if "already exists" not in message and "already exist" not in message:
            raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Delete and recreate Qdrant collections. Destructive.",
    )
    parser.add_argument(
        "--knowledge-collection",
        default=None,
        help="Qdrant collection for KB chunks. Defaults to QDRANT_KNOWLEDGE_COLLECTION.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        ensure_collections(
            recreate=args.recreate,
            knowledge_collection=args.knowledge_collection,
        )
    )
