from __future__ import annotations

from time import perf_counter

from src.graph.question_utils import build_effective_questions
from src.graph.state import BotState
from src.rag.errors import MLDependencyError


async def retrieve(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    analysis = state.get("analysis")
    if analysis is None:
        return {"retrieved_chunks": [], "metadata_filter": {}}

    filters = {
        "forum_normalized": analysis.forum_normalized,
        "category": analysis.category,
    }
    questions = build_effective_questions(
        analysis,
        state.get("message_masked") or state.get("message"),
    )

    chunks = []
    used_filters: list[dict] = []
    for question in questions:
        question_filters = {
            **filters,
            "topic": question.topic,
            "forum_normalized": question.forum_normalized or filters.get("forum_normalized"),
            "category": question.category or filters.get("category"),
        }
        try:
            found = []
            for candidate_filters in _filter_attempts(question_filters):
                used_filters.append(candidate_filters)
                found = await state["retriever"].retrieve(
                    question.text,
                    candidate_filters,
                    top_k=10,
                )
                if found:
                    break
            chunks.extend(found)
        except MLDependencyError as exc:
            if tracer:
                tracer.add_error("retrieve", int((perf_counter() - started_at) * 1000), exc)
            return {
                "retrieved_chunks": [],
                "metadata_filter": question_filters,
                "should_escalate": True,
                "escalation_reason": "ml_dependency_missing",
                "error": str(exc),
            }
        except Exception as exc:
            if tracer:
                tracer.add_error("retrieve", int((perf_counter() - started_at) * 1000), exc)
            return {
                "retrieved_chunks": [],
                "metadata_filter": question_filters,
                "should_escalate": True,
                "escalation_reason": "retrieval_failed",
                "error": str(exc),
            }

    deduped = {chunk.chunk_id: chunk for chunk in chunks}
    if tracer:
        tracer.add(
            "retrieve",
            int((perf_counter() - started_at) * 1000),
            chunks=len(deduped),
            filters=filters,
            filter_attempts=used_filters,
        )
    return {
        "retrieved_chunks": list(deduped.values()),
        "metadata_filter": filters,
        "retrieval_filter_attempts": used_filters,
    }


def _filter_attempts(filters: dict) -> list[dict]:
    attempts = [_compact_filter(filters)]
    forum = filters.get("forum_normalized")
    if forum:
        attempts.append(_compact_filter({**filters, "category": None, "topic": None}))
    return _dedupe_filters(attempts)


def _compact_filter(filters: dict) -> dict:
    return {key: value for key, value in filters.items() if value}


def _dedupe_filters(filters: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple[tuple[str, object], ...]] = set()
    for item in filters:
        key = tuple(sorted(item.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
