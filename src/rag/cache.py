from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import AsyncQdrantClient, models

from src.config import get_settings
from src.rag.embedder import Embedder


class SemanticCache:
    def __init__(self, qdrant: AsyncQdrantClient, embedder: Embedder) -> None:
        self.qdrant = qdrant
        self.embedder = embedder
        self.settings = get_settings()

    async def check(self, query: str, forum: str | None) -> str | None:
        dense, _ = await asyncio.to_thread(self.embedder.encode, query)
        must: list[models.Condition] = []
        if forum:
            must.append(
                models.FieldCondition(
                    key="forum_normalized",
                    match=models.MatchValue(value=forum),
                )
            )

        min_cached_at = datetime.now(UTC) - timedelta(hours=self.settings.cache_ttl_hours)
        must.append(
            models.FieldCondition(
                key="cached_at",
                range=models.DatetimeRange(gte=min_cached_at.isoformat()),
            )
        )

        result = await self.qdrant.query_points(
            collection_name="response_cache",
            query=dense.tolist(),
            using="dense",
            query_filter=models.Filter(must=must) if must else None,
            limit=1,
            with_payload=True,
            score_threshold=self.settings.cache_similarity_threshold,
        )
        if not result.points:
            return None
        return str((result.points[0].payload or {}).get("response") or "")

    async def save(self, query: str, forum: str | None, response: str) -> None:
        dense, _ = await asyncio.to_thread(self.embedder.encode, query)
        point_id = str(uuid5(NAMESPACE_URL, f"{forum or ''}:{query}"))
        await self.qdrant.upsert(
            collection_name="response_cache",
            points=[
                models.PointStruct(
                    id=point_id,
                    vector={"dense": dense.tolist()},
                    payload={
                        "response": response,
                        "forum_normalized": forum,
                        "cached_at": datetime.now(UTC).isoformat(),
                        "query_text": query,
                    },
                )
            ],
        )

    async def invalidate_forum(self, forum: str) -> None:
        await self.qdrant.delete(
            collection_name="response_cache",
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="forum_normalized",
                            match=models.MatchValue(value=forum),
                        )
                    ]
                )
            ),
        )
