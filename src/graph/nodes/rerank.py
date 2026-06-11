from __future__ import annotations

import asyncio
import gc
from time import perf_counter
from typing import Any

from src.config import get_settings
from src.graph.state import BotState
from src.rag.errors import MLDependencyError


async def rerank(state: BotState) -> dict:
    if state.get("should_escalate"):
        return {}

    started_at = perf_counter()
    tracer = state.get("trace")
    chunks = state.get("retrieved_chunks", [])
    query = state.get("message_masked") or state.get("message") or ""
    settings = get_settings()
    if getattr(settings, "ml_unload_after_use", False):
        await _unload_model_owner(state.get("embedder"))

    try:
        reranked = await asyncio.to_thread(state["reranker"].rerank, query, chunks, 4)
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
        if getattr(settings, "ml_unload_after_use", False):
            await _unload_model_owner(state.get("reranker"))

    max_confidence = max((chunk.reranker_score for chunk in reranked), default=0.0)
    if tracer:
        tracer.add(
            "rerank",
            int((perf_counter() - started_at) * 1000),
            max_confidence=max_confidence,
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


async def _unload_model_owner(owner: Any) -> None:
    unload = getattr(owner, "unload", None)
    if not callable(unload):
        return
    await asyncio.to_thread(unload)
    gc.collect()
