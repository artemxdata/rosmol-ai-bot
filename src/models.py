from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from src.response_contract import ResponseProfileName


class Channel(StrEnum):
    VK = "vk"
    MAX = "max"
    HDE = "hde"
    API = "api"


class Complexity(StrEnum):
    SIMPLE = "simple"
    COMPLEX = "complex"


class IncomingMessage(BaseModel):
    user_id: str
    channel: Channel
    text: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    request_id: UUID = Field(default_factory=uuid4)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    forum_context: str | None = None
    upstream_event_id: str | None = Field(default=None, max_length=255)
    upstream_event_id_source: str | None = Field(default=None, max_length=100)
    eval_run_id: str | None = Field(default=None, max_length=200)
    eval_case_id: str | None = Field(default=None, max_length=200)


class OutgoingMessage(BaseModel):
    user_id: str
    channel: Channel
    text: str
    request_id: UUID


class Session(BaseModel):
    user_id: str
    channel: Channel
    user_id_hash: str
    forum_context: str | None = None
    user_age: int | None = None
    user_region: str | None = None
    last_messages: list[dict[str, str]] = Field(default_factory=list)
    turn_count: int = 0
    extracted_entities: dict[str, Any] = Field(default_factory=dict)
    pending_clarification: str | None = None
    clarification_attempts: int = Field(default=0, ge=0)
    clarification_history: list[str] = Field(default_factory=list)
    conversation_summary: str | None = None


class MemoryRecord(BaseModel):
    user_id_hash: str
    channel: Channel
    last_forum: str | None = None
    last_topics: list[str] = Field(default_factory=list)
    turn_summary: str | None = None
    structured_context: dict[str, Any] = Field(default_factory=dict)
    interaction_count: int = 0
    last_interaction: datetime | None = None


class Question(BaseModel):
    text: str
    topic: str | None = None
    category: str | None = None
    forum_normalized: str | None = None


class QueryAnalysis(BaseModel):
    forum: str | None = None
    forum_normalized: str | None = None
    questions: list[Question] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    category: str | None = None
    complexity: Complexity = Complexity.SIMPLE
    needs_clarification: bool = False
    clarification_question: str | None = None
    should_escalate: bool = False
    escalation_reason: str | None = None
    is_technical: bool = False
    is_offtopic: bool = False
    response_profile: ResponseProfileName = ResponseProfileName.GENERIC
    extracted_params: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    chunk_id: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None


class ScoredChunk(Chunk):
    reranker_score: float


class TraceEvent(BaseModel):
    node: str
    latency_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class VerificationResult(BaseModel):
    has_hallucination: bool = False
    confidence: float = 1.0
    details: str | None = None
    triggered_llm_judge: bool = False


class TraceRecord(BaseModel):
    request_id: UUID
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    channel: Channel
    user_id_hash: str
    message_masked: str
    routing_hint: dict[str, Any] = Field(default_factory=dict)
    query_analysis: dict[str, Any] | None = None
    retrieved_chunks: list[dict[str, Any]] = Field(default_factory=list)
    reranker_scores: list[dict[str, Any]] = Field(default_factory=list)
    cache_hit: bool = False
    generator_model: str | None = None
    cited_sources: list[str] = Field(default_factory=list)
    verifier_triggered: bool = False
    response_text: str | None = None
    was_escalated: bool = False
    escalation_reason: str | None = None
    llm_usage: list[dict[str, Any]] = Field(default_factory=list)
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_total_tokens: int = 0
    llm_estimated_cost_rub: float = 0.0
    total_latency_ms: int | None = None
    trace_events: list[TraceEvent] = Field(default_factory=list)
    prompt_version: str | None = None
    error: str | None = None
