from __future__ import annotations

import re
from time import perf_counter

from src.graph.state import BotState

SOURCE_RE = re.compile(r"\s*\[src:[^\]]+\]\s*")
FULL_COVERAGE_CLAIM_RE = re.compile(
    r"(?im)^\s*источники\s+полностью\s+покрывают[^\n]*(?:\n|$)"
)
PARTIAL_COVERAGE_NOTE = (
    "По части вопроса в базе знаний нет достаточных подтверждённых данных. "
    "Передаю обращение специалисту, чтобы не дать неточный ответ."
)
AMBIGUOUS_FORUM_NOTE = (
    "Уточните, пожалуйста, название форума или мероприятия. "
    "По вашему вопросу найдены похожие источники по разным событиям, "
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
    if reason != "partial_source_coverage":
        return "Передаю обращение специалисту, чтобы не дать неточный ответ."

    partial_response = _clean_partial_response(state.get("generated_response") or "")
    if not partial_response:
        return PARTIAL_COVERAGE_NOTE
    return f"{partial_response}\n\n{PARTIAL_COVERAGE_NOTE}"


def _clean_partial_response(response: str) -> str:
    without_sources = SOURCE_RE.sub(" ", response)
    without_claims = FULL_COVERAGE_CLAIM_RE.sub("", without_sources)
    return without_claims.strip()
