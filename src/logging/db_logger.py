from __future__ import annotations

import json
from typing import Any

import asyncpg

from src.config import get_settings


async def log_request(pg_pool: asyncpg.Pool, state: dict[str, Any]) -> None:
    settings = get_settings()
    analysis = state.get("analysis")
    verification = state.get("verification")
    trace = state.get("trace")
    trace_events = trace.as_list() if trace else state.get("trace_events", [])
    await pg_pool.execute(
        """
        INSERT INTO request_traces (
            request_id, channel, user_id_hash, message_masked, query_analysis,
            metadata_filter, retrieved_chunks, reranker_scores, max_reranker_score,
            cache_hit, generator_model, cited_sources, verifier_triggered,
            verifier_result, response_text, was_escalated, escalation_reason,
            total_latency_ms, trace_events, prompt_version, error
        )
        VALUES (
            $1, $2, $3, $4, $5::jsonb,
            $6::jsonb, $7::jsonb, $8::jsonb, $9,
            $10, $11, $12, $13,
            $14::jsonb, $15, $16, $17,
            $18, $19::jsonb, $20, $21
        )
        """,
        state["request_id"],
        state.get("channel"),
        state.get("user_id_hash"),
        state.get("message_masked"),
        json.dumps(analysis.model_dump(mode="json") if analysis else None, ensure_ascii=False),
        json.dumps(state.get("metadata_filter"), ensure_ascii=False),
        json.dumps(
            [chunk.model_dump() for chunk in state.get("retrieved_chunks", [])],
            ensure_ascii=False,
        ),
        json.dumps(
            [chunk.model_dump() for chunk in state.get("reranked_chunks", [])],
            ensure_ascii=False,
        ),
        state.get("max_confidence"),
        state.get("cache_hit", False),
        state.get("generator_model"),
        state.get("cited_sources") or [],
        bool(state.get("verifier_triggered")),
        json.dumps(verification.model_dump() if verification else None, ensure_ascii=False),
        state.get("final_response") or state.get("generated_response"),
        bool(state.get("should_escalate")),
        state.get("escalation_reason"),
        state.get("total_latency_ms"),
        json.dumps(trace_events, ensure_ascii=False),
        settings.prompt_version,
        state.get("error"),
    )
