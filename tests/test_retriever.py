from __future__ import annotations

import numpy as np
import pytest
from qdrant_client import models

from src.rag.retriever import Retriever, build_filter


def test_build_filter_contains_status_and_derived_keys() -> None:
    query_filter = build_filter({"forum_normalized": "Машук", "category": "форумы"})

    assert isinstance(query_filter, models.Filter)
    field_keys = [condition.key for condition in query_filter.must if hasattr(condition, "key")]
    assert "status" in field_keys
    assert "forum_key" in field_keys
    assert "category_key" in field_keys


class FakeEmbedder:
    def encode(self, query: str):
        return np.array([0.1, 0.2]), {1: 0.5}


class FakeQdrant:
    def __init__(self) -> None:
        self.kwargs = None

    async def query_points(self, **kwargs):
        self.kwargs = kwargs

        class Result:
            points = []

        return Result()


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
