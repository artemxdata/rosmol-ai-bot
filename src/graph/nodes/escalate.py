from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState
from src.response_contract import get_response_contract

_RESPONSE_CONTRACT = get_response_contract()
OPERATOR_TRANSFER_RESPONSE = _RESPONSE_CONTRACT.message(
    "operator_transfer"
).select_text()
PARTIAL_COVERAGE_NOTE = OPERATOR_TRANSFER_RESPONSE
AMBIGUOUS_FORUM_NOTE = OPERATOR_TRANSFER_RESPONSE


async def escalate(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    reason = state.get("escalation_reason") or "needs_operator"
    if tracer:
        tracer.add("escalate", int((perf_counter() - started_at) * 1000), reason=reason)
    return {
        "should_escalate": True,
        "escalation_reason": reason,
        "final_response": _escalation_response(state, reason),
    }


def _escalation_response(state: BotState, reason: str) -> str:
    return OPERATOR_TRANSFER_RESPONSE
