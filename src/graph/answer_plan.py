from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter
from typing import Any

from src.graph.question_utils import (
    QueryProvenTopicPlan,
    build_query_proven_topic_plan,
)
from src.graph.state import BotState
from src.models import QueryAnalysis


def plan_answer(state: BotState) -> dict[str, Any]:
    """Freeze one current-request plan for all grounded RAG stages."""

    started_at = perf_counter()
    analysis = state.get("analysis")
    request_text = current_request_text(state)
    plan = build_query_proven_topic_plan(analysis, request_text)
    tracer = state.get("trace")
    if tracer:
        tracer.add(
            "plan",
            int((perf_counter() - started_at) * 1000),
            questions=len(plan.questions),
            source_aspects=len(plan.source_aspects),
            clauses=len(plan.clauses),
            unmapped_clauses=len(plan.unmapped_clauses),
            incomplete=plan.incomplete,
        )
    return {
        "answer_plan": plan,
        "answer_plan_message": request_text,
    }


def answer_plan_for_state(
    state: Mapping[str, Any],
    analysis: QueryAnalysis | None = None,
    message: str | None = None,
) -> QueryProvenTopicPlan:
    """Return the frozen plan, rebuilding only for direct or stale node calls."""

    request_text = current_request_text(state) if message is None else str(message)
    stored = state.get("answer_plan")
    stored_message = str(state.get("answer_plan_message") or "")
    if (
        isinstance(stored, QueryProvenTopicPlan)
        and _message_key(stored_message) == _message_key(request_text)
    ):
        return stored
    effective_analysis = analysis
    if effective_analysis is None:
        candidate = state.get("analysis")
        effective_analysis = candidate if isinstance(candidate, QueryAnalysis) else None
    return build_query_proven_topic_plan(effective_analysis, request_text)


def current_request_text(state: Mapping[str, Any]) -> str:
    message = str(state.get("message_masked") or state.get("message") or "")
    pending, separator, _clarification = message.partition(
        "\nУточнение пользователя:"
    )
    return pending.strip() if separator and pending.strip() else message.strip()


def _message_key(value: str) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())
