from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.graph.edges import route_after_analyze, route_after_rerank, route_after_verify
from src.graph.nodes.generate import generate
from src.models import Complexity, QueryAnalysis, Question, ScoredChunk, VerificationResult


class FailingLLM:
    async def generate(self, **kwargs):
        raise AssertionError("LLM must not be called in this test")


class CapturingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        return "LLM answer [src:ctx_1]"


def test_route_after_analyze_clarifies() -> None:
    state = {"analysis": QueryAnalysis(needs_clarification=True)}
    assert route_after_analyze(state) == "clarify"


def test_route_after_rerank_escalates_on_low_score() -> None:
    assert route_after_rerank({"max_confidence": 0.1}) == "escalate"


def test_route_after_verify_escalates_on_hallucination() -> None:
    state = {"verification": VerificationResult(has_hallucination=True)}
    assert route_after_verify(state) == "escalate"


@pytest.mark.asyncio
async def test_generate_escalates_without_source_chunks() -> None:
    result = await generate(
        {
            "analysis": QueryAnalysis(
                questions=[Question(text="Кто оплачивает проезд?", category="форумы")]
            ),
            "reranked_chunks": [],
            "llm_client": FailingLLM(),
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "no_sources_for_generation"
    assert result["cited_sources"] == []


@pytest.mark.asyncio
async def test_generate_returns_source_chunk_for_simple_high_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_high=0.7),
    )
    chunk = ScoredChunk(
        chunk_id="ctx_1",
        text="Исходный ответ из базы.",
        metadata={"chunk_id": "ctx_1"},
        score=0.9,
        reranker_score=0.91,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                questions=[Question(text="Кто оплачивает проезд?", category="форумы")]
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.91,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generated_response"] == "Исходный ответ из базы. [src:ctx_1]"
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["ctx_1"]


@pytest.mark.asyncio
async def test_generate_uses_llm_for_complex_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_high=0.7),
    )
    llm = CapturingLLM()
    chunk = ScoredChunk(
        chunk_id="ctx_1",
        text="Исходный ответ из базы.",
        metadata={"chunk_id": "ctx_1"},
        score=0.9,
        reranker_score=0.91,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                questions=[Question(text="Сложный вопрос", category="форумы")],
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.91,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert result["generated_response"] == "LLM answer [src:ctx_1]"
