from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from src.models import Chunk
from src.rag.embedder import Embedder, sparse_to_indices_values
from src.rag.filter_keys import category_filter_key, stable_text_filter_key


class Retriever:
    def __init__(
        self,
        qdrant_client: AsyncQdrantClient,
        embedder: Embedder,
        collection_name: str = "knowledge_base",
    ) -> None:
        self.qdrant = qdrant_client
        self.embedder = embedder
        self.collection_name = collection_name

    async def retrieve(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> list[Chunk]:
        dense, sparse = await asyncio.to_thread(self.embedder.encode, query)
        indices, values = sparse_to_indices_values(sparse)
        query_filter = build_filter(filters or {})

        result = await self.qdrant.query_points(
            collection_name=self.collection_name,
            prefetch=[
                models.Prefetch(
                    query=dense.tolist(),
                    using="dense",
                    filter=query_filter,
                    limit=top_k,
                ),
                models.Prefetch(
                    query=models.SparseVector(indices=indices, values=values),
                    using="sparse",
                    filter=query_filter,
                    limit=top_k,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=query_filter,
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

    forum = filters.get("forum_normalized")
    if forum:
        must.append(
            models.FieldCondition(
                key="forum_key",
                match=models.MatchValue(value=stable_text_filter_key(forum)),
            )
        )

    category = filters.get("category")
    if category:
        must.append(
            models.FieldCondition(
                key="category_key",
                match=models.MatchValue(value=category_filter_key(category)),
            )
        )

    topic = filters.get("topic")
    if topic:
        must.append(
            models.FieldCondition(
                key="topic_key",
                match=models.MatchValue(value=stable_text_filter_key(topic)),
            )
        )

    for key in ("forum_key", "category_key", "topic_key"):
        value = filters.get(key)
        if value:
            must.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))

    should: list[models.Condition] = [
        models.FieldCondition(key="valid_to", range=models.DatetimeRange(gte=str(date.today()))),
        models.IsNullCondition(is_null=models.PayloadField(key="valid_to")),
    ]
    return models.Filter(must=must, should=should)
