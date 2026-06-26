from __future__ import annotations

from typing import Any, TypedDict
from uuid import UUID

from src.logging.tracer import Tracer
from src.models import Chunk, QueryAnalysis, ScoredChunk, Session, VerificationResult


class BotState(TypedDict, total=False):
    request_id: UUID
    channel: str
    user_id: str
    user_id_hash: str
    message: str
    message_masked: str
    contextual_message: str
    routing_hint: dict[str, Any]
    session: Session
    analysis: QueryAnalysis
    metadata_filter: dict[str, Any]
    retrieved_chunks: list[Chunk]
    reranked_chunks: list[ScoredChunk]
    max_confidence: float
    generated_response: str
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
