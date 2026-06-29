from __future__ import annotations

from typing import Any

import numpy as np
import pytest

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
        self.calls.append({"collection_name": collection_name, "points": points})


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
            "intent_name": "Оплата проезда",
            "intent_examples": ["Кто оплачивает проезд?"],
        },
    )

    assert result == {
        "ok": True,
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
    assert point.vector["dense"] == [0.10000000149011612, 0.20000000298023224]
    assert point.vector["sparse"].indices == [2, 7]
    assert point.vector["sparse"].values == [0.9, 0.3]
