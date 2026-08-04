from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from eval.stage_funnel import (
    SAFE_SCHEMA_VERSION,
    build_stage_funnel_report,
    load_observations,
    load_tickets_jsonl,
    run_stage_funnel,
)


def _ticket(
    *,
    ticket_id: str = "ticket-1",
    action: str = "answer",
    answerability: str = "full",
    qrels: list[dict[str, object]] | None = None,
    claims: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "gold-ticket-v1",
        "ticket_id_hash": ticket_id,
        "evaluation_steps": [
            {
                "step_id": f"{ticket_id}-step",
                "expected_action": action,
                "answerability": answerability,
                "qrels": qrels if qrels is not None else [],
                "expected_claims": claims if claims is not None else [],
            }
        ],
    }


def _observation(
    *,
    ticket_id: str = "ticket-1",
    source_id: str = "source-a",
) -> dict[str, object]:
    return {
        "ticket_id_hash": ticket_id,
        "step_id": f"{ticket_id}-step",
        "observed_action": "answer",
        "routing_action": "answer",
        "retrieved_chunk_ids": ["noise", source_id],
        "reranked_chunk_ids": [source_id],
        "selected_source_ids": [source_id],
        "generation_contract_status": "pass",
        "cited_source_ids": [source_id],
        "verification_source_ids": [source_id],
        "verification_decision": "pass",
        "lineage_schema_version": "question-pipeline-provenance-v1",
        "lineage_attribution": "exact",
        "lineage_stage_available": {
            "retrieve": True,
            "rerank": True,
            "source_selection": True,
            "citation": True,
            "verify": True,
        },
    }


def _grounded_ticket(*, ticket_id: str = "ticket-1") -> dict[str, object]:
    return _ticket(
        ticket_id=ticket_id,
        qrels=[
            {
                "chunk_id": "source-a",
                "grade": 3,
                "supports_claim_ids": ["claim-a"],
            }
        ],
        claims=[{"claim_id": "claim-a", "required": True, "critical": True}],
    )


def _grounded_observation(*, ticket_id: str = "ticket-1") -> dict[str, object]:
    observation = _observation(ticket_id=ticket_id)
    observation["claim_verdicts"] = {"claim-a": "supported"}
    return observation


def test_report_scores_action_and_true_rank_metrics() -> None:
    answer_ticket = _grounded_ticket(ticket_id="answer-ticket")
    answer_observation = _grounded_observation(ticket_id="answer-ticket")
    escalate_ticket = _ticket(
        ticket_id="escalate-ticket",
        action="escalate",
        answerability="none",
    )
    escalate_observation = {
        "ticket_id_hash": "escalate-ticket",
        "step_id": "escalate-ticket-step",
        "observed_action": "answer",
    }

    report = build_stage_funnel_report(
        [answer_ticket, escalate_ticket],
        [answer_observation, escalate_observation],
    )

    assert report["schema_version"] == SAFE_SCHEMA_VERSION
    assert report["action_confusion_matrix"] == {
        "answer": {"answer": 1},
        "escalate": {"answer": 1},
    }
    assert report["metrics"]["action_accuracy"] == {
        "numerator": 1,
        "denominator": 2,
        "rate": 0.5,
    }
    assert report["metrics"]["action_macro_f1"]["rate"] == 0.333334
    assert report["metrics"]["retrieval_recall_at_1"] == {
        "numerator": 0,
        "denominator": 1,
        "rate": 0.0,
    }
    assert report["metrics"]["retrieval_recall_at_3"] == {
        "numerator": 1,
        "denominator": 1,
        "rate": 1.0,
    }
    assert report["metrics"]["retrieval_mrr_at_10"] == {
        "numerator": 0.5,
        "denominator": 1,
        "rate": 0.5,
    }
    assert report["metrics"]["retrieval_ndcg_at_10"]["rate"] == 0.63093
    assert report["metrics"]["rerank_survival"]["rate"] == 1.0
    assert report["metrics"]["selection_survival"]["rate"] == 1.0
    assert report["metrics"]["citation_survival"]["rate"] == 1.0
    assert report["first_loss_stage_counts"] == {"pass": 1, "final_behavior": 1}


def test_recall_uses_all_relevant_qrels_in_denominator() -> None:
    ticket = _ticket(
        qrels=[
            {"chunk_id": "source-a", "grade": 3, "supports_claim_ids": []},
            {"chunk_id": "source-b", "grade": 2, "supports_claim_ids": []},
            {"chunk_id": "context-only", "grade": 1, "supports_claim_ids": []},
        ]
    )
    observation = _observation()

    report = build_stage_funnel_report([ticket], [observation])

    assert report["metrics"]["retrieval_recall_at_3"] == {
        "numerator": 1,
        "denominator": 2,
        "rate": 0.5,
    }
    assert report["metrics"]["retrieval_mrr_at_10"]["rate"] == 0.5
    assert 0 < report["metrics"]["retrieval_ndcg_at_10"]["rate"] < 1


@pytest.mark.parametrize(
    ("mutation", "expected_stage"),
    [
        ({"routing_action": "escalate"}, "routing"),
        ({"retrieved_chunk_ids": ["noise"]}, "retrieval"),
        ({"reranked_chunk_ids": ["noise"]}, "rerank"),
        ({"selected_source_ids": ["noise"]}, "source_selection"),
        (
            {
                "generation_contract_status": "fail",
                "generation_contract_reason": "llm_source_fact_binding_failed",
            },
            "generation_contract",
        ),
        ({"cited_source_ids": ["noise"]}, "citation"),
        ({"claim_verdicts": {"claim-a": "unsupported"}}, "claim_support"),
        ({"verification_decision": "escalate"}, "verification"),
        ({"observed_action": "clarify"}, "final_behavior"),
    ],
)
def test_first_loss_stage_is_the_first_exact_failure(
    mutation: dict[str, object],
    expected_stage: str,
) -> None:
    observation = _grounded_observation()
    observation.update(mutation)

    report = build_stage_funnel_report([_grounded_ticket()], [observation])

    assert report["steps"][0]["first_loss_stage"] == expected_stage
    assert report["steps"][0]["attribution_confidence"] == "exact"


def test_missing_stage_evidence_and_claim_verdict_are_unscored() -> None:
    missing_stage = _grounded_observation(ticket_id="missing-stage")
    missing_stage.pop("selected_source_ids")
    missing_stage["lineage_attribution"] = "partial"
    missing_stage["lineage_stage_available"]["source_selection"] = False  # type: ignore[index]
    missing_verdict = _grounded_observation(ticket_id="missing-verdict")
    missing_verdict.pop("claim_verdicts")

    report = build_stage_funnel_report(
        [
            _grounded_ticket(ticket_id="missing-stage"),
            _grounded_ticket(ticket_id="missing-verdict"),
        ],
        [missing_stage, missing_verdict],
    )

    assert [row["first_loss_stage"] for row in report["steps"]] == [
        "unscored",
        "unscored",
    ]
    assert report["first_loss_stage_counts"] == {"unscored": 2}


def test_answerable_step_without_relevant_qrels_is_a_label_or_content_gap() -> None:
    ticket = _ticket(
        qrels=[{"chunk_id": "irrelevant", "grade": 1, "supports_claim_ids": []}]
    )

    report = build_stage_funnel_report([ticket], [_observation()])

    assert report["steps"][0]["first_loss_stage"] == "label_or_content_gap"
    assert "retrieval_recall_at_10" not in report["metrics"]


def test_clarification_without_qrels_is_not_mislabeled_as_content_gap() -> None:
    ticket = _ticket(action="clarify", answerability="partial")
    observation = {
        "ticket_id_hash": "ticket-1",
        "step_id": "ticket-1-step",
        "observed_action": "clarify",
    }

    report = build_stage_funnel_report([ticket], [observation])

    assert report["steps"][0]["first_loss_stage"] == "pass"


def test_legacy_union_is_coarse_and_excluded_from_stage_metrics() -> None:
    observation = {
        "ticket_id_hash": "ticket-1",
        "step_id": "ticket-1-step",
        "observed_action": "answer",
        "observed_chunk_ids": ["source-a"],
    }

    report = build_stage_funnel_report([_grounded_ticket()], [observation])

    row = report["steps"][0]
    assert row["lineage_mode"] == "legacy_union"
    assert row["first_loss_stage"] == "legacy_lineage"
    assert row["attribution_confidence"] == "coarse"
    assert "retrieval_recall_at_10" not in report["metrics"]


def test_score_case_exact_lineage_adapter_uses_ordered_stage_fields() -> None:
    observation = {
        "ticket_id_hash": "ticket-1",
        "step_id": "ticket-1-step",
        "observed_behavior": "answer",
        "retrieved_chunk_ids": ["noise", "source-a"],
        "reranked_chunk_ids": ["source-a"],
        "selected_source_ids": ["source-a"],
        "ordered_cited_source_ids": ["source-a"],
        "verification_source_ids": ["source-a"],
        "lineage_attribution": "exact",
        "lineage_schema_version": "question-pipeline-provenance-v1",
        "lineage_stage_available": {
            "retrieve": True,
            "rerank": True,
            "source_selection": True,
            "citation": True,
            "verify": True,
        },
        "generation_contract_status": "passed",
        "generation_contract_reason": "passed",
        "verification_decision": "pass",
        "verification_reason": "passed",
        "claim_verdicts": {"claim-a": "supported"},
    }

    report = build_stage_funnel_report([_grounded_ticket()], [observation])

    row = report["steps"][0]
    assert row["lineage_mode"] == "exact"
    assert row["evidence_counts"]["verified"] == 1
    assert row["first_loss_stage"] == "pass"


def test_score_case_legacy_marker_overrides_placeholder_exact_arrays() -> None:
    observation = {
        "ticket_id_hash": "ticket-1",
        "step_id": "ticket-1-step",
        "observed_behavior": "answer",
        "observed_chunk_ids": ["source-a"],
        "retrieved_chunk_ids": ["source-a"],
        "reranked_chunk_ids": [],
        "selected_source_ids": [],
        "ordered_cited_source_ids": [],
        "verification_source_ids": [],
        "lineage_attribution": "legacy_coarse",
        "lineage_stage_available": {
            "retrieve": False,
            "rerank": False,
            "source_selection": False,
            "citation": False,
            "verify": False,
        },
    }

    report = build_stage_funnel_report([_grounded_ticket()], [observation])

    row = report["steps"][0]
    assert row["lineage_mode"] == "legacy_union"
    assert row["first_loss_stage"] == "legacy_lineage"
    assert row["attribution_confidence"] == "coarse"
    assert row["evidence_counts"]["retrieved"] == 0
    assert "retrieval_recall_at_10" not in report["metrics"]


def test_safe_report_drops_all_raw_text_and_identifiers() -> None:
    canary = "CANARY-PRIVATE-TEXT-9F91"
    ticket = _ticket(
        ticket_id=canary,
        qrels=[
            {
                "chunk_id": canary,
                "grade": 3,
                "supports_claim_ids": [canary],
            }
        ],
        claims=[
            {
                "claim_id": canary,
                "required": True,
                "critical": True,
                "value_normalized": canary,
            }
        ],
    )
    ticket["turns"] = [{"text": canary}]
    observation = _observation(ticket_id=canary, source_id=canary)
    observation["query"] = canary
    observation["response"] = canary
    observation["claim_verdicts"] = {canary: "supported"}

    report = build_stage_funnel_report([ticket], [observation])
    serialized = json.dumps(report, ensure_ascii=False)

    assert canary not in serialized
    assert '"query"' not in serialized
    assert '"response"' not in serialized
    assert '"chunk_id"' not in serialized
    assert '"ticket_id_hash"' not in serialized


@pytest.mark.parametrize("grade", [-1, 4, True, 1.5, "3"])
def test_qrel_grade_must_be_an_integer_from_zero_to_three(grade: object) -> None:
    ticket = _ticket(
        qrels=[{"chunk_id": "source-a", "grade": grade, "supports_claim_ids": []}]
    )

    with pytest.raises(ValueError, match="grade"):
        build_stage_funnel_report([ticket], [])


def test_jsonl_and_report_loaders_run_without_model_calls(tmp_path: Path) -> None:
    tickets_path = tmp_path / "tickets.jsonl"
    observations_path = tmp_path / "observations.json"
    output_path = tmp_path / "safe-stage-funnel.json"
    tickets_path.write_text(
        json.dumps(_grounded_ticket(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    observations_path.write_text(
        json.dumps({"results": [_grounded_observation()]}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert len(load_tickets_jsonl(tickets_path)) == 1
    assert len(load_observations(observations_path)) == 1
    report = run_stage_funnel(tickets_path, observations_path, output_path)

    assert report["first_loss_stage_counts"] == {"pass": 1}
    assert json.loads(output_path.read_text(encoding="utf-8")) == report


def test_trace_events_can_supply_selection_and_contract_enums() -> None:
    observation = _grounded_observation()
    observation.pop("selected_source_ids")
    observation.pop("generation_contract_status")
    observation["trace_events"] = [
        {
            "node": "generate_selection",
            "metadata": {
                "schema_version": "question-pipeline-provenance-v1",
                "selected_source_ids": ["source-a"],
                "contract_status": "pass",
            },
        },
    ]

    report = build_stage_funnel_report([_grounded_ticket()], [deepcopy(observation)])

    assert report["steps"][0]["first_loss_stage"] == "pass"


def test_exact_lineage_requires_schema_and_explicit_citation_availability() -> None:
    observation = _grounded_observation()
    observation.pop("lineage_schema_version")

    with pytest.raises(ValueError, match="validated pipeline schema"):
        build_stage_funnel_report([_grounded_ticket()], [observation])

    observation = _grounded_observation()
    observation["lineage_stage_available"].pop("citation")  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="every lineage stage"):
        build_stage_funnel_report([_grounded_ticket()], [observation])


def test_graded_alternative_qrels_are_scored_per_claim_not_as_all_of() -> None:
    ticket = _ticket(
        qrels=[
            {
                "chunk_id": "source-a",
                "grade": 3,
                "supports_claim_ids": ["claim-a"],
            },
            {
                "chunk_id": "source-b",
                "grade": 3,
                "supports_claim_ids": ["claim-a"],
            },
        ],
        claims=[{"claim_id": "claim-a", "required": True, "critical": False}],
    )
    observation = _grounded_observation()

    report = build_stage_funnel_report([ticket], [observation])

    assert report["steps"][0]["first_loss_stage"] == "pass"
    assert report["metrics"]["selection_recall"] == {
        "numerator": 1,
        "denominator": 2,
        "rate": 0.5,
    }


def test_unversioned_separate_arrays_regress_to_legacy_coarse() -> None:
    observation = _grounded_observation()
    observation.pop("lineage_schema_version")
    observation.pop("lineage_attribution")
    observation.pop("lineage_stage_available")
    observation["observed_chunk_ids"] = ["source-a"]

    report = build_stage_funnel_report([_grounded_ticket()], [observation])

    row = report["steps"][0]
    assert row["lineage_mode"] == "legacy_union"
    assert row["first_loss_stage"] == "legacy_lineage"
    assert row["attribution_confidence"] == "coarse"
