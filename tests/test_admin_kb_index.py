from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from scripts.index_kb import qdrant_point_id
from src.admin import kb_index


class FakeEmbedder:
    def encode(self, text: str) -> tuple[np.ndarray, dict[str, float]]:
        assert "Примеры вопросов пользователей" in text
        assert "Ответ:" in text
        return np.asarray([0.1, 0.2], dtype=np.float32), {"7": 0.3, "2": 0.9}


class FakeQdrant:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        self.calls.append(
            {"action": "upsert", "collection_name": collection_name, "points": points}
        )

    async def delete(
        self,
        *,
        collection_name: str,
        points_selector: Any,
        wait: bool,
    ) -> None:
        self.calls.append(
            {
                "action": "delete",
                "collection_name": collection_name,
                "points_selector": points_selector,
                "wait": wait,
            }
        )


@pytest.mark.asyncio
async def test_upsert_chunk_builds_qdrant_point() -> None:
    qdrant = FakeQdrant()

    result = await kb_index.upsert_chunk(
        qdrant,  # type: ignore[arg-type]
        FakeEmbedder(),  # type: ignore[arg-type]
        collection_name="knowledge_base",
        record_payload={
            "chunk_id": "forum_travel",
            "text_clean": "Проезд оплачивает направляющая сторона.",
            "status": "published",
            "forum_normalized": "Амур",
            "category": "форумы",
            "topic": "Оплата проезда",
            "intent_name": "Оплата проезда",
            "intent_examples": ["Кто оплачивает проезд?"],
        },
    )

    assert result == {
        "ok": True,
        "action": "upserted",
        "chunk_id": "forum_travel",
        "collection": "knowledge_base",
        "status": "published",
        "forum_normalized": "Амур",
    }
    assert qdrant.calls[0]["collection_name"] == "knowledge_base"
    point = qdrant.calls[0]["points"][0]
    assert point.payload["chunk_id"] == "forum_travel"
    assert point.payload["text"] == "Проезд оплачивает направляющая сторона."
    assert point.payload["status"] == "published"
    assert point.payload["forum_normalized"] == "Амур"
    assert point.payload["forum_key"]
    assert point.payload["category_key"] == "forums"
    assert point.payload["topic_key"]
    assert "Ответ:\nПроезд оплачивает" in point.payload["embedding_text"]
    assert point.vector["dense"] == [0.10000000149011612, 0.20000000298023224]
    assert point.vector["sparse"].indices == [2, 7]
    assert point.vector["sparse"].values == [0.9, 0.3]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "archived"])
async def test_reindex_non_published_chunk_deletes_deterministic_point(
    status: str,
) -> None:
    qdrant = FakeQdrant()

    class FailIfEncoded:
        def encode(self, _text: str) -> None:
            raise AssertionError("non-published chunks must not be embedded")

    result = await kb_index.upsert_chunk(
        qdrant,  # type: ignore[arg-type]
        FailIfEncoded(),  # type: ignore[arg-type]
        collection_name="knowledge_base",
        record_payload={
            "chunk_id": "forum_travel",
            "text_clean": "Неопубликованный ответ.",
            "status": status,
            "forum_normalized": "Амур",
        },
    )

    assert result == {
        "ok": True,
        "action": "deleted",
        "chunk_id": "forum_travel",
        "collection": "knowledge_base",
        "status": status,
        "forum_normalized": "Амур",
    }
    assert len(qdrant.calls) == 1
    call = qdrant.calls[0]
    assert call["action"] == "delete"
    assert call["collection_name"] == "knowledge_base"
    assert call["points_selector"].points == [qdrant_point_id("forum_travel")]
    assert call["wait"] is True
