from __future__ import annotations

import asyncio
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import AsyncQdrantClient, models

from scripts.index_kb import KBSeedRecord, build_embedding_text
from src.rag.embedder import Embedder, sparse_to_indices_values
from src.rag.filter_keys import build_filter_key_payload


async def upsert_chunk(
    qdrant: AsyncQdrantClient,
    embedder: Embedder,
    *,
    collection_name: str,
    record_payload: dict[str, Any],
) -> dict[str, Any]:
    record = KBSeedRecord.model_validate(record_payload)
    embedding_text = build_embedding_text(record)
    dense, sparse = await asyncio.to_thread(embedder.encode, embedding_text)
    indices, values = sparse_to_indices_values(sparse)
    text = record.content
    payload = {
        **record.model_dump(),
        **build_filter_key_payload(record.model_dump()),
        "text": text,
        "embedding_text": embedding_text,
        "status": record.status,
    }

    await qdrant.upsert(
        collection_name=collection_name,
        points=[
            models.PointStruct(
                id=str(uuid5(NAMESPACE_URL, record.chunk_id)),
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
        "chunk_id": record.chunk_id,
        "collection": collection_name,
        "status": record.status,
        "forum_normalized": record.forum_normalized,
    }
