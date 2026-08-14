from __future__ import annotations

from typing import Any, TypedDict
from uuid import UUID

from src.graph.question_utils import QueryProvenTopicPlan
from src.logging.tracer import Tracer
from src.models import Chunk, QueryAnalysis, ScoredChunk, Session, VerificationResult


class BotState(TypedDict, total=False):
    request_id: UUID
    channel: str
    user_id: str
    user_id_hash: str
    upstream_event_id: str | None
    upstream_event_id_source: str | None
    eval_run_id: str | None
    eval_case_id: str | None
    message: str
    message_masked: str
    contextual_message: str
    routing_hint: dict[str, Any]
    session: Session
    analysis: QueryAnalysis
    analyzer_mode: str
    analyzer_fallback: bool
    answer_plan: QueryProvenTopicPlan
    answer_plan_message: str
    metadata_filter: dict[str, Any]
    retrieved_chunks: list[Chunk]
    retrieval_provenance: list[dict[str, Any]]
    reranked_chunks: list[ScoredChunk]
    rerank_provenance: list[dict[str, Any]]
    max_confidence: float
    semantic_recovery_attempted: bool
    semantic_recovery_reason: str
    semantic_recovery_question_count: int
    generated_response: str
    response_guard: str
    final_response: str
    cited_sources: list[str]
    verification: VerificationResult
    verifier_triggered: bool
    generator_model: str
    should_escalate: bool
    escalation_reason: str
    cache_hit: bool
    total_latency_ms: int
    error: str
    trace: Tracer
    llm_client: Any
    embedder: Any
    retriever: Any
    reranker: Any
