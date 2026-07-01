from __future__ import annotations

import asyncio
import math
import re
from datetime import date
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from src.models import Chunk
from src.rag.embedder import Embedder, sparse_to_indices_values
from src.rag.filter_keys import category_filter_key, stable_text_filter_key

KEYWORD_TOKEN_RE = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)
KEYWORD_STOPWORDS = {
    "без",
    "для",
    "его",
    "если",
    "или",
    "как",
    "кто",
    "мне",
    "над",
    "она",
    "они",
    "при",
    "про",
    "что",
    "это",
}


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
        self._query_vector_cache: dict[str, tuple[Any, dict[str, float]]] = {}
        self._keyword_payload_cache: dict[str, list[dict[str, Any]]] = {}

    async def retrieve(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> list[Chunk]:
        dense, sparse = await self._encode_query(query)
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

    async def retrieve_by_metadata(
        self,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> list[Chunk]:
        filters = filters or {}
        points, _ = await self.qdrant.scroll(
            collection_name=self.collection_name,
            scroll_filter=build_filter(filters),
            limit=top_k,
            with_payload=True,
            with_vectors=False,
        )
        if not points and _can_fallback_to_raw_metadata_filter(filters):
            points, _ = await self.qdrant.scroll(
                collection_name=self.collection_name,
                scroll_filter=build_filter(filters, use_stable_keys=False),
                limit=top_k,
                with_payload=True,
                with_vectors=False,
            )

        chunks: list[Chunk] = []
        for point in points:
            payload = point.payload or {}
            chunks.append(
                Chunk(
                    chunk_id=str(payload.get("chunk_id") or point.id),
                    text=str(payload.get("text_clean") or payload.get("text") or ""),
                    metadata=payload,
                    score=1.0,
                )
            )
        return chunks

    async def _encode_query(self, query: str) -> tuple[Any, dict[str, float]]:
        cached = self._query_vector_cache.get(query)
        if cached is not None:
            return cached

        encoded = await asyncio.to_thread(self.embedder.encode, query)
        if len(self._query_vector_cache) >= 256:
            self._query_vector_cache.clear()
        self._query_vector_cache[query] = encoded
        return encoded

    async def retrieve_keyword_candidates(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        *,
        top_k: int = 6,
        scan_limit: int = 512,
        min_score: float = 2.0,
        source_type: str = "ticket_answer_bank",
    ) -> list[Chunk]:
        query_tokens = _keyword_tokens(query)
        if not query_tokens:
            return []

        payloads = await self._keyword_payloads(source_type, scan_limit)

        scored: list[tuple[float, str, dict[str, Any]]] = []
        for payload in payloads:
            if not _payload_matches_filters(payload, filters or {}):
                continue
            haystack = _keyword_haystack(payload)
            score = _keyword_score(query, query_tokens, haystack, payload)
            if score < min_score:
                continue
            chunk_id = str(payload.get("chunk_id") or payload.get("_point_id") or "")
            if not chunk_id:
                continue
            scored.append((score, chunk_id, payload))

        scored.sort(key=lambda item: item[0], reverse=True)
        chunks: list[Chunk] = []
        for score, chunk_id, payload in scored[:top_k]:
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    text=str(payload.get("text_clean") or payload.get("text") or ""),
                    metadata=payload,
                    score=min(score, 1.0),
                )
            )
        return chunks

    async def _keyword_payloads(
        self,
        source_type: str,
        scan_limit: int,
    ) -> list[dict[str, Any]]:
        cached = self._keyword_payload_cache.get(source_type)
        if cached is not None:
            return cached

        query_filter = build_filter({"source_type": source_type})
        payloads: list[dict[str, Any]] = []
        next_page_offset: Any = None
        while len(payloads) < scan_limit:
            points, next_page_offset = await self.qdrant.scroll(
                collection_name=self.collection_name,
                scroll_filter=query_filter,
                offset=next_page_offset,
                limit=min(128, scan_limit - len(payloads)),
                with_payload=True,
            )
            for point in points:
                payload = dict(point.payload or {})
                payload.setdefault("_point_id", str(point.id))
                payloads.append(payload)
            if not next_page_offset:
                break

        self._keyword_payload_cache[source_type] = payloads
        return payloads


def build_filter(filters: dict[str, Any], *, use_stable_keys: bool = True) -> models.Filter:
    must: list[models.Condition] = [
        models.FieldCondition(key="status", match=models.MatchValue(value="published"))
    ]

    forum = filters.get("forum_normalized")
    if forum:
        if use_stable_keys:
            must.append(
                models.FieldCondition(
                    key="forum_key",
                    match=models.MatchValue(value=stable_text_filter_key(forum)),
                )
            )
        else:
            must.append(
                models.FieldCondition(
                    key="forum_normalized",
                    match=models.MatchValue(value=forum),
                )
            )

    category = filters.get("category")
    if category:
        if use_stable_keys:
            must.append(
                models.FieldCondition(
                    key="category_key",
                    match=models.MatchValue(value=category_filter_key(category)),
                )
            )
        else:
            must.append(
                models.FieldCondition(
                    key="category",
                    match=models.MatchValue(value=category),
                )
            )

    topic = filters.get("topic")
    if topic:
        if use_stable_keys:
            must.append(
                models.FieldCondition(
                    key="topic_key",
                    match=models.MatchValue(value=stable_text_filter_key(topic)),
                )
            )
        else:
            must.append(
                models.FieldCondition(
                    key="topic",
                    match=models.MatchValue(value=topic),
                )
            )

    for key in ("forum_key", "category_key", "topic_key"):
        value = filters.get(key)
        if value:
            must.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))

    source_type = filters.get("source_type")
    if source_type:
        must.append(
            models.FieldCondition(key="source_type", match=models.MatchValue(value=source_type))
        )

    should: list[models.Condition] = [
        models.FieldCondition(key="valid_to", range=models.DatetimeRange(gte=str(date.today()))),
        models.IsNullCondition(is_null=models.PayloadField(key="valid_to")),
    ]
    return models.Filter(must=must, should=should)


def _can_fallback_to_raw_metadata_filter(filters: dict[str, Any]) -> bool:
    return any(filters.get(key) for key in ("forum_normalized", "category", "topic"))


def _keyword_score(
    query: str,
    query_tokens: set[str],
    haystack: str,
    payload: dict[str, Any],
) -> float:
    haystack_tokens = _keyword_tokens(haystack)
    if not haystack_tokens:
        return 0.0

    overlap = query_tokens & haystack_tokens
    if not overlap:
        return 0.0

    base_score = len(overlap) / math.sqrt(len(query_tokens) * len(haystack_tokens))
    example_score = _intent_example_score(query, payload)
    return base_score + example_score


def _intent_example_score(query: str, payload: dict[str, Any]) -> float:
    query_normalized = _keyword_normalize(query)
    if not query_normalized:
        return 0.0

    best_score = 0.0
    for example in payload.get("intent_examples") or []:
        example_text = str(example or "")
        example_normalized = _keyword_normalize(example_text)
        if not example_normalized:
            continue
        if example_normalized == query_normalized:
            best_score = max(best_score, 3.0)
        elif query_normalized in example_normalized or example_normalized in query_normalized:
            best_score = max(best_score, 2.0)
    return best_score


def _keyword_haystack(payload: dict[str, Any]) -> str:
    examples = " ".join(str(item or "") for item in payload.get("intent_examples") or [])
    return " ".join(
        str(value or "")
        for value in (
            payload.get("embedding_text"),
            payload.get("text_clean"),
            payload.get("text"),
            payload.get("intent_name"),
            payload.get("topic"),
            payload.get("source_category"),
            payload.get("forum_normalized"),
            examples,
        )
    )


def _keyword_tokens(text: str) -> set[str]:
    return {
        token
        for token in KEYWORD_TOKEN_RE.findall(_keyword_normalize(text))
        if token not in KEYWORD_STOPWORDS
    }


def _keyword_normalize(text: str) -> str:
    return str(text or "").casefold().replace("ё", "е")


def _payload_matches_filters(payload: dict[str, Any], filters: dict[str, Any]) -> bool:
    for key, value in filters.items():
        if not value:
            continue
        if key == "forum_normalized":
            if not _payload_value_matches(
                payload,
                "forum_normalized",
                value,
                key_field="forum_key",
                key_value=stable_text_filter_key(str(value)),
            ):
                return False
        elif key == "category":
            if not _payload_value_matches(
                payload,
                "category",
                value,
                key_field="category_key",
                key_value=category_filter_key(str(value)),
            ):
                return False
        elif key == "topic":
            if not _payload_value_matches(
                payload,
                "topic",
                value,
                key_field="topic_key",
                key_value=stable_text_filter_key(str(value)),
            ):
                return False
        elif str(payload.get(key) or "") != str(value):
            return False
    return True


def _payload_value_matches(
    payload: dict[str, Any],
    field: str,
    value: Any,
    *,
    key_field: str,
    key_value: str,
) -> bool:
    return (
        str(payload.get(field) or "") == str(value)
        or str(payload.get(key_field) or "") == key_value
    )
