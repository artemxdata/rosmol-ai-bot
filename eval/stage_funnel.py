from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SAFE_SCHEMA_VERSION = "stage-funnel-safe-v1"
PIPELINE_LINEAGE_SCHEMA_VERSION = "question-pipeline-provenance-v1"
RELEVANT_GRADE = 2
AUTHORITATIVE_GRADE = 3
RANK_CUTOFFS = (1, 3, 5, 10)

ACTIONS = ("answer", "clarify", "escalate", "scope_note")
ACTION_SET = frozenset(ACTIONS)
ANSWERABILITY_VALUES = frozenset({"full", "partial", "none"})
GENERATION_STATUSES = frozenset({"pass", "partial", "fail"})
VERIFICATION_DECISIONS = frozenset({"pass", "partial", "escalate", "reject"})
LINEAGE_ATTRIBUTIONS = frozenset({"exact", "partial", "legacy_coarse"})
LINEAGE_STAGES = frozenset(
    {"retrieve", "rerank", "source_selection", "citation", "verify"}
)
CLAIM_VERDICTS = frozenset(
    {
        "supported",
        "missing",
        "unsupported",
        "contradicted",
        "off_aspect",
        "unscored",
    }
)
SUPPORTED_CLAIM_VERDICTS = frozenset({"supported"})
GENERATION_REASONS = frozenset(
    {
        "empty_generated_response",
        "generation_failed",
        "insufficient_sources",
        "insufficient_source_coverage",
        "llm_generation_failed",
        "llm_response_contract_failed",
        "llm_response_profile_failed",
        "llm_response_too_long",
        "llm_source_citation_failed",
        "llm_source_coverage_failed",
        "llm_source_fact_binding_failed",
        "no_sources_for_generation",
        "no_sources",
        "partial_source_coverage",
        "passed",
        "source_response_contract_failed",
    }
)
VERIFICATION_REASONS = frozenset(
    {
        "ambiguous_forum",
        "hallucination_detected",
        "insufficient_sources",
        "missing_source_citations",
        "non_yonote_source",
        "partial_source_coverage",
        "passed",
        "unsafe_sensitive_data_request",
        "unknown_source_citation",
        "unsupported_instruction",
        "upstream_escalation",
        "verifier_hallucination",
    }
)

FIRST_LOSS_STAGES = (
    "pass",
    "unscored",
    "label_or_content_gap",
    "legacy_lineage",
    "routing",
    "retrieval",
    "rerank",
    "source_selection",
    "generation_contract",
    "citation",
    "claim_support",
    "verification",
    "final_behavior",
)


@dataclass(frozen=True)
class GoldQrel:
    chunk_id: str
    grade: int
    supports_claim_ids: frozenset[str]


@dataclass(frozen=True)
class GoldClaim:
    claim_id: str
    required: bool
    critical: bool


@dataclass(frozen=True)
class GoldStep:
    ticket_key: str
    ticket_no: int
    step_id: str
    step_no: int
    expected_action: str
    answerability: str
    qrels: tuple[GoldQrel, ...]
    claims: tuple[GoldClaim, ...]


@dataclass(frozen=True)
class OrderedEvidence:
    present: bool
    chunk_ids: tuple[str, ...]


@dataclass(frozen=True)
class Observation:
    ticket_key: str | None
    step_id: str | None
    generic_id: str | None
    observed_action: str | None
    routing_action: str | None
    retrieved: OrderedEvidence
    reranked: OrderedEvidence
    selected: OrderedEvidence
    cited: OrderedEvidence
    verified: OrderedEvidence
    legacy_union: OrderedEvidence
    lineage_attribution: str | None
    generation_status: str | None
    generation_reason: str | None
    verification_decision: str | None
    verification_reason: str | None
    claim_verdicts: dict[str, str]


def load_tickets_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load GoldTicket v1 records without copying their text into diagnostics."""

    records: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"GoldTicket JSONL line {line_no} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"GoldTicket JSONL line {line_no} must be an object")
        records.append(value)
    return records


def load_observations(path: Path) -> list[dict[str, Any]]:
    """Load an array/report object or JSONL observations from a private artifact."""

    raw_text = path.read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"observation JSONL line {line_no} is invalid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"observation JSONL line {line_no} must be an object"
                ) from None
            rows.append(value)
        return rows

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("observations"), list):
        rows = payload["observations"]
    elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
        rows = payload["results"]
    elif isinstance(payload, dict):
        rows = [payload]
    else:
        raise ValueError("observations must be an object, array, report object, or JSONL")
    if not all(isinstance(item, dict) for item in rows):
        raise ValueError("observations must contain only objects")
    return [dict(item) for item in rows]


def build_stage_funnel_report(
    tickets: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Score an existing run without network, model, or raw-text output."""

    gold_steps = _normalize_gold_steps(tickets)
    normalized_observations = [_normalize_observation(item) for item in observations]
    _reject_ambiguous_observations(normalized_observations)

    metric_totals: dict[str, list[float]] = {}
    action_pairs: list[tuple[str, str]] = []
    rows: list[dict[str, Any]] = []
    global_step_no = 0
    for step in gold_steps:
        global_step_no += 1
        observation = _find_observation(step, gold_steps, normalized_observations)
        row = _score_step(step, observation, metric_totals)
        row["case_no"] = global_step_no
        rows.append(row)
        if observation is not None and observation.observed_action is not None:
            action_pairs.append((step.expected_action, observation.observed_action))

    action_report = _action_report(action_pairs)
    metrics = {
        **action_report["metrics"],
        **{
            name: _metric(total[0], total[1])
            for name, total in sorted(metric_totals.items())
        },
    }
    first_loss_counts = Counter(row["first_loss_stage"] for row in rows)
    confidence_counts = Counter(row["attribution_confidence"] for row in rows)
    return {
        "schema_version": SAFE_SCHEMA_VERSION,
        "tickets_total": len(tickets),
        "evaluation_steps_total": len(rows),
        "first_loss_stage_counts": {
            stage: first_loss_counts[stage]
            for stage in FIRST_LOSS_STAGES
            if first_loss_counts[stage]
        },
        "attribution_confidence_counts": dict(sorted(confidence_counts.items())),
        "action_confusion_matrix": action_report["confusion_matrix"],
        "action_per_class": action_report["per_class"],
        "metrics": metrics,
        "steps": rows,
    }


def run_stage_funnel(
    tickets_path: Path,
    observations_path: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    report = build_stage_funnel_report(
        load_tickets_jsonl(tickets_path),
        load_observations(observations_path),
    )
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    return report


def _normalize_gold_steps(tickets: list[dict[str, Any]]) -> list[GoldStep]:
    normalized: list[GoldStep] = []
    ticket_keys: set[str] = set()
    step_keys: set[tuple[str, str]] = set()
    for ticket_no, ticket in enumerate(tickets, start=1):
        if not isinstance(ticket, dict):
            raise ValueError(f"GoldTicket {ticket_no} must be an object")
        ticket_key = _first_identifier(ticket, ("ticket_id_hash", "ticket_id", "id"))
        if ticket_key is None:
            raise ValueError(f"GoldTicket {ticket_no} requires ticket_id_hash")
        if ticket_key in ticket_keys:
            raise ValueError(f"GoldTicket {ticket_no} duplicates a ticket identifier")
        ticket_keys.add(ticket_key)

        evaluation_steps = ticket.get("evaluation_steps")
        if not isinstance(evaluation_steps, list) or not evaluation_steps:
            raise ValueError(f"GoldTicket {ticket_no} requires evaluation_steps")
        for step_no, raw_step in enumerate(evaluation_steps, start=1):
            if not isinstance(raw_step, dict):
                raise ValueError(
                    f"GoldTicket {ticket_no} evaluation step {step_no} must be an object"
                )
            step_id = _first_identifier(raw_step, ("step_id", "id"))
            if step_id is None:
                raise ValueError(
                    f"GoldTicket {ticket_no} evaluation step {step_no} requires step_id"
                )
            composite_key = (ticket_key, step_id)
            if composite_key in step_keys:
                raise ValueError("GoldTicket evaluation step identifiers must be unique")
            step_keys.add(composite_key)

            expected_action = _required_enum(
                raw_step.get("expected_action"),
                ACTION_SET,
                label="expected_action",
            )
            answerability = _required_enum(
                raw_step.get("answerability"),
                ANSWERABILITY_VALUES,
                label="answerability",
            )
            normalized.append(
                GoldStep(
                    ticket_key=ticket_key,
                    ticket_no=ticket_no,
                    step_id=step_id,
                    step_no=step_no,
                    expected_action=expected_action,
                    answerability=answerability,
                    qrels=_normalize_qrels(raw_step.get("qrels") or []),
                    claims=_normalize_claims(
                        raw_step.get("expected_claims") or raw_step.get("claims") or []
                    ),
                )
            )
    return normalized


def _normalize_qrels(value: Any) -> tuple[GoldQrel, ...]:
    if not isinstance(value, list):
        raise ValueError("qrels must be an array")
    qrels: list[GoldQrel] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"qrel {index} must be an object")
        chunk_id = _first_identifier(raw, ("chunk_id", "id"))
        if chunk_id is None:
            raise ValueError(f"qrel {index} requires chunk_id")
        grade = raw.get("grade")
        if type(grade) is not int or not 0 <= grade <= 3:
            raise ValueError(f"qrel {index} grade must be an integer from 0 to 3")
        if chunk_id in seen:
            raise ValueError("qrel chunk identifiers must be unique within an evaluation step")
        seen.add(chunk_id)
        qrels.append(
            GoldQrel(
                chunk_id=chunk_id,
                grade=grade,
                supports_claim_ids=frozenset(
                    _identifier_list(raw.get("supports_claim_ids") or [])
                ),
            )
        )
    return tuple(qrels)


def _normalize_claims(value: Any) -> tuple[GoldClaim, ...]:
    if not isinstance(value, list):
        raise ValueError("expected_claims must be an array")
    claims: list[GoldClaim] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"expected claim {index} must be an object")
        claim_id = _first_identifier(raw, ("claim_id", "id"))
        if claim_id is None:
            raise ValueError(f"expected claim {index} requires claim_id")
        if claim_id in seen:
            raise ValueError("expected claim identifiers must be unique")
        seen.add(claim_id)
        required = raw.get("required", True)
        critical = raw.get("critical", False)
        if type(required) is not bool or type(critical) is not bool:
            raise ValueError("expected claim required/critical flags must be boolean")
        claims.append(GoldClaim(claim_id=claim_id, required=required, critical=critical))
    return tuple(claims)


def _normalize_observation(raw: dict[str, Any]) -> Observation:
    trace_events = raw.get("trace_events") if isinstance(raw.get("trace_events"), list) else []
    lineage_schema_version = str(raw.get("lineage_schema_version") or "").strip()
    lineage_attribution = _optional_status(
        raw.get("lineage_attribution"),
        LINEAGE_ATTRIBUTIONS,
        aliases={"coarse": "legacy_coarse", "legacy": "legacy_coarse"},
        label="lineage_attribution",
    )
    stage_available = _lineage_stage_available(raw.get("lineage_stage_available"))
    retrieved = _ordered_evidence(
        raw,
        ("retrieved_chunk_ids", "retrieved_chunks"),
    )
    reranked = _ordered_evidence(
        raw,
        ("reranked_chunk_ids", "reranked_source_ids", "reranker_scores", "reranked_chunks"),
    )
    selected = _ordered_evidence(
        raw,
        ("selected_source_ids", "selected_chunk_ids"),
    )
    cited = _ordered_evidence(
        raw,
        (
            "ordered_cited_source_ids",
            "cited_source_ids",
            "cited_chunk_ids",
            "cited_sources",
        ),
    )
    verified = _ordered_evidence(
        raw,
        ("verification_source_ids", "verified_source_ids"),
    )
    legacy_union = _ordered_evidence(raw, ("observed_chunk_ids",))

    canonical_lineage = (
        lineage_schema_version == PIPELINE_LINEAGE_SCHEMA_VERSION
        and stage_available is not None
    )
    if canonical_lineage and stage_available["source_selection"] and not selected.present:
        selected = _event_evidence(trace_events, "generate_selection")
    if canonical_lineage:
        assert stage_available is not None
        inferred_attribution = _inferred_lineage_attribution(stage_available)
        if lineage_attribution is not None and lineage_attribution != inferred_attribution:
            raise ValueError(
                "lineage_attribution conflicts with lineage_stage_available"
            )
        lineage_attribution = inferred_attribution
        evidence_by_stage = {
            "retrieve": retrieved,
            "rerank": reranked,
            "source_selection": selected,
            "citation": cited,
            "verify": verified,
        }
        missing_contract = [
            stage
            for stage, available in stage_available.items()
            if available and not evidence_by_stage[stage].present
        ]
        if missing_contract:
            raise ValueError(
                "lineage stage marked available without ordered evidence: "
                + ", ".join(sorted(missing_contract))
            )
        retrieved = _mask_evidence(retrieved, stage_available["retrieve"])
        reranked = _mask_evidence(reranked, stage_available["rerank"])
        selected = _mask_evidence(selected, stage_available["source_selection"])
        cited = _mask_evidence(cited, stage_available["citation"])
        verified = _mask_evidence(verified, stage_available["verify"])
    else:
        if lineage_attribution in {"exact", "partial"}:
            raise ValueError(
                "exact/partial lineage requires the validated pipeline schema and "
                "complete lineage_stage_available contract"
            )
        lineage_attribution = "legacy_coarse"
        retrieved = reranked = selected = cited = verified = OrderedEvidence(False, ())

    generation_status = _optional_status(
        raw.get("generation_contract_status"),
        GENERATION_STATUSES,
        aliases={"passed": "pass", "failed": "fail"},
        label="generation_contract_status",
    )
    generation_reason = _safe_reason(
        raw.get("generation_contract_reason"), GENERATION_REASONS
    )
    if (
        generation_status is None
        and canonical_lineage
        and stage_available is not None
        and stage_available["source_selection"]
    ):
        event_status, event_reason = _event_decision(trace_events, "generate_selection")
        if event_status is None:
            event_status, event_reason = _event_decision(trace_events, "generate_contract")
        generation_status = _optional_status(
            event_status,
            GENERATION_STATUSES,
            aliases={"passed": "pass", "failed": "fail"},
            label="generate_contract event status",
        )
        generation_reason = generation_reason or _safe_reason(
            event_reason, GENERATION_REASONS
        )
    if not canonical_lineage or (
        stage_available is not None and not stage_available["source_selection"]
    ):
        generation_status = None
        generation_reason = None
    if generation_status is None and generation_reason is not None:
        generation_status = "fail"

    verification_decision = _optional_status(
        raw.get("verification_decision"),
        VERIFICATION_DECISIONS,
        aliases={"passed": "pass", "failed": "escalate", "escalated": "escalate"},
        label="verification_decision",
    )
    verification_reason = _safe_reason(raw.get("verification_reason"), VERIFICATION_REASONS)
    if (
        verification_decision is None
        and canonical_lineage
        and stage_available is not None
        and stage_available["verify"]
    ):
        event_status, event_reason = _event_decision(trace_events, "verify_decision")
        verification_decision = _optional_status(
            event_status,
            VERIFICATION_DECISIONS,
            aliases={"passed": "pass", "failed": "escalate", "escalated": "escalate"},
            label="verify_decision event status",
        )
        verification_reason = verification_reason or _safe_reason(
            event_reason, VERIFICATION_REASONS
        )
    if not canonical_lineage or (
        stage_available is not None and not stage_available["verify"]
    ):
        verification_decision = None
        verification_reason = None

    return Observation(
        ticket_key=_first_identifier(raw, ("ticket_id_hash", "ticket_id")),
        step_id=_first_identifier(raw, ("step_id", "eval_case_id")),
        generic_id=_first_identifier(raw, ("id", "case_id")),
        observed_action=_optional_action(
            raw.get("observed_action")
            or raw.get("observed_behavior")
            or raw.get("ticket_outcome")
        ),
        routing_action=_optional_action(
            raw.get("routing_action") or raw.get("analysis_action")
        ),
        retrieved=retrieved,
        reranked=reranked,
        selected=selected,
        cited=cited,
        verified=verified,
        legacy_union=legacy_union,
        lineage_attribution=lineage_attribution,
        generation_status=generation_status,
        generation_reason=generation_reason,
        verification_decision=verification_decision,
        verification_reason=verification_reason,
        claim_verdicts=_normalize_claim_verdicts(raw.get("claim_verdicts")),
    )


def _score_step(
    step: GoldStep,
    observation: Observation | None,
    metric_totals: dict[str, list[float]],
) -> dict[str, Any]:
    relevant_qrels = {qrel.chunk_id for qrel in step.qrels if qrel.grade >= RELEVANT_GRADE}
    required_claims = [claim for claim in step.claims if claim.required]
    label_gap = (
        step.expected_action == "answer"
        and step.answerability in {"full", "partial"}
        and not relevant_qrels
    )

    row: dict[str, Any] = {
        "ticket_no": step.ticket_no,
        "step_no": step.step_no,
        "expected_action": step.expected_action,
        "observed_action": observation.observed_action if observation else None,
        "answerability": step.answerability,
        "lineage_mode": _lineage_mode(observation),
        "attribution_confidence": "unscored",
        "evidence_counts": {
            "qrels": len(step.qrels),
            "relevant_qrels": len(relevant_qrels),
            "required_claims": len(required_claims),
            "retrieved": len(observation.retrieved.chunk_ids) if observation else 0,
            "reranked": len(observation.reranked.chunk_ids) if observation else 0,
            "selected": len(observation.selected.chunk_ids) if observation else 0,
            "cited": len(observation.cited.chunk_ids) if observation else 0,
            "verified": len(observation.verified.chunk_ids) if observation else 0,
        },
        "stages": {
            "routing": None,
            "retrieval": None,
            "rerank": None,
            "source_selection": None,
            "generation_contract": None,
            "citation": None,
            "claim_support": None,
            "verification": None,
            "final_behavior": None,
        },
        "ranking_metrics": {},
        "survival_metrics": {},
        "generation_contract_reason": observation.generation_reason if observation else None,
        "verification_reason": observation.verification_reason if observation else None,
        "claim_verdict_counts": {},
    }

    if observation is not None:
        row["stages"]["routing"] = (
            observation.routing_action == step.expected_action
            if observation.routing_action is not None
            else None
        )
        row["stages"]["final_behavior"] = (
            observation.observed_action == step.expected_action
            if observation.observed_action is not None
            else None
        )

    if label_gap:
        row["first_loss_stage"] = "label_or_content_gap"
        row["attribution_confidence"] = "exact"
        return row
    if observation is None:
        row["first_loss_stage"] = "unscored"
        return row

    if step.expected_action != "answer":
        if row["stages"]["routing"] is False:
            row["first_loss_stage"] = "routing"
            row["attribution_confidence"] = "exact"
        elif row["stages"]["final_behavior"] is False:
            row["first_loss_stage"] = "final_behavior"
            row["attribution_confidence"] = "exact"
        elif row["stages"]["final_behavior"] is True:
            row["first_loss_stage"] = "pass"
            row["attribution_confidence"] = "exact"
        else:
            row["first_loss_stage"] = "unscored"
        return row

    if not any(
        evidence.present
        for evidence in (
            observation.retrieved,
            observation.reranked,
            observation.selected,
            observation.cited,
        )
    ) and observation.legacy_union.present:
        row["first_loss_stage"] = "legacy_lineage"
        row["attribution_confidence"] = "coarse"
        return row

    ranking_specs = (
        ("retrieval", observation.retrieved),
        ("rerank", observation.reranked),
    )
    for stage_name, evidence in ranking_specs:
        ranking = _ranking_metrics(step.qrels, evidence)
        row["ranking_metrics"][stage_name] = ranking
        _add_ranking_totals(metric_totals, stage_name, ranking)

    selection_recall = _relevant_recall_metric(
        relevant_qrels,
        observation.selected,
    )
    citation_recall = _relevant_recall_metric(
        relevant_qrels,
        observation.cited,
    )
    row["ranking_metrics"]["selection_recall"] = selection_recall
    row["ranking_metrics"]["citation_recall"] = citation_recall
    _add_metric_total(metric_totals, "selection_recall", selection_recall)
    _add_metric_total(metric_totals, "citation_recall", citation_recall)

    rerank_survival = _survival_metric(
        relevant_qrels,
        observation.retrieved,
        observation.reranked,
    )
    selection_survival = _survival_metric(
        relevant_qrels,
        observation.reranked,
        observation.selected,
    )
    citation_survival = _survival_metric(
        relevant_qrels,
        observation.selected,
        observation.cited,
    )
    row["survival_metrics"] = {
        "rerank": rerank_survival,
        "selection": selection_survival,
        "citation": citation_survival,
    }
    _add_metric_total(metric_totals, "rerank_survival", rerank_survival)
    _add_metric_total(metric_totals, "selection_survival", selection_survival)
    _add_metric_total(metric_totals, "citation_survival", citation_survival)

    row["stages"]["retrieval"] = _source_stage_status(
        step,
        observation.retrieved,
        min_grade=RELEVANT_GRADE,
    )
    row["stages"]["rerank"] = _source_stage_status(
        step,
        observation.reranked,
        min_grade=RELEVANT_GRADE,
    )
    row["stages"]["source_selection"] = _source_stage_status(
        step,
        observation.selected,
        min_grade=RELEVANT_GRADE,
    )
    row["stages"]["generation_contract"] = _generation_status(step, observation)
    row["stages"]["citation"] = _source_stage_status(
        step,
        observation.cited,
        min_grade=_citation_grade(step),
    )

    claim_status, claim_metric, claim_counts = _claim_status(step, observation)
    row["stages"]["claim_support"] = claim_status
    row["claim_verdict_counts"] = claim_counts
    if claim_metric is not None:
        row["ranking_metrics"]["required_claim_completeness"] = claim_metric
        _add_metric_total(metric_totals, "required_claim_completeness", claim_metric)

    verification_status = _verification_status(step, observation)
    row["stages"]["verification"] = verification_status

    first_loss = _first_loss(row["stages"])
    row["first_loss_stage"] = first_loss
    row["attribution_confidence"] = "unscored" if first_loss == "unscored" else "exact"
    return row


def _first_loss(stages: dict[str, bool | None]) -> str:
    ordered = (
        "routing",
        "retrieval",
        "rerank",
        "source_selection",
        "generation_contract",
        "citation",
        "claim_support",
        "verification",
        "final_behavior",
    )
    mapping = {"source_selection": "source_selection"}
    for stage in ordered:
        value = stages.get(stage)
        if value is None:
            if stage == "routing":
                continue
            return "unscored"
        if value is False:
            return mapping.get(stage, stage)
    return "pass"


def _source_stage_status(
    step: GoldStep,
    evidence: OrderedEvidence,
    *,
    min_grade: int,
) -> bool | None:
    if not evidence.present:
        return None
    eligible = [qrel for qrel in step.qrels if qrel.grade >= min_grade]
    if not eligible and min_grade == AUTHORITATIVE_GRADE:
        eligible = [qrel for qrel in step.qrels if qrel.grade >= RELEVANT_GRADE]
    if not eligible:
        return None

    required_claim_ids = {
        claim.claim_id for claim in step.claims if claim.required
    }
    mapped_claim_ids = {
        claim_id
        for qrel in eligible
        for claim_id in qrel.supports_claim_ids
        if claim_id in required_claim_ids
    }
    if required_claim_ids and mapped_claim_ids:
        if mapped_claim_ids != required_claim_ids:
            return None
        evidence_ids = set(evidence.chunk_ids)
        return all(
            any(
                qrel.chunk_id in evidence_ids and claim_id in qrel.supports_claim_ids
                for qrel in eligible
            )
            for claim_id in required_claim_ids
        )
    eligible_ids = {qrel.chunk_id for qrel in eligible}
    return bool(eligible_ids & set(evidence.chunk_ids))


def _citation_grade(step: GoldStep) -> int:
    required_claim_ids = {claim.claim_id for claim in step.claims if claim.required}
    authoritative_support = {
        claim_id
        for qrel in step.qrels
        if qrel.grade >= AUTHORITATIVE_GRADE
        for claim_id in qrel.supports_claim_ids
    }
    if required_claim_ids and required_claim_ids <= authoritative_support:
        return AUTHORITATIVE_GRADE
    return RELEVANT_GRADE


def _claim_status(
    step: GoldStep,
    observation: Observation,
) -> tuple[bool | None, dict[str, Any] | None, dict[str, int]]:
    required = [claim for claim in step.claims if claim.required]
    if not required:
        return True, None, {}
    verdicts = [observation.claim_verdicts.get(claim.claim_id) for claim in required]
    counts = Counter(verdict for verdict in verdicts if verdict is not None)
    safe_counts = {
        verdict: counts[verdict]
        for verdict in sorted(CLAIM_VERDICTS)
        if counts[verdict]
    }
    if any(verdict in (None, "unscored") for verdict in verdicts):
        return None, None, safe_counts
    supported = sum(verdict in SUPPORTED_CLAIM_VERDICTS for verdict in verdicts)
    metric = _metric(supported, len(required))
    return supported == len(required), metric, safe_counts


def _verification_status(step: GoldStep, observation: Observation) -> bool | None:
    decision = observation.verification_decision
    if decision is None:
        return None
    if decision in {"escalate", "reject"}:
        return False
    if decision == "partial" and step.answerability == "full":
        return False
    return True


def _generation_status(step: GoldStep, observation: Observation) -> bool | None:
    status = observation.generation_status
    if status is None:
        return None
    if status == "fail":
        return False
    if status == "partial" and step.answerability == "full":
        return False
    return True


def _ranking_metrics(
    qrels: tuple[GoldQrel, ...],
    evidence: OrderedEvidence,
) -> dict[str, dict[str, Any]]:
    relevant_ids = {qrel.chunk_id for qrel in qrels if qrel.grade >= RELEVANT_GRADE}
    if not evidence.present or not relevant_ids:
        return {
            **{f"recall_at_{cutoff}": _metric(0, 0) for cutoff in RANK_CUTOFFS},
            "mrr_at_10": _metric(0, 0),
            "ndcg_at_10": _metric(0, 0),
        }

    metrics = {
        f"recall_at_{cutoff}": _metric(
            len(relevant_ids & set(evidence.chunk_ids[:cutoff])),
            len(relevant_ids),
        )
        for cutoff in RANK_CUTOFFS
    }
    first_rank = next(
        (
            rank
            for rank, chunk_id in enumerate(evidence.chunk_ids[:10], start=1)
            if chunk_id in relevant_ids
        ),
        None,
    )
    metrics["mrr_at_10"] = _metric(0.0 if first_rank is None else 1.0 / first_rank, 1)

    grade_by_id = {qrel.chunk_id: qrel.grade for qrel in qrels}
    dcg = sum(
        (2 ** grade_by_id.get(chunk_id, 0) - 1) / math.log2(rank + 1)
        for rank, chunk_id in enumerate(evidence.chunk_ids[:10], start=1)
    )
    ideal_grades = sorted((qrel.grade for qrel in qrels if qrel.grade > 0), reverse=True)[:10]
    idcg = sum(
        (2**grade - 1) / math.log2(rank + 1)
        for rank, grade in enumerate(ideal_grades, start=1)
    )
    metrics["ndcg_at_10"] = _metric(dcg, idcg)
    return metrics


def _relevant_recall_metric(
    relevant_ids: set[str],
    evidence: OrderedEvidence,
) -> dict[str, Any]:
    if not evidence.present or not relevant_ids:
        return _metric(0, 0)
    return _metric(len(relevant_ids & set(evidence.chunk_ids)), len(relevant_ids))


def _survival_metric(
    relevant_ids: set[str],
    before: OrderedEvidence,
    after: OrderedEvidence,
) -> dict[str, Any]:
    if not before.present or not after.present:
        return _metric(0, 0)
    before_relevant = relevant_ids & set(before.chunk_ids)
    if not before_relevant:
        return _metric(0, 0)
    survived = before_relevant & set(after.chunk_ids)
    return _metric(len(survived), len(before_relevant))


def _add_ranking_totals(
    totals: dict[str, list[float]],
    prefix: str,
    metrics: dict[str, dict[str, Any]],
) -> None:
    for name, metric in metrics.items():
        aggregate_name = f"{prefix}_{name}"
        if name == "ndcg_at_10":
            if metric["rate"] is not None:
                _add_numbers(totals, aggregate_name, float(metric["rate"]), 1.0)
            continue
        _add_metric_total(totals, aggregate_name, metric)


def _add_metric_total(
    totals: dict[str, list[float]],
    name: str,
    metric: dict[str, Any],
) -> None:
    if not metric["denominator"]:
        return
    _add_numbers(
        totals,
        name,
        float(metric["numerator"]),
        float(metric["denominator"]),
    )


def _add_numbers(
    totals: dict[str, list[float]],
    name: str,
    numerator: float,
    denominator: float,
) -> None:
    bucket = totals.setdefault(name, [0.0, 0.0])
    bucket[0] += numerator
    bucket[1] += denominator


def _action_report(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    confusion: dict[str, dict[str, int]] = {}
    for expected, observed in pairs:
        row = confusion.setdefault(expected, {})
        row[observed] = row.get(observed, 0) + 1
    safe_confusion = {
        expected: {observed: row[observed] for observed in ACTIONS if row.get(observed)}
        for expected, row in ((action, confusion.get(action, {})) for action in ACTIONS)
        if row
    }

    matches = sum(expected == observed for expected, observed in pairs)
    present_classes = sorted({value for pair in pairs for value in pair})
    per_class: dict[str, Any] = {}
    f1_values: list[float] = []
    for action in present_classes:
        true_positive = sum(
            expected == action and observed == action for expected, observed in pairs
        )
        false_positive = sum(
            expected != action and observed == action for expected, observed in pairs
        )
        false_negative = sum(
            expected == action and observed != action for expected, observed in pairs
        )
        precision = _metric(true_positive, true_positive + false_positive)
        recall = _metric(true_positive, true_positive + false_negative)
        f1 = _metric(2 * true_positive, 2 * true_positive + false_positive + false_negative)
        if f1["rate"] is not None:
            f1_values.append(float(f1["rate"]))
        per_class[action] = {
            "support": sum(expected == action for expected, _observed in pairs),
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    return {
        "confusion_matrix": safe_confusion,
        "per_class": per_class,
        "metrics": {
            "action_accuracy": _metric(matches, len(pairs)),
            "action_macro_f1": _metric(sum(f1_values), len(f1_values)),
        },
    }


def _metric(numerator: float | int, denominator: float | int) -> dict[str, Any]:
    numerator_value = _rounded_number(numerator)
    denominator_value = _rounded_number(denominator)
    return {
        "numerator": numerator_value,
        "denominator": denominator_value,
        "rate": (
            round(float(numerator) / float(denominator), 6)
            if float(denominator) > 0
            else None
        ),
    }


def _rounded_number(value: float | int) -> float | int:
    if isinstance(value, int):
        return value
    if float(value).is_integer():
        return int(value)
    return round(float(value), 6)


def _ordered_evidence(raw: dict[str, Any], fields: tuple[str, ...]) -> OrderedEvidence:
    for field in fields:
        if field not in raw or raw[field] is None:
            continue
        return OrderedEvidence(True, tuple(_chunk_ids(raw[field], label=field)))
    return OrderedEvidence(False, ())


def _lineage_stage_available(value: Any) -> dict[str, bool] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("lineage_stage_available must be an object")
    if set(value) != LINEAGE_STAGES:
        raise ValueError(
            "lineage_stage_available must explicitly contain every lineage stage"
        )
    result: dict[str, bool] = {}
    for key, available in value.items():
        if type(available) is not bool:
            raise ValueError("lineage_stage_available contains an invalid entry")
        result[key] = available
    return result


def _inferred_lineage_attribution(stage_available: dict[str, bool]) -> str:
    if all(stage_available.values()):
        return "exact"
    if any(stage_available.values()):
        return "partial"
    return "legacy_coarse"


def _mask_evidence(evidence: OrderedEvidence, available: bool | None) -> OrderedEvidence:
    if available is False:
        return OrderedEvidence(False, ())
    return evidence


def _event_evidence(events: list[Any], node: str) -> OrderedEvidence:
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("node") != node:
            continue
        metadata = event.get("metadata")
        if (
            not isinstance(metadata, dict)
            or metadata.get("schema_version") != PIPELINE_LINEAGE_SCHEMA_VERSION
        ):
            continue
        evidence = _ordered_evidence(
            metadata,
            ("selected_source_ids", "selected_chunk_ids", "chunk_ids"),
        )
        if evidence.present:
            return evidence
    return OrderedEvidence(False, ())


def _event_decision(events: list[Any], node: str) -> tuple[Any, Any]:
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("node") != node:
            continue
        metadata = event.get("metadata")
        if (
            isinstance(metadata, dict)
            and metadata.get("schema_version") == PIPELINE_LINEAGE_SCHEMA_VERSION
        ):
            return (
                metadata.get("contract_status")
                or metadata.get("status")
                or metadata.get("decision"),
                metadata.get("reason"),
            )
    return None, None


def _chunk_ids(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{label} must be an ordered array")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            chunk_id = item.get("chunk_id") or item.get("id")
            if not chunk_id and isinstance(item.get("metadata"), dict):
                chunk_id = item["metadata"].get("chunk_id")
        else:
            chunk_id = item
        normalized = str(chunk_id or "").strip()
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def _normalize_claim_verdicts(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    pairs: Iterable[tuple[Any, Any]]
    if isinstance(value, dict):
        pairs = value.items()
    elif isinstance(value, list):
        extracted: list[tuple[Any, Any]] = []
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"claim verdict {index} must be an object")
            extracted.append((item.get("claim_id") or item.get("id"), item.get("verdict")))
        pairs = extracted
    else:
        raise ValueError("claim_verdicts must be an object or array")

    normalized: dict[str, str] = {}
    aliases = {
        "present_supported": "supported",
        "pass": "supported",
        "not_present": "missing",
        "not_scored": "unscored",
    }
    for raw_claim_id, raw_verdict in pairs:
        claim_id = str(raw_claim_id or "").strip()
        if not claim_id:
            raise ValueError("claim verdict requires claim_id")
        verdict = str(raw_verdict or "").strip().casefold()
        verdict = aliases.get(verdict, verdict)
        if verdict not in CLAIM_VERDICTS:
            raise ValueError("claim verdict contains an unknown enum value")
        if claim_id in normalized:
            raise ValueError("claim verdict identifiers must be unique")
        normalized[claim_id] = verdict
    return normalized


def _lineage_mode(observation: Observation | None) -> str:
    if observation is None:
        return "missing"
    if observation.lineage_attribution == "legacy_coarse":
        return "legacy_union"
    if observation.lineage_attribution in {"exact", "partial"}:
        return observation.lineage_attribution
    if any(
        evidence.present
        for evidence in (
            observation.retrieved,
            observation.reranked,
            observation.selected,
            observation.cited,
        )
    ):
        return "separate"
    if observation.legacy_union.present:
        return "legacy_union"
    return "missing"


def _find_observation(
    step: GoldStep,
    all_steps: list[GoldStep],
    observations: list[Observation],
) -> Observation | None:
    ticket_steps = sum(candidate.ticket_key == step.ticket_key for candidate in all_steps)
    ranked_matches: list[tuple[int, Observation]] = []
    for observation in observations:
        rank: int | None = None
        if observation.ticket_key == step.ticket_key and observation.step_id == step.step_id:
            rank = 0
        elif (
            observation.step_id == step.step_id
            and observation.ticket_key in (None, step.ticket_key)
        ):
            rank = 1
        elif observation.generic_id == step.step_id:
            rank = 2
        elif ticket_steps == 1 and observation.generic_id == step.ticket_key:
            rank = 3
        elif ticket_steps == 1 and observation.ticket_key == step.ticket_key:
            rank = 4
        if rank is not None:
            ranked_matches.append((rank, observation))
    if not ranked_matches:
        return None
    best_rank = min(rank for rank, _observation in ranked_matches)
    best = [observation for rank, observation in ranked_matches if rank == best_rank]
    if len(best) != 1:
        raise ValueError("observations contain an ambiguous evaluation-step match")
    return best[0]


def _reject_ambiguous_observations(observations: list[Observation]) -> None:
    exact_keys: set[tuple[str, str]] = set()
    for observation in observations:
        if observation.ticket_key is None or observation.step_id is None:
            continue
        key = (observation.ticket_key, observation.step_id)
        if key in exact_keys:
            raise ValueError("observations contain duplicate ticket/step identifiers")
        exact_keys.add(key)


def _required_enum(value: Any, allowed: frozenset[str], *, label: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in allowed:
        raise ValueError(f"{label} contains an unknown enum value")
    return normalized


def _optional_status(
    value: Any,
    allowed: frozenset[str],
    *,
    aliases: dict[str, str],
    label: str,
) -> str | None:
    if value is None or value == "":
        return None
    normalized = str(value).strip().casefold()
    normalized = aliases.get(normalized, normalized)
    if normalized not in allowed:
        raise ValueError(f"{label} contains an unknown enum value")
    return normalized


def _optional_action(value: Any) -> str | None:
    if value is None or value == "":
        return None
    normalized = str(value).strip().casefold()
    normalized = {
        "answered": "answer",
        "clarification": "clarify",
        "escalated": "escalate",
    }.get(normalized, normalized)
    if normalized not in ACTION_SET:
        raise ValueError("observed action contains an unknown enum value")
    return normalized


def _safe_reason(value: Any, allowed: frozenset[str]) -> str | None:
    if value is None or value == "":
        return None
    normalized = str(value).strip().casefold()
    return normalized if normalized in allowed else "other"


def _first_identifier(value: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        normalized = str(value.get(field) or "").strip()
        if normalized:
            return normalized
    return None


def _identifier_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple | set):
        raise ValueError("identifier collection must be an array")
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a safe offline stage-funnel report from GoldTicket observations."
    )
    parser.add_argument("--tickets", required=True, type=Path)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run_stage_funnel(args.tickets, args.observations, args.output)
    summary = {key: value for key, value in report.items() if key != "steps"}
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
