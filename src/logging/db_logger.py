from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from src.config import get_settings

SOURCE_RE = re.compile(r"\[src:([^\]]+)\]")


async def log_request(pg_pool: asyncpg.Pool, state: dict[str, Any]) -> None:
    settings = get_settings()
    analysis = state.get("analysis")
    verification = state.get("verification")
    trace = state.get("trace")
    trace_events = trace.as_list() if trace else state.get("trace_events", [])
    await pg_pool.execute(
        """
        INSERT INTO request_traces (
            request_id, channel, user_id_hash, message_masked, routing_hint, query_analysis,
            metadata_filter, retrieved_chunks, reranker_scores, max_reranker_score,
            cache_hit, generator_model, cited_sources, verifier_triggered,
            verifier_result, response_text, was_escalated, escalation_reason,
            llm_usage, llm_prompt_tokens, llm_completion_tokens, llm_total_tokens,
            llm_estimated_cost_rub, total_latency_ms, trace_events, prompt_version, error,
            upstream_event_id, upstream_event_id_source, ticket_id_hash,
            eval_run_id, eval_case_id, ticket_outcome
        )
        VALUES (
            $1, $2, $3, $4, $5::jsonb, $6::jsonb,
            $7::jsonb, $8::jsonb, $9::jsonb, $10,
            $11, $12, $13, $14,
            $15::jsonb, $16, $17, $18,
            $19::jsonb, $20, $21, $22,
            $23, $24, $25::jsonb, $26, $27,
            $28, $29, $30, $31, $32, $33
        )
        """,
        state["request_id"],
        state.get("channel"),
        state.get("user_id_hash"),
        state.get("message_masked"),
        json.dumps(state.get("routing_hint") or {}, ensure_ascii=False),
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
        _cited_sources_from_state(state),
        bool(state.get("verifier_triggered")),
        json.dumps(verification.model_dump() if verification else None, ensure_ascii=False),
        state.get("final_response") or state.get("generated_response"),
        bool(state.get("should_escalate")),
        state.get("escalation_reason"),
        json.dumps(state.get("llm_usage") or [], ensure_ascii=False),
        state.get("llm_prompt_tokens", 0),
        state.get("llm_completion_tokens", 0),
        state.get("llm_total_tokens", 0),
        state.get("llm_estimated_cost_rub", 0.0),
        state.get("total_latency_ms"),
        json.dumps(trace_events, ensure_ascii=False),
        settings.prompt_version,
        state.get("error"),
        state.get("upstream_event_id"),
        state.get("upstream_event_id_source"),
        state.get("user_id_hash"),
        state.get("eval_run_id"),
        state.get("eval_case_id"),
        _ticket_outcome_from_state(state),
    )


async def update_delivery_outcome(
    pg_pool: asyncpg.Pool,
    request_id: UUID,
    *,
    status: str,
    attempted: bool,
    http_status: int | None = None,
    retry_after_seconds: float | None = None,
    error_code: str | None = None,
) -> None:
    await pg_pool.execute(
        """
        UPDATE request_traces
        SET delivery_status = $2,
            delivery_attempted = $3,
            delivery_http_status = $4,
            delivery_retry_after_seconds = $5,
            delivery_error_code = $6,
            delivered_at = CASE WHEN $2 = 'delivered' THEN $7::timestamptz ELSE NULL END
        WHERE request_id = $1
        """,
        request_id,
        status,
        attempted,
        http_status,
        retry_after_seconds,
        error_code,
        datetime.now(UTC),
    )


def _ticket_outcome_from_state(state: dict[str, Any]) -> str:
    if state.get("error"):
        return "error"
    if state.get("should_escalate") or state.get("was_escalated"):
        return "escalated"
    if state.get("escalation_reason") == "rate_limited":
        return "rate_limited"
    if not (state.get("final_response") or state.get("generated_response")):
        return "no_response"
    analysis = state.get("analysis")
    needs_clarification = getattr(analysis, "needs_clarification", False)
    if isinstance(analysis, dict):
        needs_clarification = analysis.get("needs_clarification", False)
    if bool(needs_clarification):
        return "clarification"
    return "answered"


def _cited_sources_from_state(state: dict[str, Any]) -> list[str]:
    cited_sources = [str(item) for item in state.get("cited_sources") or [] if item]
    if cited_sources:
        return cited_sources
    seen: set[str] = set()
    recovered: list[str] = []
    for chunk_id in SOURCE_RE.findall(str(state.get("generated_response") or "")):
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        recovered.append(chunk_id)
    return recovered
