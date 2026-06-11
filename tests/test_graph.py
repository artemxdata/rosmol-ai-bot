from __future__ import annotations

import pytest

from src.graph.edges import route_after_analyze, route_after_rerank, route_after_verify
from src.graph.nodes.generate import generate
from src.models import QueryAnalysis, Question, VerificationResult


class FailingLLM:
    async def generate(self, **kwargs):
        raise AssertionError("LLM must not be called without source chunks")


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
