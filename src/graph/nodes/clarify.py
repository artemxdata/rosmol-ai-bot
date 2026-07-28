from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState
from src.response_contract import get_response_contract

_RESPONSE_CONTRACT = get_response_contract()
OFFTOPIC_SCOPE_NOTE = _RESPONSE_CONTRACT.message("capabilities").select_text()
UNKNOWN_FORUM_RESPONSE = _RESPONSE_CONTRACT.message("unknown_forum").select_text()


async def clarify(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    analysis = state.get("analysis")
    if analysis and analysis.is_offtopic:
        text = OFFTOPIC_SCOPE_NOTE
    else:
        text = (
            analysis.clarification_question
            if analysis and analysis.clarification_question
            else UNKNOWN_FORUM_RESPONSE
        )
    if tracer:
        tracer.add(
            "clarify",
            int((perf_counter() - started_at) * 1000),
            offtopic=bool(analysis and analysis.is_offtopic),
        )
    return {
        "final_response": text,
        "should_escalate": (
            False if analysis and analysis.is_offtopic else state.get("should_escalate", False)
        ),
        "escalation_reason": (
            None if analysis and analysis.is_offtopic else state.get("escalation_reason")
        ),
    }
