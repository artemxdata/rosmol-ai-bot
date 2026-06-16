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


@pytest.mark.asyncio
async def test_verifier_escalates_when_answer_admits_sources_are_insufficient() -> None:
    result = await verify(
        {
            "generated_response": (
                "В предоставленных источниках нет информации о возврате средств. "
                "Рекомендую направить ваш вопрос специалисту службы поддержки."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.7)
            ],
            "max_confidence": 0.7,
        }
    )

    assert result["verification"].has_hallucination is False
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"
    assert result["verifier_triggered"] is False


@pytest.mark.asyncio
async def test_verifier_escalates_polite_specialist_handoff_for_missing_facts() -> None:
    result = await verify(
        {
            "generated_response": (
                "В предоставленных источниках нет информации о порядке обращения. "
                "Пожалуйста, передайте ваш вопрос специалисту для получения точной консультации."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.7)
            ],
            "max_confidence": 0.7,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_when_answer_says_info_absent_and_redirects() -> None:
    result = await verify(
        {
            "generated_response": (
                "Из представленных источников невозможно ответить на вопрос. "
                "Информация об условиях отсутствует. Рекомендую обратиться к специалистам."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.8)
            ],
            "max_confidence": 0.8,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_missing_concrete_info_with_care_service_redirect() -> None:
    result = await verify(
        {
            "generated_response": (
                "В предоставленных источниках нет конкретной информации о возврате средств. "
                "Рекомендую обратиться напрямую в Службу Заботы Росмолодёжи."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.7)
            ],
            "max_confidence": 0.7,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_missing_precise_info_with_support_redirect() -> None:
    result = await verify(
        {
            "generated_response": (
                "На данный момент точной информации о различиях между форумами нет. "
                "Рекомендуем обратиться непосредственно в службу поддержки Росмолодёжи."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.8)
            ],
            "max_confidence": 0.8,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_missing_info_with_direct_handoff() -> None:
    result = await verify(
        {
            "generated_response": (
                "Точная информация о различиях между форумами отсутствует. "
                "Передам ваш запрос специалисту для уточнения деталей."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.8)
            ],
            "max_confidence": 0.8,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_missing_info_with_this_request_handoff() -> None:
    result = await verify(
        {
            "generated_response": (
                "Информация о порядке оплаты проезда на форуме Машук отсутствует. "
                "Передайте этот запрос специалисту для уточнения деталей."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.8)
            ],
            "max_confidence": 0.8,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_allows_support_instruction_when_sources_are_sufficient() -> None:
    llm = JudgeLLM('{"has_hallucination": false, "confidence": 0.9, "details": "grounded"}')

    result = await verify(
        {
            "generated_response": (
                "Если проблема сохраняется, обратитесь в службу поддержки с описанием ошибки."
            ),
            "generator_model": "GigaChat/GigaChat-2-Max",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="ctx_1",
                    text="При технической ошибке обратитесь в службу поддержки.",
                    metadata={},
                    reranker_score=0.7,
                )
            ],
            "max_confidence": 0.7,
            "llm_client": llm,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result
    assert llm.calls == 1
