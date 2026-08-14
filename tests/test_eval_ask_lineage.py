from __future__ import annotations

import pytest

from eval.run_ask import _normalize_case, score_case
from eval.stage_funnel import build_stage_funnel_report
from src.graph.provenance import PROVENANCE_SCHEMA_VERSION


def test_score_case_exports_separate_exact_pipeline_lineage() -> None:
    trace = {
        "retrieved_chunks": [
            {"chunk_id": "retrieved_only"},
            {"chunk_id": "selected"},
        ],
        "reranker_scores": [
            {"chunk_id": "selected", "reranker_score": 0.93},
        ],
        "cited_sources": ["cited"],
        "trace_events": [
            {
                "node": "retrieve",
                "metadata": {
                    "question_provenance": [
                        {
                            "schema_version": PROVENANCE_SCHEMA_VERSION,
                            "question_id": "q1",
                            "retrieved_chunk_ids": ["retrieved_only", "selected"],
                        }
                    ]
                },
            },
            {
                "node": "rerank",
                "metadata": {
                    "confidence_source": "reranker",
                    "confidence_components": {
                        "raw_reranker_max": 0.93,
                        "decision_confidence": 0.93,
                    },
                    "question_provenance": [
                        {
                            "schema_version": PROVENANCE_SCHEMA_VERSION,
                            "question_id": "q1",
                            "output_chunks": [
                                {"chunk_id": "selected", "score": 0.93}
                            ],
                        }
                    ],
                },
            },
            {
                "node": "generate_selection",
                "metadata": {
                    "schema_version": PROVENANCE_SCHEMA_VERSION,
                    "mode": "llm",
                    "selected_source_ids": ["selected"],
                    "question_source_overlaps": [
                        {
                            "question_id": "q1",
                            "binding_scope": "candidate_overlap_coarse_unattributed",
                            "candidate_overlap_source_ids": ["selected"],
                        }
                    ],
                    "candidate_uncovered_question_ids": [],
                    "contract_status": "passed",
                    "reason": "passed",
                },
            },
            {
                "node": "verify_decision",
                "metadata": {
                    "schema_version": PROVENANCE_SCHEMA_VERSION,
                    "decision": "pass",
                    "reason": "passed",
                    "referenced_source_ids": ["cited"],
                },
            },
        ],
    }

    result = score_case(
        {"id": "case-1", "query": "safe query"},
        {"http_status": 200, "response": "safe response"},
        trace,
    )

    assert result["observed_chunk_ids"] == ["cited", "retrieved_only", "selected"]
    assert result["observed_chunk_ids_scope"] == (
        "union_retrieved_reranked_cited_legacy"
    )
    assert result["retrieved_chunk_ids"] == ["retrieved_only", "selected"]
    assert result["reranked_chunk_ids"] == ["selected"]
    assert result["selected_source_ids"] == ["selected"]
    assert result["ordered_cited_source_ids"] == ["cited"]
    assert result["verification_source_ids"] == ["cited"]
    assert result["lineage_schema_version"] == PROVENANCE_SCHEMA_VERSION
    assert result["lineage_attribution"] == "exact"
    assert result["lineage_stage_available"] == {
        "retrieve": True,
        "rerank": True,
        "source_selection": True,
        "citation": True,
        "verify": True,
    }
    assert result["question_lineage"] == [
        {
            "question_id": "q1",
            "retrieved_chunk_ids": ["retrieved_only", "selected"],
            "reranked_chunk_ids": ["selected"],
            "candidate_overlap_source_ids": ["selected"],
            "selection_binding_scope": "candidate_overlap_coarse_unattributed",
            "source_missing": False,
        }
    ]


def test_score_case_marks_old_union_trace_as_legacy_coarse() -> None:
    result = score_case(
        {"id": "case-old", "query": "safe query"},
        {"http_status": 200, "response": "safe response"},
        {
            "retrieved_chunks": [{"chunk_id": "old"}],
            "reranker_scores": [],
            "cited_sources": [],
        },
    )

    assert result["observed_chunk_ids"] == ["old"]
    assert result["retrieved_chunk_ids"] == ["old"]
    assert result["lineage_schema_version"] == "legacy-union-v1"
    assert result["lineage_attribution"] == "legacy_coarse"


def test_score_case_exports_bounded_semantic_recovery_diagnostics() -> None:
    result = score_case(
        {"id": "case-recovery", "query": "safe query"},
        {"http_status": 200, "response": "safe response"},
        {
            "trace_events": [
                {
                    "node": "semantic_recovery",
                    "metadata": {
                        "status": "ok",
                        "reason": "low_confidence",
                        "model": "private-model-name-is-not-exported",
                        "model_questions": 3,
                        "effective_questions": 5,
                    },
                }
            ]
        },
    )

    assert result["semantic_recovery_attempted"] is True
    assert result["semantic_recovery_status"] == "ok"
    assert result["semantic_recovery_reason"] == "low_confidence"
    assert result["semantic_recovery_model_questions"] == 3
    assert result["semantic_recovery_effective_questions"] == 5
    assert "semantic_recovery_model" not in result


def test_gold_ticket_identity_survives_ask_scoring_and_matches_stage_funnel() -> None:
    ticket_hash = "a" * 24
    normalized = _normalize_case(
        {
            "id": f"{ticket_hash}::s001",
            "ticket_id_hash": ticket_hash,
            "step_id": "s001",
            "query": "deidentified question",
            "privacy_class": "private_ticket_derived",
            "split": "calibration",
            "label_status": "human_reviewed",
            "requires_human_review": False,
            "tags": ["gold_ticket:v1", "split:calibration"],
            "expected_behavior": "answer",
            "expected_chunk_ids": ["source-a"],
            "expected_cited_chunk_ids": ["source-a"],
        }
    )
    observation = score_case(
        normalized,
        {"http_status": 200, "response": "answer [src:source-a]"},
        {
            "retrieved_chunks": [{"chunk_id": "source-a"}],
            "reranker_scores": [{"chunk_id": "source-a"}],
            "cited_sources": ["source-a"],
            "trace_events": [
                {
                    "node": "generate_selection",
                    "metadata": {
                        "schema_version": PROVENANCE_SCHEMA_VERSION,
                        "selected_source_ids": ["source-a"],
                        "contract_status": "passed",
                        "reason": "passed",
                    },
                },
                {
                    "node": "verify_decision",
                    "metadata": {
                        "schema_version": PROVENANCE_SCHEMA_VERSION,
                        "decision": "pass",
                        "reason": "passed",
                        "referenced_source_ids": ["source-a"],
                    },
                },
            ],
        },
    )
    observation["claim_verdicts"] = {}
    report = build_stage_funnel_report(
        [
            {
                "ticket_id_hash": ticket_hash,
                "evaluation_steps": [
                    {
                        "step_id": "s001",
                        "expected_action": "answer",
                        "answerability": "full",
                        "qrels": [
                            {
                                "chunk_id": "source-a",
                                "grade": 3,
                                "supports_claim_ids": [],
                            }
                        ],
                        "expected_claims": [],
                    }
                ],
            }
        ],
        [observation],
    )

    assert observation["ticket_id_hash"] == ticket_hash
    assert observation["step_id"] == "s001"
    assert report["steps"][0]["lineage_mode"] == "partial"
    assert report["steps"][0]["first_loss_stage"] == "unscored"


def test_unversioned_lookalike_events_remain_legacy_coarse() -> None:
    result = score_case(
        {"id": "case-lookalike", "query": "safe query"},
        {"http_status": 200, "response": "safe response"},
        {
            "retrieved_chunks": [{"chunk_id": "source-a"}],
            "reranker_scores": [{"chunk_id": "source-a"}],
            "cited_sources": ["source-a"],
            "trace_events": [
                {
                    "node": "generate_selection",
                    "metadata": {
                        "selected_source_ids": ["source-a"],
                        "contract_status": "passed",
                    },
                },
                {
                    "node": "verify_decision",
                    "metadata": {
                        "referenced_source_ids": ["source-a"],
                        "decision": "pass",
                    },
                },
            ],
        },
    )

    assert result["lineage_schema_version"] == "legacy-union-v1"
    assert result["lineage_attribution"] == "legacy_coarse"
    assert result["lineage_stage_available"] == {
        "retrieve": False,
        "rerank": False,
        "source_selection": False,
        "citation": True,
        "verify": False,
    }
    assert result["selected_source_ids"] == []
    assert result["verification_source_ids"] == []


def test_gold_ticket_tag_rejects_missing_safe_identity() -> None:
    with pytest.raises(ValueError, match="ticket_id_hash"):
        _normalize_case(
            {
                "id": "unsafe",
                "query": "deidentified question",
                "privacy_class": "private_ticket_derived",
                "split": "calibration",
                "label_status": "human_reviewed",
                "requires_human_review": False,
                "tags": ["gold_ticket:v1", "split:calibration"],
            }
        )
