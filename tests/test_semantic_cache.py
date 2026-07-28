from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.models import QueryAnalysis
from src.rag.cache import CACHE_SCHEMA_VERSION, CachedResponse, SemanticCache


class FakeEmbedder:
    def encode(self, _text: str):
        return np.array([0.1, 0.2]), {}


class FakeQdrant:
    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload
        self.query_kwargs = None
        self.upsert_kwargs = None
        self.delete_kwargs = None

    async def query_points(self, **kwargs):
        self.query_kwargs = kwargs
        points = [] if self.payload is None else [SimpleNamespace(payload=self.payload)]
        return SimpleNamespace(points=points)

    async def upsert(self, **kwargs):
        self.upsert_kwargs = kwargs

    async def delete(self, **kwargs):
        self.delete_kwargs = kwargs


def _entry(*, forum: str | None = "Амур") -> CachedResponse:
    return CachedResponse(
        response="Проезд оплачивает участник.",
        forum_normalized=forum,
        analysis=QueryAnalysis(
            forum="Амур" if forum else None,
            forum_normalized=forum,
            category="форумы",
            topics=["oplata_proezda"],
        ),
        cited_sources=["yonote_amur_travel"],
        factual_source_type="yonote",
        generator_model="source_chunk",
    )


@pytest.mark.asyncio
async def test_semantic_cache_round_trips_structured_grounded_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.rag.cache.get_settings",
        lambda: SimpleNamespace(cache_ttl_hours=24, cache_similarity_threshold=0.95),
    )
    qdrant = FakeQdrant()
    cache = SemanticCache(qdrant, FakeEmbedder())  # type: ignore[arg-type]
    expected = _entry()

    await cache.save("Кто платит за проезд на Амур?", expected)
    payload = qdrant.upsert_kwargs["points"][0].payload
    assert payload["cache_schema_version"] == CACHE_SCHEMA_VERSION
    assert payload["scope_key"] == "Амур"
    assert payload["cited_sources"] == ["yonote_amur_travel"]
    assert payload["factual_source_type"] == "yonote"
    assert payload["analysis"]["topics"] == ["oplata_proezda"]

    qdrant.payload = payload
    actual = await cache.check("Кто платит за проезд на Амур?", "Амур")

    assert actual == expected


@pytest.mark.asyncio
async def test_semantic_cache_global_lookup_cannot_match_event_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.rag.cache.get_settings",
        lambda: SimpleNamespace(cache_ttl_hours=24, cache_similarity_threshold=0.95),
    )
    qdrant = FakeQdrant()
    cache = SemanticCache(qdrant, FakeEmbedder())  # type: ignore[arg-type]

    await cache.check("Кто платит за проезд?", None)

    scope_condition = next(
        condition
        for condition in qdrant.query_kwargs["query_filter"].must
        if getattr(condition, "key", None) == "scope_key"
    )
    assert scope_condition.match.value == "__global__"


@pytest.mark.asyncio
async def test_semantic_cache_rejects_legacy_text_only_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.rag.cache.get_settings",
        lambda: SimpleNamespace(cache_ttl_hours=24, cache_similarity_threshold=0.95),
    )
    qdrant = FakeQdrant(payload={"response": "Старый ответ"})
    cache = SemanticCache(qdrant, FakeEmbedder())  # type: ignore[arg-type]

    assert await cache.check("Вопрос", None) is None


@pytest.mark.asyncio
async def test_semantic_cache_rejects_non_yonote_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.rag.cache.get_settings",
        lambda: SimpleNamespace(cache_ttl_hours=24, cache_similarity_threshold=0.95),
    )
    payload = _entry().model_dump(mode="json")
    payload["factual_source_type"] = "xlsx"
    qdrant = FakeQdrant(payload=payload)
    cache = SemanticCache(qdrant, FakeEmbedder())  # type: ignore[arg-type]

    assert await cache.check("Вопрос", "Амур") is None


@pytest.mark.asyncio
async def test_semantic_cache_can_invalidate_global_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.rag.cache.get_settings",
        lambda: SimpleNamespace(cache_ttl_hours=24, cache_similarity_threshold=0.95),
    )
    qdrant = FakeQdrant()
    cache = SemanticCache(qdrant, FakeEmbedder())  # type: ignore[arg-type]

    await cache.invalidate_forum(None)

    selector = qdrant.delete_kwargs["points_selector"]
    condition = selector.filter.must[0]
    assert condition.key == "scope_key"
    assert condition.match.value == "__global__"
