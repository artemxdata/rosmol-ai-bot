from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DATA_ROOT = (PROJECT_ROOT / "data" / "private").resolve()
CASES_PATH = PRIVATE_DATA_ROOT / "eval" / "phase0-real-rag-7d244e4" / "phase0-cases.json"
MANIFEST_PATH = PRIVATE_DATA_ROOT / "eval" / "phase0-real-rag-7d244e4" / "phase0-manifest.json"

PHASE0_EVAL_RUN_ID = "ask-eval-61971dbd-75ff-44b0-8eef-0e64c5b27168"
PHASE0_CASES_SHA256 = "aff198bbc98d07894a3e1676e3457891e3a38f674315051505b681641fe9d02d"
PHASE0_MANIFEST_SHA256 = "8cf9959aaf9caf8728b386214ebba826f7bb0eb349f27fd2737e2830eb353264"
PHASE0_CASE_MEMBERSHIP_SHA256 = "60a9528cf4ef192f5e1132d93e3ec70447f6ec30bce85a818df19658993ebfd6"
PHASE0_CASES_TOTAL = 30
PHASE0_RUN_STARTED_AT = datetime(2026, 8, 6, 12, 10, 56, 774654, tzinfo=UTC)
PHASE0_RUN_COMPLETED_AT = datetime(2026, 8, 6, 12, 15, 30, 205184, tzinfo=UTC)

EXPORT_SCHEMA_VERSION = "phase0-trace-review-export-v1"
ROW_SCHEMA_VERSION = "phase0-trace-review-row-v1"
MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_INPUT_BYTES = 2 * 1024 * 1024
SSH_TIMEOUT_SECONDS = 90
LOCAL_EXPORT_TIMEOUT_SECONDS = 90
SSH_TARGET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}")
SAFE_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,79}")
QUESTION_ID_RE = re.compile(r"q[1-9][0-9]*")
HASHED_FILTER_RE = re.compile(r"sha256:[0-9a-f]{64}")
HASHED_CHUNK_ID_RE = re.compile(r"id_sha256:[0-9a-f]{64}")

PROVENANCE_SCHEMA_VERSION = "question-pipeline-provenance-v2"
MAX_PROVENANCE_QUESTIONS = 12
MAX_PROVENANCE_ATTEMPTS = 6
MAX_PROVENANCE_CANDIDATES = 48
MAX_PROVENANCE_CHUNK_IDS = 64
MAX_PROVENANCE_SOURCE_IDS = 64
MAX_RERANK_SCORES = 16
MAX_TRACE_EVENTS = 256
MAX_LLM_USAGE_EVENTS = 16
MAX_RETRIEVED_CHUNKS = 2048
MAX_RERANKED_CHUNKS = 256
MAX_TELEMETRY_COUNT = 1_000_000
MAX_LATENCY_MS = 3_600_000
MAX_SAFE_STRING_LENGTH = 256

ROW_FIELDS = frozenset(
    {
        "schema_version",
        "eval_run_id",
        "eval_case_id",
        "request_id",
        "timestamp",
        "query_analysis",
        "retrieved_chunks",
        "reranked_chunks",
        "reranker_trace",
        "retrieve_trace",
        "generation_trace",
        "selected_source_ids",
        "generate_retries",
        "verify_trace",
        "verification_source_ids",
        "trace_timeline",
        "max_reranker_score",
        "cache_hit",
        "generator_model",
        "cited_source_ids",
        "verifier_triggered",
        "verifier_result",
        "was_escalated",
        "escalation_reason",
        "ticket_outcome",
        "llm_usage",
        "llm_prompt_tokens",
        "llm_completion_tokens",
        "llm_total_tokens",
        "llm_estimated_cost_rub",
        "total_latency_ms",
        "prompt_version",
        "error_present",
        "error_code",
    }
)

QUERY_ANALYSIS_FIELDS = frozenset(
    {
        "forum",
        "forum_normalized",
        "category",
        "complexity",
        "needs_clarification",
        "should_escalate",
        "escalation_reason",
        "is_technical",
        "is_offtopic",
        "response_profile",
        "questions_count",
        "topics_count",
    }
)
QUERY_ANALYSIS_STRING_FIELDS = frozenset(
    {
        "forum",
        "forum_normalized",
        "category",
        "complexity",
        "escalation_reason",
        "response_profile",
    }
)
QUERY_ANALYSIS_BOOL_FIELDS = frozenset(
    {
        "needs_clarification",
        "should_escalate",
        "is_technical",
        "is_offtopic",
    }
)
RESPONSE_PROFILES = frozenset(
    {
        "dates",
        "application",
        "eligibility",
        "documents",
        "selection_status",
        "program",
        "travel",
        "accommodation",
        "food",
        "accessibility",
        "grants",
        "technical",
        "generic",
    }
)
VERIFIER_RESULT_FIELDS = frozenset({"has_hallucination", "confidence", "triggered_llm_judge"})
LLM_USAGE_FIELDS = frozenset(
    {
        "model",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "estimated_cost_rub",
        "priced",
    }
)
RETRIEVE_TRACE_FIELDS = frozenset(
    {
        "retrieval_method",
        "metadata_lookup_attempted",
        "metadata_lookup_succeeded",
        "metadata_lookup_result_count",
        "hybrid_candidates_present",
        "question_provenance",
    }
)
RETRIEVE_QUESTION_FIELDS = frozenset(
    {
        "schema_version",
        "question_id",
        "attempts",
        "attempts_total",
        "attempts_recorded",
        "attempts_truncated_count",
        "retrieved_chunk_ids",
        "retrieved_chunk_ids_total",
        "retrieved_chunk_ids_recorded",
        "retrieved_chunk_ids_truncated_count",
        "questions_total",
        "questions_recorded",
        "questions_truncated_count",
        "attributable_questions_total",
        "skipped_reason",
    }
)
RETRIEVE_ATTEMPT_FIELDS = frozenset(
    {
        "attempt_no",
        "scope",
        "filters",
        "top_k",
        "retrieval_method",
        "metadata_lookup_attempted",
        "metadata_lookup_succeeded",
        "metadata_lookup_result_count",
        "hybrid_candidates_present",
        "candidates",
        "candidates_total",
        "candidates_recorded",
        "candidates_truncated_count",
    }
)
RETRIEVE_CANDIDATE_FIELDS = frozenset({"chunk_id", "method", "score"})
RERANKER_TRACE_FIELDS = frozenset(
    {
        "max_confidence",
        "confidence_source",
        "reranker_invoked",
        "raw_reranker_scores",
        "raw_reranker_scores_total",
        "raw_reranker_scores_recorded",
        "raw_reranker_scores_truncated_count",
        "raw_reranker_max",
        "score_origin",
        "synthetic_score_applied",
        "synthetic_high_score_applied",
        "floor_applied",
        "confidence_components",
        "question_provenance",
    }
)
RERANK_QUESTION_FIELDS = frozenset(
    {
        "schema_version",
        "question_id",
        "input_chunk_ids",
        "input_chunk_ids_total",
        "input_chunk_ids_recorded",
        "input_chunk_ids_truncated_count",
        "output_chunks",
        "dropped_chunk_ids",
        "questions_total",
        "questions_recorded",
        "questions_truncated_count",
    }
)
GENERATION_TRACE_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "generator_path",
        "source_chunk_applied",
        "selected_source_ids",
        "selected_source_ids_total",
        "selected_source_ids_recorded",
        "selected_source_ids_truncated_count",
        "cited_source_ids",
        "cited_source_ids_total",
        "cited_source_ids_recorded",
        "cited_source_ids_truncated_count",
        "selection_binding_scope",
        "question_source_overlaps",
        "candidate_uncovered_question_ids",
        "question_overlaps_total",
        "question_overlaps_recorded",
        "question_overlaps_truncated_count",
        "contract_status",
        "reason",
    }
)
QUESTION_SOURCE_OVERLAP_FIELDS = frozenset(
    {"question_id", "binding_scope", "candidate_overlap_source_ids"}
)
VERIFY_TRACE_FIELDS = frozenset(
    {
        "schema_version",
        "decision",
        "reason",
        "referenced_source_ids",
        "referenced_source_ids_total",
        "referenced_source_ids_recorded",
        "referenced_source_ids_truncated_count",
        "reference_scope",
        "candidate_uncovered_question_ids",
        "verifier_triggered",
    }
)
GENERATE_RETRY_FIELDS = frozenset({"latency_ms", "reason", "chunks"})
TIMELINE_FIELDS = frozenset({"node", "latency_ms", "error_present", "error_code"})

RETRIEVAL_METHODS = frozenset({"metadata", "hybrid", "mixed", "none"})
ATTEMPT_SCOPES = frozenset(
    {"strict", "relaxed_topic", "relaxed_category", "relaxed_forum", "relaxed_global"}
)
CANDIDATE_METHODS = frozenset({"metadata", "hybrid", "keyword", "shared_hybrid", "shared_keyword"})
FILTER_FIELDS = frozenset({"source_type", "category", "topic", "forum_normalized"})
CONFIDENCE_SOURCES = frozenset({"none", "reranker", "retrieval_exact_filter"})
SCORE_ORIGINS = frozenset({"none", "reranker", "synthetic", "mixed"})
CONFIDENCE_COMPONENT_FIELDS = frozenset(
    {
        "raw_reranker_max",
        "reranked_output_max",
        "retrieval_exact_filter_floor",
        "decision_confidence",
    }
)
GENERATION_MODES = frozenset(
    {
        "unknown",
        "general_catalog_source_chunk",
        "complex_source_chunk",
        "complex_deterministic_source_chunk",
        "complex_single_official_source_chunk",
        "complex_partial_source_chunk",
        "complex_source_only_escalation",
        "source_chunk",
        "partial_source_chunk",
        "source_only_escalation",
        "llm",
    }
)
CONTRACT_STATUSES = frozenset({"passed", "partial", "failed"})
VERIFY_DECISIONS = frozenset({"pass", "partial", "escalate", "reject"})
REFERENCE_SCOPES = frozenset(
    {
        "actual_response_explicit",
        "actual_response_unknown_reference",
        "inherited_state_coarse",
        "actual_response_unreferenced",
    }
)
TRACE_NODES = frozenset(
    {
        "analyze",
        "analyze_llm",
        "retrieve",
        "keyword_recall",
        "rerank",
        "generate",
        "generate_retry",
        "generate_selection",
        "guard",
        "verify",
        "verify_decision",
        "respond",
        "clarify",
        "escalate",
    }
)
TICKET_OUTCOMES = frozenset(
    {"answered", "clarification", "error", "escalated", "no_response", "rate_limited"}
)
ROW_ERROR_CODES = frozenset(
    {
        "request_timeout",
        "analyzer_failed",
        "retrieval_failed",
        "rerank_failed",
        "llm_generation_failed",
        "ml_dependency_missing",
        "other",
    }
)
TRACE_ERROR_CODES = frozenset(
    {
        "analyze_error",
        "retrieve_error",
        "rerank_error",
        "generate_error",
        "guard_error",
        "verify_error",
        "respond_error",
        "other",
    }
)
SAFE_FAILURE_CODES = frozenset(
    {
        "docker_access_failed",
        "interactive_tty_required",
        "local_start_failed",
        "local_stream_failed",
        "local_timeout",
        "local_exit",
        "postgres_container_missing",
        "postgres_container_not_running",
        "postgres_export_failed",
        "ssh_timeout",
        "ssh_start_failed",
        "ssh_stream_failed",
        "ssh_exit",
        "remote_failure",
        "remote_output_too_large",
    }
)

REMOTE_DOCKER_ACCESS_EXIT = 40
REMOTE_POSTGRES_CONTAINER_MISSING_EXIT = 41
REMOTE_POSTGRES_CONTAINER_NOT_RUNNING_EXIT = 42
REMOTE_POSTGRES_EXPORT_EXIT = 43
REMOTE_FAILURE_CODES = {
    REMOTE_DOCKER_ACCESS_EXIT: "docker_access_failed",
    REMOTE_POSTGRES_CONTAINER_MISSING_EXIT: "postgres_container_missing",
    REMOTE_POSTGRES_CONTAINER_NOT_RUNNING_EXIT: "postgres_container_not_running",
    REMOTE_POSTGRES_EXPORT_EXIT: "postgres_export_failed",
}


class EvidenceUnavailableError(RuntimeError):
    """The approved run has no retained trace rows to export."""


class SafeExportFailure(RuntimeError):
    """An operational export failure with a stable, payload-free CLI code."""

    def __init__(self, code: str) -> None:
        if code not in SAFE_FAILURE_CODES:
            raise ValueError("unsupported safe export failure code")
        self.code = code
        super().__init__(code)


FORBIDDEN_OUTPUT_FIELDS = frozenset(
    {
        "user_id",
        "user_id_hash",
        "ticket_id",
        "ticket_id_hash",
        "upstream_event_id",
        "upstream_event_id_source",
        "message_masked",
    }
)
FORBIDDEN_RAW_ERROR_FIELDS = frozenset(
    {
        "error",
        "error_message",
        "error_text",
        "error_details",
        "exception",
        "stderr",
        "stack",
        "stack_trace",
        "traceback",
    }
)


def _event_projection(node: str, fields: tuple[str, ...]) -> str:
    pairs = ",\n".join(
        f"                            '{field}', event->'metadata'->'{field}'" for field in fields
    )
    return f"""
        COALESCE((
            SELECT jsonb_strip_nulls(jsonb_build_object(
{pairs}
            ))
            FROM jsonb_array_elements(COALESCE(rt.trace_events, '[]'::jsonb))
                WITH ORDINALITY AS events(event, ordinal)
            WHERE event->>'node' = '{node}'
            ORDER BY ordinal DESC
            LIMIT 1
        ), '{{}}'::jsonb)
    """.strip()


RETRIEVE_TRACE_SQL = _event_projection(
    "retrieve",
    (
        "retrieval_method",
        "metadata_lookup_attempted",
        "metadata_lookup_succeeded",
        "metadata_lookup_result_count",
        "hybrid_candidates_present",
        "question_provenance",
    ),
)
RERANKER_TRACE_SQL = _event_projection(
    "rerank",
    (
        "max_confidence",
        "confidence_source",
        "reranker_invoked",
        "raw_reranker_scores",
        "raw_reranker_scores_total",
        "raw_reranker_scores_recorded",
        "raw_reranker_scores_truncated_count",
        "raw_reranker_max",
        "score_origin",
        "synthetic_score_applied",
        "synthetic_high_score_applied",
        "floor_applied",
        "confidence_components",
        "question_provenance",
    ),
)
GENERATION_TRACE_SQL = _event_projection(
    "generate_selection",
    (
        "schema_version",
        "mode",
        "generator_path",
        "source_chunk_applied",
        "selected_source_ids",
        "selected_source_ids_total",
        "selected_source_ids_recorded",
        "selected_source_ids_truncated_count",
        "cited_source_ids",
        "cited_source_ids_total",
        "cited_source_ids_recorded",
        "cited_source_ids_truncated_count",
        "selection_binding_scope",
        "question_source_overlaps",
        "candidate_uncovered_question_ids",
        "question_overlaps_total",
        "question_overlaps_recorded",
        "question_overlaps_truncated_count",
        "contract_status",
        "reason",
    ),
)
VERIFY_TRACE_SQL = _event_projection(
    "verify_decision",
    (
        "schema_version",
        "decision",
        "reason",
        "referenced_source_ids",
        "referenced_source_ids_total",
        "referenced_source_ids_recorded",
        "referenced_source_ids_truncated_count",
        "reference_scope",
        "candidate_uncovered_question_ids",
        "verifier_triggered",
    ),
)

PHASE0_COPY_SQL = f"""
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '30s';
COPY (
    SELECT replace(replace(encode(convert_to(jsonb_build_object(
        'schema_version', '{ROW_SCHEMA_VERSION}',
        'eval_run_id', rt.eval_run_id,
        'eval_case_id', rt.eval_case_id,
        'request_id', rt.request_id::text,
        'timestamp', rt.timestamp,
        'query_analysis', CASE
            WHEN rt.query_analysis IS NULL THEN NULL
            ELSE jsonb_strip_nulls(jsonb_build_object(
                'forum', rt.query_analysis->'forum',
                'forum_normalized', rt.query_analysis->'forum_normalized',
                'category', rt.query_analysis->'category',
                'complexity', rt.query_analysis->'complexity',
                'needs_clarification', rt.query_analysis->'needs_clarification',
                'should_escalate', rt.query_analysis->'should_escalate',
                'escalation_reason', rt.query_analysis->'escalation_reason',
                'is_technical', rt.query_analysis->'is_technical',
                'is_offtopic', rt.query_analysis->'is_offtopic',
                'response_profile', rt.query_analysis->'response_profile',
                'questions_count', CASE
                    WHEN jsonb_typeof(rt.query_analysis->'questions') = 'array'
                    THEN jsonb_array_length(rt.query_analysis->'questions')
                    ELSE NULL
                END,
                'topics_count', CASE
                    WHEN jsonb_typeof(rt.query_analysis->'topics') = 'array'
                    THEN jsonb_array_length(rt.query_analysis->'topics')
                    ELSE NULL
                END
            ))
        END,
        'retrieved_chunks', COALESCE((
            SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                'chunk_id', chunk->>'chunk_id',
                'retrieval_score', chunk->'score'
            )) ORDER BY ordinal)
            FROM jsonb_array_elements(COALESCE(rt.retrieved_chunks, '[]'::jsonb))
                WITH ORDINALITY AS chunks(chunk, ordinal)
        ), '[]'::jsonb),
        'reranked_chunks', COALESCE((
            SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                'chunk_id', chunk->>'chunk_id',
                'retrieval_score', chunk->'score',
                'reranker_score', chunk->'reranker_score'
            )) ORDER BY ordinal)
            FROM jsonb_array_elements(COALESCE(rt.reranker_scores, '[]'::jsonb))
                WITH ORDINALITY AS chunks(chunk, ordinal)
        ), '[]'::jsonb),
        'retrieve_trace', {RETRIEVE_TRACE_SQL},
        'reranker_trace', {RERANKER_TRACE_SQL},
        'generation_trace', {GENERATION_TRACE_SQL},
        'selected_source_ids', COALESCE((
            SELECT event->'metadata'->'selected_source_ids'
            FROM jsonb_array_elements(COALESCE(rt.trace_events, '[]'::jsonb))
                WITH ORDINALITY AS events(event, ordinal)
            WHERE event->>'node' = 'generate_selection'
            ORDER BY ordinal DESC
            LIMIT 1
        ), '[]'::jsonb),
        'generate_retries', COALESCE((
            SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                'latency_ms', event->'latency_ms',
                'reason', event->'metadata'->'reason',
                'chunks', event->'metadata'->'chunks'
            )) ORDER BY ordinal)
            FROM jsonb_array_elements(COALESCE(rt.trace_events, '[]'::jsonb))
                WITH ORDINALITY AS events(event, ordinal)
            WHERE event->>'node' = 'generate_retry'
        ), '[]'::jsonb),
        'verify_trace', {VERIFY_TRACE_SQL},
        'verification_source_ids', COALESCE((
            SELECT event->'metadata'->'referenced_source_ids'
            FROM jsonb_array_elements(COALESCE(rt.trace_events, '[]'::jsonb))
                WITH ORDINALITY AS events(event, ordinal)
            WHERE event->>'node' = 'verify_decision'
            ORDER BY ordinal DESC
            LIMIT 1
        ), '[]'::jsonb),
        'trace_timeline', COALESCE((
            SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                'node', event->>'node',
                'latency_ms', event->'latency_ms',
                'error_present', NULLIF(BTRIM(event->>'error'), '') IS NOT NULL,
                'error_code', CASE
                    WHEN NULLIF(BTRIM(event->>'error'), '') IS NULL THEN NULL
                    WHEN event->>'node' = 'analyze' THEN 'analyze_error'
                    WHEN event->>'node' = 'retrieve' THEN 'retrieve_error'
                    WHEN event->>'node' = 'rerank' THEN 'rerank_error'
                    WHEN event->>'node' = 'generate' THEN 'generate_error'
                    WHEN event->>'node' = 'guard' THEN 'guard_error'
                    WHEN event->>'node' = 'verify' THEN 'verify_error'
                    WHEN event->>'node' = 'respond' THEN 'respond_error'
                    ELSE 'other'
                END
            )) ORDER BY ordinal)
            FROM jsonb_array_elements(COALESCE(rt.trace_events, '[]'::jsonb))
                WITH ORDINALITY AS events(event, ordinal)
        ), '[]'::jsonb),
        'max_reranker_score', rt.max_reranker_score,
        'cache_hit', rt.cache_hit,
        'generator_model', rt.generator_model,
        'cited_source_ids', to_jsonb(COALESCE(rt.cited_sources, ARRAY[]::text[])),
        'verifier_triggered', rt.verifier_triggered,
        'verifier_result', CASE
            WHEN rt.verifier_result IS NULL THEN NULL
            ELSE jsonb_strip_nulls(jsonb_build_object(
                'has_hallucination', rt.verifier_result->'has_hallucination',
                'confidence', rt.verifier_result->'confidence',
                'triggered_llm_judge', rt.verifier_result->'triggered_llm_judge'
            ))
        END,
        'was_escalated', rt.was_escalated,
        'escalation_reason', rt.escalation_reason,
        'ticket_outcome', rt.ticket_outcome,
        'llm_usage', COALESCE((
            SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                'model', usage->'model',
                'latency_ms', usage->'latency_ms',
                'prompt_tokens', usage->'prompt_tokens',
                'completion_tokens', usage->'completion_tokens',
                'total_tokens', usage->'total_tokens',
                'estimated_cost_rub', usage->'estimated_cost_rub',
                'priced', usage->'priced'
            )) ORDER BY ordinal)
            FROM jsonb_array_elements(COALESCE(rt.llm_usage, '[]'::jsonb))
                WITH ORDINALITY AS usage_events(usage, ordinal)
        ), '[]'::jsonb),
        'llm_prompt_tokens', rt.llm_prompt_tokens,
        'llm_completion_tokens', rt.llm_completion_tokens,
        'llm_total_tokens', rt.llm_total_tokens,
        'llm_estimated_cost_rub', rt.llm_estimated_cost_rub,
        'total_latency_ms', rt.total_latency_ms,
        'prompt_version', rt.prompt_version,
        'error_present', NULLIF(BTRIM(rt.error), '') IS NOT NULL,
        'error_code', CASE
            WHEN NULLIF(BTRIM(rt.error), '') IS NULL THEN NULL
            WHEN rt.error = 'request_timeout' THEN 'request_timeout'
            WHEN rt.escalation_reason = 'analyzer_failed' THEN 'analyzer_failed'
            WHEN rt.escalation_reason = 'retrieval_failed' THEN 'retrieval_failed'
            WHEN rt.escalation_reason = 'rerank_failed' THEN 'rerank_failed'
            WHEN rt.escalation_reason = 'llm_generation_failed' THEN 'llm_generation_failed'
            WHEN rt.escalation_reason = 'ml_dependency_missing' THEN 'ml_dependency_missing'
            ELSE 'other'
        END
    )::text, 'UTF8'), 'base64'), chr(10), ''), chr(13), '')
    FROM request_traces AS rt
    WHERE rt.eval_run_id = '{PHASE0_EVAL_RUN_ID}'
    ORDER BY rt.timestamp, rt.request_id
) TO STDOUT WITH (FORMAT text);
COMMIT;
""".strip()


def export_phase0_trace_review(
    *,
    ssh_target: str,
    eval_run_id: str,
    output_path: Path,
    interactive_sudo: bool = False,
) -> dict[str, Any]:
    """Fetch, validate and exclusively persist the approved Phase 0 trace projection."""

    if SSH_TARGET_RE.fullmatch(ssh_target) is None:
        raise ValueError("SSH target contains unsupported characters")
    if eval_run_id != PHASE0_EVAL_RUN_ID:
        raise ValueError("eval run ID differs from the approved Phase 0 run")

    destination = _validated_output_path(output_path)
    output_parent_identity = _prepare_output_parent(destination)
    expected_case_ids, cases_sha256, manifest_sha256 = _load_approved_membership()
    if interactive_sudo and not sys.stdin.isatty():
        raise SafeExportFailure("interactive_tty_required")
    remote_command = _remote_command(interactive_sudo=interactive_sudo)
    ssh_args = [
        "ssh",
        "-tt" if interactive_sudo else "-T",
        "-o",
        "BatchMode=yes",
        "--",
        ssh_target,
        remote_command,
    ]
    if interactive_sudo:
        print(
            "phase0_trace_export=AUTH reason=server_sudo_required",
            file=sys.stderr,
            flush=True,
        )
        completed = _run_bounded_ssh(ssh_args, interactive=True)
    else:
        completed = _run_bounded_ssh(ssh_args)
    if completed.returncode != 0:
        code = (
            "ssh_exit"
            if completed.returncode == 255 or completed.returncode < 0
            else REMOTE_FAILURE_CODES.get(completed.returncode, "remote_failure")
        )
        raise SafeExportFailure(code)
    payload = completed.stdout
    if payload == b"":
        raise EvidenceUnavailableError("approved Phase 0 trace evidence is unavailable")
    if len(payload) > MAX_EXPORT_BYTES:
        raise SafeExportFailure("remote_output_too_large")

    rows = _parse_and_validate_rows(
        payload,
        expected_case_ids=expected_case_ids,
        eval_run_id=eval_run_id,
    )
    return _persist_validated_report(
        rows=rows,
        eval_run_id=eval_run_id,
        destination=destination,
        output_parent_identity=output_parent_identity,
        cases_sha256=cases_sha256,
        manifest_sha256=manifest_sha256,
    )


def export_phase0_trace_review_server_local(
    *,
    eval_run_id: str,
    output_path: Path,
    interactive_sudo: bool = False,
) -> dict[str, Any]:
    """Export on the checked-out server without SSH or private frozen inputs."""

    if eval_run_id != PHASE0_EVAL_RUN_ID:
        raise ValueError("eval run ID differs from the approved Phase 0 run")

    destination = _validated_output_path(output_path)
    output_parent_identity = _prepare_output_parent(destination)
    if interactive_sudo and not sys.stdin.isatty():
        raise SafeExportFailure("interactive_tty_required")
    command = _server_local_command(interactive_sudo=interactive_sudo)
    if interactive_sudo:
        print(
            "phase0_trace_export=AUTH reason=server_sudo_required",
            file=sys.stderr,
            flush=True,
        )
    completed = _run_bounded_local(command, interactive=interactive_sudo)
    if completed.returncode != 0:
        code = (
            "local_exit"
            if completed.returncode < 0
            else REMOTE_FAILURE_CODES.get(completed.returncode, "local_exit")
        )
        raise SafeExportFailure(code)
    payload = completed.stdout
    if payload == b"":
        raise EvidenceUnavailableError("approved Phase 0 trace evidence is unavailable")
    if len(payload) > MAX_EXPORT_BYTES:
        raise SafeExportFailure("remote_output_too_large")

    rows = _parse_and_validate_rows(
        payload,
        expected_case_membership_sha256=PHASE0_CASE_MEMBERSHIP_SHA256,
        eval_run_id=eval_run_id,
    )
    return _persist_validated_report(
        rows=rows,
        eval_run_id=eval_run_id,
        destination=destination,
        output_parent_identity=output_parent_identity,
        cases_sha256=PHASE0_CASES_SHA256,
        manifest_sha256=PHASE0_MANIFEST_SHA256,
    )


def _persist_validated_report(
    *,
    rows: list[dict[str, Any]],
    eval_run_id: str,
    destination: Path,
    output_parent_identity: tuple[int, int],
    cases_sha256: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    report = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "eval_run_id": eval_run_id,
        "run_window": {
            "started_at": PHASE0_RUN_STARTED_AT.isoformat().replace("+00:00", "Z"),
            "completed_at": PHASE0_RUN_COMPLETED_AT.isoformat().replace("+00:00", "Z"),
        },
        "bindings": {
            "cases_file_sha256": cases_sha256,
            "manifest_file_sha256": manifest_sha256,
        },
        "cases_total": len(rows),
        "rows": rows,
    }
    encoded = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_EXPORT_BYTES:
        raise ValueError("Validated Phase 0 trace report exceeds the size limit")
    _write_exclusive_atomic(
        destination,
        encoded,
        expected_parent_identity=output_parent_identity,
    )
    return {
        "status": "OK",
        "path": str(destination),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "cases_total": len(rows),
    }


def _remote_command(*, interactive_sudo: bool = False) -> str:
    return shlex.join(["sh", "-c", _docker_export_script(interactive_sudo=interactive_sudo)])


def _server_local_command(*, interactive_sudo: bool = False) -> list[str]:
    return ["sh", "-c", _docker_export_script(interactive_sudo=interactive_sudo)]


def _docker_export_script(*, interactive_sudo: bool = False) -> str:
    postgres_argv = [
        "exec",
        "-i",
        "rosmol-postgres",
        "psql",
        "-X",
        "--quiet",
        "--tuples-only",
        "--no-align",
        "--set=ON_ERROR_STOP=1",
        "--no-password",
        "--username=rosmol",
        "--dbname=rosmol_ai_bot",
        "--command",
        PHASE0_COPY_SQL,
    ]
    postgres_command = shlex.join(postgres_argv)
    docker_socket = "unix:///var/run/docker.sock"
    if interactive_sudo:
        sudo_probe = f"sudo -p '' docker --host {docker_socket} version"
        sudo_prefix = f"sudo -p '' docker --host {docker_socket}"
    else:
        sudo_probe = f"sudo --non-interactive docker --host {docker_socket} version"
        sudo_prefix = f"sudo --non-interactive docker --host {docker_socket}"
    script = f"""
set -u
if docker --host {docker_socket} version >/dev/null 2>&1; then
    set -- docker --host {docker_socket}
elif {sudo_probe} >/dev/null 2>&1; then
    set -- {sudo_prefix}
else
    exit {REMOTE_DOCKER_ACCESS_EXIT}
fi
if ! "$@" inspect rosmol-postgres >/dev/null 2>&1; then
    exit {REMOTE_POSTGRES_CONTAINER_MISSING_EXIT}
fi
running=$("$@" inspect --format '{{{{.State.Running}}}}' rosmol-postgres 2>/dev/null) \
    || exit {REMOTE_POSTGRES_CONTAINER_MISSING_EXIT}
if [ "$running" != "true" ]; then
    exit {REMOTE_POSTGRES_CONTAINER_NOT_RUNNING_EXIT}
fi
if ! "$@" {postgres_command}; then
    exit {REMOTE_POSTGRES_EXPORT_EXIT}
fi
""".strip()
    return script


def _run_bounded_ssh(
    args: list[str],
    *,
    interactive: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Drain both SSH pipes incrementally and kill on timeout or bounded overflow."""

    return _run_bounded_process(
        args,
        interactive=interactive,
        timeout_seconds=SSH_TIMEOUT_SECONDS,
        start_failure_code="ssh_start_failed",
        stream_failure_code="ssh_stream_failed",
        timeout_failure_code="ssh_timeout",
    )


def _run_bounded_local(
    args: list[str],
    *,
    interactive: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Run the server-local Docker projection with bounded, non-emitting pipes."""

    return _run_bounded_process(
        args,
        interactive=interactive,
        timeout_seconds=LOCAL_EXPORT_TIMEOUT_SECONDS,
        start_failure_code="local_start_failed",
        stream_failure_code="local_stream_failed",
        timeout_failure_code="local_timeout",
    )


def _run_bounded_process(
    args: list[str],
    *,
    interactive: bool,
    timeout_seconds: int,
    start_failure_code: str,
    stream_failure_code: str,
    timeout_failure_code: str,
) -> subprocess.CompletedProcess[bytes]:

    try:
        process = subprocess.Popen(
            args,
            stdin=None if interactive else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise SafeExportFailure(start_failure_code) from exc
    if process.stdout is None or process.stderr is None:
        _kill_process(process)
        raise SafeExportFailure(stream_failure_code)

    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    read_failed = threading.Event()

    def drain(stream: Any, destination: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    read_failed.set()
                    _kill_process(process)
                    break
                remaining = MAX_EXPORT_BYTES - len(destination)
                if len(chunk) > remaining:
                    if remaining > 0:
                        destination.extend(chunk[:remaining])
                    overflow.set()
                    _kill_process(process)
                    break
                destination.extend(chunk)
        except OSError:
            read_failed.set()
            _kill_process(process)
        finally:
            try:
                stream.close()
            except OSError:
                read_failed.set()

    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process(process)
        try:
            returncode = process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            returncode = -1
    except OSError:
        read_failed.set()
        _kill_process(process)
        returncode = -1
    for reader in readers:
        reader.join(timeout=5)
    if any(reader.is_alive() for reader in readers):
        _kill_process(process)
        raise SafeExportFailure(stream_failure_code)
    if timed_out:
        raise SafeExportFailure(timeout_failure_code)
    if overflow.is_set():
        raise SafeExportFailure("remote_output_too_large")
    if read_failed.is_set():
        raise SafeExportFailure(stream_failure_code)
    return subprocess.CompletedProcess(
        args,
        returncode,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
    )


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass


def _validated_output_path(path: Path) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise FileExistsError("Phase 0 trace output must not be a symlink")
    private_root = _resolved_private_root()
    try:
        canonical_parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError("output parent must already exist") from exc
    if not canonical_parent.is_dir() or not canonical_parent.is_relative_to(private_root):
        raise ValueError("output must stay under data/private")
    destination = canonical_parent / candidate.name
    if destination == PRIVATE_DATA_ROOT:
        raise ValueError("output must be a file under data/private")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Phase 0 trace output must not already exist")
    return destination


def _prepare_output_parent(path: Path) -> tuple[int, int]:
    _resolved_private_root()
    return _assert_output_parent(path)


def _resolved_private_root() -> Path:
    resolved = PRIVATE_DATA_ROOT.resolve(strict=True)
    if resolved != PRIVATE_DATA_ROOT or not resolved.is_dir():
        raise ValueError("private data root changed during export")
    return resolved


def _assert_output_parent(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    output_must_exist: bool = False,
) -> tuple[int, int]:
    private_root = _resolved_private_root()
    resolved_parent = path.parent.resolve(strict=True)
    if (
        resolved_parent != path.parent
        or not resolved_parent.is_relative_to(private_root)
        or not resolved_parent.is_dir()
    ):
        raise ValueError("output parent changed during export")
    stat_result = resolved_parent.stat()
    identity = (int(stat_result.st_dev), int(stat_result.st_ino))
    if expected_identity is not None and identity != expected_identity:
        raise ValueError("output parent changed during export")
    if output_must_exist:
        if path.is_symlink() or not path.is_file():
            raise ValueError("output publication changed during export")
    elif path.exists() or path.is_symlink():
        raise FileExistsError("Phase 0 trace output must not already exist")
    return identity


def _load_approved_membership() -> tuple[list[str], str, str]:
    cases_payload, cases_sha256 = _read_approved_json(
        CASES_PATH,
        expected_sha256=PHASE0_CASES_SHA256,
        label="Phase 0 cases",
    )
    manifest_payload, manifest_sha256 = _read_approved_json(
        MANIFEST_PATH,
        expected_sha256=PHASE0_MANIFEST_SHA256,
        label="Phase 0 manifest",
    )
    if not isinstance(cases_payload, list) or not all(
        isinstance(item, dict) for item in cases_payload
    ):
        raise ValueError("Phase 0 cases must be an array of objects")
    manifest_cases = manifest_payload.get("cases") if isinstance(manifest_payload, dict) else None
    if not isinstance(manifest_cases, list) or not all(
        isinstance(item, dict) for item in manifest_cases
    ):
        raise ValueError("Phase 0 manifest must contain a case array")
    case_ids = [_case_id(item) for item in cases_payload]
    manifest_case_ids = [_case_id(item) for item in manifest_cases]
    if (
        len(case_ids) != PHASE0_CASES_TOTAL
        or len(set(case_ids)) != PHASE0_CASES_TOTAL
        or manifest_case_ids != case_ids
    ):
        raise ValueError("Phase 0 cases and manifest membership differ from approval")
    return case_ids, cases_sha256, manifest_sha256


def _read_approved_json(
    path: Path,
    *,
    expected_sha256: str,
    label: str,
) -> tuple[Any, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    payload = path.read_bytes()
    if not payload or len(payload) > MAX_INPUT_BYTES:
        raise ValueError(f"{label} has an invalid size")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        raise ValueError(f"{label} SHA-256 differs from approval")
    try:
        return json.loads(payload.decode("utf-8-sig"), parse_constant=_reject_constant), digest
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc


def _case_id(item: dict[str, Any]) -> str:
    value = item.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Phase 0 case ID must be a non-empty string")
    return value


def _parse_and_validate_rows(
    payload: bytes,
    *,
    expected_case_ids: list[str] | None = None,
    expected_case_membership_sha256: str | None = None,
    eval_run_id: str,
) -> list[dict[str, Any]]:
    if (expected_case_ids is None) == (expected_case_membership_sha256 is None):
        raise ValueError("exactly one Phase 0 membership binding is required")
    payload = _normalize_copy_transport(payload)
    if not payload.endswith(b"\n"):
        raise ValueError("Phase 0 trace transport has invalid framing")
    encoded_lines = payload[:-1].split(b"\n")
    if not encoded_lines or any(not line for line in encoded_lines):
        raise ValueError("Phase 0 trace transport has invalid framing")
    rows: list[dict[str, Any]] = []
    for line_number, encoded_line in enumerate(encoded_lines, start=1):
        try:
            decoded = base64.b64decode(encoded_line, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"Phase 0 trace transport line {line_number} is not strict base64"
            ) from exc
        if not decoded or base64.b64encode(decoded) != encoded_line:
            raise ValueError("Phase 0 trace transport contains a non-canonical row")
        try:
            row = json.loads(
                decoded.decode("utf-8"),
                parse_constant=_reject_constant,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Phase 0 trace export line {line_number} is not valid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError("Phase 0 trace rows must be JSON objects")
        _validate_row(row, eval_run_id=eval_run_id)
        rows.append(row)

    if len(rows) != PHASE0_CASES_TOTAL:
        raise ValueError("Phase 0 trace export must contain exactly 30 rows")
    case_ids = [str(row["eval_case_id"]) for row in rows]
    request_ids = [str(row["request_id"]) for row in rows]
    if len(set(case_ids)) != PHASE0_CASES_TOTAL:
        raise ValueError("Phase 0 trace export contains duplicate case IDs")
    if len(set(request_ids)) != PHASE0_CASES_TOTAL:
        raise ValueError("Phase 0 trace export contains duplicate request IDs")
    by_case_id = {str(row["eval_case_id"]): row for row in rows}
    if expected_case_ids is not None:
        if set(case_ids) != set(expected_case_ids):
            raise ValueError("Phase 0 trace membership differs from frozen cases")
        return [by_case_id[case_id] for case_id in expected_case_ids]
    if _case_membership_sha256(case_ids) != expected_case_membership_sha256:
        raise ValueError("Phase 0 trace membership differs from the approved aggregate")
    return [by_case_id[case_id] for case_id in sorted(case_ids)]


def _normalize_copy_transport(payload: bytes) -> bytes:
    """Accept only canonical LF or uniform SSH-PTY CRLF record framing."""

    if b"\r" not in payload:
        return payload
    without_crlf = payload.replace(b"\r\n", b"")
    if b"\r" in without_crlf or b"\n" in without_crlf:
        raise ValueError("Phase 0 trace transport has invalid CRLF framing")
    return payload.replace(b"\r\n", b"\n")


def _case_membership_sha256(case_ids: list[str]) -> str:
    canonical = json.dumps(
        sorted(case_ids),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_row(row: dict[str, Any], *, eval_run_id: str) -> None:
    if set(row) != ROW_FIELDS:
        raise ValueError("Phase 0 trace row fields differ from the safe projection")
    if FORBIDDEN_OUTPUT_FIELDS & set(row):
        raise ValueError("Phase 0 trace row contains a forbidden identity field")
    if row.get("schema_version") != ROW_SCHEMA_VERSION:
        raise ValueError("Phase 0 trace row schema is invalid")
    if row.get("eval_run_id") != eval_run_id:
        raise ValueError("Phase 0 trace row belongs to a different eval run")
    if not isinstance(row.get("eval_case_id"), str) or not row["eval_case_id"].strip():
        raise ValueError("Phase 0 trace row has an invalid case ID")
    try:
        UUID(str(row.get("request_id") or ""))
    except ValueError as exc:
        raise ValueError("Phase 0 trace row has an invalid request ID") from exc
    timestamp = _parse_utc_timestamp(row.get("timestamp"))
    if not PHASE0_RUN_STARTED_AT <= timestamp <= PHASE0_RUN_COMPLETED_AT:
        raise ValueError("Phase 0 trace timestamp falls outside the approved run window")
    if row.get("cache_hit") is not False:
        raise ValueError("Phase 0 trace export contains a cache hit or untyped cache field")
    for field in (
        "retrieved_chunks",
        "reranked_chunks",
        "selected_source_ids",
        "generate_retries",
        "trace_timeline",
        "cited_source_ids",
        "verification_source_ids",
        "llm_usage",
    ):
        if not isinstance(row.get(field), list):
            raise ValueError(f"Phase 0 trace field {field} must be an array")
    for field in ("retrieve_trace", "reranker_trace", "generation_trace", "verify_trace"):
        if not isinstance(row.get(field), dict):
            raise ValueError(f"Phase 0 trace field {field} must be an object")
    _validate_query_analysis(row.get("query_analysis"))
    _validate_verifier_result(row.get("verifier_result"))
    _validate_llm_usage(row["llm_usage"])
    _validate_retrieve_trace(row["retrieve_trace"])
    _validate_reranker_trace(row["reranker_trace"])
    _validate_generation_trace(row["generation_trace"])
    _validate_verify_trace(row["verify_trace"])
    _validate_generate_retries(row["generate_retries"])
    _validate_source_id_array(
        row["selected_source_ids"],
        label="selected source IDs",
    )
    _validate_source_id_array(
        row["verification_source_ids"],
        label="verification source IDs",
    )
    _validate_source_id_array(row["cited_source_ids"], label="cited source IDs")
    if row["generation_trace"] and (
        row["selected_source_ids"] != row["generation_trace"]["selected_source_ids"]
    ):
        raise ValueError("Phase 0 selected source IDs contradict generation provenance")
    if row["verify_trace"] and (
        row["verification_source_ids"] != row["verify_trace"]["referenced_source_ids"]
    ):
        raise ValueError("Phase 0 verification source IDs contradict verify provenance")
    if type(row.get("was_escalated")) is not bool:
        raise ValueError("Phase 0 escalation flag must be boolean")
    if type(row.get("verifier_triggered")) is not bool:
        raise ValueError("Phase 0 verifier flag must be boolean")
    if row["verify_trace"] and (
        row["verifier_triggered"] != row["verify_trace"]["verifier_triggered"]
    ):
        raise ValueError("Phase 0 verifier flag contradicts verify provenance")
    _validate_optional_safe_reason(row.get("escalation_reason"), label="escalation reason")
    if row.get("ticket_outcome") not in TICKET_OUTCOMES:
        raise ValueError("Phase 0 ticket outcome is not allowlisted")
    _validate_optional_bounded_string(row.get("generator_model"), label="generator model")
    _validate_optional_bounded_string(row.get("prompt_version"), label="prompt version")
    _validate_optional_score(row.get("max_reranker_score"), label="max reranker score")
    for field in ("llm_prompt_tokens", "llm_completion_tokens", "llm_total_tokens"):
        _validate_nonnegative_int(row.get(field), label=field)
    _validate_nonnegative_number(
        row.get("llm_estimated_cost_rub"),
        label="LLM estimated cost",
    )
    if row.get("total_latency_ms") is not None:
        _validate_nonnegative_int(
            row["total_latency_ms"],
            label="total latency",
            maximum=MAX_LATENCY_MS,
        )
    _validate_error_telemetry(
        row.get("error_present"),
        row.get("error_code"),
        allowed_codes=ROW_ERROR_CODES,
        label="request",
    )
    _validate_trace_timeline(row["trace_timeline"])
    _reject_forbidden_nested_keys(row)
    _reject_raw_chunk_text(row["retrieved_chunks"], field="retrieved_chunks")
    _reject_raw_chunk_text(row["reranked_chunks"], field="reranked_chunks")


def _reject_raw_chunk_text(chunks: list[Any], *, field: str) -> None:
    maximum = MAX_RETRIEVED_CHUNKS if field == "retrieved_chunks" else MAX_RERANKED_CHUNKS
    if len(chunks) > maximum:
        raise ValueError(f"Phase 0 {field} exceeds its bounded size")
    allowed = (
        {"chunk_id", "retrieval_score"}
        if field == "retrieved_chunks"
        else {"chunk_id", "retrieval_score", "reranker_score"}
    )
    for chunk in chunks:
        if not isinstance(chunk, dict) or not set(chunk).issubset(allowed):
            raise ValueError(f"Phase 0 {field} contains raw or unexpected chunk fields")
        _validate_chunk_id(chunk.get("chunk_id"), label=field)
        if "retrieval_score" in chunk:
            _validate_score(
                chunk["retrieval_score"],
                label=f"{field} retrieval score",
                minimum=-1.0,
            )
        if "reranker_score" in chunk:
            _validate_score(chunk["reranker_score"], label=f"{field} reranker score")


def _validate_retrieve_trace(value: dict[str, Any]) -> None:
    if not value:
        return
    if set(value) != RETRIEVE_TRACE_FIELDS:
        raise ValueError("Phase 0 retrieve trace differs from the exact v2 projection")
    if value["retrieval_method"] not in RETRIEVAL_METHODS:
        raise ValueError("Phase 0 retrieve method is not allowlisted")
    for field in (
        "metadata_lookup_attempted",
        "metadata_lookup_succeeded",
        "hybrid_candidates_present",
    ):
        if type(value[field]) is not bool:
            raise ValueError("Phase 0 retrieve trace contains an untyped flag")
    _validate_nonnegative_int(
        value["metadata_lookup_result_count"],
        label="metadata lookup result count",
    )
    rows = value["question_provenance"]
    if not isinstance(rows, list) or len(rows) > MAX_PROVENANCE_QUESTIONS:
        raise ValueError("Phase 0 retrieve question provenance is not bounded")
    seen: set[str] = set()
    for row in rows:
        _validate_retrieve_question(row)
        question_id = row["question_id"]
        if question_id in seen:
            raise ValueError("Phase 0 retrieve question provenance contains duplicates")
        seen.add(question_id)


def _validate_retrieve_question(value: Any) -> None:
    required = {"schema_version", "question_id", "attempts", "retrieved_chunk_ids"}
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or not set(value).issubset(RETRIEVE_QUESTION_FIELDS)
    ):
        raise ValueError("Phase 0 retrieve question differs from the v2 schema")
    _validate_provenance_header(value, allow_shared=True)
    attempts = value["attempts"]
    if not isinstance(attempts, list) or len(attempts) > MAX_PROVENANCE_ATTEMPTS:
        raise ValueError("Phase 0 retrieve attempts are not bounded")
    for expected_attempt, attempt in enumerate(attempts, start=1):
        _validate_retrieve_attempt(attempt, expected_attempt=expected_attempt)
    _validate_source_id_array(
        value["retrieved_chunk_ids"],
        label="retrieved provenance chunk IDs",
        maximum=MAX_PROVENANCE_CHUNK_IDS,
    )
    if "skipped_reason" in value:
        if value["skipped_reason"] != "unscoped_multi_topic_question":
            raise ValueError("Phase 0 retrieve skipped reason is not allowlisted")
        if attempts or value["retrieved_chunk_ids"]:
            raise ValueError("Phase 0 skipped retrieve question contains candidates")
    else:
        _validate_count_triplet(value, "attempts", len(attempts), MAX_PROVENANCE_ATTEMPTS)
        _validate_count_triplet(
            value,
            "retrieved_chunk_ids",
            len(value["retrieved_chunk_ids"]),
            MAX_PROVENANCE_CHUNK_IDS,
        )
    _validate_optional_count_triplet(
        value,
        "questions",
        maximum_recorded=MAX_PROVENANCE_QUESTIONS,
    )
    if "attributable_questions_total" in value:
        _validate_nonnegative_int(
            value["attributable_questions_total"],
            label="attributable question count",
        )


def _validate_retrieve_attempt(value: Any, *, expected_attempt: int) -> None:
    if not isinstance(value, dict) or set(value) != RETRIEVE_ATTEMPT_FIELDS:
        raise ValueError("Phase 0 retrieve attempt differs from the exact v2 schema")
    if value["attempt_no"] != expected_attempt:
        raise ValueError("Phase 0 retrieve attempt ordering is invalid")
    if value["scope"] not in ATTEMPT_SCOPES:
        raise ValueError("Phase 0 retrieve attempt scope is not allowlisted")
    if value["top_k"] not in {10, 30}:
        raise ValueError("Phase 0 retrieve top-k is not allowlisted")
    if value["retrieval_method"] not in {"metadata", "hybrid"}:
        raise ValueError("Phase 0 retrieve attempt method is not allowlisted")
    for field in (
        "metadata_lookup_attempted",
        "metadata_lookup_succeeded",
        "hybrid_candidates_present",
    ):
        if type(value[field]) is not bool:
            raise ValueError("Phase 0 retrieve attempt contains an untyped flag")
    _validate_nonnegative_int(
        value["metadata_lookup_result_count"],
        label="attempt metadata result count",
        maximum=30,
    )
    _validate_safe_filters(value["filters"])
    candidates = value["candidates"]
    if not isinstance(candidates, list) or len(candidates) > MAX_PROVENANCE_CANDIDATES:
        raise ValueError("Phase 0 retrieve candidates are not bounded")
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if (
            not isinstance(candidate, dict)
            or not {"chunk_id", "method"}.issubset(candidate)
            or not set(candidate).issubset(RETRIEVE_CANDIDATE_FIELDS)
        ):
            raise ValueError("Phase 0 retrieve candidate differs from the exact schema")
        _validate_chunk_id(candidate["chunk_id"], label="retrieve candidate")
        if candidate["method"] not in CANDIDATE_METHODS:
            raise ValueError("Phase 0 retrieve candidate method is not allowlisted")
        identity = (candidate["method"], candidate["chunk_id"])
        if identity in seen:
            raise ValueError("Phase 0 retrieve candidates contain duplicates")
        seen.add(identity)
        if "score" in candidate:
            _validate_score(candidate["score"], label="retrieve candidate score", minimum=-1.0)
    _validate_count_triplet(
        value,
        "candidates",
        len(candidates),
        MAX_PROVENANCE_CANDIDATES,
    )


def _validate_safe_filters(value: Any) -> None:
    if not isinstance(value, dict) or not set(value).issubset(FILTER_FIELDS):
        raise ValueError("Phase 0 retrieve filters differ from the safe allowlist")
    for key, item in value.items():
        if not isinstance(item, str):
            raise ValueError("Phase 0 retrieve filter fingerprint is untyped")
        if key == "source_type" and item == "yonote":
            continue
        if HASHED_FILTER_RE.fullmatch(item) is None:
            raise ValueError("Phase 0 retrieve filter contains raw text")


def _validate_reranker_trace(value: dict[str, Any]) -> None:
    if not value:
        return
    required = RERANKER_TRACE_FIELDS - {"raw_reranker_max", "confidence_components"}
    if not required.issubset(value) or not set(value).issubset(RERANKER_TRACE_FIELDS):
        raise ValueError("Phase 0 reranker trace differs from the exact v2 projection")
    _validate_score(value["max_confidence"], label="reranker confidence")
    if value["confidence_source"] not in CONFIDENCE_SOURCES:
        raise ValueError("Phase 0 reranker confidence source is not allowlisted")
    if value["score_origin"] not in SCORE_ORIGINS:
        raise ValueError("Phase 0 reranker score origin is not allowlisted")
    for field in (
        "reranker_invoked",
        "synthetic_score_applied",
        "synthetic_high_score_applied",
        "floor_applied",
    ):
        if type(value[field]) is not bool:
            raise ValueError("Phase 0 reranker trace contains an untyped flag")
    scores = value["raw_reranker_scores"]
    if not isinstance(scores, list) or len(scores) > MAX_RERANK_SCORES:
        raise ValueError("Phase 0 raw reranker scores are not bounded")
    for score in scores:
        _validate_score(score, label="raw reranker score")
    _validate_count_triplet(value, "raw_reranker_scores", len(scores), MAX_RERANK_SCORES)
    if "raw_reranker_max" in value:
        _validate_score(value["raw_reranker_max"], label="raw reranker maximum")
    components = value.get("confidence_components")
    if components is not None:
        if not isinstance(components, dict) or set(components) != CONFIDENCE_COMPONENT_FIELDS:
            raise ValueError("Phase 0 confidence components differ from the exact schema")
        for field, score in components.items():
            if field == "raw_reranker_max" and score is None:
                continue
            _validate_score(score, label="reranker confidence component")
    rows = value["question_provenance"]
    if not isinstance(rows, list) or len(rows) > MAX_PROVENANCE_QUESTIONS:
        raise ValueError("Phase 0 reranker question provenance is not bounded")
    seen: set[str] = set()
    for row in rows:
        _validate_rerank_question(row)
        if row["question_id"] in seen:
            raise ValueError("Phase 0 reranker question provenance contains duplicates")
        seen.add(row["question_id"])


def _validate_rerank_question(value: Any) -> None:
    required = {
        "schema_version",
        "question_id",
        "input_chunk_ids",
        "input_chunk_ids_total",
        "input_chunk_ids_recorded",
        "input_chunk_ids_truncated_count",
        "output_chunks",
        "dropped_chunk_ids",
    }
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or not set(value).issubset(RERANK_QUESTION_FIELDS)
    ):
        raise ValueError("Phase 0 rerank question differs from the exact v2 schema")
    _validate_provenance_header(value, allow_shared=True)
    input_ids = value["input_chunk_ids"]
    dropped_ids = value["dropped_chunk_ids"]
    _validate_source_id_array(input_ids, label="rerank input IDs", maximum=MAX_PROVENANCE_CHUNK_IDS)
    _validate_source_id_array(
        dropped_ids,
        label="rerank dropped IDs",
        maximum=MAX_PROVENANCE_CHUNK_IDS,
    )
    _validate_count_triplet(
        value,
        "input_chunk_ids",
        len(input_ids),
        MAX_PROVENANCE_CHUNK_IDS,
    )
    output = value["output_chunks"]
    if not isinstance(output, list) or len(output) > MAX_PROVENANCE_CHUNK_IDS:
        raise ValueError("Phase 0 rerank output chunks are not bounded")
    output_ids: list[str] = []
    for item in output:
        if (
            not isinstance(item, dict)
            or "chunk_id" not in item
            or not set(item).issubset({"chunk_id", "score"})
        ):
            raise ValueError("Phase 0 rerank output chunk differs from the exact schema")
        _validate_chunk_id(item["chunk_id"], label="rerank output")
        output_ids.append(item["chunk_id"])
        if "score" in item:
            _validate_score(item["score"], label="rerank output score")
    if len(set(output_ids)) != len(output_ids):
        raise ValueError("Phase 0 rerank output contains duplicate chunk IDs")
    if set(output_ids) & set(dropped_ids) or set(output_ids) | set(dropped_ids) != set(input_ids):
        raise ValueError("Phase 0 rerank output does not partition recorded input IDs")
    _validate_optional_count_triplet(
        value,
        "questions",
        maximum_recorded=MAX_PROVENANCE_QUESTIONS,
    )


def _validate_generation_trace(value: dict[str, Any]) -> None:
    if not value:
        return
    if set(value) != GENERATION_TRACE_FIELDS:
        raise ValueError("Phase 0 generation trace differs from the exact v2 schema")
    if value["schema_version"] != PROVENANCE_SCHEMA_VERSION:
        raise ValueError("Phase 0 generation provenance version is invalid")
    if value["mode"] not in GENERATION_MODES or value["generator_path"] != value["mode"]:
        raise ValueError("Phase 0 generation mode is not allowlisted")
    if type(value["source_chunk_applied"]) is not bool:
        raise ValueError("Phase 0 generation source flag is untyped")
    for field in ("selected_source_ids", "cited_source_ids"):
        _validate_source_id_array(value[field], label=field)
        _validate_count_triplet(value, field, len(value[field]), MAX_PROVENANCE_SOURCE_IDS)
    if value["selection_binding_scope"] != "global_exact_question_unattributed":
        raise ValueError("Phase 0 generation selection scope is not allowlisted")
    overlaps = value["question_source_overlaps"]
    if not isinstance(overlaps, list) or len(overlaps) > MAX_PROVENANCE_QUESTIONS:
        raise ValueError("Phase 0 generation question overlaps are not bounded")
    seen: set[str] = set()
    for overlap in overlaps:
        if not isinstance(overlap, dict) or set(overlap) != QUESTION_SOURCE_OVERLAP_FIELDS:
            raise ValueError("Phase 0 generation overlap differs from the exact schema")
        _validate_question_id(overlap["question_id"], allow_shared=False)
        if overlap["question_id"] in seen:
            raise ValueError("Phase 0 generation overlaps contain duplicate questions")
        seen.add(overlap["question_id"])
        if overlap["binding_scope"] != "candidate_overlap_coarse_unattributed":
            raise ValueError("Phase 0 generation overlap scope is not allowlisted")
        _validate_source_id_array(
            overlap["candidate_overlap_source_ids"],
            label="candidate overlap source IDs",
        )
        if not set(overlap["candidate_overlap_source_ids"]).issubset(value["selected_source_ids"]):
            raise ValueError("Phase 0 generation overlap contains an unselected source")
    uncovered = value["candidate_uncovered_question_ids"]
    _validate_question_id_array(uncovered, label="uncovered questions")
    if not set(uncovered).issubset(seen):
        raise ValueError("Phase 0 uncovered questions differ from recorded overlaps")
    _validate_count_triplet(
        value,
        "question_overlaps",
        len(overlaps),
        MAX_PROVENANCE_QUESTIONS,
    )
    if value["contract_status"] not in CONTRACT_STATUSES:
        raise ValueError("Phase 0 generation contract status is not allowlisted")
    _validate_safe_reason(value["reason"], label="generation reason")


def _validate_verify_trace(value: dict[str, Any]) -> None:
    if not value:
        return
    if set(value) != VERIFY_TRACE_FIELDS:
        raise ValueError("Phase 0 verify trace differs from the exact v2 schema")
    if value["schema_version"] != PROVENANCE_SCHEMA_VERSION:
        raise ValueError("Phase 0 verify provenance version is invalid")
    if value["decision"] not in VERIFY_DECISIONS:
        raise ValueError("Phase 0 verify decision is not allowlisted")
    _validate_safe_reason(value["reason"], label="verify reason")
    references = value["referenced_source_ids"]
    _validate_source_id_array(references, label="verify referenced source IDs")
    _validate_count_triplet(
        value,
        "referenced_source_ids",
        len(references),
        MAX_PROVENANCE_SOURCE_IDS,
    )
    if value["reference_scope"] not in REFERENCE_SCOPES:
        raise ValueError("Phase 0 verify reference scope is not allowlisted")
    _validate_question_id_array(
        value["candidate_uncovered_question_ids"],
        label="verify uncovered questions",
    )
    if type(value["verifier_triggered"]) is not bool:
        raise ValueError("Phase 0 verify trigger flag is untyped")


def _validate_generate_retries(value: list[Any]) -> None:
    if len(value) > MAX_PROVENANCE_ATTEMPTS:
        raise ValueError("Phase 0 generate retries are not bounded")
    for event in value:
        if not isinstance(event, dict) or set(event) != GENERATE_RETRY_FIELDS:
            raise ValueError("Phase 0 generate retry differs from the exact schema")
        _validate_nonnegative_int(
            event["latency_ms"],
            label="retry latency",
            maximum=MAX_LATENCY_MS,
        )
        _validate_safe_reason(event["reason"], label="retry reason")
        _validate_nonnegative_int(
            event["chunks"],
            label="retry chunk count",
            maximum=MAX_PROVENANCE_SOURCE_IDS,
        )


def _validate_trace_timeline(value: list[Any]) -> None:
    if len(value) > MAX_TRACE_EVENTS:
        raise ValueError("Phase 0 trace timeline is not bounded")
    expected_error_codes = {
        "analyze": "analyze_error",
        "retrieve": "retrieve_error",
        "rerank": "rerank_error",
        "generate": "generate_error",
        "guard": "guard_error",
        "verify": "verify_error",
        "respond": "respond_error",
    }
    for event in value:
        if (
            not isinstance(event, dict)
            or not {"node", "latency_ms", "error_present"}.issubset(event)
            or not set(event).issubset(TIMELINE_FIELDS)
        ):
            raise ValueError("Phase 0 trace timeline differs from the exact schema")
        if event["node"] not in TRACE_NODES:
            raise ValueError("Phase 0 trace timeline node is not allowlisted")
        _validate_nonnegative_int(
            event["latency_ms"],
            label="timeline latency",
            maximum=MAX_LATENCY_MS,
        )
        _validate_error_telemetry(
            event["error_present"],
            event.get("error_code"),
            allowed_codes=TRACE_ERROR_CODES,
            label="timeline",
        )
        if event["error_present"] and event["error_code"] != expected_error_codes.get(
            event["node"], "other"
        ):
            raise ValueError("Phase 0 trace timeline error code contradicts its node")


def _validate_provenance_header(value: dict[str, Any], *, allow_shared: bool) -> None:
    if value["schema_version"] != PROVENANCE_SCHEMA_VERSION:
        raise ValueError("Phase 0 question provenance version is invalid")
    _validate_question_id(value["question_id"], allow_shared=allow_shared)


def _validate_question_id(value: Any, *, allow_shared: bool) -> None:
    if value == "shared" and allow_shared:
        return
    if not isinstance(value, str) or QUESTION_ID_RE.fullmatch(value) is None:
        raise ValueError("Phase 0 provenance question ID is invalid")


def _validate_question_id_array(value: Any, *, label: str) -> None:
    if not isinstance(value, list) or len(value) > MAX_PROVENANCE_QUESTIONS:
        raise ValueError(f"Phase 0 {label} is not a bounded array")
    for item in value:
        _validate_question_id(item, allow_shared=False)
    if len(set(value)) != len(value):
        raise ValueError(f"Phase 0 {label} contains duplicates")


def _validate_source_id_array(
    value: Any,
    *,
    label: str,
    maximum: int = MAX_PROVENANCE_SOURCE_IDS,
) -> None:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"Phase 0 {label} is not a bounded array")
    for item in value:
        _validate_chunk_id(item, label=label)
    if len(set(value)) != len(value):
        raise ValueError(f"Phase 0 {label} contains duplicates")


def _validate_chunk_id(value: Any, *, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"Phase 0 {label} contains an invalid chunk ID")
    if HASHED_CHUNK_ID_RE.fullmatch(value) is not None:
        return
    if len(value) > MAX_SAFE_STRING_LENGTH or any(ord(character) < 32 for character in value):
        raise ValueError(f"Phase 0 {label} contains an invalid chunk ID")


def _validate_count_triplet(
    value: dict[str, Any],
    prefix: str,
    actual_recorded: int,
    maximum_recorded: int,
) -> None:
    keys = {f"{prefix}_total", f"{prefix}_recorded", f"{prefix}_truncated_count"}
    if not keys.issubset(value):
        raise ValueError(f"Phase 0 {prefix} truncation counters are incomplete")
    total = value[f"{prefix}_total"]
    recorded = value[f"{prefix}_recorded"]
    truncated = value[f"{prefix}_truncated_count"]
    _validate_nonnegative_int(total, label=f"{prefix} total")
    _validate_nonnegative_int(recorded, label=f"{prefix} recorded", maximum=maximum_recorded)
    _validate_nonnegative_int(truncated, label=f"{prefix} truncated")
    if recorded != actual_recorded or total - recorded != truncated:
        raise ValueError(f"Phase 0 {prefix} truncation counters are inconsistent")


def _validate_optional_count_triplet(
    value: dict[str, Any],
    prefix: str,
    *,
    maximum_recorded: int,
) -> None:
    keys = {f"{prefix}_total", f"{prefix}_recorded", f"{prefix}_truncated_count"}
    present = keys & set(value)
    if not present:
        return
    if present != keys:
        raise ValueError(f"Phase 0 {prefix} truncation counters are incomplete")
    _validate_count_triplet(
        value,
        prefix,
        value[f"{prefix}_recorded"],
        maximum_recorded,
    )


def _validate_nonnegative_int(
    value: Any,
    *,
    label: str,
    maximum: int = MAX_TELEMETRY_COUNT,
) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"Phase 0 {label} is not a bounded non-negative integer")


def _validate_score(
    value: Any,
    *,
    label: str,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> None:
    if not _is_finite_number(value):
        raise ValueError(f"Phase 0 {label} is not finite")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"Phase 0 {label} is not finite") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"Phase 0 {label} is outside the allowed range")


def _validate_optional_score(value: Any, *, label: str) -> None:
    if value is not None:
        _validate_score(value, label=label)


def _validate_nonnegative_number(value: Any, *, label: str) -> None:
    if not _is_finite_number(value):
        raise ValueError(f"Phase 0 {label} is not finite")
    try:
        negative = float(value) < 0
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"Phase 0 {label} is not finite") from exc
    if negative:
        raise ValueError(f"Phase 0 {label} must be non-negative")


def _validate_bounded_string(value: Any, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > MAX_SAFE_STRING_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"Phase 0 {label} is not a bounded scalar string")


def _validate_optional_bounded_string(value: Any, *, label: str) -> None:
    if value is not None:
        _validate_bounded_string(value, label=label)


def _validate_safe_reason(value: Any, *, label: str) -> None:
    if not isinstance(value, str) or SAFE_REASON_RE.fullmatch(value) is None:
        raise ValueError(f"Phase 0 {label} is not a controlled reason")


def _validate_optional_safe_reason(value: Any, *, label: str) -> None:
    if value is not None:
        _validate_safe_reason(value, label=label)


def _validate_query_analysis(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or not set(value).issubset(QUERY_ANALYSIS_FIELDS):
        raise ValueError("Phase 0 query analysis differs from the scalar allowlist")
    for field in QUERY_ANALYSIS_STRING_FIELDS & set(value):
        _validate_bounded_string(value[field], label=f"query analysis {field}")
    if "complexity" in value and value["complexity"] not in {"simple", "complex"}:
        raise ValueError("Phase 0 query analysis complexity is not allowlisted")
    if "response_profile" in value and value["response_profile"] not in RESPONSE_PROFILES:
        raise ValueError("Phase 0 query analysis response profile is not allowlisted")
    if "escalation_reason" in value:
        _validate_safe_reason(
            value["escalation_reason"],
            label="query analysis escalation reason",
        )
    for field in QUERY_ANALYSIS_BOOL_FIELDS & set(value):
        if type(value[field]) is not bool:
            raise ValueError("Phase 0 query analysis contains an untyped flag")
    for field in {"questions_count", "topics_count"} & set(value):
        _validate_nonnegative_int(value[field], label=f"query analysis {field}")


def _validate_verifier_result(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or not set(value).issubset(VERIFIER_RESULT_FIELDS):
        raise ValueError("Phase 0 verifier result differs from the typed allowlist")
    for field in {"has_hallucination", "triggered_llm_judge"} & set(value):
        if type(value[field]) is not bool:
            raise ValueError("Phase 0 verifier result contains an untyped flag")
    if "confidence" in value:
        _validate_score(value["confidence"], label="verifier confidence")


def _validate_llm_usage(events: list[Any]) -> None:
    if len(events) > MAX_LLM_USAGE_EVENTS:
        raise ValueError("Phase 0 LLM usage event list is not bounded")
    for event in events:
        if not isinstance(event, dict) or set(event) != LLM_USAGE_FIELDS:
            raise ValueError("Phase 0 LLM usage event differs from the exact allowlist")
        _validate_bounded_string(event["model"], label="LLM model")
        for field in (
            "latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ):
            _validate_nonnegative_int(event[field], label=f"LLM usage {field}")
        cost = event["estimated_cost_rub"]
        _validate_nonnegative_number(cost, label="LLM usage cost")
        if type(event["priced"]) is not bool:
            raise ValueError("Phase 0 LLM usage event contains an untyped priced flag")


def _is_finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _validate_error_telemetry(
    present: Any,
    code: Any,
    *,
    allowed_codes: frozenset[str],
    label: str,
) -> None:
    if type(present) is not bool:
        raise ValueError(f"Phase 0 {label} error presence must be boolean")
    if present is False:
        if code is not None:
            raise ValueError(f"Phase 0 {label} error code contradicts absence")
        return
    if not isinstance(code, str) or code not in allowed_codes:
        raise ValueError(f"Phase 0 {label} error code is not allowlisted")


def _reject_forbidden_nested_keys(value: Any) -> None:
    if isinstance(value, dict):
        normalized_keys = {_normalized_field_key(key) for key in value}
        forbidden_identity = {_normalized_field_key(key) for key in FORBIDDEN_OUTPUT_FIELDS}
        forbidden_errors = {_normalized_field_key(key) for key in FORBIDDEN_RAW_ERROR_FIELDS}
        if forbidden_identity & normalized_keys:
            raise ValueError("Phase 0 trace projection contains a forbidden identity field")
        if forbidden_errors & normalized_keys:
            raise ValueError("Phase 0 trace projection contains raw error telemetry")
        for nested in value.values():
            _reject_forbidden_nested_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_nested_keys(nested)


def _normalized_field_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _parse_utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Phase 0 trace timestamp must be a string")
    normalized = value.replace("Z", "+00:00")
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("Phase 0 trace timestamp is invalid") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise ValueError("Phase 0 trace timestamp must use UTC")
    return timestamp.astimezone(UTC)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _write_exclusive_atomic(
    path: Path,
    payload: bytes,
    *,
    expected_parent_identity: tuple[int, int],
) -> None:
    _assert_output_parent(path, expected_identity=expected_parent_identity)
    staging_root = _resolved_private_root()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=staging_root,
        prefix=".phase0-trace-export.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    published = False
    publication_verified = False
    temporary_identity: tuple[int, int] | None = None
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        temporary_identity = _regular_file_identity(temporary)
        _resolved_private_root()
        _assert_output_parent(path, expected_identity=expected_parent_identity)
        os.link(temporary, path)
        published = True
        _assert_output_parent(
            path,
            expected_identity=expected_parent_identity,
            output_must_exist=True,
        )
        if _regular_file_identity(path) != temporary_identity:
            raise ValueError("output publication changed during export")
        temporary.unlink()
        publication_verified = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if published and not publication_verified and temporary_identity is not None:
            _unlink_if_identity_matches(path, temporary_identity)
        temporary.unlink(missing_ok=True)


def _regular_file_identity(path: Path) -> tuple[int, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("export staging file changed during publication")
    stat_result = path.stat()
    return int(stat_result.st_dev), int(stat_result.st_ino)


def _unlink_if_identity_matches(path: Path, identity: tuple[int, int]) -> None:
    """Remove only our published hard link after a failed post-publication check."""

    try:
        if _regular_file_identity(path) == identity:
            path.unlink()
    except (FileNotFoundError, OSError, ValueError):
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export the fixed Phase 0 request trace projection either server-locally or "
            "through owner-controlled SSH. The command reads no env files, DSNs, tokens "
            "or API keys."
        )
    )
    execution = parser.add_mutually_exclusive_group(required=True)
    execution.add_argument("--ssh-target")
    execution.add_argument(
        "--server-local",
        action="store_true",
        help="run Docker/PostgreSQL projection on this host without SSH",
    )
    parser.add_argument("--eval-run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--interactive-sudo",
        action="store_true",
        help=(
            "allow sudo authentication through the owner's TTY; the exporter never "
            "reads or stores the password"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if bool(getattr(args, "server_local", False)):
            result = export_phase0_trace_review_server_local(
                eval_run_id=args.eval_run_id,
                output_path=args.output,
                interactive_sudo=bool(getattr(args, "interactive_sudo", False)),
            )
        else:
            result = export_phase0_trace_review(
                ssh_target=args.ssh_target,
                eval_run_id=args.eval_run_id,
                output_path=args.output,
                interactive_sudo=bool(getattr(args, "interactive_sudo", False)),
            )
    except EvidenceUnavailableError:
        print("phase0_trace_export=STOP reason=evidence_unavailable")
        return 2
    except SafeExportFailure as exc:
        print(f"phase0_trace_export=FAIL reason={exc.code}", file=sys.stderr)
        return 1
    except FileExistsError:
        print("phase0_trace_export=FAIL reason=output_exists", file=sys.stderr)
        return 1
    except ValueError:
        print("phase0_trace_export=FAIL reason=validation_failed", file=sys.stderr)
        return 1
    except OSError:
        print("phase0_trace_export=FAIL reason=io_failure", file=sys.stderr)
        return 1
    except RuntimeError:
        print("phase0_trace_export=FAIL reason=runtime_failure", file=sys.stderr)
        return 1
    print("phase0_trace_export=OK")
    print(f"path={result['path']}")
    print(f"sha256={result['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
