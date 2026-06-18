from __future__ import annotations

import re
from time import perf_counter

from src.graph.state import BotState

SOURCE_RE = re.compile(r"\s*\[src:[^\]]+\]\s*")
PARTIAL_COVERAGE_NOTE = (
    "По части вопроса в базе знаний нет достаточных подтверждённых данных. "
    "Передаю обращение специалисту, чтобы не дать неточный ответ."
)


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
    if reason != "partial_source_coverage":
        return "Передаю обращение специалисту, чтобы не дать неточный ответ."

    partial_response = SOURCE_RE.sub(" ", state.get("generated_response") or "").strip()
    if not partial_response:
        return PARTIAL_COVERAGE_NOTE
    return f"{partial_response}\n\n{PARTIAL_COVERAGE_NOTE}"
