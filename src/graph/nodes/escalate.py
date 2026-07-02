from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState

PARTIAL_COVERAGE_NOTE = (
    "По части вопроса в базе знаний нет достаточных подтверждённых данных. "
    "Передаю обращение специалисту, чтобы не дать неточный ответ."
)
AMBIGUOUS_FORUM_NOTE = (
    "Уточни, пожалуйста, название форума или мероприятия. "
    "По твоему вопросу найдены похожие источники по разным событиям, "
    "и я не хочу смешать условия."
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
    if reason == "ambiguous_forum_context":
        return AMBIGUOUS_FORUM_NOTE
    if reason == "partial_source_coverage":
        return PARTIAL_COVERAGE_NOTE
    return "Передаю обращение специалисту, чтобы не дать неточный ответ."
