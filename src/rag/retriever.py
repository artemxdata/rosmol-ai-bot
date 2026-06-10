from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from src.models import Chunk
from src.rag.embedder import Embedder, sparse_to_indices_values


class Retriever:
    def __init__(self, qdrant_client: AsyncQdrantClient, embedder: Embedder) -> None:
        self.qdrant = qdrant_client
        self.embedder = embedder

    async def retrieve(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> list[Chunk]:
        dense, sparse = await asyncio.to_thread(self.embedder.encode, query)
        indices, values = sparse_to_indices_values(sparse)

        result = await self.qdrant.query_points(
            collection_name="knowledge_base",
            prefetch=[
                models.Prefetch(query=dense.tolist(), using="dense", limit=top_k),
                models.Prefetch(
                    query=models.SparseVector(indices=indices, values=values),
                    using="sparse",
                    limit=top_k,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=build_filter(filters or {}),
            limit=top_k,
            with_payload=True,
        )

        chunks: list[Chunk] = []
        for point in result.points:
            payload = point.payload or {}
            chunks.append(
                Chunk(
                    chunk_id=str(payload.get("chunk_id") or point.id),
                    text=str(payload.get("text_clean") or payload.get("text") or ""),
                    metadata=payload,
                    score=point.score,
                )
            )
        return chunks


def build_filter(filters: dict[str, Any]) -> models.Filter:
    must: list[models.Condition] = [
        models.FieldCondition(key="status", match=models.MatchValue(value="published"))
    ]

    for key in ("forum_normalized", "category", "topic"):
        value = filters.get(key)
        if value:
            must.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))

    should: list[models.Condition] = [
        models.FieldCondition(key="valid_to", range=models.DatetimeRange(gte=str(date.today()))),
        models.IsNullCondition(is_null=models.PayloadField(key="valid_to")),
    ]
    return models.Filter(must=must, should=should)
