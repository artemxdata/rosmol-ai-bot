from __future__ import annotations

from src.graph.edges import route_after_analyze, route_after_rerank, route_after_verify
from src.models import QueryAnalysis, VerificationResult


def test_route_after_analyze_clarifies() -> None:
    state = {"analysis": QueryAnalysis(needs_clarification=True)}
    assert route_after_analyze(state) == "clarify"


def test_route_after_rerank_escalates_on_low_score() -> None:
    assert route_after_rerank({"max_confidence": 0.1}) == "escalate"


def test_route_after_verify_escalates_on_hallucination() -> None:
    state = {"verification": VerificationResult(has_hallucination=True)}
    assert route_after_verify(state) == "escalate"
