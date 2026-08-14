from __future__ import annotations

import re
from time import perf_counter
from typing import Any

from src.config import get_settings
from src.graph.answer_plan import current_request_text
from src.graph.question_utils import QueryProvenTopicPlan
from src.graph.state import BotState
from src.llm.cascade import select_analyzer_model
from src.llm.json_utils import parse_llm_json
from src.llm.prompts import (
    SEMANTIC_RECOVERY_SYSTEM,
    build_semantic_recovery_user,
)
from src.models import Complexity, Question

_MAX_MODEL_QUESTIONS = 4
_MAX_QUESTION_CHARS = 320


async def semantic_recovery(state: BotState) -> dict[str, Any]:
    """Rewrite one failed grounded search, once, without producing an answer."""

    started_at = perf_counter()
    tracer = state.get("trace")
    analysis = state.get("analysis")
    reason = str(state.get("escalation_reason") or "low_confidence")
    if analysis is None or state.get("semantic_recovery_attempted"):
        return _failed_recovery(reason=reason)

    model = select_analyzer_model(Complexity.COMPLEX)
    contextual_message = str(
        state.get("contextual_message")
        or current_request_text(state)
    ).strip()
    try:
        content = await state["llm_client"].generate(
            model=model,
            system=SEMANTIC_RECOVERY_SYSTEM,
            user=build_semantic_recovery_user(
                contextual_message,
                analysis,
                failure_reason=reason,
            ),
            response_format="json",
            temperature=0.0,
            max_tokens=420,
        )
        payload = parse_llm_json(content)
        rewritten = _recovery_questions(payload)
        if not rewritten:
            raise ValueError("semantic recovery returned no usable questions")
    except Exception as exc:
        if tracer:
            tracer.add(
                "semantic_recovery",
                int((perf_counter() - started_at) * 1000),
                status="failed",
                reason="semantic_recovery_failed",
                error_type=type(exc).__name__,
            )
        return _failed_recovery(reason=reason)

    max_questions = int(
        getattr(get_settings(), "semantic_recovery_max_questions", 6)
    )
    questions = _merge_questions(
        rewritten,
        list(analysis.questions or []),
        category=analysis.category,
        forum_normalized=analysis.forum_normalized,
        limit=max_questions,
    )
    recovered_analysis = analysis.model_copy(
        update={
            "questions": questions,
            "complexity": Complexity.COMPLEX,
            "needs_clarification": False,
            "clarification_question": None,
            "should_escalate": False,
            "escalation_reason": None,
        }
    )
    if tracer:
        tracer.add(
            "semantic_recovery",
            int((perf_counter() - started_at) * 1000),
            status="ok",
            reason=reason,
            model=model,
            model_questions=len(rewritten),
            effective_questions=len(questions),
        )
    return {
        "analysis": recovered_analysis,
        "answer_plan": QueryProvenTopicPlan(),
        "answer_plan_message": current_request_text(state),
        "semantic_recovery_attempted": True,
        "semantic_recovery_reason": reason,
        "semantic_recovery_question_count": len(questions),
        "retrieved_chunks": [],
        "retrieval_provenance": [],
        "reranked_chunks": [],
        "rerank_provenance": [],
        "max_confidence": 0.0,
        "generated_response": "",
        "cited_sources": [],
        "should_escalate": False,
        "escalation_reason": "",
    }


def _recovery_questions(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_questions[:_MAX_MODEL_QUESTIONS]:
        value = item.get("text") if isinstance(item, dict) else item
        text = _bounded_text(value)
        normalized = _normalized(text)
        if not text or normalized in seen:
            continue
        seen.add(normalized)
        result.append(text)
    return result


def _merge_questions(
    rewritten: list[str],
    existing: list[Question],
    *,
    category: str | None,
    forum_normalized: str | None,
    limit: int,
) -> list[Question]:
    result: list[Question] = []
    seen: set[str] = set()
    candidates = [*rewritten, *(question.text for question in existing)]
    for value in candidates:
        text = _bounded_text(value)
        normalized = _normalized(text)
        if not text or normalized in seen:
            continue
        seen.add(normalized)
        result.append(
            Question(
                text=text,
                topic=None,
                category=category,
                forum_normalized=forum_normalized,
            )
        )
        if len(result) >= limit:
            break
    return result


def _bounded_text(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("[src:", "[")
    return text[:_MAX_QUESTION_CHARS].rstrip()


def _normalized(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _failed_recovery(*, reason: str) -> dict[str, Any]:
    return {
        "semantic_recovery_attempted": True,
        "semantic_recovery_reason": reason,
        "semantic_recovery_question_count": 0,
        "should_escalate": True,
        "escalation_reason": reason,
        "generated_response": "",
        "cited_sources": [],
    }
