from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.build_ticket_product_review import (  # noqa: E402
    _balanced_profile_route_quotas,
    source_case_fingerprint,
)
from src.response_contract import ResponseProfileName  # noqa: E402
from src.security.private_dataset_registry import (  # noqa: E402
    HASH_FIELDS,
    REGISTRY_SCHEMA,
    validate_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DATA_ROOT = (PROJECT_ROOT / "data" / "private").resolve()
VERSIONED_DATA_ROOT = (PROJECT_ROOT / "data").resolve()
DATASET_CONFIG_ROOT = (PROJECT_ROOT / "eval" / "datasets").resolve()

DEFAULT_SOURCE_DIR = Path(
    "data/private/tickets/product_baseline_20260729_roles_v1"
)
DEFAULT_CONFIG = Path("eval/datasets/gold150_sanity_v2.json")
DEFAULT_KB_SEED = Path("data/knowledge_base_seed.json")
DEFAULT_OUTPUT_DIR = Path("data/private/eval/gold150_sanity_v2")

_HASH_RE = re.compile(r"^[0-9a-f]{12,64}$")
_SAFE_DATASET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_METADATA_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:@|/+\-]{0,200}$")
_SAFE_PROFILES = frozenset(profile.value for profile in ResponseProfileName)
_SAFE_ROUTES = frozenset({"answer", "clarify", "escalate"})
_SAFE_ROLE_STATUSES = frozenset(
    {"complete", "partial", "unresolved", "ambiguous", "unknown", "not_available"}
)
_SAFE_MULTITURN_STATUSES = frozenset(
    {"single_turn", "multi_turn", "unknown", "not_available"}
)
_SAFE_SOURCE_SCHEMAS = frozenset({"1.0.0", "product-eval-case.v2"})
_KNOWN_RISK_FLAGS = frozenset(
    {
        "multi_turn",
        "time_sensitive",
        "critical_profile",
        "operator_route",
        "role_review_required",
    }
)
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "query",
        "question",
        "text",
        "text_masked",
        "messages",
        "turns",
        "answer",
        "response",
        "operator_answer",
        "expected_claims",
        "review_note",
    }
)


class GoldSamplingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(
        pattern=r"^(gold150-sampling|gold-ticket-sampling)\.v1$"
    )
    dataset_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{2,63}$")
    dataset_version: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,31}$")
    registry_created_at: str = Field(
        pattern=(
            r"^20\d{2}-(0[1-9]|1[0-2])-([0-2]\d|3[01])T"
            r"([01]\d|2[0-3]):[0-5]\d:[0-5]\d\+00:00$"
        )
    )
    purpose: str = Field(pattern=r"^(sanity_calibration|independent_holdout)$")
    source_split: str = Field(pattern=r"^(calibration|holdout)$")
    measurement_unit: str = Field(pattern=r"^full_ticket$")
    target_total: int = Field(gt=0)
    traffic_target: int = Field(gt=0)
    risk_target: int = Field(gt=0)
    traffic_strata: tuple[str, str]
    risk_slot_quotas: dict[str, int]
    critical_profiles: tuple[str, ...]
    stable_rank_namespace: str = Field(min_length=1, max_length=200)
    operator_answers_used_as_facts: bool
    weak_labels_are_sampling_hints_only: bool
    review_policy: dict[str, Any]

    @model_validator(mode="after")
    def validate_contract(self) -> GoldSamplingConfig:
        expected_split = {
            "sanity_calibration": "calibration",
            "independent_holdout": "holdout",
        }[self.purpose]
        if self.source_split != expected_split:
            raise ValueError("sampling purpose and source split are inconsistent")
        if self.target_total != self.traffic_target + self.risk_target:
            raise ValueError("target_total must equal traffic_target + risk_target")
        if self.traffic_strata != (
            "expected_response_profile",
            "expected_route",
        ):
            raise ValueError("traffic strata must remain profile × route")
        if set(self.risk_slot_quotas) != _KNOWN_RISK_FLAGS:
            raise ValueError("risk_slot_quotas must contain the exact v1 risk flags")
        if any(value < 0 for value in self.risk_slot_quotas.values()):
            raise ValueError("risk quotas must be non-negative")
        if sum(self.risk_slot_quotas.values()) != self.risk_target:
            raise ValueError("risk quotas must sum to risk_target")
        if not self.critical_profiles or len(set(self.critical_profiles)) != len(
            self.critical_profiles
        ):
            raise ValueError("critical profiles must be unique and non-empty")
        if not set(self.critical_profiles).issubset(_SAFE_PROFILES):
            raise ValueError("critical profiles must use the response contract enum")
        if self.operator_answers_used_as_facts:
            raise ValueError("operator answers cannot be factual ground truth")
        if not self.weak_labels_are_sampling_hints_only:
            raise ValueError("weak labels must remain sampling hints only")
        required_review_policy = {
            "primary_human_review_required": True,
            "dual_review_all_critical": True,
            "adjudication_required_on_disagreement": True,
        }
        if any(
            self.review_policy.get(key) is not value
            for key, value in required_review_policy.items()
        ):
            raise ValueError("review policy weakens mandatory human governance")
        audit_fraction = self.review_policy.get(
            "deterministic_secondary_audit_fraction"
        )
        if (
            isinstance(audit_fraction, bool)
            or not isinstance(audit_fraction, (int, float))
            or not 0 < float(audit_fraction) <= 1
        ):
            raise ValueError("secondary audit fraction must be in (0, 1]")
        return self


@dataclass(frozen=True, slots=True)
class GoldSampleCandidate:
    ticket_id_hash: str
    duplicate_component_id: str
    source_case_fingerprint: str
    source_schema_version: str
    available_at: str
    source_channel: str
    intent_hint: str
    entity_class_hint: str
    profile_hint: str
    route_hint: str
    escalation_reason_hint: str
    time_sensitive: bool
    multiturn_status: str
    role_reconstruction_status: str
    risk_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SelectedGoldCandidate:
    candidate: GoldSampleCandidate
    segment: str
    stratum: str
    stable_rank_sha256: str


def build_gold_ticket_dataset(
    *,
    cases_path: Path,
    conversations_path: Path,
    normalized_tickets_path: Path,
    artifact_manifest_path: Path,
    config_path: Path,
    kb_seed_path: Path,
    output_dir: Path,
    exclusion_paths: Iterable[Path] = (),
    overwrite: bool = False,
) -> dict[str, Any]:
    config_file = _versioned_config_path(config_path)
    config_payload = _read_json_object(config_file, label="sampling config")
    config = GoldSamplingConfig.model_validate(config_payload)
    cases_file = _private_input(cases_path, suffix=".jsonl")
    conversations_file = _private_input(conversations_path, suffix=".json")
    normalized_file = _private_input(normalized_tickets_path, suffix=".jsonl")
    artifact_file = _private_input(artifact_manifest_path, suffix=".json")
    exclusions = tuple(_private_input_any(path) for path in exclusion_paths)
    destination = _private_output_dir(output_dir)
    seed_file = _versioned_seed_path(kb_seed_path)

    output_paths = {
        "selection": destination / f"{config.dataset_id}_selection.json",
        "review_queue": destination / f"{config.dataset_id}_review.jsonl",
        "registry": destination / f"{config.dataset_id}_registry.json",
    }
    _validate_output_destination(destination, overwrite=overwrite)

    source_hashes = _validated_artifact_bundle(
        artifact_file,
        required_paths=(cases_file, conversations_file, normalized_file),
    )
    cases = _read_case_hints(cases_file, expected_split=config.source_split)
    conversations = _read_conversation_index(
        conversations_file,
        expected_split=config.source_split,
    )
    required_ticket_ids = set(cases) & set(conversations)
    if len(required_ticket_ids) < config.target_total:
        raise ValueError("insufficient joined ticket candidates for the requested sample")
    normalized_index = _read_normalized_index(
        normalized_file,
        required_ticket_ids=required_ticket_ids,
    )
    excluded_ids, excluded_components = _read_exclusions(exclusions)
    candidates = _build_candidates(
        cases,
        conversations,
        normalized_index,
        config=config,
        excluded_ids=excluded_ids,
        excluded_components=excluded_components,
        source_manifest_sha256=source_hashes["artifact_manifest_sha256"],
    )
    selected = select_gold_ticket_candidates(
        candidates,
        config=config,
        source_manifest_sha256=source_hashes["artifact_manifest_sha256"],
    )
    selected_ids = {item.candidate.ticket_id_hash for item in selected}
    normalized_records = _read_selected_normalized_records(
        normalized_file,
        selected_ids=selected_ids,
    )
    if set(normalized_records) != selected_ids:
        raise ValueError("selected tickets are missing from the normalized source")

    selection_payload = _build_selection_payload(
        selected,
        config=config,
        source_hashes=source_hashes,
        config_sha256=_file_sha256(config_file),
    )
    _assert_metadata_only(selection_payload)
    selection_bytes = _json_bytes(selection_payload)
    review_bytes = _review_jsonl_bytes(
        selected,
        normalized_records=normalized_records,
        config=config,
        source_hashes=source_hashes,
    )
    knowledge_snapshot = _knowledge_snapshot(seed_file)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".staging",
        )
    )
    os.chmod(staging, 0o700)
    staged_paths = {
        name: staging / path.name for name, path in output_paths.items()
    }
    published = False
    try:
        _atomic_write_bytes(
            staged_paths["selection"],
            selection_bytes,
            overwrite=False,
        )
        _atomic_write_bytes(
            staged_paths["review_queue"],
            review_bytes,
            overwrite=False,
        )
        registry_payload = _build_registry_payload(
            selected,
            config=config,
            source_hashes=source_hashes,
            selection_path=staged_paths["selection"],
            review_path=staged_paths["review_queue"],
            output_dir=destination,
            knowledge_snapshot=knowledge_snapshot,
            excluded_ids=excluded_ids,
            excluded_components=excluded_components,
            normalized_records=normalized_records,
        )
        _assert_metadata_only(registry_payload)
        _atomic_write_bytes(
            staged_paths["registry"],
            _json_bytes(registry_payload),
            overwrite=False,
        )
        if destination.exists() or destination.is_symlink():
            raise ValueError("GoldTicket dataset version already exists")
        os.replace(staging, destination)
        published = True
    finally:
        if not published:
            _discard_staging_directory(staging, destination=destination)

    segment_counts = Counter(item.segment for item in selected)
    return {
        "dataset_id": config.dataset_id,
        "selected_total": len(selected),
        "traffic_selected": segment_counts["traffic"],
        "risk_selected": segment_counts["risk"],
        "unique_duplicate_components": len(
            {item.candidate.duplicate_component_id for item in selected}
        ),
        "selection_file": output_paths["selection"].name,
        "review_queue_file": output_paths["review_queue"].name,
        "registry_file": output_paths["registry"].name,
        "weak_labels_are_sampling_hints_only": True,
        "operator_answers_used_as_facts": False,
    }


def select_gold_ticket_candidates(
    candidates: Iterable[GoldSampleCandidate],
    *,
    config: GoldSamplingConfig,
    source_manifest_sha256: str,
) -> list[SelectedGoldCandidate]:
    representatives: dict[str, GoldSampleCandidate] = {}
    for candidate in candidates:
        current = representatives.get(candidate.duplicate_component_id)
        if current is None or _candidate_rank(
            candidate,
            config=config,
            source_manifest_sha256=source_manifest_sha256,
        ) < _candidate_rank(
            current,
            config=config,
            source_manifest_sha256=source_manifest_sha256,
        ):
            representatives[candidate.duplicate_component_id] = candidate
    population = list(representatives.values())
    if len(population) < config.target_total:
        raise ValueError("not enough unique duplicate components for GoldTicket sample")

    grouped: dict[tuple[str, str], list[GoldSampleCandidate]] = defaultdict(list)
    for candidate in population:
        grouped[(candidate.profile_hint, candidate.route_hint)].append(candidate)
    for values in grouped.values():
        values.sort(
            key=lambda item: _candidate_rank(
                item,
                config=config,
                source_manifest_sha256=source_manifest_sha256,
            )
        )
    quotas = _balanced_profile_route_quotas(
        grouped,
        target=config.traffic_target,
        population=len(population),
    )
    traffic: list[SelectedGoldCandidate] = []
    for key in sorted(grouped):
        for candidate in grouped[key][: quotas.get(key, 0)]:
            rank = _stable_rank(
                candidate,
                config=config,
                source_manifest_sha256=source_manifest_sha256,
            )
            traffic.append(
                SelectedGoldCandidate(
                    candidate=candidate,
                    segment="traffic",
                    stratum=f"{key[0]}|{key[1]}",
                    stable_rank_sha256=rank,
                )
            )
    if len(traffic) != config.traffic_target:
        raise ValueError("traffic quota allocation did not produce the exact target")

    traffic_components = {
        item.candidate.duplicate_component_id for item in traffic
    }
    remaining = [
        candidate
        for candidate in population
        if candidate.duplicate_component_id not in traffic_components
    ]
    risk = _select_risk_candidates(
        remaining,
        config=config,
        source_manifest_sha256=source_manifest_sha256,
    )
    selected = sorted(
        traffic,
        key=lambda item: (item.stratum, item.stable_rank_sha256),
    ) + risk
    components = [item.candidate.duplicate_component_id for item in selected]
    if len(selected) != config.target_total or len(set(components)) != len(components):
        raise ValueError("GoldTicket selection is incomplete or contains duplicate components")
    return selected


def _select_risk_candidates(
    candidates: list[GoldSampleCandidate],
    *,
    config: GoldSamplingConfig,
    source_manifest_sha256: str,
) -> list[SelectedGoldCandidate]:
    ordered_candidates = sorted(
        candidates,
        key=lambda item: _candidate_rank(
            item,
            config=config,
            source_manifest_sha256=source_manifest_sha256,
        ),
    )
    slots = [
        (risk_flag, ordinal)
        for risk_flag, quota in config.risk_slot_quotas.items()
        for ordinal in range(quota)
    ]
    candidate_to_slot: dict[str, tuple[str, int]] = {}
    slot_to_candidate: dict[tuple[str, int], GoldSampleCandidate] = {}

    def augment(
        slot: tuple[str, int],
        *,
        visited_components: set[str],
    ) -> bool:
        risk_flag = slot[0]
        for candidate in ordered_candidates:
            component_id = candidate.duplicate_component_id
            if risk_flag not in candidate.risk_flags or component_id in visited_components:
                continue
            visited_components.add(component_id)
            previous_slot = candidate_to_slot.get(component_id)
            if previous_slot is not None and not augment(
                previous_slot,
                visited_components=visited_components,
            ):
                continue
            candidate_to_slot[component_id] = slot
            slot_to_candidate[slot] = candidate
            return True
        return False

    for slot in slots:
        if not augment(slot, visited_components=set()):
            raise ValueError(f"risk quota is infeasible for slot {slot[0]!r}")

    selected: list[SelectedGoldCandidate] = []
    for slot in slots:
        candidate = slot_to_candidate[slot]
        selected.append(
            SelectedGoldCandidate(
                candidate=candidate,
                segment="risk",
                stratum=slot[0],
                stable_rank_sha256=_stable_rank(
                    candidate,
                    config=config,
                    source_manifest_sha256=source_manifest_sha256,
                ),
            )
        )
    return selected


def _build_candidates(
    cases: Mapping[str, dict[str, Any]],
    conversations: Mapping[str, dict[str, Any]],
    normalized_index: Mapping[str, dict[str, Any]],
    *,
    config: GoldSamplingConfig,
    excluded_ids: set[str],
    excluded_components: set[str],
    source_manifest_sha256: str,
) -> list[GoldSampleCandidate]:
    result: list[GoldSampleCandidate] = []
    for ticket_id in sorted(set(cases) & set(conversations) & set(normalized_index)):
        case = cases[ticket_id]
        conversation = conversations[ticket_id]
        normalized = normalized_index[ticket_id]
        component_id = _required_hash(
            case.get("duplicate_component_id")
            or case.get("duplicate_cluster_id"),
            field="duplicate_component_id",
        )
        conversation_component = _required_hash(
            conversation.get("duplicate_component_id"),
            field="conversation duplicate_component_id",
        )
        if component_id != conversation_component:
            raise ValueError("case and conversation duplicate components differ")
        if ticket_id in excluded_ids or component_id in excluded_components:
            continue
        profile = _required_enum(
            case.get("expected_response_profile"),
            field="expected_response_profile",
            allowed=_SAFE_PROFILES,
        )
        route = _required_enum(
            case.get("expected_route"), field="expected_route", allowed=_SAFE_ROUTES
        )
        role_status = _required_enum(
            case.get("role_reconstruction_status"),
            field="role_reconstruction_status",
            allowed=_SAFE_ROLE_STATUSES,
        )
        multiturn_status = _required_enum(
            case.get("multiturn_status"),
            field="multiturn_status",
            allowed=_SAFE_MULTITURN_STATUSES,
        )
        risk_flags: list[str] = []
        if multiturn_status == "multi_turn" or int(conversation.get("turns_count") or 0) > 1:
            risk_flags.append("multi_turn")
        if case.get("time_sensitive") is True:
            risk_flags.append("time_sensitive")
        if profile in set(config.critical_profiles):
            risk_flags.append("critical_profile")
        if route == "escalate":
            risk_flags.append("operator_route")
        if role_status != "complete" or int(normalized["review_required_turns_count"]) > 0:
            risk_flags.append("role_review_required")
        candidate = GoldSampleCandidate(
            ticket_id_hash=ticket_id,
            duplicate_component_id=component_id,
            source_case_fingerprint=source_case_fingerprint(case),
            source_schema_version=_required_enum(
                case.get("schema_version"),
                field="schema_version",
                allowed=_SAFE_SOURCE_SCHEMAS,
            ),
            available_at=str(case.get("available_at") or ""),
            source_channel=_coarse_source_channel(case.get("channel")),
            intent_hint=f"profile:{profile}",
            entity_class_hint=_entity_class_hint(case),
            profile_hint=profile,
            route_hint=route,
            escalation_reason_hint=(
                "present" if case.get("expected_escalation_reason") else "none"
            ),
            time_sensitive=case.get("time_sensitive") is True,
            multiturn_status=multiturn_status,
            role_reconstruction_status=role_status,
            risk_flags=tuple(sorted(risk_flags)),
        )
        _stable_rank(
            candidate,
            config=config,
            source_manifest_sha256=source_manifest_sha256,
        )
        result.append(candidate)
    if len(result) < config.target_total:
        raise ValueError("eligible GoldTicket population is smaller than target_total")
    return result


def _build_selection_payload(
    selected: list[SelectedGoldCandidate],
    *,
    config: GoldSamplingConfig,
    source_hashes: Mapping[str, str],
    config_sha256: str,
) -> dict[str, Any]:
    records = []
    for item in selected:
        candidate = item.candidate
        records.append(
            {
                "case_id_hash": candidate.ticket_id_hash,
                "duplicate_component_id": candidate.duplicate_component_id,
                "source_schema_version": candidate.source_schema_version,
                "source_case_fingerprint": candidate.source_case_fingerprint,
                "sample_segment": item.segment,
                "selection_stratum": item.stratum,
                "stable_rank_sha256": item.stable_rank_sha256,
                "risk_flags": list(candidate.risk_flags),
                "weak_label_hints": {
                    "provenance": "deterministic_query_only_v2",
                    "sampling_only": True,
                    "intent_hint": candidate.intent_hint,
                    "entity_class_hint": candidate.entity_class_hint,
                    "response_profile_hint": candidate.profile_hint,
                    "expected_action_hint": candidate.route_hint,
                    "escalation_reason_hint": candidate.escalation_reason_hint,
                    "time_sensitive_hint": candidate.time_sensitive,
                    "multiturn_status_hint": candidate.multiturn_status,
                    "role_reconstruction_status_hint": (
                        candidate.role_reconstruction_status
                    ),
                },
            }
        )
    payload: dict[str, Any] = {
        "schema_version": "gold-ticket-selection.v1",
        "dataset_id": config.dataset_id,
        "purpose": config.purpose,
        "measurement_unit": config.measurement_unit,
        "source_split": config.source_split,
        "sampling_config_sha256": config_sha256,
        "source_artifacts": dict(sorted(source_hashes.items())),
        "target_total": config.target_total,
        "selected_total": len(records),
        "traffic_selected": sum(item.segment == "traffic" for item in selected),
        "risk_selected": sum(item.segment == "risk" for item in selected),
        "operator_answers_used_as_facts": False,
        "weak_labels_are_sampling_hints_only": True,
        "records": records,
    }
    payload["selection_sha256"] = _canonical_sha256(payload)
    return payload


def _review_jsonl_bytes(
    selected: list[SelectedGoldCandidate],
    *,
    normalized_records: Mapping[str, dict[str, Any]],
    config: GoldSamplingConfig,
    source_hashes: Mapping[str, str],
) -> bytes:
    lines: list[str] = []
    for item in selected:
        candidate = item.candidate
        normalized = normalized_records[candidate.ticket_id_hash]
        turns = []
        for raw_turn in normalized.get("dialogue_turns") or []:
            text = str(raw_turn.get("text_masked") or "").strip()
            if not text:
                continue
            source_index = int(raw_turn.get("turn_index") or 0)
            turns.append(
                {
                    "turn_id": f"t{source_index + 1:03d}",
                    "source_turn_index": source_index,
                    "role_candidate": str(raw_turn.get("role") or "unknown"),
                    "assistant_kind_candidate": raw_turn.get("assistant_kind"),
                    "role_confidence_hint": str(
                        raw_turn.get("role_confidence") or "low"
                    ),
                    "role_reason_hint": str(raw_turn.get("role_reason") or ""),
                    "text_deidentified_for_private_review": text,
                    "privacy_status": "best_effort_private_only",
                    "requires_human_role_review": True,
                    "reviewed_role": None,
                    "include_in_replay": None,
                }
            )
        if not turns:
            raise ValueError("selected normalized ticket has no reviewable turns")
        record = {
            "schema_version": "gold-ticket-review-candidate.v1",
            "dataset_id": config.dataset_id,
            "ticket_id_hash": candidate.ticket_id_hash,
            "duplicate_component_id": candidate.duplicate_component_id,
            "split": config.source_split,
            "measurement_unit": "full_ticket",
            "source_binding": {
                "artifact_manifest_sha256": source_hashes[
                    "artifact_manifest_sha256"
                ],
                "normalized_source_sha256": source_hashes[
                    "normalized_source_sha256"
                ],
                "source_record_fingerprint": candidate.source_case_fingerprint,
                "available_at": candidate.available_at,
                "source_channel": candidate.source_channel,
            },
            "turns": turns,
            "weak_label_hints": {
                "provenance": "deterministic_query_only_v2",
                "sampling_only": True,
                "must_not_be_exported_as_gold": True,
                "intent_hint": candidate.intent_hint,
                "entity_class_hint": candidate.entity_class_hint,
                "response_profile_hint": candidate.profile_hint,
                "expected_action_hint": candidate.route_hint,
                "escalation_reason_hint": candidate.escalation_reason_hint,
                "time_sensitive_hint": candidate.time_sensitive,
                "multiturn_status_hint": candidate.multiturn_status,
                "role_reconstruction_status_hint": (
                    candidate.role_reconstruction_status
                ),
            },
            "sample_assignment": {
                "segment": item.segment,
                "stratum": item.stratum,
                "risk_flags": list(candidate.risk_flags),
                "stable_rank_sha256": item.stable_rank_sha256,
            },
            "operator_evidence": {
                "available": any(
                    turn.get("assistant_kind_candidate") == "operator"
                    for turn in turns
                ),
                "used_as_factual_truth": False,
                "used_for_sampling": False,
            },
            "human_annotation": {
                "status": "pending",
                "evaluation_steps": [],
                "expected_ticket_outcome": None,
                "primary_reviewer_id": None,
                "primary_reviewed_at": None,
                "secondary_reviewer_id": None,
                "secondary_reviewed_at": None,
                "adjudicator_id": None,
                "adjudicated_at": None,
            },
            "privacy_review": {
                "status": "pending",
                "raw_text_exported": False,
            },
        }
        lines.append(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _build_registry_payload(
    selected: list[SelectedGoldCandidate],
    *,
    config: GoldSamplingConfig,
    source_hashes: Mapping[str, str],
    selection_path: Path,
    review_path: Path,
    output_dir: Path,
    knowledge_snapshot: Mapping[str, Any],
    excluded_ids: set[str],
    excluded_components: set[str],
    normalized_records: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    selected_ids = sorted(item.candidate.ticket_id_hash for item in selected)
    relative_output = output_dir.relative_to(PRIVATE_DATA_ROOT)
    contains_operator_answers = any(
        any(
            isinstance(turn, Mapping) and turn.get("assistant_kind") == "operator"
            for turn in record.get("dialogue_turns") or []
        )
        for record in normalized_records.values()
    )
    hashes = {
        "artifact_manifest_sha256": source_hashes["artifact_manifest_sha256"],
        "source_snapshot_sha256": _canonical_sha256(
            dict(sorted(source_hashes.items()))
        ),
        "kb_seed_sha256": knowledge_snapshot["canonical_seed_sha256"],
        "selection_manifest_sha256": _file_sha256(selection_path),
        "review_manifest_sha256": _file_sha256(review_path),
        "case_ids_sha256": hashlib.sha256(
            ("\n".join(selected_ids) + "\n").encode("utf-8")
        ).hexdigest(),
        "duplicate_exclusion_sha256": _canonical_sha256(
            {
                "case_ids": sorted(excluded_ids),
                "duplicate_component_ids": sorted(excluded_components),
            }
        ),
    }
    if set(hashes) != set(HASH_FIELDS):  # pragma: no cover - integration guard.
        raise AssertionError("private registry hash contract changed")
    payload: dict[str, Any] = {
        "dataset_id": config.dataset_id,
        "version": config.dataset_version,
        "relative_root": f"data/private/{relative_output.as_posix()}",
        "kind": "gold_ticket",
        "purpose": "product_quality",
        "privacy_class": "deidentified_private",
        "export_class": "private_only",
        "state": "draft",
        "created_at": config.registry_created_at,
        "lifecycle_updated_at": config.registry_created_at,
        "frozen_at": None,
        "delete_after": None,
        "retention_policy_ref": "unapproved",
        "owner_role": "quality_owner",
        "source_dataset_ids": [],
        "requires_parent_bytes": True,
        "contains_raw_text": False,
        "pii_possible": True,
        "contains_operator_answers": contains_operator_answers,
        "evaluation_role": (
            "holdout"
            if config.purpose == "independent_holdout"
            else "calibration_sanity"
        ),
        "independent_evaluation": config.purpose == "independent_holdout",
        "human_review_status": "pending",
        "cases_total": len(selected),
        "hashes": hashes,
        "supersedes": None,
        "superseded_by": None,
        "hold_reason": "active_review",
        "frozen_payload_sha256": None,
    }
    validated = validate_registry(
        {
            "schema": REGISTRY_SCHEMA,
            "updated_at": config.registry_created_at,
            "datasets": [payload],
        }
    )
    return validated["datasets"][0]


def _read_case_hints(path: Path, *, expected_split: str) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for line_number, payload in _read_jsonl(path):
        if payload.get("split") != expected_split:
            continue
        for flag in ("operator_answer_included", "operator_answer_used_as_fact"):
            if payload.get(flag) is not False:
                raise ValueError(
                    f"source case line {line_number} has unsafe {flag}"
                )
        ticket_id = _required_hash(
            payload.get("ticket_id_hash"),
            field="ticket_id_hash",
        )
        if ticket_id in cases:
            raise ValueError("source cases contain duplicate ticket IDs")
        source_case_fingerprint(payload)
        cases[ticket_id] = payload
    if not cases:
        raise ValueError("source case split is empty")
    return cases


def _read_conversation_index(
    path: Path,
    *,
    expected_split: str,
) -> dict[str, dict[str, Any]]:
    payload = _read_json(path, label="conversation source")
    if not isinstance(payload, list):
        raise ValueError("conversation source must be a JSON array")
    conversations: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict) or item.get("split") != expected_split:
            continue
        if item.get("operator_answer_included") is not False or item.get(
            "operator_answer_used_as_fact"
        ) is not False:
            raise ValueError("conversation source has unsafe operator-answer flags")
        ticket_id = _required_hash(
            item.get("ticket_id_hash"),
            field="conversation ticket_id_hash",
        )
        if ticket_id in conversations:
            raise ValueError("conversation source contains duplicate ticket IDs")
        conversations[ticket_id] = item
    if not conversations:
        raise ValueError("conversation source split is empty")
    return conversations


def _read_normalized_index(
    path: Path,
    *,
    required_ticket_ids: set[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for _, payload in _read_jsonl(path):
        ticket_id = str(payload.get("ticket_hash") or "").strip()
        if ticket_id not in required_ticket_ids:
            continue
        if ticket_id in result:
            raise ValueError("normalized source contains duplicate ticket IDs")
        turns = payload.get("dialogue_turns")
        if not isinstance(turns, list) or not any(
            isinstance(turn, dict) and str(turn.get("text_masked") or "").strip()
            for turn in turns
        ):
            continue
        result[ticket_id] = {
            "review_required_turns_count": int(
                payload.get("review_required_turns_count") or 0
            )
        }
    return result


def _read_selected_normalized_records(
    path: Path,
    *,
    selected_ids: set[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for _, payload in _read_jsonl(path):
        ticket_id = str(payload.get("ticket_hash") or "").strip()
        if ticket_id not in selected_ids:
            continue
        if ticket_id in result:
            raise ValueError("normalized source contains duplicate selected ticket")
        result[ticket_id] = payload
    return result


def _read_exclusions(paths: Iterable[Path]) -> tuple[set[str], set[str]]:
    case_ids: set[str] = set()
    component_ids: set[str] = set()
    for path in paths:
        if path.suffix.casefold() == ".csv":
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as file:
                    rows = list(csv.DictReader(file))
            except (OSError, UnicodeDecodeError, csv.Error) as exc:
                raise ValueError("could not read exclusion CSV") from exc
        else:
            payload = _read_json(path, label="exclusion manifest")
            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict):
                rows = payload.get("records") or []
            else:
                raise ValueError("exclusion manifest must contain records")
        if not isinstance(rows, list):
            raise ValueError("exclusion records must be a list")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("exclusion record must be an object")
            case_id = str(
                row.get("case_id_hash") or row.get("ticket_id_hash") or ""
            ).strip()
            component_id = str(
                row.get("duplicate_component_id")
                or row.get("duplicate_cluster_id")
                or ""
            ).strip()
            if case_id:
                case_ids.add(_required_hash(case_id, field="excluded case ID"))
            if component_id:
                component_ids.add(
                    _required_hash(component_id, field="excluded component ID")
                )
    return case_ids, component_ids


def _validated_artifact_bundle(
    manifest_path: Path,
    *,
    required_paths: tuple[Path, ...],
) -> dict[str, str]:
    manifest = _read_json_object(manifest_path, label="artifact manifest")
    if manifest.get("complete") is not True:
        raise ValueError("artifact manifest is not complete")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("artifact manifest has no artifacts")
    by_relative: dict[str, dict[str, Any]] = {}
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError("artifact manifest item must be an object")
        relative = str(item.get("path") or "").replace("\\", "/")
        if not relative or relative in by_relative:
            raise ValueError("artifact manifest has invalid or duplicate paths")
        by_relative[relative] = item
    result = {"artifact_manifest_sha256": _file_sha256(manifest_path)}
    labels = (
        "cases_source_sha256",
        "conversations_source_sha256",
        "normalized_source_sha256",
    )
    for label, path in zip(labels, required_paths, strict=True):
        try:
            relative = path.relative_to(manifest_path.parent).as_posix()
        except ValueError as exc:
            raise ValueError("artifact input is outside its manifest directory") from exc
        evidence = by_relative.get(relative)
        if not isinstance(evidence, dict):
            raise ValueError("required source is absent from artifact manifest")
        digest = _file_sha256(path)
        if evidence.get("sha256") != digest or evidence.get("size_bytes") != path.stat().st_size:
            raise ValueError("required source differs from artifact manifest evidence")
        result[label] = digest
    return result


def _knowledge_snapshot(path: Path) -> dict[str, Any]:
    payload = _read_json(path, label="knowledge seed")
    if not isinstance(payload, list) or not payload:
        raise ValueError("knowledge seed must be a non-empty array")
    published_yonote = sum(
        isinstance(item, dict)
        and item.get("status") == "published"
        and item.get("source_type") == "yonote"
        for item in payload
    )
    if published_yonote <= 0:
        raise ValueError("knowledge seed has no published Yonote chunks")
    return {
        "seed_file_sha256": _file_sha256(path),
        "canonical_seed_sha256": _canonical_sha256(payload),
        "hash_canonicalization": "json_sort_keys_compact_utf8_v1",
        "published_yonote_chunks": published_yonote,
        "source_type": "yonote",
    }


def _entity_class_hint(case: Mapping[str, Any]) -> str:
    has_entity = bool(str(case.get("entity") or "").strip())
    return "named" if has_entity else "unspecified"


def _coarse_source_channel(value: Any) -> str:
    channel = str(value or "").strip().casefold()
    aliases = {
        "api": "api",
        "hde": "hde",
        "vk": "vk",
        "web": "web",
        "telegram": "telegram",
        "whatsapp": "whatsapp",
        "вконтакте": "vk",
        "вк умный бот": "vk",
        "есз текстовая линия": "hde",
        "max бот": "max",
    }
    return aliases.get(channel, "other")


def _candidate_rank(
    candidate: GoldSampleCandidate,
    *,
    config: GoldSamplingConfig,
    source_manifest_sha256: str,
) -> tuple[str, str, str]:
    return (
        _stable_rank(
            candidate,
            config=config,
            source_manifest_sha256=source_manifest_sha256,
        ),
        candidate.duplicate_component_id,
        candidate.ticket_id_hash,
    )


def _stable_rank(
    candidate: GoldSampleCandidate,
    *,
    config: GoldSamplingConfig,
    source_manifest_sha256: str,
) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", source_manifest_sha256):
        raise ValueError("source manifest SHA-256 is invalid")
    value = "\0".join(
        (
            config.stable_rank_namespace,
            config.dataset_id,
            source_manifest_sha256,
            candidate.duplicate_component_id,
            candidate.ticket_id_hash,
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _assert_metadata_only(payload: Any) -> None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).casefold() in _FORBIDDEN_METADATA_KEYS:
                raise ValueError("metadata artifact contains a text-bearing field")
            _assert_metadata_only(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_metadata_only(item)
    elif isinstance(payload, str) and _SAFE_METADATA_VALUE_RE.fullmatch(payload) is None:
        raise ValueError("metadata artifact contains a non-governed string value")


def _private_input(path: Path, *, suffix: str) -> Path:
    _reject_link_components(path, root=PRIVATE_DATA_ROOT, label="private source")
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("private source must stay under data/private")
    if resolved.suffix.casefold() != suffix or not resolved.is_file():
        raise ValueError(f"private source must be an existing {suffix} file")
    _ensure_regular_single_link(resolved, label="private source")
    return resolved


def _private_input_any(path: Path) -> Path:
    _reject_link_components(path, root=PRIVATE_DATA_ROOT, label="exclusion source")
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("exclusion source must stay under data/private")
    if resolved.suffix.casefold() not in {".json", ".csv"} or not resolved.is_file():
        raise ValueError("exclusion source must be an existing JSON or CSV file")
    _ensure_regular_single_link(resolved, label="exclusion source")
    return resolved


def _private_output_dir(path: Path) -> Path:
    _reject_link_components(path, root=PRIVATE_DATA_ROOT, label="GoldTicket output")
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("GoldTicket output must stay under data/private")
    relative_parts = resolved.relative_to(PRIVATE_DATA_ROOT).parts
    if len(relative_parts) < 2:
        raise ValueError("GoldTicket output must use a dedicated version directory")
    if any(_SAFE_DATASET_RE.fullmatch(part) is None for part in relative_parts):
        raise ValueError("GoldTicket output path must contain only governed safe labels")
    return resolved


def _versioned_config_path(path: Path) -> Path:
    _reject_link_components(path, root=DATASET_CONFIG_ROOT, label="sampling config")
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(DATASET_CONFIG_ROOT):
        raise ValueError("sampling config must stay under eval/datasets")
    if resolved.suffix.casefold() != ".json" or not resolved.is_file():
        raise ValueError("sampling config must be an existing JSON file")
    _ensure_regular_single_link(resolved, label="sampling config")
    return resolved


def _versioned_seed_path(path: Path) -> Path:
    _reject_link_components(path, root=VERSIONED_DATA_ROOT, label="knowledge seed")
    resolved = path.expanduser().resolve()
    if (
        not resolved.is_relative_to(VERSIONED_DATA_ROOT)
        or resolved.is_relative_to(PRIVATE_DATA_ROOT)
        or resolved.suffix.casefold() != ".json"
        or not resolved.is_file()
    ):
        raise ValueError("knowledge seed must be a versioned JSON file under data/")
    _ensure_regular_single_link(resolved, label="knowledge seed")
    return resolved


def _validate_output_destination(destination: Path, *, overwrite: bool) -> None:
    if overwrite:
        raise ValueError(
            "unsafe overwrite is disabled; publish a new immutable dataset version"
        )
    if destination.exists() or destination.is_symlink():
        raise ValueError("GoldTicket dataset version already exists")


def _discard_staging_directory(staging: Path, *, destination: Path) -> None:
    expected_prefix = f".{destination.name}."
    if (
        staging.parent != destination.parent
        or not staging.name.startswith(expected_prefix)
        or not staging.name.endswith(".staging")
    ):
        raise RuntimeError("refusing to clean an unexpected staging directory")
    if not staging.exists():
        return
    if _is_link_or_reparse_point(staging) or not staging.is_dir():
        raise RuntimeError("refusing to clean an unsafe staging path")
    for child in staging.iterdir():
        if (
            _is_link_or_reparse_point(child)
            or not child.is_file()
            or child.stat().st_nlink != 1
        ):
            raise RuntimeError("refusing to clean an unsafe staging artifact")
        child.unlink()
    staging.rmdir()


def _reject_link_components(path: Path, *, root: Path, label: str) -> None:
    root_absolute = Path(os.path.abspath(root.expanduser()))
    candidate = Path(os.path.abspath(path.expanduser()))
    if not candidate.is_relative_to(root_absolute):
        raise ValueError(
            f"{label} must stay under its governed root (data/private for private artifacts)"
        )
    current = root_absolute
    if _is_link_or_reparse_point(current):
        raise ValueError(f"{label} root must not be a link")
    for part in candidate.relative_to(root_absolute).parts:
        current /= part
        if current.exists() and _is_link_or_reparse_point(current):
            raise ValueError(f"{label} must not traverse links")


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(int(getattr(info, "st_file_attributes", 0)) & 0x400)


def _ensure_regular_single_link(path: Path, *, label: str) -> None:
    if _is_link_or_reparse_point(path) or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    if path.stat().st_nlink != 1:
        raise ValueError(f"{label} must not be a hardlink")


def _required_hash(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not _HASH_RE.fullmatch(text):
        raise ValueError(f"invalid {field}")
    return text


def _required_enum(value: Any, *, field: str, allowed: frozenset[str]) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise ValueError(f"invalid {field}; expected a governed enum value")
    return text


def _read_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        file = path.open("r", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError("could not read JSONL source") from exc
    with file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"invalid JSONL object at line {line_number}") from None
            if not isinstance(payload, dict):
                raise ValueError(f"JSONL line {line_number} must be an object")
            yield line_number, payload


def _read_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read valid {label}") from exc


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    payload = _read_json(path, label=label)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, payload: bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        if path.exists():
            if not overwrite:
                raise ValueError("GoldTicket output already exists")
            if path.is_symlink() or path.stat().st_nlink != 1:
                raise ValueError("GoldTicket output alias is unsafe")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a deterministic private GoldTicket review sample."
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_SOURCE_DIR / "product_calibration_cases.jsonl",
    )
    parser.add_argument(
        "--conversations",
        type=Path,
        default=DEFAULT_SOURCE_DIR / "product_calibration_conversations.json",
    )
    parser.add_argument(
        "--normalized-tickets",
        type=Path,
        default=DEFAULT_SOURCE_DIR / "tickets_normalized.jsonl",
    )
    parser.add_argument(
        "--artifact-manifest",
        type=Path,
        default=DEFAULT_SOURCE_DIR / "artifact_manifest.json",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--kb-seed", type=Path, default=DEFAULT_KB_SEED)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--exclude-selection", type=Path, action="append", default=[])
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Rejected by design; publish a new immutable dataset version instead.",
    )
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    result = build_gold_ticket_dataset(
        cases_path=args.cases,
        conversations_path=args.conversations,
        normalized_tickets_path=args.normalized_tickets,
        artifact_manifest_path=args.artifact_manifest,
        config_path=args.config,
        kb_seed_path=args.kb_seed,
        output_dir=args.output_dir,
        exclusion_paths=args.exclude_selection,
        overwrite=args.overwrite,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
