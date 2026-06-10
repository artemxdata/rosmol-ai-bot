from __future__ import annotations

import asyncio
from time import perf_counter

from src.graph.state import BotState


async def rerank(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    chunks = state.get("retrieved_chunks", [])
    query = state.get("message_masked") or state.get("message") or ""
    reranked = await asyncio.to_thread(state["reranker"].rerank, query, chunks, 4)
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
    return {"reranked_chunks": reranked, "max_confidence": max_confidence}
