from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.models import QueryAnalysis
from src.rag.cache import (
    CACHE_SCHEMA_VERSION,
    CachedResponse,
    SemanticCache,
    _query_fingerprint,
)


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
        points = (
            []
            if self.payload is None
            else [SimpleNamespace(payload=self.payload, score=0.999)]
        )
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
    assert payload["query_fingerprint"] == _query_fingerprint(
        "Кто платит за проезд на Амур?"
    )
    assert payload["cited_sources"] == ["yonote_amur_travel"]
    assert payload["factual_source_type"] == "yonote"
    assert payload["analysis"]["topics"] == ["oplata_proezda"]

    qdrant.payload = payload
    actual = await cache.check("Кто платит за проезд на Амур?", "Амур")

    assert actual == expected


@pytest.mark.asyncio
async def test_semantic_cache_normalized_equivalent_query_has_same_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.rag.cache.get_settings",
        lambda: SimpleNamespace(cache_ttl_hours=24, cache_similarity_threshold=0.95),
    )
    qdrant = FakeQdrant()
    cache = SemanticCache(qdrant, FakeEmbedder())  # type: ignore[arg-type]
    expected = _entry()
    original_query = "  КТО\u00a0ПЛАТИТ ЗА ПРОЕЗД НА ЁЛКУ?  "
    equivalent_query = "кто платит   за проезд на елку?"

    await cache.save(original_query, expected)
    original_point = qdrant.upsert_kwargs["points"][0]
    qdrant.payload = original_point.payload

    assert await cache.check(equivalent_query, "Амур") == expected
    fingerprint_condition = next(
        condition
        for condition in qdrant.query_kwargs["query_filter"].must
        if getattr(condition, "key", None) == "query_fingerprint"
    )
    assert fingerprint_condition.match.value == original_point.payload[
        "query_fingerprint"
    ]

    await cache.save(equivalent_query, expected)
    assert qdrant.upsert_kwargs["points"][0].id == original_point.id


@pytest.mark.asyncio
async def test_semantic_cache_uses_raw_identity_without_storing_raw_pii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.rag.cache.get_settings",
        lambda: SimpleNamespace(cache_ttl_hours=24, cache_similarity_threshold=0.95),
    )
    qdrant = FakeQdrant()
    cache = SemanticCache(qdrant, FakeEmbedder())  # type: ignore[arg-type]
    masked_query = "Что было [ДАТА]?"
    first_identity = "Что было 15.07.2026?"
    second_identity = "Что было 16.08.2026?"

    await cache.save(
        masked_query,
        _entry(forum=None),
        query_identity=first_identity,
    )
    payload = qdrant.upsert_kwargs["points"][0].payload
    assert payload["query_text"] == masked_query
    assert payload["query_fingerprint"] == _query_fingerprint(first_identity)
    assert first_identity not in payload.values()

    qdrant.payload = payload
    assert (
        await cache.check(
            masked_query,
            None,
            query_identity=second_identity,
        )
        is None
    )
    assert (
        await cache.check(
            masked_query,
            None,
            query_identity=first_identity,
        )
        == _entry(forum=None)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constraint", "query"),
    [
        ("age", "Мне 20 лет, когда проходит форум Машук?"),
        ("shift", "Когда проходит первая смена форума Машук?"),
        ("profile", "Когда программа для наставников на форуме Машук?"),
        ("region", "Когда Машук для участников из Томской области?"),
        ("status", "Когда Машук, если моя заявка уже одобрена?"),
    ],
)
async def test_semantic_cache_does_not_cross_query_constraint_scope(
    monkeypatch: pytest.MonkeyPatch,
    constraint: str,
    query: str,
) -> None:
    monkeypatch.setattr(
        "src.rag.cache.get_settings",
        lambda: SimpleNamespace(cache_ttl_hours=24, cache_similarity_threshold=0.95),
    )
    qdrant = FakeQdrant()
    cache = SemanticCache(qdrant, FakeEmbedder())  # type: ignore[arg-type]
    base_query = "Когда проходит форум Машук?"

    await cache.save(base_query, _entry(forum="Машук"))
    qdrant.payload = qdrant.upsert_kwargs["points"][0].payload

    assert await cache.check(query, "Машук") is None, constraint
    fingerprint_condition = next(
        condition
        for condition in qdrant.query_kwargs["query_filter"].must
        if getattr(condition, "key", None) == "query_fingerprint"
    )
    assert fingerprint_condition.match.value != qdrant.payload[
        "query_fingerprint"
    ]


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
async def test_semantic_cache_ignores_pre_correction_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.rag.cache.get_settings",
        lambda: SimpleNamespace(cache_ttl_hours=24, cache_similarity_threshold=0.95),
    )
    qdrant = FakeQdrant()
    cache = SemanticCache(qdrant, FakeEmbedder())  # type: ignore[arg-type]

    await cache.check("Когда опубликуют результаты?", None)

    version_condition = next(
        condition
        for condition in qdrant.query_kwargs["query_filter"].must
        if getattr(condition, "key", None) == "cache_schema_version"
    )
    assert CACHE_SCHEMA_VERSION == 5
    assert version_condition.match.value == 5


@pytest.mark.asyncio
async def test_semantic_cache_ignores_schema_v4_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.rag.cache.get_settings",
        lambda: SimpleNamespace(cache_ttl_hours=24, cache_similarity_threshold=0.95),
    )
    qdrant = FakeQdrant()
    cache = SemanticCache(qdrant, FakeEmbedder())  # type: ignore[arg-type]
    query = "Когда опубликуют результаты?"

    await cache.save(query, _entry(forum=None))
    payload = dict(qdrant.upsert_kwargs["points"][0].payload)
    payload["cache_schema_version"] = 4
    qdrant.payload = payload

    assert await cache.check(query, None) is None


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
    qdrant = FakeQdrant()
    cache = SemanticCache(qdrant, FakeEmbedder())  # type: ignore[arg-type]
    await cache.save("Вопрос", _entry())
    payload = dict(qdrant.upsert_kwargs["points"][0].payload)
    payload["factual_source_type"] = "xlsx"
    qdrant.payload = payload

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
