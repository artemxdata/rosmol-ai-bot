from __future__ import annotations

from src.config import get_settings
from src.graph.state import BotState


def route_after_analyze(state: BotState) -> str:
    analysis = state.get("analysis")
    if state.get("should_escalate") or (analysis and analysis.should_escalate):
        return "escalate"
    if analysis and analysis.needs_clarification:
        return "clarify"
    return "retrieve"


def route_after_rerank(state: BotState) -> str:
    threshold = get_settings().reranker_threshold_low
    if float(state.get("max_confidence") or 0) < threshold:
        return "escalate"
    return "generate"


def route_after_verify(state: BotState) -> str:
    verification = state.get("verification")
    if verification and verification.has_hallucination:
        return "escalate"
    return "respond"
