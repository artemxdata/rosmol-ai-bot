from __future__ import annotations

import asyncio
import gc
import re
from time import perf_counter
from typing import Any

from src.config import get_settings
from src.graph.question_utils import FALLBACK_QUESTION_MARKERS, build_effective_questions
from src.graph.state import BotState
from src.models import Chunk, ScoredChunk
from src.rag.errors import MLDependencyError

MAX_RERANKED_CHUNKS = 8
QUESTION_CANDIDATE_LIMIT = 3
QUERY_CANDIDATE_LIMIT = 3
TOKEN_PATTERN = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)
STOPWORDS = {
    "для",
    "как",
    "какие",
    "какой",
    "есть",
    "или",
    "при",
    "что",
    "это",
    "нужны",
    "нужно",
    "необходимы",
}


async def rerank(state: BotState) -> dict:
    if state.get("should_escalate"):
        return {}

    started_at = perf_counter()
    tracer = state.get("trace")
    chunks = state.get("retrieved_chunks", [])
    query = state.get("message_masked") or state.get("message") or ""
    settings = get_settings()
    if _should_unload_model(settings, "ml_unload_embedder_after_use"):
        await _unload_model_owner(state.get("embedder"))

    try:
        reranked = await asyncio.to_thread(
            _rerank_for_state,
            state["reranker"],
            state,
            query,
            chunks,
        )
    except MLDependencyError as exc:
        if tracer:
            tracer.add_error("rerank", int((perf_counter() - started_at) * 1000), exc)
        return {
            "reranked_chunks": [],
            "max_confidence": 0.0,
            "should_escalate": True,
            "escalation_reason": "ml_dependency_missing",
            "error": str(exc),
        }
    except Exception as exc:
        if tracer:
            tracer.add_error("rerank", int((perf_counter() - started_at) * 1000), exc)
        return {
            "reranked_chunks": [],
            "max_confidence": 0.0,
            "should_escalate": True,
            "escalation_reason": "rerank_failed",
            "error": str(exc),
        }
    finally:
        if _should_unload_model(settings, "ml_unload_reranker_after_use"):
            await _unload_model_owner(state.get("reranker"))

    max_confidence = max((chunk.reranker_score for chunk in reranked), default=0.0)
    confidence_source = "reranker"
    retrieval_confidence_floor = _retrieval_confidence_floor(state, chunks)
    if retrieval_confidence_floor > max_confidence:
        max_confidence = retrieval_confidence_floor
        confidence_source = "retrieval_forum_exact"
    if tracer:
        tracer.add(
            "rerank",
            int((perf_counter() - started_at) * 1000),
            max_confidence=max_confidence,
            confidence_source=confidence_source,
        )
    if max_confidence <= 0:
        return {
            "reranked_chunks": reranked,
            "max_confidence": max_confidence,
            "should_escalate": True,
            "escalation_reason": "no_relevant_chunks",
        }
    result = {"reranked_chunks": reranked, "max_confidence": max_confidence}
    if max_confidence < get_settings().reranker_threshold_low:
        result.update({"should_escalate": True, "escalation_reason": "low_confidence"})
    return result


def _rerank_for_state(
    reranker: Any,
    state: BotState,
    query: str,
    chunks: list[Chunk],
) -> list[ScoredChunk]:
    analysis = state.get("analysis")
    questions = (
        [
            question.text.strip()
            for question in build_effective_questions(analysis, query)
            if question.text.strip()
        ]
        if analysis
        else []
    )
    if len(questions) <= 1:
        rerank_query = questions[0] if questions else query
        candidates = _candidate_chunks_for_question(
            rerank_query,
            chunks,
            min(MAX_RERANKED_CHUNKS, max(4, len(chunks))),
        )
        return reranker.rerank(rerank_query, candidates, 4)

    selected: list[ScoredChunk] = []
    seen: set[str] = set()
    per_question_limit = 2 if len(questions) <= 3 else 1
    group_specs: list[tuple[str, list[Chunk], int]] = []

    for question in questions:
        candidates = _candidate_chunks_for_question(question, chunks, QUESTION_CANDIDATE_LIMIT)
        if candidates:
            _append_chunk(selected, seen, _source_candidate(candidates[0]))
        group_specs.append((question, candidates, per_question_limit))

    target_size = max(4, min(MAX_RERANKED_CHUNKS, len(questions)))
    query_candidates = _candidate_chunks_for_question(query, chunks, QUERY_CANDIDATE_LIMIT)
    group_specs.append((query, query_candidates, 4))
    group_results = _rerank_groups(reranker, group_specs)

    for ranked_chunks in group_results[:-1]:
        for chunk in ranked_chunks:
            if _append_chunk(selected, seen, chunk):
                break

    for chunk in group_results[-1]:
        _append_chunk(selected, seen, chunk)
        if len(selected) >= target_size:
            break

    return selected[:MAX_RERANKED_CHUNKS]


def _rerank_groups(
    reranker: Any,
    groups: list[tuple[str, list[Chunk], int]],
) -> list[list[ScoredChunk]]:
    rerank_groups = getattr(reranker, "rerank_groups", None)
    if callable(rerank_groups):
        return rerank_groups(groups)
    return [reranker.rerank(query, chunks, top_k) for query, chunks, top_k in groups]


def _candidate_chunks_for_question(
    question: str,
    chunks: list[Chunk],
    limit: int,
) -> list[Chunk]:
    question_tokens = _tokens(question)
    if not question_tokens:
        return chunks[:limit]

    scored = []
    for index, chunk in enumerate(chunks):
        metadata = chunk.metadata or {}
        metadata_haystack = " ".join(
            str(value or "")
            for value in (
                metadata.get("intent_name"),
                metadata.get("topic"),
                metadata.get("source_category"),
            )
        )
        text_haystack = chunk.text[:500]
        haystack = f"{metadata_haystack} {text_haystack}"
        overlap = len(question_tokens & _tokens(haystack))
        score = (
            _marker_bonus(question, metadata_haystack, weight=20.0)
            + _marker_bonus(question, text_haystack, weight=8.0)
            + overlap
            + float(chunk.score or 0)
        )
        scored.append((score, -index, chunk))

    ranked = [chunk for score, _, chunk in sorted(scored, reverse=True) if score > 0]
    return ranked[:limit] or chunks[:limit]


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in (
            raw_token.casefold().replace("ё", "е") for raw_token in TOKEN_PATTERN.findall(text)
        )
        if token not in STOPWORDS
    }


def _marker_bonus(question: str, haystack: str, *, weight: float) -> float:
    question_normalized = question.casefold().replace("ё", "е")
    haystack_normalized = haystack.casefold().replace("ё", "е")
    bonus = 0.0
    for markers, _ in FALLBACK_QUESTION_MARKERS:
        if not any(marker in question_normalized for marker in markers):
            continue
        if any(marker in haystack_normalized for marker in markers):
            bonus += weight
    return bonus


def _source_candidate(chunk: Chunk) -> ScoredChunk:
    return ScoredChunk(
        **chunk.model_dump(exclude={"score"}),
        score=chunk.score,
        reranker_score=0.0,
    )


def _append_chunk(
    selected: list[ScoredChunk],
    seen: set[str],
    chunk: ScoredChunk,
) -> bool:
    if chunk.chunk_id in seen:
        for index, existing in enumerate(selected):
            if existing.chunk_id != chunk.chunk_id:
                continue
            if chunk.reranker_score > existing.reranker_score:
                selected[index] = chunk
            return True
        return True
    selected.append(chunk)
    seen.add(chunk.chunk_id)
    return True


async def _unload_model_owner(owner: Any) -> None:
    unload = getattr(owner, "unload", None)
    if not callable(unload):
        return
    await asyncio.to_thread(unload)
    gc.collect()


def _should_unload_model(settings: Any, field_name: str) -> bool:
    explicit_value = getattr(settings, field_name, None)
    if explicit_value is not None:
        return bool(explicit_value)
    return bool(getattr(settings, "ml_unload_after_use", False))


def _retrieval_confidence_floor(state: BotState, chunks: list[Chunk]) -> float:
    analysis = state.get("analysis")
    forum = getattr(analysis, "forum_normalized", None) if analysis else None
    if not forum or not chunks:
        return 0.0

    top_chunk = max(chunks, key=lambda chunk: float(chunk.score or 0.0))
    metadata = top_chunk.metadata or {}
    if metadata.get("forum_normalized") != forum:
        return 0.0
    top_score = float(top_chunk.score or 0.0)
    if top_score < 0.8:
        return 0.0
    settings = get_settings()
    if top_score >= 0.95:
        return float(settings.reranker_threshold_high)
    return float(settings.reranker_threshold_low)
