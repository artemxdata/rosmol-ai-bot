from __future__ import annotations

import numpy as np
import pytest
from qdrant_client import models

from src.rag.retriever import Retriever, build_filter


def test_build_filter_contains_status_and_derived_keys() -> None:
    query_filter = build_filter(
        {
            "forum_normalized": "Машук",
            "category": "форумы",
            "source_type": "ticket_answer_bank",
        }
    )

    assert isinstance(query_filter, models.Filter)
    field_keys = [condition.key for condition in query_filter.must if hasattr(condition, "key")]
    assert "status" in field_keys
    assert "forum_key" in field_keys
    assert "category_key" in field_keys
    assert "source_type" in field_keys


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def encode(self, query: str):
        self.calls += 1
        return np.array([0.1, 0.2]), {1: 0.5}


class FakeQdrant:
    def __init__(self) -> None:
        self.kwargs = None

    async def query_points(self, **kwargs):
        self.kwargs = kwargs

        class Result:
            points = []

        return Result()


class FakeScrollQdrant(FakeQdrant):
    def __init__(self) -> None:
        super().__init__()
        self.scroll_kwargs = None
        self.scroll_calls = []

    async def scroll(self, **kwargs):
        self.scroll_kwargs = kwargs
        self.scroll_calls.append(kwargs)

        class Point:
            def __init__(self, point_id: str, payload: dict) -> None:
                self.id = point_id
                self.payload = payload

        return (
            [
                Point(
                    "generic",
                    {
                        "chunk_id": "generic",
                        "source_type": "ticket_answer_bank",
                        "category": "гранты",
                        "text_clean": "Generic grant reporting answer.",
                        "intent_examples": ["How to submit a report?"],
                    },
                ),
                Point(
                    "exact",
                    {
                        "chunk_id": "exact",
                        "source_type": "ticket_answer_bank",
                        "category": "гранты",
                        "text_clean": "Use the personal account for the exact report case.",
                        "intent_examples": ["Can I upload a grant report after correction?"],
                    },
                ),
            ],
            None,
        )


class FakeRawFallbackScrollQdrant(FakeQdrant):
    def __init__(self) -> None:
        super().__init__()
        self.scroll_calls = []

    async def scroll(self, **kwargs):
        self.scroll_calls.append(kwargs)

        class Point:
            def __init__(self, point_id: str, payload: dict) -> None:
                self.id = point_id
                self.payload = payload

        if len(self.scroll_calls) == 1:
            return ([], None)

        return (
            [
                Point(
                    "date",
                    {
                        "chunk_id": "date",
                        "source_type": "xlsx",
                        "category": "форумы",
                        "forum_normalized": "День молодёжи",
                        "topic": "sut_festivalya_i_data",
                        "text_clean": "27 июня 2026 года пройдёт День молодёжи.",
                    },
                )
            ],
            None,
        )


@pytest.mark.asyncio
async def test_retriever_applies_filter_to_prefetches() -> None:
    qdrant = FakeQdrant()
    retriever = Retriever(
        qdrant,
        FakeEmbedder(),  # type: ignore[arg-type]
        collection_name="knowledge_base_sandbox",
    )

    await retriever.retrieve("гранты", {"category": "гранты"}, top_k=5)

    assert qdrant.kwargs is not None
    assert qdrant.kwargs["collection_name"] == "knowledge_base_sandbox"
    query_filter = qdrant.kwargs["query_filter"]
    assert qdrant.kwargs["prefetch"][0].filter == query_filter
    assert qdrant.kwargs["prefetch"][1].filter == query_filter


@pytest.mark.asyncio
async def test_retriever_reuses_query_embedding_for_filter_attempts() -> None:
    qdrant = FakeQdrant()
    embedder = FakeEmbedder()
    retriever = Retriever(
        qdrant,
        embedder,  # type: ignore[arg-type]
        collection_name="knowledge_base_sandbox",
    )

    await retriever.retrieve("статус заявки", {"category": "платформа_фгаис"}, top_k=5)
    await retriever.retrieve("статус заявки", {}, top_k=5)

    assert embedder.calls == 1


@pytest.mark.asyncio
async def test_retriever_metadata_lookup_does_not_embed_query() -> None:
    qdrant = FakeScrollQdrant()
    embedder = FakeEmbedder()
    retriever = Retriever(
        qdrant,
        embedder,  # type: ignore[arg-type]
        collection_name="knowledge_base_sandbox",
    )

    chunks = await retriever.retrieve_by_metadata(
        {"category": "форумы", "forum_normalized": "День молодёжи", "topic": "programma"},
        top_k=3,
    )

    assert chunks
    assert embedder.calls == 0
    assert qdrant.scroll_kwargs is not None
    assert qdrant.scroll_kwargs["collection_name"] == "knowledge_base_sandbox"
    assert qdrant.scroll_kwargs["limit"] == 3
    field_keys = [
        condition.key
        for condition in qdrant.scroll_kwargs["scroll_filter"].must
        if hasattr(condition, "key")
    ]
    assert "category_key" in field_keys
    assert "forum_key" in field_keys
    assert "topic_key" in field_keys


@pytest.mark.asyncio
async def test_retriever_metadata_lookup_falls_back_to_raw_payload_fields() -> None:
    qdrant = FakeRawFallbackScrollQdrant()
    embedder = FakeEmbedder()
    retriever = Retriever(
        qdrant,
        embedder,  # type: ignore[arg-type]
        collection_name="knowledge_base_sandbox",
    )

    chunks = await retriever.retrieve_by_metadata(
        {
            "category": "форумы",
            "forum_normalized": "День молодёжи",
            "topic": "sut_festivalya_i_data",
        },
        top_k=3,
    )

    assert [chunk.chunk_id for chunk in chunks] == ["date"]
    assert embedder.calls == 0
    assert len(qdrant.scroll_calls) == 2
    first_field_keys = [
        condition.key
        for condition in qdrant.scroll_calls[0]["scroll_filter"].must
        if hasattr(condition, "key")
    ]
    second_field_keys = [
        condition.key
        for condition in qdrant.scroll_calls[1]["scroll_filter"].must
        if hasattr(condition, "key")
    ]
    assert "forum_key" in first_field_keys
    assert "category_key" in first_field_keys
    assert "topic_key" in first_field_keys
    assert "forum_normalized" in second_field_keys
    assert "category" in second_field_keys
    assert "topic" in second_field_keys


@pytest.mark.asyncio
async def test_retriever_keyword_candidates_prioritize_exact_intent_examples() -> None:
    qdrant = FakeScrollQdrant()
    retriever = Retriever(
        qdrant,
        FakeEmbedder(),  # type: ignore[arg-type]
        collection_name="knowledge_base_sandbox",
    )

    chunks = await retriever.retrieve_keyword_candidates(
        "Can I upload a grant report after correction?",
        {"category": "гранты"},
        top_k=2,
    )

    assert [chunk.chunk_id for chunk in chunks] == ["exact"]
    assert qdrant.scroll_kwargs is not None
    assert qdrant.scroll_kwargs["collection_name"] == "knowledge_base_sandbox"
    field_keys = [
        condition.key
        for condition in qdrant.scroll_kwargs["scroll_filter"].must
        if hasattr(condition, "key")
    ]
    assert "source_type" in field_keys
