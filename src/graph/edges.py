from __future__ import annotations

from src.config import get_settings
from src.graph.state import BotState

_SEMANTIC_RECOVERY_REASONS = frozenset(
    {
        "low_confidence",
        "no_relevant_chunks",
        "no_sources_for_generation",
        "insufficient_sources",
    }
)


def route_after_analyze(state: BotState) -> str:
    analysis = state.get("analysis")
    if analysis and analysis.is_offtopic:
        return "clarify"
    if state.get("should_escalate") or (analysis and analysis.should_escalate):
        return "escalate"
    if analysis and analysis.needs_clarification:
        return "clarify"
    return "retrieve"


def route_after_rerank(state: BotState) -> str:
    threshold = get_settings().reranker_threshold_low
    if float(state.get("max_confidence") or 0) < threshold:
        if _can_attempt_semantic_recovery(state):
            return "recover"
        return "escalate"
    if state.get("should_escalate"):
        return "escalate"
    return "generate"


def route_after_generate(state: BotState) -> str:
    if not state.get("should_escalate"):
        return "guard"
    if _can_attempt_semantic_recovery(state):
        return "recover"
    return "escalate"


def route_after_semantic_recovery(state: BotState) -> str:
    if state.get("should_escalate"):
        return "escalate"
    return "retrieve"


def route_after_verify(state: BotState) -> str:
    if state.get("should_escalate"):
        return "escalate"
    verification = state.get("verification")
    if verification and verification.has_hallucination:
        return "escalate"
    return "respond"


def _can_attempt_semantic_recovery(state: BotState) -> bool:
    if state.get("semantic_recovery_attempted"):
        return False
    if not getattr(get_settings(), "semantic_recovery_enabled", True):
        return False
    if state.get("llm_client") is None:
        return False
    analysis = state.get("analysis")
    if analysis is None or analysis.is_offtopic or analysis.should_escalate:
        return False
    return str(state.get("escalation_reason") or "") in _SEMANTIC_RECOVERY_REASONS
