from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from eval.gold_ticket import (
    GoldTicketContentV1,
    GoldTicketV1,
    canonical_gold_ticket_sha256,
    gold_ticket_to_legacy_ask_cases,
    seal_gold_ticket,
)


def _content_payload() -> dict[str, object]:
    return {
        "schema_version": "gold-ticket.v1",
        "dataset_id": "gold150_sanity_v1",
        "ticket_id_hash": "a" * 24,
        "duplicate_component_id": "b" * 12,
        "split": "calibration",
        "measurement_unit": "full_ticket",
        "source_binding": {
            "artifact_manifest_sha256": "1" * 64,
            "normalized_source_sha256": "2" * 64,
            "source_record_fingerprint": "3" * 64,
            "available_at": "2026-06-01T00:00:00Z",
            "source_channel": "hde",
        },
        "turns": [
            {
                "turn_id": "t001",
                "source_turn_index": 0,
                "role_candidate": "user",
                "reviewed_role": "user",
                "assistant_kind": None,
                "text_deidentified": "Когда проходит выбранное мероприятие?",
                "include_in_replay": True,
                "privacy_verdict": "approved",
            }
        ],
        "evaluation_steps": [
            {
                "step_id": "s001",
                "user_turn_ids": ["t001"],
                "history_turn_ids": [],
                "expected_action": "answer",
                "expected_escalation_reason": None,
                "intents": ["forums.dates"],
                "entities": [
                    {
                        "entity_type": "event",
                        "canonical_value": "event-a",
                        "source_turn_id": "t001",
                    }
                ],
                "requested_aspects": ["dates"],
                "constraints": [],
                "answerability": "full",
                "missing_aspects": [],
                "qrels": [
                    {
                        "chunk_id": "yonote-date",
                        "grade": 3,
                        "supports_claim_ids": ["c001"],
                        "source_span": {
                            "start": 10,
                            "end": 30,
                            "sha256": "4" * 64,
                        },
                    },
                    {
                        "chunk_id": "yonote-context",
                        "grade": 2,
                        "supports_claim_ids": [],
                        "source_span": None,
                    },
                ],
                "expected_claims": [
                    {
                        "claim_id": "c001",
                        "aspect": "dates",
                        "predicate": "event_date",
                        "value_normalized": "approved event date",
                        "qualifiers": {},
                        "polarity": "positive",
                        "modality": "fact",
                        "required": True,
                        "critical": False,
                    }
                ],
                "forbidden_profiles": ["travel"],
            }
        ],
        "expected_ticket_outcome": "bot_resolved_first_turn",
        "operator_evidence": {
            "available": True,
            "behavior_tags": ["answer"],
            "used_as_factual_truth": False,
        },
        "review_provenance": {
            "status": "human_reviewed",
            "primary_reviewer_id": "reviewer-a",
            "primary_reviewed_at": "2026-08-04T00:00:00Z",
            "secondary_reviewer_id": None,
            "secondary_reviewed_at": None,
            "disagreement": False,
            "adjudicator_id": None,
            "adjudicated_at": None,
        },
        "privacy_provenance": {
            "status": "approved",
            "scanner": "pii-masker-v1",
            "reviewer_id": "privacy-a",
            "reviewed_at": "2026-08-04T00:00:00Z",
            "raw_text_exported": False,
        },
        "knowledge_snapshot": {
            "canonical_seed_sha256": "5" * 64,
            "published_yonote_chunks": 10,
            "source_type": "yonote",
        },
    }


def test_gold_ticket_seal_is_canonical_and_round_trips() -> None:
    payload = _content_payload()

    ticket = seal_gold_ticket(payload)
    reparsed = GoldTicketV1.model_validate(ticket.model_dump(mode="json"))

    assert reparsed == ticket
    assert ticket.record_sha256 == canonical_gold_ticket_sha256(payload)
    assert seal_gold_ticket(deepcopy(payload)).record_sha256 == ticket.record_sha256


def test_gold_ticket_schema_forbids_unknown_fields() -> None:
    payload = _content_payload()
    payload["unexpected"] = True

    with pytest.raises(ValidationError, match="unexpected"):
        GoldTicketContentV1.model_validate(payload)


def test_gold_ticket_requires_grade_three_support_for_each_required_claim() -> None:
    payload = _content_payload()
    step = payload["evaluation_steps"][0]  # type: ignore[index]
    step["qrels"][0]["supports_claim_ids"] = []  # type: ignore[index]

    with pytest.raises(ValidationError, match="required claim"):
        GoldTicketContentV1.model_validate(payload)


def test_gold_ticket_rejects_qrel_link_to_unknown_claim() -> None:
    payload = _content_payload()
    step = payload["evaluation_steps"][0]  # type: ignore[index]
    step["qrels"][0]["supports_claim_ids"] = ["unknown"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="unknown expected claim"):
        GoldTicketContentV1.model_validate(payload)


def test_gold_ticket_critical_claim_requires_independent_second_review() -> None:
    payload = _content_payload()
    step = payload["evaluation_steps"][0]  # type: ignore[index]
    step["expected_claims"][0]["critical"] = True  # type: ignore[index]

    with pytest.raises(ValidationError, match="secondary reviewer"):
        GoldTicketContentV1.model_validate(payload)

    review = payload["review_provenance"]  # type: ignore[assignment]
    review["secondary_reviewer_id"] = "reviewer-b"  # type: ignore[index]
    review["secondary_reviewed_at"] = "2026-08-04T01:00:00Z"  # type: ignore[index]
    assert seal_gold_ticket(payload).review_provenance.secondary_reviewer_id == "reviewer-b"


def test_gold_ticket_record_hash_detects_mutation() -> None:
    ticket = seal_gold_ticket(_content_payload())
    payload = ticket.model_dump(mode="json")
    payload["expected_ticket_outcome"] = "unresolved"

    with pytest.raises(ValidationError, match="record_sha256"):
        GoldTicketV1.model_validate(payload)


def test_gold_ticket_projects_to_existing_ask_contract() -> None:
    ticket = seal_gold_ticket(_content_payload())

    cases = gold_ticket_to_legacy_ask_cases(ticket)

    assert len(cases) == 1
    assert cases[0]["ticket_id_hash"] == ticket.ticket_id_hash
    assert cases[0]["step_id"] == "s001"
    assert cases[0]["expected_behavior"] == "answer"
    assert cases[0]["expected_response_profile"] == "dates"
    assert cases[0]["expected_chunk_ids"] == ["yonote-date"]
    assert cases[0]["expected_cited_chunk_ids"] == ["yonote-date"]
    assert "qrels:legacy-single-authoritative-source" in cases[0]["tags"]
    assert cases[0]["label_status"] == "human_reviewed"
    assert cases[0]["requires_human_review"] is False


def test_multi_profile_projection_does_not_invent_single_routing_profile() -> None:
    payload = _content_payload()
    step = payload["evaluation_steps"][0]  # type: ignore[index]
    step["requested_aspects"] = ["dates", "program"]  # type: ignore[index]
    ticket = seal_gold_ticket(payload)

    case = gold_ticket_to_legacy_ask_cases(ticket)[0]

    assert "expected_response_profile" not in case
    assert "profile:dates" in case["tags"]
    assert "profile:program" in case["tags"]


def test_holdout_projection_remains_owned_by_sealed_holdout_tooling() -> None:
    payload = _content_payload()
    payload["split"] = "holdout"
    ticket = seal_gold_ticket(payload)

    with pytest.raises(ValueError, match="sealed holdout"):
        gold_ticket_to_legacy_ask_cases(ticket)


def test_legacy_projection_rejects_ordered_multi_turn_ticket() -> None:
    payload = _content_payload()
    payload["expected_ticket_outcome"] = "bot_resolved_multi_turn"
    first_step = deepcopy(payload["evaluation_steps"][0])  # type: ignore[index]
    second_step = deepcopy(first_step)
    second_step["step_id"] = "s002"  # type: ignore[index]
    second_step["history_turn_ids"] = ["t001"]  # type: ignore[index]
    second_step["user_turn_ids"] = ["t002"]  # type: ignore[index]
    payload["turns"].append(  # type: ignore[union-attr]
        {
            **deepcopy(payload["turns"][0]),  # type: ignore[index]
            "turn_id": "t002",
            "source_turn_index": 1,
            "text_deidentified": "РЈС‚РѕС‡РЅРµРЅРёРµ РїРѕСЃР»Рµ РїРµСЂРІРѕРіРѕ РѕС‚РІРµС‚Р°",
        }
    )
    payload["evaluation_steps"] = [first_step, second_step]

    ticket = seal_gold_ticket(payload)

    with pytest.raises(ValueError, match="ordered multi-turn"):
        gold_ticket_to_legacy_ask_cases(ticket)


def test_legacy_projection_rejects_alternative_authoritative_qrels() -> None:
    payload = _content_payload()
    step = payload["evaluation_steps"][0]  # type: ignore[index]
    step["qrels"].append(  # type: ignore[index]
        {
            "chunk_id": "yonote-date-alternative",
            "grade": 3,
            "supports_claim_ids": ["c001"],
            "source_span": {"start": 40, "end": 60, "sha256": "5" * 64},
        }
    )
    ticket = seal_gold_ticket(payload)

    with pytest.raises(ValueError, match="graded alternatives"):
        gold_ticket_to_legacy_ask_cases(ticket)
