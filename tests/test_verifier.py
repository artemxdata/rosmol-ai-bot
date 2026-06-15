from __future__ import annotations

import pytest

from src.graph.nodes.verify import verify
from src.models import ScoredChunk


class JudgeLLM:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    async def generate(self, **kwargs) -> str:
        self.calls += 1
        return self.payload


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
            "generator_model": "source_chunk",
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.9)
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert result["verifier_triggered"] is False


@pytest.mark.asyncio
async def test_verifier_judges_llm_generation_even_with_high_confidence() -> None:
    llm = JudgeLLM('{"has_hallucination": false, "confidence": 0.88, "details": "grounded"}')

    result = await verify(
        {
            "generated_response": "Ответ по источнику.",
            "generator_model": "GigaChat/GigaChat-2-Max",
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.9)
            ],
            "max_confidence": 0.9,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert result["verification"].has_hallucination is False
    assert result["verification"].triggered_llm_judge is True
    assert result["verifier_triggered"] is True


@pytest.mark.asyncio
async def test_verifier_rejects_no_question_response_when_user_asked_question() -> None:
    result = await verify(
        {
            "message_masked": "Какие документы нужны на Российский Север?",
            "generated_response": "Похоже, у вас пока нет вопросов. Задайте ваш вопрос.",
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.9)
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is True
    assert result["verifier_triggered"] is False
