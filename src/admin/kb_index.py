from __future__ import annotations

import asyncio
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from scripts.index_kb import (
    KBSeedRecord,
    build_qdrant_payload,
    qdrant_point_id,
)
from src.rag.embedder import Embedder, sparse_to_indices_values


async def upsert_chunk(
    qdrant: AsyncQdrantClient,
    embedder: Embedder,
    *,
    collection_name: str,
    record_payload: dict[str, Any],
) -> dict[str, Any]:
    record = KBSeedRecord.model_validate(record_payload)
    if record.status != "published":
        await qdrant.delete(
            collection_name=collection_name,
            points_selector=models.PointIdsList(
                points=[qdrant_point_id(record.chunk_id)]
            ),
            wait=True,
        )
        return {
            "ok": True,
            "action": "deleted",
            "chunk_id": record.chunk_id,
            "collection": collection_name,
            "status": record.status,
            "forum_normalized": record.forum_normalized,
        }

    payload = build_qdrant_payload(record)
    embedding_text = str(payload["embedding_text"])
    dense, sparse = await asyncio.to_thread(embedder.encode, embedding_text)
    indices, values = sparse_to_indices_values(sparse)

    await qdrant.upsert(
        collection_name=collection_name,
        points=[
            models.PointStruct(
                id=qdrant_point_id(record.chunk_id),
                vector={
                    "dense": dense.tolist(),
                    "sparse": models.SparseVector(indices=indices, values=values),
                },
                payload=payload,
            )
        ],
    )
    return {
        "ok": True,
        "action": "upserted",
        "chunk_id": record.chunk_id,
        "collection": collection_name,
        "status": record.status,
        "forum_normalized": record.forum_normalized,
    }
