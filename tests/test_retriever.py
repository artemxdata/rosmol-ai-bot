from __future__ import annotations

from qdrant_client import models

from src.rag.retriever import build_filter


def test_build_filter_contains_status_and_forum() -> None:
    query_filter = build_filter({"forum_normalized": "Машук", "category": "форумы"})

    assert isinstance(query_filter, models.Filter)
    field_keys = [condition.key for condition in query_filter.must if hasattr(condition, "key")]
    assert "status" in field_keys
    assert "forum_normalized" in field_keys
    assert "category" in field_keys
