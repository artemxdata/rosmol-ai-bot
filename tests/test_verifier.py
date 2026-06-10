from __future__ import annotations

import pytest

from src.graph.nodes.verify import verify
from src.models import ScoredChunk


@pytest.mark.asyncio
async def test_verifier_rejects_unknown_source_marker() -> None:
    result = await verify(
        {
            "generated_response": "Ответ [src:missing]",
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.9)
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is True


@pytest.mark.asyncio
async def test_verifier_accepts_high_confidence_without_judge() -> None:
    result = await verify(
        {
            "generated_response": "Ответ [src:ctx_1]",
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.9)
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert result["verifier_triggered"] is False
