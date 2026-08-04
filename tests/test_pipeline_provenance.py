from __future__ import annotations

import json

import pytest

import src.graph.nodes.generate as generate_node
import src.graph.nodes.verify as verify_node
from src.graph.nodes.retrieve import retrieve
from src.graph.nodes.verify import verify
from src.graph.provenance import (
    MAX_PROVENANCE_CANDIDATES,
    MAX_PROVENANCE_QUESTIONS,
    PROVENANCE_SCHEMA_VERSION,
    chunk_candidate_batch,
    chunk_candidates,
    rerank_question_provenance,
    safe_filter,
    source_selection_provenance,
)
from src.logging.tracer import Tracer
from src.models import Chunk, QueryAnalysis, Question, ScoredChunk, VerificationResult


def test_provenance_never_copies_query_or_chunk_text() -> None:
    canary = "RAW_PRIVATE_CANARY_7d2f"
    chunk = Chunk(
        chunk_id="yonote_safe_id",
        text=canary,
        metadata={"source_type": "yonote", "query": canary},
        score=0.75,
    )

    payload = {
        "filters": safe_filter(
            {
                "source_type": "yonote",
                "category": "форумы",
                "query": canary,
            }
        ),
        "candidates": chunk_candidates([chunk], method="hybrid"),
    }

    assert payload["filters"]["source_type"] == "yonote"
    assert payload["filters"]["category"].startswith("sha256:")
    assert "форумы" not in json.dumps(payload, ensure_ascii=False)
    assert canary not in json.dumps(payload, ensure_ascii=False)


def test_candidate_provenance_is_bounded_with_explicit_counters() -> None:
    chunks = [
        Chunk(chunk_id=f"chunk_{index}", text="source", metadata={}, score=0.5)
        for index in range(MAX_PROVENANCE_CANDIDATES + 7)
    ]

    rows, counts = chunk_candidate_batch(((chunks, "hybrid"),))

    assert len(rows) == MAX_PROVENANCE_CANDIDATES
    assert counts == {
        "candidates_total": MAX_PROVENANCE_CANDIDATES + 7,
        "candidates_recorded": MAX_PROVENANCE_CANDIDATES,
        "candidates_truncated_count": 7,
    }


@pytest.mark.asyncio
async def test_retrieve_trace_does_not_copy_model_filter_values() -> None:
    canary = "MODEL_FILTER_PRIVATE_CANARY"

    class Retriever:
        async def retrieve_by_metadata(self, filters: dict, top_k: int) -> list[Chunk]:
            _ = filters, top_k
            return [
                Chunk(
                    chunk_id="safe_chunk",
                    text="source",
                    metadata={"source_type": "yonote"},
                    score=0.9,
                )
            ]

    tracer = Tracer()
    await retrieve(
        {
            "trace": tracer,
            "analysis": QueryAnalysis(
                category=canary,
                forum_normalized=canary,
                questions=[
                    Question(
                        text="Safe question",
                        topic=canary,
                        category=canary,
                        forum_normalized=canary,
                    )
                ],
            ),
            "message_masked": "Safe question",
            "retriever": Retriever(),
        }
    )

    event = next(event for event in tracer.events if event.node == "retrieve")
    serialized = json.dumps(event.metadata, ensure_ascii=False)
    assert canary not in serialized
    assert "sha256:" in serialized


def test_rerank_provenance_keeps_question_stage_boundaries() -> None:
    reranked = [
        ScoredChunk(
            chunk_id="b",
            text="source b",
            metadata={"source_type": "yonote"},
            score=0.7,
            reranker_score=0.91,
        ),
        ScoredChunk(
            chunk_id="c",
            text="source c",
            metadata={"source_type": "yonote"},
            score=0.6,
            reranker_score=0.82,
        ),
    ]
    retrieval = [
        {"question_id": "q1", "retrieved_chunk_ids": ["a", "b"]},
        {"question_id": "q2", "retrieved_chunk_ids": ["c"]},
    ]

    result = rerank_question_provenance(retrieval, reranked)

    assert result[0]["output_chunks"] == [{"chunk_id": "b", "score": 0.91}]
    assert result[0]["dropped_chunk_ids"] == ["a"]
    assert result[1]["output_chunks"] == [{"chunk_id": "c", "score": 0.82}]


def test_rerank_provenance_bounds_question_count() -> None:
    retrieval = [
        {
            "question_id": f"q{index + 1}",
            "retrieved_chunk_ids": [f"chunk_{index}"],
            "questions_total": MAX_PROVENANCE_QUESTIONS + 3,
        }
        for index in range(MAX_PROVENANCE_QUESTIONS + 3)
    ]

    result = rerank_question_provenance(retrieval, [])

    assert len(result) == MAX_PROVENANCE_QUESTIONS
    assert result[0]["questions_total"] == MAX_PROVENANCE_QUESTIONS + 3
    assert result[0]["questions_recorded"] == MAX_PROVENANCE_QUESTIONS
    assert result[0]["questions_truncated_count"] == 3


def test_source_overlap_counters_exclude_shared_fallback() -> None:
    rows, uncovered, counts = source_selection_provenance(
        [
            {
                "question_id": "q1",
                "retrieved_chunk_ids": ["selected"],
                "questions_total": 2,
                "attributable_questions_total": 1,
            },
            {"question_id": "shared", "retrieved_chunk_ids": ["selected"]},
        ],
        ["selected"],
    )

    assert rows[0]["candidate_overlap_source_ids"] == ["selected"]
    assert uncovered == []
    assert counts == {
        "question_overlaps_total": 1,
        "question_overlaps_recorded": 1,
        "question_overlaps_truncated_count": 0,
    }


@pytest.mark.asyncio
async def test_generate_records_global_selection_and_coarse_question_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = ScoredChunk(
        chunk_id="yonote_answer",
        text="Подтверждённый ответ.",
        metadata={"source_type": "yonote"},
        score=0.9,
        reranker_score=0.95,
    )

    async def fake_core(state: dict) -> dict:
        _ = state
        return {
            "generated_response": "Подтверждённый ответ. [src:yonote_answer]",
            "generator_model": "source_chunk",
            "cited_sources": ["yonote_answer"],
        }

    async def fake_contract(state: dict, result: dict) -> dict:
        _ = state
        return result

    monkeypatch.setattr(generate_node, "_generate_core", fake_core)
    monkeypatch.setattr(generate_node, "_enforce_generation_contract", fake_contract)
    tracer = Tracer()
    tracer.add("generate", 1, mode="source_chunk")
    result = await generate_node.generate(
        {
            "trace": tracer,
            "reranked_chunks": [chunk],
            "retrieval_provenance": [
                {
                    "schema_version": PROVENANCE_SCHEMA_VERSION,
                    "question_id": "q1",
                    "retrieved_chunk_ids": ["yonote_answer"],
                }
            ],
        }
    )

    metadata = tracer.events[-1].metadata
    assert tracer.events[-1].node == "generate_selection"
    assert metadata["selected_source_ids"] == ["yonote_answer"]
    assert metadata["cited_source_ids"] == ["yonote_answer"]
    assert metadata["selection_binding_scope"] == "global_exact_question_unattributed"
    assert metadata["question_source_overlaps"] == [
        {
            "question_id": "q1",
            "binding_scope": "candidate_overlap_coarse_unattributed",
            "candidate_overlap_source_ids": ["yonote_answer"],
        }
    ]
    assert metadata["candidate_uncovered_question_ids"] == []
    assert "_selected_source_ids" not in result


@pytest.mark.asyncio
async def test_verify_records_controlled_escalation_decision() -> None:
    tracer = Tracer()
    tracer.add(
        "generate_selection",
        0,
        candidate_uncovered_question_ids=["q1"],
    )

    result = await verify(
        {
            "trace": tracer,
            "generated_response": "",
            "reranked_chunks": [],
            "should_escalate": True,
            "escalation_reason": "insufficient_sources",
        }
    )

    assert result["verification"].has_hallucination is False
    event = tracer.events[-1]
    assert event.node == "verify_decision"
    assert event.metadata["decision"] == "escalate"
    assert event.metadata["reason"] == "insufficient_sources"
    assert event.metadata["candidate_uncovered_question_ids"] == ["q1"]


def test_verify_uses_citations_from_replacement_response_only() -> None:
    tracer = Tracer()
    chunks = [
        ScoredChunk(
            chunk_id=chunk_id,
            text="source",
            metadata={"source_type": "yonote"},
            score=0.9,
            reranker_score=0.9,
        )
        for chunk_id in ("stale", "replacement")
    ]
    state = {
        "trace": tracer,
        "generated_response": "Old answer [src:stale]",
        "cited_sources": ["stale"],
        "reranked_chunks": chunks,
    }
    result = {
        "verification": VerificationResult(),
        "generated_response": "New answer [src:replacement]",
    }

    verify_node._trace_verify_decision(state, result, latency_ms=1)

    event = tracer.events[-1]
    assert event.metadata["referenced_source_ids"] == ["replacement"]
    assert event.metadata["reference_scope"] == "actual_response_explicit"


def test_verify_marks_unchanged_inherited_citations_as_coarse() -> None:
    tracer = Tracer()
    chunk = ScoredChunk(
        chunk_id="inherited",
        text="source",
        metadata={"source_type": "yonote"},
        score=0.9,
        reranker_score=0.9,
    )
    state = {
        "trace": tracer,
        "generated_response": "Direct source answer without an inline marker.",
        "cited_sources": ["inherited"],
        "reranked_chunks": [chunk],
    }

    verify_node._trace_verify_decision(
        state,
        {"verification": VerificationResult()},
        latency_ms=1,
    )

    event = tracer.events[-1]
    assert event.metadata["referenced_source_ids"] == ["inherited"]
    assert event.metadata["reference_scope"] == "inherited_state_coarse"


def test_verify_does_not_inherit_state_citations_over_unknown_inline_reference() -> None:
    tracer = Tracer()
    chunk = ScoredChunk(
        chunk_id="stale",
        text="source",
        metadata={"source_type": "yonote"},
        score=0.9,
        reranker_score=0.9,
    )
    state = {
        "trace": tracer,
        "generated_response": "Answer [src:unknown]",
        "cited_sources": ["stale"],
        "reranked_chunks": [chunk],
    }

    verify_node._trace_verify_decision(
        state,
        {"verification": VerificationResult()},
        latency_ms=1,
    )

    event = tracer.events[-1]
    assert event.metadata["referenced_source_ids"] == []
    assert event.metadata["reference_scope"] == "actual_response_unknown_reference"
