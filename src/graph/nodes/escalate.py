from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState


async def escalate(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    reason = state.get("escalation_reason") or "needs_operator"
    if tracer:
        tracer.add("escalate", int((perf_counter() - started_at) * 1000), reason=reason)
    return {
        "should_escalate": True,
        "escalation_reason": reason,
        "final_response": "Передаю обращение специалисту, чтобы не дать неточный ответ.",
    }
