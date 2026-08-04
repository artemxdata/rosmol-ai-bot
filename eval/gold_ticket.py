from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GOLD_TICKET_SCHEMA_VERSION = "gold-ticket.v1"
GOLD_TICKET_MEASUREMENT_UNIT = "full_ticket"

_HASH_PATTERN = r"^[0-9a-f]{12,64}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SAFE_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}$"
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class StrictGoldModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetSplit(StrEnum):
    CALIBRATION = "calibration"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class DialogueRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class AssistantKind(StrEnum):
    BOT = "bot"
    OPERATOR = "operator"
    UNKNOWN = "unknown"


class ExpectedAction(StrEnum):
    ANSWER = "answer"
    CLARIFY = "clarify"
    ESCALATE = "escalate"
    SCOPE_NOTE = "scope_note"


class Answerability(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


class TicketOutcome(StrEnum):
    BOT_RESOLVED_FIRST_TURN = "bot_resolved_first_turn"
    BOT_RESOLVED_MULTI_TURN = "bot_resolved_multi_turn"
    OPERATOR_REQUIRED = "operator_required"
    SCOPE_RESOLVED = "scope_resolved"
    UNRESOLVED = "unresolved"


class ConstraintDimension(StrEnum):
    EVENT = "event"
    AGE = "age"
    SHIFT = "shift"
    ROLE = "role"
    REGION = "region"
    STATUS = "status"
    TIME_SCOPE = "time_scope"


class GoldSourceBinding(StrictGoldModel):
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    normalized_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_record_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    available_at: str = Field(min_length=1, max_length=80)
    source_channel: str = Field(min_length=1, max_length=80)


class GoldKnowledgeSnapshot(StrictGoldModel):
    canonical_seed_sha256: str = Field(pattern=_SHA256_PATTERN)
    published_yonote_chunks: int = Field(ge=1)
    source_type: Literal["yonote"] = "yonote"


class GoldTurn(StrictGoldModel):
    turn_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)
    source_turn_index: int = Field(ge=0)
    role_candidate: DialogueRole
    reviewed_role: Literal[
        DialogueRole.USER,
        DialogueRole.ASSISTANT,
        DialogueRole.SYSTEM,
    ]
    assistant_kind: AssistantKind | None = None
    text_deidentified: str = Field(min_length=1, max_length=4000)
    include_in_replay: bool
    privacy_verdict: Literal["approved"]

    @field_validator("text_deidentified")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip() or _CONTROL_RE.search(value):
            raise ValueError("turn text must be non-empty and contain no control characters")
        return value.strip()

    @model_validator(mode="after")
    def validate_role_shape(self) -> GoldTurn:
        if self.include_in_replay != (self.reviewed_role == DialogueRole.USER):
            raise ValueError("only reviewed user turns may be included in replay")
        if self.reviewed_role != DialogueRole.ASSISTANT and self.assistant_kind is not None:
            raise ValueError("assistant_kind is only valid for assistant turns")
        return self


class GoldEntity(StrictGoldModel):
    entity_type: str = Field(min_length=1, max_length=80)
    canonical_value: str = Field(min_length=1, max_length=300)
    source_turn_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)


class GoldConstraint(StrictGoldModel):
    dimension: ConstraintDimension
    operator: Literal["equals", "range", "contains", "applies"] = "equals"
    value: str = Field(min_length=1, max_length=300)
    applies_to_aspects: tuple[str, ...] = ()
    source_turn_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)

    @field_validator("applies_to_aspects")
    @classmethod
    def validate_aspects(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError("constraint aspect bindings must be unique and non-empty")
        return values


class GoldExpectedClaim(StrictGoldModel):
    claim_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)
    aspect: str = Field(min_length=1, max_length=80)
    predicate: str = Field(min_length=1, max_length=120)
    value_normalized: str = Field(min_length=1, max_length=1000)
    qualifiers: dict[str, str] = Field(default_factory=dict)
    polarity: Literal["positive", "negative"] = "positive"
    modality: Literal["fact", "required", "optional", "allowed", "forbidden"] = "fact"
    required: bool = True
    critical: bool = False

    @field_validator("qualifiers")
    @classmethod
    def validate_qualifiers(cls, values: dict[str, str]) -> dict[str, str]:
        if any(
            not key.strip()
            or not value.strip()
            or len(key) > 80
            or len(value) > 300
            for key, value in values.items()
        ):
            raise ValueError("claim qualifiers must be bounded non-empty strings")
        return values


class GoldSourceSpan(StrictGoldModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_range(self) -> GoldSourceSpan:
        if self.end <= self.start:
            raise ValueError("source span end must be greater than start")
        return self


class GoldQrel(StrictGoldModel):
    chunk_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)
    grade: int = Field(ge=0, le=3)
    supports_claim_ids: tuple[str, ...] = ()
    source_span: GoldSourceSpan | None = None

    @model_validator(mode="after")
    def validate_grade_shape(self) -> GoldQrel:
        if len(set(self.supports_claim_ids)) != len(self.supports_claim_ids):
            raise ValueError("qrel claim links must be unique")
        if self.grade == 0 and (self.supports_claim_ids or self.source_span is not None):
            raise ValueError("hard-negative qrels cannot support claims or source spans")
        if self.grade == 3 and self.source_span is None:
            raise ValueError("grade-3 qrels require an exact source span")
        return self


class GoldEvaluationStep(StrictGoldModel):
    step_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)
    user_turn_ids: tuple[str, ...] = Field(min_length=1)
    history_turn_ids: tuple[str, ...] = ()
    expected_action: ExpectedAction
    expected_escalation_reason: str | None = Field(default=None, max_length=120)
    intents: tuple[str, ...] = Field(min_length=1)
    entities: tuple[GoldEntity, ...] = ()
    requested_aspects: tuple[str, ...] = ()
    constraints: tuple[GoldConstraint, ...] = ()
    answerability: Answerability
    missing_aspects: tuple[str, ...] = ()
    qrels: tuple[GoldQrel, ...] = ()
    expected_claims: tuple[GoldExpectedClaim, ...] = ()
    forbidden_profiles: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_semantics(self) -> GoldEvaluationStep:
        for label, values in (
            ("user_turn_ids", self.user_turn_ids),
            ("history_turn_ids", self.history_turn_ids),
            ("intents", self.intents),
            ("requested_aspects", self.requested_aspects),
            ("missing_aspects", self.missing_aspects),
            ("forbidden_profiles", self.forbidden_profiles),
        ):
            if any(not value.strip() for value in values) or len(set(values)) != len(values):
                raise ValueError(f"{label} must contain unique non-empty values")

        if self.expected_action == ExpectedAction.ESCALATE:
            if not (self.expected_escalation_reason or "").strip():
                raise ValueError("escalation steps require an escalation reason")
        elif self.expected_escalation_reason is not None:
            raise ValueError("non-escalation steps cannot have an escalation reason")

        if self.answerability == Answerability.PARTIAL and not self.missing_aspects:
            raise ValueError("partial answerability requires missing_aspects")
        if self.answerability == Answerability.FULL and self.missing_aspects:
            raise ValueError("full answerability cannot have missing_aspects")
        if self.expected_action == ExpectedAction.ANSWER:
            if self.answerability == Answerability.NONE:
                raise ValueError("answer action cannot be marked unanswerable")
        elif self.expected_claims:
            raise ValueError("non-answer steps cannot contain expected factual claims")

        claim_ids = [claim.claim_id for claim in self.expected_claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("expected claim IDs must be unique within a step")
        qrel_ids = [qrel.chunk_id for qrel in self.qrels]
        if len(set(qrel_ids)) != len(qrel_ids):
            raise ValueError("qrel chunk IDs must be unique within a step")
        known_claim_ids = set(claim_ids)
        for qrel in self.qrels:
            unknown = set(qrel.supports_claim_ids) - known_claim_ids
            if unknown:
                raise ValueError("qrel references an unknown expected claim")

        required_ids = {claim.claim_id for claim in self.expected_claims if claim.required}
        supported_ids = {
            claim_id
            for qrel in self.qrels
            if qrel.grade == 3
            for claim_id in qrel.supports_claim_ids
        }
        if required_ids - supported_ids:
            raise ValueError("every required claim needs grade-3 source support")
        if self.expected_action == ExpectedAction.ANSWER and not any(
            qrel.grade == 3 for qrel in self.qrels
        ):
            raise ValueError("answer steps require at least one grade-3 Yonote qrel")
        return self


class GoldOperatorEvidence(StrictGoldModel):
    available: bool
    behavior_tags: tuple[str, ...] = ()
    used_as_factual_truth: Literal[False] = False


class GoldReviewProvenance(StrictGoldModel):
    status: Literal["human_reviewed", "adjudicated"]
    primary_reviewer_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)
    primary_reviewed_at: datetime
    secondary_reviewer_id: str | None = Field(default=None, pattern=_SAFE_IDENTIFIER_PATTERN)
    secondary_reviewed_at: datetime | None = None
    disagreement: bool = False
    adjudicator_id: str | None = Field(default=None, pattern=_SAFE_IDENTIFIER_PATTERN)
    adjudicated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_review_chain(self) -> GoldReviewProvenance:
        if bool(self.secondary_reviewer_id) != bool(self.secondary_reviewed_at):
            raise ValueError("secondary reviewer and timestamp must be provided together")
        if self.secondary_reviewer_id == self.primary_reviewer_id:
            raise ValueError("primary and secondary reviewers must be distinct")
        adjudication_complete = bool(self.adjudicator_id) and bool(self.adjudicated_at)
        if self.status == "adjudicated" and not adjudication_complete:
            raise ValueError("adjudicated review requires adjudicator and timestamp")
        if self.disagreement and self.status != "adjudicated":
            raise ValueError("review disagreement requires adjudication")
        if not self.disagreement and self.status != "adjudicated" and adjudication_complete:
            raise ValueError("adjudication fields require adjudicated status")
        return self


class GoldPrivacyProvenance(StrictGoldModel):
    status: Literal["approved"]
    scanner: str = Field(min_length=1, max_length=120)
    reviewer_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)
    reviewed_at: datetime
    raw_text_exported: Literal[False] = False


class GoldTicketContentV1(StrictGoldModel):
    schema_version: Literal[GOLD_TICKET_SCHEMA_VERSION] = GOLD_TICKET_SCHEMA_VERSION
    dataset_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)
    ticket_id_hash: str = Field(pattern=_HASH_PATTERN)
    duplicate_component_id: str = Field(pattern=_HASH_PATTERN)
    split: DatasetSplit
    measurement_unit: Literal[GOLD_TICKET_MEASUREMENT_UNIT] = (
        GOLD_TICKET_MEASUREMENT_UNIT
    )
    source_binding: GoldSourceBinding
    turns: tuple[GoldTurn, ...] = Field(min_length=1)
    evaluation_steps: tuple[GoldEvaluationStep, ...] = Field(min_length=1)
    expected_ticket_outcome: TicketOutcome
    operator_evidence: GoldOperatorEvidence
    review_provenance: GoldReviewProvenance
    privacy_provenance: GoldPrivacyProvenance
    knowledge_snapshot: GoldKnowledgeSnapshot

    @model_validator(mode="after")
    def validate_ticket(self) -> GoldTicketContentV1:
        turn_ids = [turn.turn_id for turn in self.turns]
        turn_indexes = [turn.source_turn_index for turn in self.turns]
        if len(set(turn_ids)) != len(turn_ids) or len(set(turn_indexes)) != len(turn_indexes):
            raise ValueError("ticket turns must have unique IDs and source indexes")
        step_ids = [step.step_id for step in self.evaluation_steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("ticket evaluation step IDs must be unique")

        turns_by_id = {turn.turn_id: turn for turn in self.turns}
        for step in self.evaluation_steps:
            referenced = set(step.user_turn_ids) | set(step.history_turn_ids)
            if referenced - set(turns_by_id):
                raise ValueError("evaluation step references an unknown turn")
            if any(
                turns_by_id[turn_id].reviewed_role != DialogueRole.USER
                for turn_id in step.user_turn_ids
            ):
                raise ValueError("evaluation user_turn_ids must reference reviewed user turns")
            if set(step.user_turn_ids) & set(step.history_turn_ids):
                raise ValueError("current and history turn IDs must not overlap")
            aspect_set = set(step.requested_aspects)
            if any(
                constraint.applies_to_aspects
                and not set(constraint.applies_to_aspects).issubset(aspect_set)
                for constraint in step.constraints
            ):
                raise ValueError("constraint refers to an unrequested aspect")

        has_critical_claim = any(
            claim.critical
            for step in self.evaluation_steps
            for claim in step.expected_claims
        )
        if has_critical_claim and self.review_provenance.secondary_reviewer_id is None:
            raise ValueError("critical claims require an independent secondary reviewer")

        actions = {step.expected_action for step in self.evaluation_steps}
        if (
            self.expected_ticket_outcome == TicketOutcome.OPERATOR_REQUIRED
            and ExpectedAction.ESCALATE not in actions
        ):
            raise ValueError("operator_required outcome requires an escalation step")
        if (
            self.expected_ticket_outcome == TicketOutcome.SCOPE_RESOLVED
            and ExpectedAction.SCOPE_NOTE not in actions
        ):
            raise ValueError("scope_resolved outcome requires a scope_note step")
        if (
            self.expected_ticket_outcome == TicketOutcome.BOT_RESOLVED_FIRST_TURN
            and len(self.evaluation_steps) != 1
        ):
            raise ValueError("first-turn resolution must contain one evaluation step")
        if (
            self.expected_ticket_outcome == TicketOutcome.BOT_RESOLVED_MULTI_TURN
            and len(self.evaluation_steps) < 2
        ):
            raise ValueError("multi-turn resolution requires at least two evaluation steps")
        return self


class GoldTicketV1(GoldTicketContentV1):
    record_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_record_hash(self) -> GoldTicketV1:
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"record_sha256"})
        )
        if self.record_sha256 != expected:
            raise ValueError("GoldTicket record_sha256 does not match canonical content")
        return self


def canonical_gold_ticket_sha256(value: GoldTicketContentV1 | Mapping[str, Any]) -> str:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, GoldTicketContentV1)
        else dict(value)
    )
    payload.pop("record_sha256", None)
    content = GoldTicketContentV1.model_validate(payload)
    return _sha256_payload(content.model_dump(mode="json"))


def seal_gold_ticket(value: GoldTicketContentV1 | Mapping[str, Any]) -> GoldTicketV1:
    content = (
        value
        if isinstance(value, GoldTicketContentV1)
        else GoldTicketContentV1.model_validate(value)
    )
    payload = content.model_dump(mode="json")
    payload["record_sha256"] = _sha256_payload(payload)
    return GoldTicketV1.model_validate(payload)


def gold_ticket_to_legacy_ask_cases(ticket: GoldTicketV1) -> list[dict[str, Any]]:
    """Project human gold into the current `/ask` eval contract.

    Holdout export remains owned by the existing sealed holdout tooling. GoldTicket
    projection is intentionally limited to calibration/validation datasets.
    """

    if ticket.split == DatasetSplit.HOLDOUT:
        raise ValueError("sealed holdout projection requires the existing holdout contract")
    if len(ticket.evaluation_steps) != 1:
        raise ValueError(
            "legacy /ask projection cannot preserve ordered multi-turn evaluation steps"
        )
    if any(
        step.history_turn_ids or len(step.user_turn_ids) != 1
        for step in ticket.evaluation_steps
    ):
        raise ValueError(
            "legacy /ask projection cannot preserve GoldTicket turn history"
        )
    turns = {turn.turn_id: turn for turn in ticket.turns}
    cases: list[dict[str, Any]] = []
    for step in ticket.evaluation_steps:
        query = turns[step.user_turn_ids[0]].text_deidentified
        expected_chunks, expected_citations = _legacy_source_requirements(step)
        tags = {
            "gold_ticket:v1",
            f"dataset:{ticket.dataset_id}",
            f"split:{ticket.split.value}",
            f"ticket_outcome:{ticket.expected_ticket_outcome.value}",
            "qrels:legacy-single-authoritative-source",
            *(f"profile:{profile}" for profile in step.requested_aspects),
        }
        case: dict[str, Any] = {
            "id": f"{ticket.ticket_id_hash}::{step.step_id}",
            "ticket_id_hash": ticket.ticket_id_hash,
            "step_id": step.step_id,
            "query": query,
            "user_id": f"gold-{ticket.split.value}-{ticket.ticket_id_hash}",
            "channel": "api",
            "split": ticket.split.value,
            "privacy_class": "private_ticket_derived",
            "label_status": "human_reviewed",
            "requires_human_review": False,
            "expected_behavior": step.expected_action.value,
            "expected_escalated": step.expected_action == ExpectedAction.ESCALATE,
            "expected_escalation_reason": step.expected_escalation_reason,
            "expected_chunk_ids": expected_chunks,
            "expected_cited_chunk_ids": expected_citations,
            "allowed_cited_source_types": ["yonote"],
            "forbidden_response_profiles": sorted(step.forbidden_profiles),
            "tags": sorted(tags),
        }
        if len(step.requested_aspects) == 1:
            case["expected_response_profile"] = step.requested_aspects[0]
        cases.append(case)
    return cases


def _legacy_source_requirements(
    step: GoldEvaluationStep,
) -> tuple[list[str], list[str]]:
    """Return the only graded-qrel shape the legacy all-of scorer can preserve.

    GoldTicket's canonical scorer supports graded alternatives and per-claim source
    mappings.  The legacy `/ask` contract does not, so projection is deliberately
    restricted to one authoritative source that satisfies every required claim.
    Contextual grade-2 qrels remain Gold-only and are not promoted to requirements.
    """

    if step.expected_action != ExpectedAction.ANSWER:
        return [], []

    required_claim_ids = {
        claim.claim_id for claim in step.expected_claims if claim.required
    }
    authoritative = [qrel for qrel in step.qrels if qrel.grade == 3]
    compatible = [
        qrel
        for qrel in authoritative
        if not required_claim_ids
        or required_claim_ids <= set(qrel.supports_claim_ids)
    ]
    if len(authoritative) != 1 or len(compatible) != 1:
        raise ValueError(
            "legacy /ask qrel projection requires one authoritative source "
            "covering every required claim; graded alternatives require the "
            "canonical stage-funnel scorer"
        )
    source_ids = [compatible[0].chunk_id]
    return source_ids, source_ids


def _sha256_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
