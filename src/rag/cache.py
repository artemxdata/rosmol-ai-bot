from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field, ValidationError
from qdrant_client import AsyncQdrantClient, models

from src.config import get_settings
from src.models import QueryAnalysis
from src.rag.embedder import Embedder

CACHE_SCHEMA_VERSION = 5
GLOBAL_CACHE_SCOPE = "__global__"


class CachedResponse(BaseModel):
    response: str = Field(min_length=1)
    forum_normalized: str | None = None
    analysis: QueryAnalysis
    cited_sources: list[str] = Field(min_length=1)
    factual_source_type: Literal["yonote"]
    generator_model: str | None = None
    verifier_triggered: bool = False
    disposition: Literal["answered"] = "answered"


class SemanticCache:
    def __init__(self, qdrant: AsyncQdrantClient, embedder: Embedder) -> None:
        self.qdrant = qdrant
        self.embedder = embedder
        self.settings = get_settings()

    async def check(
        self,
        query: str,
        forum: str | None,
        *,
        query_identity: str | None = None,
    ) -> CachedResponse | None:
        dense, _ = await asyncio.to_thread(self.embedder.encode, query)
        scope_key = _scope_key(forum)
        query_fingerprint = _query_fingerprint(
            query if query_identity is None else query_identity
        )
        must: list[models.Condition] = [
            models.FieldCondition(
                key="scope_key",
                match=models.MatchValue(value=scope_key),
            ),
            models.FieldCondition(
                key="cache_schema_version",
                match=models.MatchValue(value=CACHE_SCHEMA_VERSION),
            ),
            models.FieldCondition(
                key="query_fingerprint",
                match=models.MatchValue(value=query_fingerprint),
            ),
        ]

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
        payload = result.points[0].payload or {}
        if (
            payload.get("cache_schema_version") != CACHE_SCHEMA_VERSION
            or payload.get("scope_key") != scope_key
            or payload.get("query_fingerprint") != query_fingerprint
        ):
            return None
        try:
            return CachedResponse.model_validate(payload)
        except ValidationError:
            return None

    async def save(
        self,
        query: str,
        cached_response: CachedResponse,
        *,
        query_identity: str | None = None,
    ) -> None:
        dense, _ = await asyncio.to_thread(self.embedder.encode, query)
        forum = cached_response.forum_normalized
        scope_key = _scope_key(forum)
        query_fingerprint = _query_fingerprint(
            query if query_identity is None else query_identity
        )
        point_id = str(
            uuid5(NAMESPACE_URL, f"{scope_key}:{query_fingerprint}")
        )
        payload = cached_response.model_dump(mode="json")
        payload.update(
            {
                "cache_schema_version": CACHE_SCHEMA_VERSION,
                "scope_key": scope_key,
                "query_fingerprint": query_fingerprint,
                "cached_at": datetime.now(UTC).isoformat(),
                "query_text": query,
            }
        )
        await self.qdrant.upsert(
            collection_name="response_cache",
            points=[
                models.PointStruct(
                    id=point_id,
                    vector={"dense": dense.tolist()},
                    payload=payload,
                )
            ],
        )

    async def invalidate_forum(self, forum: str | None) -> None:
        await self.qdrant.delete(
            collection_name="response_cache",
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="scope_key",
                            match=models.MatchValue(value=_scope_key(forum)),
                        )
                    ]
                )
            ),
        )


def _scope_key(forum: str | None) -> str:
    return str(forum or "").strip() or GLOBAL_CACHE_SCOPE


def _query_fingerprint(query: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(query or ""))
    normalized = normalized.casefold().replace("ё", "е")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
