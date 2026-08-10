from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from scripts.analyze_ticket_dataset import private_id_hash
from src.graph.nodes.analyze import (
    GRATITUDE_PHRASES,
    _is_greeting_message,
)
from src.kb.source_extractors import read_xlsx_sheets
from src.security.pii_masker import PIIMasker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DATA_ROOT = (PROJECT_ROOT / "data" / "private").resolve()
DEFAULT_SOURCE_PATH = PRIVATE_DATA_ROOT / "july_vk_max_tickets.jsonl"

EXPECTED_SOURCE_SHA256 = (
    "bc669899e49638c6d196c3e552142372adfc73f4fce5b972f4350d6ab4252dd1"
)
EXPECTED_SOURCE_ROWS = 852
EXPECTED_CHANNEL_COUNTS = {"vk": 628, "max": 224}
EXPECTED_SOCIAL_ONLY = 119
EXPECTED_SOCIAL_ONLY_CLOSED = 118
EXPECTED_ELIGIBLE = 733
EXPECTED_OPAQUE_SELECTED = 12
EXPECTED_OPAQUE_TERMINAL = 8
EXPECTED_STRATUM_COUNTS = {
    "vk/forum": 176,
    "vk/no_forum": 374,
    "max/forum": 69,
    "max/no_forum": 114,
}
PHASE0_STRATUM_QUOTAS = {
    "vk/forum": 11,
    "vk/no_forum": 11,
    "max/forum": 4,
    "max/no_forum": 4,
}
PHASE0_SAMPLE_SIZE = 30
PHASE0_SELECTION_SEED = "20260804"
EXPECTED_ORDERED_SELECTION_SHA256 = (
    "4127a5ec72a6a5166b6c1a545fc7dfacebb73452dd6d9fe35816d03f36016a33"
)
EXPECTED_PHASE0_CASES_FILE_SHA256 = (
    "aff198bbc98d07894a3e1676e3457891e3a38f674315051505b681641fe9d02d"
)
EXPECTED_OWNER_NO_CONTINUATION_IDS = 167
PHASE0_APPROVAL_ID = "RAG-PHASE0-30-20260805"
PHASE0_COST_CAP_RUB = 200.0

SOCIAL_CONTRACT_VERSION = "social_only_v1"
FIRST_CONTENT_CONTRACT_VERSION = "earliest_raw_non_social_v1"
OWNER_JOIN_CONTRACT_VERSION = "owner_exact_ticket_id_join_v1"
SELECTION_CONTRACT_VERSION = "fixed_strata_sha256_v1"
DEIDENTIFICATION_CONTRACT_VERSION = "local_pii_masker_v1"
MANIFEST_SCHEMA_VERSION = "social-ticket-phase0-manifest-v1"
SAFE_METRICS_SCHEMA_VERSION = "social-ticket-phase0-safe-metrics-v1"
BILLING_RECONCILIATION_SCHEMA_VERSION = "phase0-provider-billing-v1"
PHASE0_ANALYZER_MODES = frozenset({"deterministic", "llm", "fallback"})
PHASE0_REPORT_CLASSIFICATION = {
    "evaluation_classification": "source_observed_diagnostic",
    "provisional": True,
    "calibration_only": True,
    "independent_evaluation": False,
    "previously_exposed": True,
    "product_verdict_eligible": False,
    "human_product_verdict": False,
}
OWNER_JOIN_FIELDS = frozenset(
    {
        "contract",
        "status",
        "completeness",
        "list_sha256",
        "declared_ids_total",
        "matched_source_total",
        "unmatched_source_total",
        "matched_eligible_total",
        "matched_social_only_total",
        "source_format",
    }
)
PHASE0_JOINT_CONFIRMED_THRESHOLD = 0.60
PHASE0_JOINT_PARTIAL_THRESHOLD = 0.30
PHASE0_BILLING_MAX_RELATIVE_DISCREPANCY = 0.10

SOURCE_FIELDS = frozenset(
    {
        "bot_turns",
        "category",
        "channel",
        "closed_at",
        "closed_without_operator",
        "counted_in_conversion",
        "created_at",
        "forum",
        "is_substantive",
        "ticket_id",
        "topic",
        "user_turns",
        "was_escalated",
    }
)
STARTUP_COMMANDS = frozenset({"start", "начать"})
SELECTION_HASH_DOMAIN = "rosmol-phase0-social-selection-v1"
ORDER_HASH_DOMAIN = "rosmol-phase0-social-order-v1"
OWNER_ID_COLUMN = "unique_id"
SAFE_SLICE_MIN_CASES = 5
PUBLIC_BEHAVIOR_LABELS = frozenset(
    {"answer", "clarify", "scope_note", "escalate", "unknown"}
)
PUBLIC_GENERATOR_MODEL_LABELS = frozenset(
    {
        "ai-sage/GigaChat3-10B-A1.8B",
        "not_run",
        "source_chunk",
        "source_only",
        "unknown",
    }
)
PUBLIC_ESCALATION_REASON_LABELS = frozenset(
    {
        "attachment_only",
        "low_confidence",
        "needs_operator",
        "no_relevant_chunks",
        "none",
        "operator_requested",
        "personal_status",
        "rate_limited",
        "repeated_support_failure",
        "safety_abuse",
        "safety_bullying",
        "safety_dangerous_instruction",
        "safety_medical_emergency",
        "safety_psychological_crisis",
        "safety_self_harm",
        "safety_threat",
        "unsafe_sensitive_data_request",
        "unsupported_instruction",
    }
)
PUBLIC_GENERATOR_PATH_LABELS = frozenset(
    {
        "complex_deterministic_source_chunk",
        "complex_partial_source_chunk",
        "complex_single_official_source_chunk",
        "complex_source_chunk",
        "complex_source_only_escalation",
        "general_catalog_source_chunk",
        "llm",
        "partial_source_chunk",
        "source_chunk",
        "source_only_escalation",
        "unknown",
    }
)
PUBLIC_RERANKER_SCORE_ORIGIN_LABELS = frozenset(
    {"mixed", "none", "reranker", "synthetic", "unknown"}
)
PUBLIC_CATEGORY_ALIASES = {
    "forums": "forums",
    "grants": "grants",
    "navigation": "navigation",
    "offtopic": "offtopic",
    "platform": "fgais_platform",
    "support": "technical_support",
    "гранты": "grants",
    "другое": "other",
    "навигация": "navigation",
    "общее": "general",
    "платформа фгаис": "fgais_platform",
    "платформа_фгаис": "fgais_platform",
    "техподдержка": "technical_support",
    "форумы": "forums",
}
PHASE0_PROVENANCE_PATHS = (
    "eval/cost_governance.py",
    "eval/run_ask.py",
    "eval/social_ticket_benchmark.py",
    "scripts/analyze_ticket_dataset.py",
    "src/config.py",
    "src/graph/nodes/analyze.py",
    "src/kb/source_extractors.py",
    "src/security/eval_cache_bypass.py",
    "src/security/pii_masker.py",
)

_NON_WORD_RE = re.compile(r"[^\w\s-]+", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_TELEMETRY_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


SocialTurnKind = Literal["startup", "greeting", "gratitude"]
SelectionMode = Literal["normalized_text", "opaque_nontext"]


@dataclass(frozen=True)
class FirstContentTurn:
    index: int
    text: str
    mode: SelectionMode
    query_sha256: str


@dataclass(frozen=True)
class OwnerIdList:
    ids: frozenset[str]
    file_sha256: str
    source_format: str


@dataclass(frozen=True)
class CohortCase:
    ticket_id: str
    ticket_id_hash: str
    query: str
    query_sha256: str
    selected_turn_index: int
    selection_mode: SelectionMode
    channel: str
    category: str | None
    forum: str | None
    user_turns_count: int
    source_closed_without_operator: bool
    source_was_escalated: bool
    source_no_continuation: bool | None

    @property
    def case_id(self) -> str:
        return f"social-p0-{self.ticket_id_hash}"

    @property
    def stratum(self) -> str:
        return _stratum(self.channel, self.forum)

    @property
    def dialogue_length_bucket(self) -> str:
        if self.user_turns_count == 1:
            return "1"
        if self.user_turns_count == 2:
            return "2"
        return "3_plus"


@dataclass(frozen=True)
class CohortBuild:
    cases: tuple[CohortCase, ...]
    source_rows_total: int
    channel_counts: Mapping[str, int]
    social_only_total: int
    social_only_closed: int
    opaque_selected_total: int
    opaque_terminal_total: int
    selected_turn_index_counts: Mapping[int, int]
    stratum_counts: Mapping[str, int]
    owner_join: Mapping[str, Any]


def normalize_social_text_v1(value: str) -> str:
    """Normalize exactly like the deterministic bot-interaction classifier."""

    normalized = value.casefold().replace("ё", "е")
    normalized = _NON_WORD_RE.sub(" ", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def social_turn_kind_v1(value: str) -> SocialTurnKind | None:
    normalized = normalize_social_text_v1(value)
    if not normalized:
        return None
    # Startup is checked first because the canonical greeting set already contains
    # these commands, while the owner contract requires a separate diagnostic group.
    if normalized in STARTUP_COMMANDS:
        return "startup"
    if _is_greeting_message(normalized):
        return "greeting"
    if normalized in GRATITUDE_PHRASES:
        return "gratitude"
    return None


def is_social_only_v1(user_turns: Sequence[str]) -> bool:
    return bool(user_turns) and all(
        social_turn_kind_v1(turn) is not None for turn in user_turns
    )


def select_first_content_turn_v1(user_turns: Sequence[str]) -> FirstContentTurn:
    """Select the earliest raw non-social turn without skipping opaque input."""

    if not user_turns:
        raise ValueError("user_turns must not be empty")
    for index, turn in enumerate(user_turns):
        if not isinstance(turn, str) or not turn.strip():
            raise ValueError("user_turns must contain non-empty strings")
        if social_turn_kind_v1(turn) is not None:
            continue
        stripped = turn.strip()
        if len(stripped) > 4000:
            raise ValueError("selected first-content turn exceeds /ask limit")
        normalized = normalize_social_text_v1(stripped)
        return FirstContentTurn(
            index=index,
            text=stripped,
            mode="normalized_text" if normalized else "opaque_nontext",
            query_sha256=_text_sha256(stripped),
        )
    raise ValueError("social-only ticket has no first-content turn")


def load_source_jsonl(
    path: Path,
    *,
    expected_sha256: str | None = EXPECTED_SOURCE_SHA256,
) -> list[dict[str, Any]]:
    _ensure_regular_file(path, label="source dataset")
    actual_sha256 = _file_sha256(path)
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise ValueError("source dataset SHA-256 mismatch")

    records: list[dict[str, Any]] = []
    ticket_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, start=1):
            if not raw_line.strip():
                continue
            try:
                raw = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid source JSONL at line {line_number}") from exc
            record = _validate_source_record(raw, line_number=line_number)
            ticket_id = record["ticket_id"]
            if ticket_id in ticket_ids:
                raise ValueError(f"duplicate source ticket at line {line_number}")
            ticket_ids.add(ticket_id)
            records.append(record)
    return records


def load_owner_no_continuation_ids(
    path: Path,
    *,
    id_column: str = OWNER_ID_COLUMN,
    sheet_name: str | None = None,
    expected_count: int = EXPECTED_OWNER_NO_CONTINUATION_IDS,
) -> OwnerIdList:
    """Load an exact owner membership list without logging or normalizing IDs."""

    _ensure_regular_file(path, label="owner ID list")
    suffix = path.suffix.casefold()
    if suffix == ".json":
        values = _owner_ids_from_json(path, id_column=id_column)
        source_format = "json"
    elif suffix == ".csv":
        values = _owner_ids_from_csv(path, id_column=id_column)
        source_format = "csv"
    elif suffix == ".xlsx":
        values = _owner_ids_from_xlsx(
            path,
            id_column=id_column,
            sheet_name=sheet_name,
        )
        source_format = "xlsx"
    else:
        raise ValueError("owner ID list must be JSON, CSV, or XLSX")

    normalized: list[str] = []
    for row_number, value in enumerate(values, start=1):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"owner ID row {row_number} must be a non-empty string")
        normalized.append(value.strip())
    if len(normalized) != expected_count:
        raise ValueError(
            f"owner ID list must contain exactly {expected_count} non-empty rows"
        )
    if len(set(normalized)) != len(normalized):
        raise ValueError("owner ID list contains duplicates")
    return OwnerIdList(
        ids=frozenset(normalized),
        file_sha256=_file_sha256(path),
        source_format=source_format,
    )


def build_cohort_v1(
    records: Sequence[Mapping[str, Any]],
    *,
    owner_ids: OwnerIdList | None = None,
    enforce_july_contract: bool = True,
) -> CohortBuild:
    source_ids = {str(record["ticket_id"]) for record in records}
    if len(source_ids) != len(records):
        raise ValueError("source records contain duplicate ticket IDs")

    owner_matches = source_ids & owner_ids.ids if owner_ids is not None else set()
    owner_unmatched = owner_ids.ids - source_ids if owner_ids is not None else set()
    cases: list[CohortCase] = []
    social_only_total = 0
    social_only_closed = 0
    opaque_selected_total = 0
    opaque_terminal_total = 0
    social_owner_matches = 0
    channel_counts: Counter[str] = Counter()
    selected_turn_index_counts: Counter[int] = Counter()

    hashes: set[str] = set()
    for record in records:
        channel = str(record["channel"])
        channel_counts[channel] += 1
        user_turns = tuple(str(turn) for turn in record["user_turns"])
        ticket_id = str(record["ticket_id"])
        if is_social_only_v1(user_turns):
            social_only_total += 1
            social_only_closed += int(record["closed_without_operator"] is True)
            social_owner_matches += int(ticket_id in owner_matches)
            continue

        first_content = select_first_content_turn_v1(user_turns)
        ticket_hash = private_id_hash(ticket_id)
        if ticket_hash in hashes:
            raise ValueError("private ticket hash collision")
        hashes.add(ticket_hash)
        opaque_selected_total += int(first_content.mode == "opaque_nontext")
        opaque_terminal_total += int(
            first_content.mode == "opaque_nontext"
            and first_content.index == len(user_turns) - 1
        )
        selected_turn_index_counts[first_content.index] += 1
        cases.append(
            CohortCase(
                ticket_id=ticket_id,
                ticket_id_hash=ticket_hash,
                query=first_content.text,
                query_sha256=first_content.query_sha256,
                selected_turn_index=first_content.index,
                selection_mode=first_content.mode,
                channel=channel,
                category=_optional_string(record.get("category")),
                forum=_optional_string(record.get("forum")),
                user_turns_count=len(user_turns),
                source_closed_without_operator=bool(
                    record["closed_without_operator"]
                ),
                source_was_escalated=bool(record["was_escalated"]),
                source_no_continuation=(
                    ticket_id in owner_ids.ids if owner_ids is not None else None
                ),
            )
        )

    stratum_counts = Counter(case.stratum for case in cases)
    owner_join: dict[str, Any]
    if owner_ids is None:
        owner_join = {
            "contract": OWNER_JOIN_CONTRACT_VERSION,
            "status": "not_provided",
            "completeness": "unavailable",
            "list_sha256": None,
            "declared_ids_total": None,
            "matched_source_total": None,
            "unmatched_source_total": None,
            "matched_eligible_total": None,
            "matched_social_only_total": None,
            "source_format": None,
        }
    else:
        matched_eligible = sum(case.source_no_continuation is True for case in cases)
        owner_join = {
            "contract": OWNER_JOIN_CONTRACT_VERSION,
            "status": "joined",
            "completeness": (
                "complete_for_current_source"
                if not owner_unmatched
                else "partial_external_population_join"
            ),
            "list_sha256": owner_ids.file_sha256,
            "declared_ids_total": len(owner_ids.ids),
            "matched_source_total": len(owner_matches),
            "unmatched_source_total": len(owner_unmatched),
            "matched_eligible_total": matched_eligible,
            "matched_social_only_total": social_owner_matches,
            "source_format": owner_ids.source_format,
        }

    result = CohortBuild(
        cases=tuple(cases),
        source_rows_total=len(records),
        channel_counts=dict(sorted(channel_counts.items())),
        social_only_total=social_only_total,
        social_only_closed=social_only_closed,
        opaque_selected_total=opaque_selected_total,
        opaque_terminal_total=opaque_terminal_total,
        selected_turn_index_counts=dict(sorted(selected_turn_index_counts.items())),
        stratum_counts=dict(sorted(stratum_counts.items())),
        owner_join=owner_join,
    )
    if enforce_july_contract:
        _validate_july_contract(records, result)
    return result


def selection_key_v1(ticket_id_hash: str, *, seed: str) -> str:
    return _domain_hash(SELECTION_HASH_DOMAIN, seed, ticket_id_hash)


def execution_order_key_v1(ticket_id_hash: str, *, seed: str) -> str:
    return _domain_hash(ORDER_HASH_DOMAIN, seed, ticket_id_hash)


def select_phase0_cases_v1(
    cohort: CohortBuild,
    *,
    seed: str = PHASE0_SELECTION_SEED,
    quotas: Mapping[str, int] = PHASE0_STRATUM_QUOTAS,
) -> list[dict[str, Any]]:
    if seed != PHASE0_SELECTION_SEED:
        raise ValueError("phase0 selection seed differs from the approved seed")
    if dict(quotas) != PHASE0_STRATUM_QUOTAS:
        raise ValueError("phase0 quotas differ from the approved 11/11/4/4 quotas")

    grouped: dict[str, list[CohortCase]] = defaultdict(list)
    for case in cohort.cases:
        grouped[case.stratum].append(case)

    selected: list[dict[str, Any]] = []
    for stratum in PHASE0_STRATUM_QUOTAS:
        population = grouped.get(stratum, [])
        quota = quotas[stratum]
        if len(population) < quota:
            raise ValueError(f"stratum {stratum} has fewer cases than its fixed quota")
        ranked = sorted(
            population,
            key=lambda item: (
                selection_key_v1(item.ticket_id_hash, seed=seed),
                item.ticket_id_hash,
            ),
        )
        weight = len(population) / quota
        for rank, case in enumerate(ranked[:quota], start=1):
            selected.append(
                {
                    "case": case,
                    "stratum": stratum,
                    "selection_rank_within_stratum": rank,
                    "selection_key": selection_key_v1(
                        case.ticket_id_hash,
                        seed=seed,
                    ),
                    "execution_order_key": execution_order_key_v1(
                        case.ticket_id_hash,
                        seed=seed,
                    ),
                    "post_stratification_weight": weight,
                    "weight_numerator": len(population),
                    "weight_denominator": quota,
                }
            )

    selected.sort(
        key=lambda item: (
            item["execution_order_key"],
            item["case"].ticket_id_hash,
        )
    )
    for execution_order, item in enumerate(selected, start=1):
        item["execution_order"] = execution_order
    return selected


def build_phase0_artifacts(
    *,
    source_path: Path,
    cases_output_path: Path,
    manifest_output_path: Path,
    owner_ids_path: Path | None = None,
    owner_id_column: str = OWNER_ID_COLUMN,
    owner_sheet_name: str | None = None,
    private_root: Path = PRIVATE_DATA_ROOT,
    seed: str = PHASE0_SELECTION_SEED,
    telemetry_git_sha: str,
) -> dict[str, Any]:
    if seed != PHASE0_SELECTION_SEED:
        raise ValueError("phase0 build seed differs from the approved seed")
    if _TELEMETRY_GIT_SHA_RE.fullmatch(telemetry_git_sha) is None:
        raise ValueError("telemetry Git SHA must be an exact lowercase 40-hex commit")
    _validate_builder_git_provenance(telemetry_git_sha)
    source = _private_path(
        source_path,
        private_root=private_root,
        label="source dataset",
        must_exist=True,
    )
    cases_output = _private_path(
        cases_output_path,
        private_root=private_root,
        label="private cases output",
        must_exist=False,
    )
    manifest_output = _private_path(
        manifest_output_path,
        private_root=private_root,
        label="private manifest output",
        must_exist=False,
    )
    if cases_output == manifest_output:
        raise ValueError("cases and manifest outputs must differ")
    _ensure_output_absent(cases_output)
    _ensure_output_absent(manifest_output)

    owner_ids: OwnerIdList | None = None
    if owner_ids_path is not None:
        owner_path = _private_path(
            owner_ids_path,
            private_root=private_root,
            label="owner ID list",
            must_exist=True,
        )
        owner_ids = load_owner_no_continuation_ids(
            owner_path,
            id_column=owner_id_column,
            sheet_name=owner_sheet_name,
        )

    records = load_source_jsonl(source)
    cohort = build_cohort_v1(records, owner_ids=owner_ids)
    selected = select_phase0_cases_v1(cohort, seed=seed)
    masker = PIIMasker()
    prepared_queries = [
        _deidentify_query(item["case"].query, masker=masker) for item in selected
    ]
    cases_payload = [
        _runner_case(item["case"], query=prepared["text"])
        for item, prepared in zip(selected, prepared_queries, strict=True)
    ]
    cases_bytes = _json_bytes(cases_payload)
    cases_file_sha256 = hashlib.sha256(cases_bytes).hexdigest()
    if cases_file_sha256 != EXPECTED_PHASE0_CASES_FILE_SHA256:
        raise ValueError(
            "phase0 cases bytes differ from the exact approval-bound payload"
        )
    ordered_case_ids = [item["case"].case_id for item in selected]
    ordered_selection_sha256 = _canonical_sha256(ordered_case_ids)

    manifest_cases = [
        _manifest_case(
            item,
            deidentified_query_sha256=prepared["sha256"],
            pii_types_detected=prepared["pii_types_detected"],
            runner_case_sha256=_canonical_sha256(runner_case),
        )
        for item, prepared, runner_case in zip(
            selected,
            prepared_queries,
            cases_payload,
            strict=True,
        )
    ]
    manifest_core: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "contracts": {
            "social_only": SOCIAL_CONTRACT_VERSION,
            "first_content": FIRST_CONTENT_CONTRACT_VERSION,
            "owner_no_continuation": OWNER_JOIN_CONTRACT_VERSION,
            "selection": SELECTION_CONTRACT_VERSION,
            "deidentification": DEIDENTIFICATION_CONTRACT_VERSION,
        },
        "source": {
            "file_sha256": _file_sha256(source),
            "rows_total": cohort.source_rows_total,
            "channel_counts": dict(cohort.channel_counts),
            "period": "2026-07",
            "tool_sha256": _file_sha256(Path(__file__)),
            "social_classifier_dependency_sha256": _file_sha256(
                PROJECT_ROOT / "src" / "graph" / "nodes" / "analyze.py"
            ),
        },
        "telemetry": {"git_sha": telemetry_git_sha},
        "approval": {
            "id": PHASE0_APPROVAL_ID,
            "hard_cap_rub": PHASE0_COST_CAP_RUB,
            "case_count": PHASE0_SAMPLE_SIZE,
            "telemetry_git_sha": telemetry_git_sha,
            "ordered_selection_sha256": ordered_selection_sha256,
            "cache_bypass_required": True,
            "selective_reruns_forbidden": True,
            "provider_billing_reconciliation_required": True,
        },
        "deidentification": {
            "contract": DEIDENTIFICATION_CONTRACT_VERSION,
            "performed_locally": True,
            "single_turn_only": True,
            "raw_query_exported": False,
            "scanned_cases": len(prepared_queries),
            "changed_cases": sum(
                bool(prepared["pii_types_detected"])
                for prepared in prepared_queries
            ),
            "pii_type_counts": dict(
                sorted(
                    Counter(
                        pii_type
                        for prepared in prepared_queries
                        for pii_type in prepared["pii_types_detected"]
                    ).items()
                )
            ),
        },
        "population": {
            "social_only_total": cohort.social_only_total,
            "social_only_closed_without_operator": cohort.social_only_closed,
            "eligible_total": len(cohort.cases),
            "opaque_nontext_selected_total": cohort.opaque_selected_total,
            "opaque_nontext_terminal_total": cohort.opaque_terminal_total,
            "selected_turn_index_counts": {
                str(key): value
                for key, value in cohort.selected_turn_index_counts.items()
            },
            "stratum_counts": dict(cohort.stratum_counts),
        },
        "owner_join": dict(cohort.owner_join),
        "selection": {
            "seed": seed,
            "sample_size": len(selected),
            "quotas": dict(PHASE0_STRATUM_QUOTAS),
            "hash_algorithm": "sha256-domain-separated-v1",
            "global_order": "sha256-domain-separated-v1",
            "post_stratification": {
                stratum: {
                    "population": cohort.stratum_counts[stratum],
                    "sample": PHASE0_STRATUM_QUOTAS[stratum],
                    "weight_numerator": cohort.stratum_counts[stratum],
                    "weight_denominator": PHASE0_STRATUM_QUOTAS[stratum],
                    "weight": (
                        cohort.stratum_counts[stratum]
                        / PHASE0_STRATUM_QUOTAS[stratum]
                    ),
                }
                for stratum in PHASE0_STRATUM_QUOTAS
            },
        },
        "cases": manifest_cases,
    }
    manifest_core_sha256 = _canonical_sha256(manifest_core)
    manifest = {
        **manifest_core,
        "integrity": {
            "cases_file_sha256": cases_file_sha256,
            "ordered_selection_sha256": ordered_selection_sha256,
            "manifest_core_sha256": manifest_core_sha256,
        },
    }
    _validate_manifest_integrity(manifest)
    manifest_bytes = _json_bytes(manifest)
    _write_private_pair_no_overwrite(
        first_path=cases_output,
        first_bytes=cases_bytes,
        second_path=manifest_output,
        second_bytes=manifest_bytes,
    )
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source_file_sha256": manifest["source"]["file_sha256"],
        "source_rows_total": cohort.source_rows_total,
        "social_only_total": cohort.social_only_total,
        "social_only_closed_without_operator": cohort.social_only_closed,
        "eligible_total": len(cohort.cases),
        "opaque_nontext_selected_total": cohort.opaque_selected_total,
        "opaque_nontext_terminal_total": cohort.opaque_terminal_total,
        "stratum_counts": dict(cohort.stratum_counts),
        "sample_quotas": dict(PHASE0_STRATUM_QUOTAS),
        "sample_size": len(selected),
        "owner_join": dict(cohort.owner_join),
        "cases_file_sha256": cases_file_sha256,
        "ordered_selection_sha256": ordered_selection_sha256,
        "manifest_core_sha256": manifest_core_sha256,
        "telemetry_git_sha": telemetry_git_sha,
        "approval_id": PHASE0_APPROVAL_ID,
        "cost_cap_rub": PHASE0_COST_CAP_RUB,
        "deidentification": dict(manifest["deidentification"]),
    }


def _validate_builder_git_provenance(telemetry_git_sha: str) -> None:
    """Require the builder and classifier dependencies to be clean at HEAD."""

    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        if head != telemetry_git_sha:
            raise ValueError(
                "telemetry Git SHA must equal the checked-out builder HEAD"
            )
        status = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                *PHASE0_PROVENANCE_PATHS,
            ],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        if status:
            raise ValueError(
                "Phase 0 builder or classifier dependencies differ from telemetry HEAD"
            )
        tracked = subprocess.run(
            [
                "git",
                "ls-tree",
                "-r",
                "--name-only",
                telemetry_git_sha,
                "--",
                *PHASE0_PROVENANCE_PATHS,
            ],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Phase 0 builder Git provenance could not be verified") from exc
    if set(tracked) != set(PHASE0_PROVENANCE_PATHS):
        raise ValueError(
            "Phase 0 builder dependencies are not all tracked by telemetry HEAD"
        )


def build_safe_phase0_metrics(
    manifest: Mapping[str, Any],
    ask_report: Mapping[str, Any],
    billing_reconciliation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_manifest_integrity(manifest)
    manifest_cases = manifest.get("cases")
    results = ask_report.get("results")
    if not isinstance(manifest_cases, list) or not isinstance(results, list):
        raise ValueError("manifest and ask report must contain case arrays")
    if len(manifest_cases) != PHASE0_SAMPLE_SIZE or len(results) != PHASE0_SAMPLE_SIZE:
        raise ValueError("phase0 manifest and report must each contain 30 cases")

    manifest_by_id = _unique_rows_by_id(manifest_cases, label="manifest")
    results_by_id = _unique_rows_by_id(results, label="ask report")
    if set(manifest_by_id) != set(results_by_id):
        raise ValueError("ask report case membership differs from phase0 manifest")
    manifest_order = [str(row["id"]) for row in manifest_cases]
    result_order = [str(row["id"]) for row in results]
    if result_order != manifest_order:
        raise ValueError("ask report case order differs from phase0 manifest")
    expected_cases_sha = str(
        (manifest.get("integrity") or {}).get("cases_file_sha256") or ""
    )
    report_cases_sha = str(ask_report.get("cases_file_sha256") or "")
    if not report_cases_sha or report_cases_sha != expected_cases_sha:
        raise ValueError("ask report cases SHA-256 differs from phase0 manifest")

    joined: list[dict[str, Any]] = []
    for case_id, source_case in manifest_by_id.items():
        result = results_by_id[case_id]
        behavior = str(result.get("observed_behavior") or "unknown")
        escalated = (
            result.get("was_escalated")
            if type(result.get("was_escalated")) is bool
            else None
        )
        answer_no_operator = (
            behavior == "answer" and escalated is False
            if escalated is not None
            else None
        )
        containment = (
            behavior in {"answer", "scope_note"} and escalated is False
            if escalated is not None
            else None
        )
        generator_model = result.get("generator_model")
        if not isinstance(generator_model, str) or not generator_model.strip():
            generator_model = None
        joined.append(
            {
                **source_case,
                "observed_behavior": behavior,
                "was_escalated": escalated,
                "answer_no_operator": answer_no_operator,
                "containment": containment,
                "http_success": result.get("http_success"),
                "trace_found": result.get("trace_found"),
                "cache_hit": result.get("cache_hit"),
                "generator_model": generator_model,
                "escalation_reason": str(result.get("escalation_reason") or "none"),
                "latency_ms": _finite_number(result.get("latency_ms")),
                "trace_total_latency_ms": _finite_number(
                    result.get("trace_total_latency_ms")
                ),
                "llm_estimated_cost_rub": _nonnegative_finite_number(
                    result.get("llm_estimated_cost_rub")
                ),
                "llm_accounting_present": (
                    result.get("llm_accounting_present")
                    if type(result.get("llm_accounting_present")) is bool
                    else None
                ),
                "max_reranker_score": _finite_number(
                    result.get("max_reranker_score")
                ),
                "analyzer_execution_mode": result.get(
                    "analyzer_execution_mode"
                ),
                "metadata_lookup_attempted": result.get(
                    "metadata_lookup_attempted"
                ),
                "metadata_primary_succeeded": result.get(
                    "metadata_primary_succeeded"
                ),
                "hybrid_candidates_present": result.get(
                    "hybrid_candidates_present"
                ),
                "reranker_invoked": result.get("reranker_invoked"),
                "reranker_score_origin": result.get("reranker_score_origin"),
                "reranker_synthetic_high_score_applied": result.get(
                    "reranker_synthetic_high_score_applied"
                ),
                "generator_path": result.get("generator_path"),
                "source_chunk_applied": result.get("source_chunk_applied"),
            }
        )

    for item in joined:
        analyzer_mode = item.get("analyzer_execution_mode")
        analyzer_typed = analyzer_mode in PHASE0_ANALYZER_MODES
        item["analysis_bypass"] = (
            analyzer_mode in {"deterministic", "fallback"}
            if analyzer_typed
            else None
        )
        joint_inputs_typed = analyzer_typed and all(
            type(item.get(field)) is bool
            for field in (
                "metadata_primary_succeeded",
                "reranker_synthetic_high_score_applied",
                "source_chunk_applied",
            )
        )
        item["joint_bypass"] = (
            item["analysis_bypass"] is True
            and item["metadata_primary_succeeded"] is True
            and item["reranker_synthetic_high_score_applied"] is True
            and item["source_chunk_applied"] is True
            if joint_inputs_typed
            else None
        )
        item["clarification"] = item.get("observed_behavior") == "clarify"

    generator_counts = Counter(
        str(item["generator_model"] or "unknown") for item in joined
    )
    escalation_counts = Counter(item["escalation_reason"] for item in joined)
    owner_available = all(
        isinstance(item.get("source_no_continuation"), bool) for item in joined
    )
    source_no_continuation = (
        _safe_binary_rate_summary(joined, "source_no_continuation")
        if owner_available
        else None
    )
    owner_behavior_matrix: dict[str, Any] | None = None
    if owner_available:
        matrix: dict[str, Counter[str]] = defaultdict(Counter)
        for item in joined:
            matrix[str(bool(item["source_no_continuation"]))][
                item["observed_behavior"]
            ] += 1
        owner_behavior_matrix = _safe_categorical_matrix(matrix)

    raw_report_classification = ask_report.get("report_classification")
    report_classification = _report_classification_projection(
        raw_report_classification
    )
    execution_binding_checks = _phase0_execution_binding_checks(
        manifest,
        ask_report,
        expected_cases_sha=expected_cases_sha,
    )
    embedded_billing = ask_report.get("provider_billing_reconciliation")
    effective_billing = (
        billing_reconciliation
        if billing_reconciliation is not None
        else embedded_billing if isinstance(embedded_billing, Mapping) else None
    )
    billing_summary, billing_check = _phase0_billing_reconciliation(
        manifest,
        ask_report,
        effective_billing,
        expected_cases_sha=expected_cases_sha,
    )
    execution_binding_checks["provider_billing_reconciliation"] = billing_check
    phase0_gate = _build_phase0_gate(
        joined,
        raw_report_classification,
        execution_binding_checks=execution_binding_checks,
    )
    joint_bypass_summary = phase0_gate["joint_bypass"]
    public_outcomes = _safe_public_outcome_summaries(joined)

    return {
        "schema_version": SAFE_METRICS_SCHEMA_VERSION,
        "manifest_core_sha256": (manifest.get("integrity") or {}).get(
            "manifest_core_sha256"
        ),
        "ordered_selection_sha256": (manifest.get("integrity") or {}).get(
            "ordered_selection_sha256"
        ),
        "cases_file_sha256": expected_cases_sha,
        "source_file_sha256": (manifest.get("source") or {}).get("file_sha256"),
        "telemetry_git_sha": (manifest.get("telemetry") or {}).get("git_sha"),
        "report_classification": report_classification,
        "cases_total": len(joined),
        "phase0_gate": phase0_gate,
        "provider_billing_reconciliation": billing_summary,
        "integrity": {
            "http_success_rate": _safe_optional_bool_rate(joined, "http_success"),
            "trace_coverage_rate": _safe_optional_bool_rate(joined, "trace_found"),
            "cache_hit_rate": _safe_optional_bool_rate(joined, "cache_hit"),
        },
        "sample": {
            "stratum_counts": _safe_categorical_counts(
                Counter(item["stratum"] for item in joined),
                namespace="stratum",
            ),
            "behavior_counts": public_outcomes["behavior_counts"],
            "source_closed_without_operator": _safe_binary_rate_summary(
                joined,
                "source_closed_without_operator",
            ),
            "source_no_continuation": source_no_continuation,
            "answer_no_operator": public_outcomes["answer_no_operator"],
            "clarification": public_outcomes["clarification"],
            "containment": public_outcomes["containment"],
            "escalation": public_outcomes["escalation"],
            "cross_metric_suppression": public_outcomes["suppression"],
        },
        "owner_join": {
            **_owner_join_projection(manifest.get("owner_join")),
            "sample_behavior_matrix": owner_behavior_matrix,
        },
        "diagnostics": {
            "generator_model_counts": _safe_categorical_counts(
                generator_counts,
                namespace="generator_model",
            ),
            "escalation_reason_counts": _safe_categorical_counts(
                escalation_counts,
                namespace="escalation_reason",
            ),
            "source_chunk_rate": _value_rate(
                joined,
                "generator_model",
                "source_chunk",
            ),
            "deterministic_analyzer_rate": _value_rate(
                joined,
                "analyzer_execution_mode",
                "deterministic",
            ),
            "analysis_bypass": _safe_binary_rate_summary(joined, "analysis_bypass"),
            "metadata_lookup_attempt_rate": _safe_optional_bool_rate(
                joined,
                "metadata_lookup_attempted",
            ),
            "metadata_primary_success_rate": _safe_optional_bool_rate(
                joined,
                "metadata_primary_succeeded",
            ),
            "hybrid_candidate_rate": _safe_optional_bool_rate(
                joined,
                "hybrid_candidates_present",
            ),
            "hybrid_candidates": _safe_binary_rate_summary(
                joined,
                "hybrid_candidates_present",
            ),
            "reranker_invocation_rate": _safe_optional_bool_rate(
                joined,
                "reranker_invoked",
            ),
            "reranker_invocation": _safe_binary_rate_summary(
                joined, "reranker_invoked"
            ),
            "reranker_synthetic_high_score_rate": _safe_optional_bool_rate(
                joined,
                "reranker_synthetic_high_score_applied",
            ),
            "source_chunk_applied_rate": _safe_optional_bool_rate(
                joined,
                "source_chunk_applied",
            ),
            "joint_bypass": joint_bypass_summary,
            # Backward-compatible secondary alias. The gate decision uses the
            # post-stratified primary rate above, never this unweighted value.
            "joint_bypass_rate": joint_bypass_summary["unweighted_rate"],
            "generator_path_counts": _safe_categorical_counts(
                Counter(
                    str(item.get("generator_path") or "unknown")
                    for item in joined
                ),
                namespace="generator_path",
            ),
            "reranker_score_origin_counts": _safe_categorical_counts(
                Counter(
                    str(item.get("reranker_score_origin") or "unknown")
                    for item in joined
                ),
                namespace="reranker_score_origin",
            ),
            "max_reranker_score": _number_summary(
                [item["max_reranker_score"] for item in joined]
            ),
            "latency_ms": _number_summary([item["latency_ms"] for item in joined]),
            "trace_total_latency_ms": _number_summary(
                [item["trace_total_latency_ms"] for item in joined]
            ),
            "llm_estimated_cost_rub": _nonnegative_number_total(
                [item["llm_estimated_cost_rub"] for item in joined]
            ),
        },
        "slices": {
            "channel": _safe_slice(joined, "source_channel"),
            "forum_presence": _safe_slice(joined, "source_forum_presence"),
            "category": _safe_slice(joined, "source_category"),
            "forum": _safe_slice(joined, "source_forum"),
            "dialogue_length": _safe_slice(joined, "source_dialogue_length_bucket"),
        },
    }


def write_safe_phase0_metrics(
    *,
    manifest_path: Path,
    ask_report_path: Path,
    billing_reconciliation_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    manifest = _read_json_object(manifest_path, label="phase0 manifest")
    report = _read_json_object(ask_report_path, label="ask report")
    billing = _read_json_object(
        billing_reconciliation_path,
        label="provider billing reconciliation",
    )
    phase0_run = report.get("phase0_run")
    if not isinstance(phase0_run, Mapping) or (
        phase0_run.get("manifest_file_sha256")
        != _file_sha256(manifest_path)
    ):
        raise ValueError(
            "ask report is not bound to the exact phase0 manifest file"
        )
    metrics = build_safe_phase0_metrics(manifest, report, billing)
    _atomic_write_no_overwrite(output_path, _json_bytes(metrics), private_mode=False)
    return metrics


def _validate_source_record(value: Any, *, line_number: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != SOURCE_FIELDS:
        raise ValueError(f"source line {line_number} has an invalid schema")
    ticket_id = value.get("ticket_id")
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        raise ValueError(f"source line {line_number} has no ticket ID")
    user_turns = value.get("user_turns")
    bot_turns = value.get("bot_turns")
    if (
        not isinstance(user_turns, list)
        or not user_turns
        or any(not isinstance(turn, str) or not turn.strip() for turn in user_turns)
    ):
        raise ValueError(f"source line {line_number} has invalid user turns")
    if not isinstance(bot_turns, list) or any(
        not isinstance(turn, str) for turn in bot_turns
    ):
        raise ValueError(f"source line {line_number} has invalid bot turns")
    if value.get("channel") not in {"vk", "max"}:
        raise ValueError(f"source line {line_number} has an invalid channel")
    for field in (
        "closed_without_operator",
        "counted_in_conversion",
        "is_substantive",
        "was_escalated",
    ):
        if type(value.get(field)) is not bool:
            raise ValueError(f"source line {line_number} has an invalid {field} flag")
    for field in ("category", "forum", "topic"):
        if value.get(field) is not None and not isinstance(value.get(field), str):
            raise ValueError(f"source line {line_number} has an invalid {field}")
    for field in ("created_at", "closed_at"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"source line {line_number} has an invalid timestamp")
    normalized = dict(value)
    normalized["ticket_id"] = ticket_id.strip()
    normalized["user_turns"] = [turn.strip() for turn in user_turns]
    return normalized


def _validate_july_contract(
    records: Sequence[Mapping[str, Any]],
    cohort: CohortBuild,
) -> None:
    failures: list[str] = []
    if len(records) != EXPECTED_SOURCE_ROWS:
        failures.append("rows_total")
    if dict(cohort.channel_counts) != EXPECTED_CHANNEL_COUNTS:
        failures.append("channel_counts")
    if any(not str(record["created_at"]).startswith("2026-07-") for record in records):
        failures.append("created_at_period")
    if any(record["counted_in_conversion"] is not True for record in records):
        failures.append("counted_in_conversion")
    if any(
        bool(record["closed_without_operator"]) == bool(record["was_escalated"])
        for record in records
    ):
        failures.append("closed_escalated_inverse")
    if cohort.social_only_total != EXPECTED_SOCIAL_ONLY:
        failures.append("social_only_total")
    if cohort.social_only_closed != EXPECTED_SOCIAL_ONLY_CLOSED:
        failures.append("social_only_closed")
    if len(cohort.cases) != EXPECTED_ELIGIBLE:
        failures.append("eligible_total")
    if cohort.opaque_selected_total != EXPECTED_OPAQUE_SELECTED:
        failures.append("opaque_selected_total")
    if cohort.opaque_terminal_total != EXPECTED_OPAQUE_TERMINAL:
        failures.append("opaque_terminal_total")
    if dict(cohort.stratum_counts) != dict(sorted(EXPECTED_STRATUM_COUNTS.items())):
        failures.append("stratum_counts")
    if failures:
        raise ValueError(
            "July source contract mismatch: " + ", ".join(sorted(failures))
        )


def _deidentify_query(text: str, *, masker: PIIMasker) -> dict[str, Any]:
    """Fail closed unless local PII masking can scan the exact exported turn."""

    deidentified, mapping = masker.mask(text)
    deidentified = deidentified.strip()
    if not deidentified:
        raise ValueError("deidentified first-content turn must not be empty")
    if len(deidentified) > 4000:
        raise ValueError("deidentified first-content turn exceeds /ask limit")
    rescanned, residual_mapping = masker.mask(deidentified)
    if rescanned != deidentified or residual_mapping:
        raise ValueError("deidentified first-content turn failed the residual PII scan")
    return {
        "text": deidentified,
        "sha256": _text_sha256(deidentified),
        "pii_types_detected": sorted(str(key) for key in mapping),
    }


def _runner_case(case: CohortCase, *, query: str) -> dict[str, Any]:
    return {
        "id": case.case_id,
        "query": query,
        "privacy_class": "private_ticket_derived",
        "split": "calibration",
        "label_status": "source_observed_diagnostic",
        "requires_human_review": False,
        "user_id": case.case_id,
        "channel": case.channel,
        "tags": [
            "benchmark:social_only_v1",
            "measurement:source_observed_diagnostic",
            "split:calibration",
        ],
    }


def _manifest_case(
    item: Mapping[str, Any],
    *,
    runner_case_sha256: str,
    deidentified_query_sha256: str | None = None,
    pii_types_detected: Sequence[str] = (),
) -> dict[str, Any]:
    case: CohortCase = item["case"]
    return {
        "id": case.case_id,
        "source_ticket_id_hash": case.ticket_id_hash,
        "query_sha256": case.query_sha256,
        "deidentified_query_sha256": (
            deidentified_query_sha256 or case.query_sha256
        ),
        "runner_case_sha256": runner_case_sha256,
        "pii_types_detected": sorted(set(pii_types_detected)),
        "selected_turn_index": case.selected_turn_index,
        "selection_mode": case.selection_mode,
        "source_channel": case.channel,
        "source_category": case.category,
        "source_forum": case.forum,
        "source_forum_presence": "forum" if case.forum else "no_forum",
        "source_user_turns_count": case.user_turns_count,
        "source_dialogue_length_bucket": case.dialogue_length_bucket,
        "source_closed_without_operator": case.source_closed_without_operator,
        "source_was_escalated": case.source_was_escalated,
        "source_no_continuation": case.source_no_continuation,
        "stratum": item["stratum"],
        "selection_rank_within_stratum": item["selection_rank_within_stratum"],
        "selection_key": item["selection_key"],
        "execution_order_key": item["execution_order_key"],
        "execution_order": item["execution_order"],
        "post_stratification_weight": item["post_stratification_weight"],
        "weight_numerator": item["weight_numerator"],
        "weight_denominator": item["weight_denominator"],
    }


def _owner_ids_from_json(path: Path, *, id_column: str) -> list[Any]:
    payload = _read_json(path, label="owner JSON")
    if isinstance(payload, dict):
        if set(payload) == {"ticket_ids"}:
            payload = payload["ticket_ids"]
        elif set(payload) == {id_column}:
            payload = payload[id_column]
        else:
            raise ValueError("owner JSON object must contain only ticket_ids")
    if not isinstance(payload, list):
        raise ValueError("owner JSON must be an array")
    if all(isinstance(item, str) for item in payload):
        return payload
    if all(isinstance(item, dict) and set(item) == {id_column} for item in payload):
        return [item[id_column] for item in payload]
    raise ValueError("owner JSON rows must contain only the exact ID column")


def _owner_ids_from_csv(path: Path, *, id_column: str) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            column = _exact_header(reader.fieldnames, id_column=id_column)
            return [str(row.get(column) or "") for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError("owner CSV is not a readable UTF-8 table") from exc


def _owner_ids_from_xlsx(
    path: Path,
    *,
    id_column: str,
    sheet_name: str | None,
) -> list[str]:
    try:
        sheets = read_xlsx_sheets(path)
    except Exception as exc:
        raise ValueError("owner XLSX is not a readable workbook") from exc
    candidates: list[tuple[str, list[Any], int]] = []
    for current_sheet, rows in sheets.items():
        if sheet_name is not None and current_sheet != sheet_name:
            continue
        if not rows:
            continue
        headers = list(rows[0].cells)
        try:
            column = _exact_header(headers, id_column=id_column)
        except ValueError:
            continue
        column_index = headers.index(column)
        candidates.append((current_sheet, rows, column_index))
    if len(candidates) != 1:
        raise ValueError("owner XLSX must contain exactly one selected ID table")
    _, rows, column_index = candidates[0]
    return [row.cell(column_index) for row in rows[1:] if row.cell(column_index)]


def _exact_header(headers: Iterable[str] | None, *, id_column: str) -> str:
    if headers is None:
        raise ValueError("owner table has no header")
    matches = [
        header
        for header in headers
        if isinstance(header, str)
        and header.strip().casefold() == id_column.strip().casefold()
    ]
    if len(matches) != 1:
        raise ValueError("owner table must contain the exact ID column once")
    return matches[0]


def _stratum(channel: str, forum: str | None) -> str:
    return f"{channel}/{'forum' if forum else 'no_forum'}"


def _domain_hash(domain: str, seed: str, ticket_id_hash: str) -> str:
    payload = f"{domain}\0{seed}\0{ticket_id_hash}".encode()
    return hashlib.sha256(payload).hexdigest()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _private_path(
    path: Path,
    *,
    private_root: Path,
    label: str,
    must_exist: bool,
) -> Path:
    root = private_root.resolve(strict=True)
    candidate = path.expanduser().resolve(strict=must_exist)
    if not candidate.is_relative_to(root):
        raise ValueError(f"{label} must stay under data/private")
    if must_exist:
        _ensure_regular_file(candidate, label=label)
    return candidate


def _ensure_regular_file(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")


def _ensure_output_absent(path: Path) -> None:
    if path.is_symlink() or path.exists():
        raise FileExistsError("private output must be absent")


def _atomic_write_no_overwrite(
    path: Path,
    payload: bytes,
    *,
    private_mode: bool,
) -> None:
    _ensure_output_absent(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        _ensure_output_absent(path)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FileExistsError("private output must be absent") from exc
        temporary.unlink()
        if private_mode:
            os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _write_private_pair_no_overwrite(
    *,
    first_path: Path,
    first_bytes: bytes,
    second_path: Path,
    second_bytes: bytes,
) -> None:
    _atomic_write_no_overwrite(first_path, first_bytes, private_mode=True)
    try:
        _atomic_write_no_overwrite(second_path, second_bytes, private_mode=True)
    except Exception:
        # The first file was created by this call at an already-validated exact path.
        first_path.unlink(missing_ok=True)
        raise


def _read_json(path: Path, *, label: str) -> Any:
    _ensure_regular_file(path, label=label)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    payload = _read_json(path, label=label)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _validate_manifest_integrity(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported phase0 manifest schema")
    telemetry = manifest.get("telemetry")
    if not isinstance(telemetry, Mapping):
        raise ValueError("phase0 manifest lacks telemetry identity")
    telemetry_git_sha = str(telemetry.get("git_sha") or "")
    if _TELEMETRY_GIT_SHA_RE.fullmatch(telemetry_git_sha) is None:
        raise ValueError("phase0 manifest telemetry Git SHA is invalid")

    contracts = manifest.get("contracts")
    expected_contracts = {
        "social_only": SOCIAL_CONTRACT_VERSION,
        "first_content": FIRST_CONTENT_CONTRACT_VERSION,
        "owner_no_continuation": OWNER_JOIN_CONTRACT_VERSION,
        "selection": SELECTION_CONTRACT_VERSION,
        "deidentification": DEIDENTIFICATION_CONTRACT_VERSION,
    }
    if contracts != expected_contracts:
        raise ValueError("phase0 manifest contracts differ from the approved contract")

    source = manifest.get("source")
    if not isinstance(source, Mapping) or any(
        (
            source.get("file_sha256") != EXPECTED_SOURCE_SHA256,
            source.get("rows_total") != EXPECTED_SOURCE_ROWS,
            source.get("channel_counts") != EXPECTED_CHANNEL_COUNTS,
            source.get("period") != "2026-07",
        )
    ):
        raise ValueError("phase0 manifest source cohort differs from the approved source")

    population = manifest.get("population")
    if not isinstance(population, Mapping) or any(
        (
            population.get("social_only_total") != EXPECTED_SOCIAL_ONLY,
            population.get("social_only_closed_without_operator")
            != EXPECTED_SOCIAL_ONLY_CLOSED,
            population.get("eligible_total") != EXPECTED_ELIGIBLE,
            population.get("opaque_nontext_selected_total") != EXPECTED_OPAQUE_SELECTED,
            population.get("opaque_nontext_terminal_total") != EXPECTED_OPAQUE_TERMINAL,
            population.get("selected_turn_index_counts")
            != {"0": 425, "1": 300, "2": 8},
            population.get("stratum_counts")
            != dict(sorted(EXPECTED_STRATUM_COUNTS.items())),
        )
    ):
        raise ValueError("phase0 manifest population differs from the approved cohort")

    selection = manifest.get("selection")
    if not isinstance(selection, Mapping) or any(
        (
            selection.get("seed") != PHASE0_SELECTION_SEED,
            selection.get("sample_size") != PHASE0_SAMPLE_SIZE,
            selection.get("quotas") != PHASE0_STRATUM_QUOTAS,
            selection.get("hash_algorithm") != "sha256-domain-separated-v1",
            selection.get("global_order") != "sha256-domain-separated-v1",
        )
    ):
        raise ValueError("phase0 manifest selection differs from the approved selection")
    expected_post_stratification = {
        stratum: {
            "population": EXPECTED_STRATUM_COUNTS[stratum],
            "sample": PHASE0_STRATUM_QUOTAS[stratum],
            "weight_numerator": EXPECTED_STRATUM_COUNTS[stratum],
            "weight_denominator": PHASE0_STRATUM_QUOTAS[stratum],
            "weight": EXPECTED_STRATUM_COUNTS[stratum]
            / PHASE0_STRATUM_QUOTAS[stratum],
        }
        for stratum in PHASE0_STRATUM_QUOTAS
    }
    if selection.get("post_stratification") != expected_post_stratification:
        raise ValueError("phase0 post-stratification weights differ from the contract")

    owner_join = manifest.get("owner_join")
    _validate_owner_join(owner_join)
    owner_membership_available = bool(
        isinstance(owner_join, Mapping) and owner_join.get("status") == "joined"
    )

    approval = manifest.get("approval")
    expected_approval = {
        "id": PHASE0_APPROVAL_ID,
        "hard_cap_rub": PHASE0_COST_CAP_RUB,
        "case_count": PHASE0_SAMPLE_SIZE,
        "telemetry_git_sha": telemetry_git_sha,
        "ordered_selection_sha256": EXPECTED_ORDERED_SELECTION_SHA256,
        "cache_bypass_required": True,
        "selective_reruns_forbidden": True,
        "provider_billing_reconciliation_required": True,
    }
    if approval != expected_approval:
        raise ValueError("phase0 manifest approval binding is invalid")

    deidentification = manifest.get("deidentification")
    if not isinstance(deidentification, Mapping) or any(
        (
            deidentification.get("contract") != DEIDENTIFICATION_CONTRACT_VERSION,
            deidentification.get("performed_locally") is not True,
            deidentification.get("single_turn_only") is not True,
            deidentification.get("raw_query_exported") is not False,
            deidentification.get("scanned_cases") != PHASE0_SAMPLE_SIZE,
        )
    ):
        raise ValueError("phase0 manifest deidentification contract is invalid")

    integrity = manifest.get("integrity")
    if not isinstance(integrity, Mapping):
        raise ValueError("phase0 manifest lacks integrity metadata")
    core = {key: value for key, value in manifest.items() if key != "integrity"}
    if integrity.get("manifest_core_sha256") != _canonical_sha256(core):
        raise ValueError("phase0 manifest core SHA-256 mismatch")
    if (
        integrity.get("cases_file_sha256")
        != EXPECTED_PHASE0_CASES_FILE_SHA256
    ):
        raise ValueError(
            "phase0 cases file SHA-256 differs from the approval-bound payload"
        )
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) != PHASE0_SAMPLE_SIZE:
        raise ValueError("phase0 manifest must contain the exact 30 approved cases")
    ordered_ids = [row.get("id") for row in cases if isinstance(row, dict)]
    computed_ordered_sha = _canonical_sha256(ordered_ids)
    if (
        len(ordered_ids) != len(cases)
        or computed_ordered_sha != EXPECTED_ORDERED_SELECTION_SHA256
        or integrity.get("ordered_selection_sha256") != computed_ordered_sha
    ):
        raise ValueError("phase0 ordered selection SHA-256 mismatch")

    stratum_counts: Counter[str] = Counter()
    stratum_ranks: dict[str, set[int]] = defaultdict(set)
    detected_pii_types: Counter[str] = Counter()
    changed_cases = 0
    for execution_order, row in enumerate(cases, start=1):
        if not isinstance(row, Mapping):
            raise ValueError("phase0 manifest case rows must be objects")
        ticket_hash = str(row.get("source_ticket_id_hash") or "")
        stratum = str(row.get("stratum") or "")
        if row.get("id") != f"social-p0-{ticket_hash}":
            raise ValueError("phase0 case ID is not bound to its private ticket hash")
        if stratum not in PHASE0_STRATUM_QUOTAS:
            raise ValueError("phase0 case has an unknown stratum")
        if row.get("execution_order") != execution_order:
            raise ValueError("phase0 case execution order is invalid")
        expected_weight = EXPECTED_STRATUM_COUNTS[stratum] / PHASE0_STRATUM_QUOTAS[stratum]
        if any(
            (
                row.get("weight_numerator") != EXPECTED_STRATUM_COUNTS[stratum],
                row.get("weight_denominator") != PHASE0_STRATUM_QUOTAS[stratum],
                row.get("post_stratification_weight") != expected_weight,
                row.get("source_channel") != stratum.split("/", maxsplit=1)[0],
                row.get("source_forum_presence")
                != stratum.split("/", maxsplit=1)[1],
                _SHA256_RE.fullmatch(str(row.get("query_sha256") or "")) is None,
                _SHA256_RE.fullmatch(
                    str(row.get("deidentified_query_sha256") or "")
                )
                is None,
                _SHA256_RE.fullmatch(str(row.get("runner_case_sha256") or ""))
                is None,
                row.get("runner_case_sha256") == "0" * 64,
            )
        ):
            raise ValueError("phase0 case weighting or query identity is invalid")
        source_no_continuation = row.get("source_no_continuation")
        if (
            owner_membership_available
            and type(source_no_continuation) is not bool
        ) or (
            not owner_membership_available and source_no_continuation is not None
        ):
            raise ValueError("phase0 case owner membership evidence is inconsistent")
        rank = row.get("selection_rank_within_stratum")
        if isinstance(rank, bool) or not isinstance(rank, int):
            raise ValueError("phase0 selection rank is invalid")
        stratum_counts[stratum] += 1
        stratum_ranks[stratum].add(rank)
        pii_types = row.get("pii_types_detected")
        if not isinstance(pii_types, list) or any(
            not isinstance(item, str) or not item for item in pii_types
        ):
            raise ValueError("phase0 case PII scan evidence is invalid")
        changed_cases += int(bool(pii_types))
        detected_pii_types.update(pii_types)
    if dict(stratum_counts) != PHASE0_STRATUM_QUOTAS or any(
        stratum_ranks[stratum] != set(range(1, quota + 1))
        for stratum, quota in PHASE0_STRATUM_QUOTAS.items()
    ):
        raise ValueError("phase0 case quotas or selection ranks are invalid")
    if (
        deidentification.get("changed_cases") != changed_cases
        or deidentification.get("pii_type_counts")
        != dict(sorted(detected_pii_types.items()))
    ):
        raise ValueError("phase0 deidentification summary does not match its cases")


def _unique_rows_by_id(rows: Sequence[Any], *, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{label} rows must be objects")
        case_id = str(row.get("id") or "").strip()
        if not case_id:
            raise ValueError(f"{label} row has no case ID")
        if case_id in result:
            raise ValueError(f"{label} contains duplicate case IDs")
        result[case_id] = row
    return result


def _validate_owner_join(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != OWNER_JOIN_FIELDS:
        raise ValueError("phase0 owner join must use the exact safe schema")
    if value.get("contract") != OWNER_JOIN_CONTRACT_VERSION:
        raise ValueError("phase0 owner join contract is invalid")
    status = value.get("status")
    count_fields = (
        "declared_ids_total",
        "matched_source_total",
        "unmatched_source_total",
        "matched_eligible_total",
        "matched_social_only_total",
    )
    if status == "not_provided":
        if value.get("completeness") != "unavailable" or any(
            value.get(field) is not None
            for field in (*count_fields, "list_sha256", "source_format")
        ):
            raise ValueError("phase0 unavailable owner join is internally inconsistent")
        return
    if status != "joined":
        raise ValueError("phase0 owner join status is invalid")
    counts = {field: value.get(field) for field in count_fields}
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for count in counts.values()
    ):
        raise ValueError("phase0 owner join counts are invalid")
    if any(
        (
            counts["declared_ids_total"] != EXPECTED_OWNER_NO_CONTINUATION_IDS,
            counts["matched_source_total"] + counts["unmatched_source_total"]
            != EXPECTED_OWNER_NO_CONTINUATION_IDS,
            counts["matched_eligible_total"] + counts["matched_social_only_total"]
            != counts["matched_source_total"],
            value.get("completeness")
            not in {
                "complete_for_current_source",
                "partial_external_population_join",
            },
            _SHA256_RE.fullmatch(str(value.get("list_sha256") or "")) is None,
            value.get("source_format") not in {"json", "csv", "xlsx"},
        )
    ):
        raise ValueError("phase0 joined owner membership is internally inconsistent")
    expected_completeness = (
        "complete_for_current_source"
        if counts["unmatched_source_total"] == 0
        else "partial_external_population_join"
    )
    if value.get("completeness") != expected_completeness:
        raise ValueError("phase0 owner join completeness does not match its counts")


def _owner_join_projection(value: Any) -> dict[str, Any]:
    owner_join = value if isinstance(value, Mapping) else {}
    count_fields = (
        "declared_ids_total",
        "matched_source_total",
        "unmatched_source_total",
        "matched_eligible_total",
        "matched_social_only_total",
    )
    normalized_counts = {
        field: (
            owner_join.get(field)
            if isinstance(owner_join.get(field), int)
            and not isinstance(owner_join.get(field), bool)
            else None
        )
        for field in count_fields
    }
    complementary_suppression = any(
        count is not None and 0 <= count < SAFE_SLICE_MIN_CASES
        for count in normalized_counts.values()
    )
    visible_counts = {
        field: None if complementary_suppression else count
        for field, count in normalized_counts.items()
    }
    suppressed_fields = (
        list(count_fields) if complementary_suppression else []
    )
    return {
        "contract": owner_join.get("contract"),
        "status": owner_join.get("status"),
        "completeness": (
            None
            if complementary_suppression
            else owner_join.get("completeness")
        ),
        "completeness_suppressed": complementary_suppression,
        "list_sha256": owner_join.get("list_sha256"),
        "source_format": owner_join.get("source_format"),
        "counts": visible_counts,
        "suppressed_count_fields": sorted(suppressed_fields),
        "minimum_public_cell_size": SAFE_SLICE_MIN_CASES,
        "complementary_suppression": complementary_suppression,
    }


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_finite_number(value: Any) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number >= 0 else None


def _nonnegative_number_total(values: Sequence[Any]) -> float | None:
    numbers = [_nonnegative_finite_number(value) for value in values]
    if any(number is None for number in numbers):
        return None
    return round(sum(float(number) for number in numbers if number is not None), 6)


def _bool_rate(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    if not rows:
        return None
    return sum(row.get(field) is True for row in rows) / len(rows)


def _optional_bool_rate(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    scored = [row[field] for row in rows if type(row.get(field)) is bool]
    if not scored:
        return None
    return sum(value is True for value in scored) / len(scored)


def _safe_optional_bool_rate(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> float | None:
    return _safe_binary_rate_summary(rows, field)["unweighted_rate"]


def _weighted_bool_rate(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    scored = [row for row in rows if type(row.get(field)) is bool]
    if not scored:
        return None
    denominator = sum(float(row["post_stratification_weight"]) for row in scored)
    if denominator <= 0:
        return None
    numerator = sum(
        float(row["post_stratification_weight"])
        for row in scored
        if row[field] is True
    )
    return numerator / denominator


def _rate_summary(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    scored = [row for row in rows if type(row.get(field)) is bool]
    return {
        "scored": len(scored),
        "true": sum(row[field] is True for row in scored),
        "unweighted_rate": _optional_bool_rate(rows, field),
        "post_stratified_rate": _weighted_bool_rate(rows, field),
    }


def _phase0_execution_binding_checks(
    manifest: Mapping[str, Any],
    ask_report: Mapping[str, Any],
    *,
    expected_cases_sha: str,
) -> dict[str, dict[str, Any]]:
    telemetry_git_sha = str(
        (manifest.get("telemetry") or {}).get("git_sha") or ""
    )
    runtime_identity = ask_report.get("runtime_identity")
    runtime = runtime_identity if isinstance(runtime_identity, Mapping) else {}
    runtime_fields = (
        "expected_runtime_git_sha",
        "preflight_release_git_sha",
        "postflight_release_git_sha",
        "verified_release_git_sha",
    )
    runtime_matched = sum(
        runtime.get(field) == telemetry_git_sha for field in runtime_fields
    )
    runtime_passed = bool(
        runtime.get("required") is True
        and runtime.get("status") == "verified"
        and runtime.get("matched_expected_runtime") is True
        and runtime_matched == len(runtime_fields)
    )

    phase0_run_value = ask_report.get("phase0_run")
    phase0_run = (
        phase0_run_value if isinstance(phase0_run_value, Mapping) else {}
    )
    manifest_binding_sha256 = _canonical_sha256(manifest)
    manifest_file_sha256 = str(
        phase0_run.get("manifest_file_sha256") or ""
    )
    phase0_run_passed = bool(
        phase0_run.get("status") == "completed"
        and phase0_run.get("completed") is True
        and phase0_run.get("expected_cases_total") == PHASE0_SAMPLE_SIZE
        and phase0_run.get("executed_cases_total") == PHASE0_SAMPLE_SIZE
        and phase0_run.get("cases_file_sha256") == expected_cases_sha
        and _SHA256_RE.fullmatch(manifest_file_sha256) is not None
        and manifest_file_sha256 != "0" * 64
        and phase0_run.get("manifest_binding_sha256")
        == manifest_binding_sha256
        and phase0_run.get("ordered_selection_sha256")
        == EXPECTED_ORDERED_SELECTION_SHA256
        and phase0_run.get("runtime_git_sha") == telemetry_git_sha
        and phase0_run.get("approval_id") == PHASE0_APPROVAL_ID
        and phase0_run.get("cost_scope") == "phase0-social-30"
        and phase0_run.get("integrity_failures") == []
        and phase0_run.get("selective_reruns_forbidden") is True
    )

    eval_run_id = str(ask_report.get("eval_run_id") or "")
    cost_control_value = ask_report.get("cost_control")
    cost_control = (
        cost_control_value if isinstance(cost_control_value, Mapping) else {}
    )
    reservation_value = cost_control.get("reservation")
    reservation = reservation_value if isinstance(reservation_value, Mapping) else {}
    cost_control_passed = bool(
        cost_control.get("strict_live") is True
        and cost_control.get("high_cost_approval_id") == PHASE0_APPROVAL_ID
        and cost_control.get("pricing_complete") is True
        and ask_report.get("llm_budget_rub") == PHASE0_COST_CAP_RUB
        and ask_report.get("llm_budget_exceeded") is False
        and ask_report.get("llm_budget_stopped") is not True
        and ask_report.get("llm_pricing_stopped") is not True
    )
    reservation_passed = bool(
        eval_run_id
        and reservation.get("valid") is True
        and reservation.get("run_id") == eval_run_id
        and reservation.get("scope") == "phase0-social-30"
        and reservation.get("runtime_git_sha") == telemetry_git_sha
        and reservation.get("manifest_sha256") == expected_cases_sha
        and reservation.get("cases_file_sha256") == expected_cases_sha
        and reservation.get("manifest_matches_cases_file") is True
        and reservation.get("case_count") == PHASE0_SAMPLE_SIZE
        and reservation.get("approved_cap_rub") == PHASE0_COST_CAP_RUB
        and reservation.get("approval_required") is True
        and reservation.get("high_cost_approval_id") == PHASE0_APPROVAL_ID
    )
    result_cost = _nonnegative_number_total(
        [row.get("llm_estimated_cost_rub") for row in ask_report.get("results", [])]
        if isinstance(ask_report.get("results"), list)
        else []
    )
    report_cost = _nonnegative_finite_number(
        ask_report.get("llm_estimated_cost_rub")
    )
    estimated_cost_passed = bool(
        result_cost is not None
        and report_cost is not None
        and math.isclose(result_cost, report_cost, rel_tol=1e-9, abs_tol=1e-6)
        and report_cost <= PHASE0_COST_CAP_RUB
    )

    report_results_value = ask_report.get("results")
    report_results = (
        report_results_value if isinstance(report_results_value, list) else []
    )
    trace_found = sum(
        isinstance(row, Mapping) and row.get("trace_found") is True
        for row in report_results
    )
    trace_binding_match = sum(
        isinstance(row, Mapping) and row.get("trace_binding_match") is True
        for row in report_results
    )
    trace_run_id_match = sum(
        isinstance(row, Mapping)
        and isinstance(row.get("trace_eval_run_id"), str)
        and row.get("trace_eval_run_id") == eval_run_id
        for row in report_results
    )
    trace_case_id_match = sum(
        isinstance(row, Mapping)
        and isinstance(row.get("id"), str)
        and isinstance(row.get("trace_eval_case_id"), str)
        and row.get("trace_eval_case_id") == row.get("id")
        for row in report_results
    )
    trace_binding_passed = bool(
        eval_run_id
        and len(report_results) == PHASE0_SAMPLE_SIZE
        and trace_found == PHASE0_SAMPLE_SIZE
        and trace_binding_match == PHASE0_SAMPLE_SIZE
        and trace_run_id_match == PHASE0_SAMPLE_SIZE
        and trace_case_id_match == PHASE0_SAMPLE_SIZE
    )

    query_error_fields_present = sum(
        isinstance(row, Mapping) and "error" in row for row in report_results
    )
    query_errors_clear = sum(
        isinstance(row, Mapping)
        and "error" in row
        and row.get("error") in (None, "")
        for row in report_results
    )
    query_errors_passed = bool(
        len(report_results) == PHASE0_SAMPLE_SIZE
        and query_error_fields_present == PHASE0_SAMPLE_SIZE
        and query_errors_clear == PHASE0_SAMPLE_SIZE
    )

    cardinality_value = ask_report.get("trace_cardinality")
    cardinality = cardinality_value if isinstance(cardinality_value, Mapping) else {}
    case_counts_value = cardinality.get("case_counts")
    case_counts = case_counts_value if isinstance(case_counts_value, Mapping) else {}
    expected_case_ids = {
        str(row.get("id"))
        for row in report_results
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    single_trace_cases = sum(
        isinstance(case_id, str) and type(count) is int and count == 1
        for case_id, count in case_counts.items()
    )
    missing_case_ids = cardinality.get("missing_case_ids")
    duplicate_case_ids = cardinality.get("duplicate_case_ids")
    unknown_case_ids = cardinality.get("unknown_case_ids")
    cardinality_query_error = ask_report.get("trace_cardinality_error")
    cardinality_passed = bool(
        isinstance(cardinality_value, Mapping)
        and cardinality_query_error in (None, "")
        and cardinality.get("eval_run_id") == eval_run_id
        and type(cardinality.get("expected_cases_total")) is int
        and cardinality.get("expected_cases_total") == PHASE0_SAMPLE_SIZE
        and type(cardinality.get("traces_total")) is int
        and cardinality.get("traces_total") == PHASE0_SAMPLE_SIZE
        and isinstance(case_counts_value, Mapping)
        and set(case_counts) == expected_case_ids
        and len(case_counts) == PHASE0_SAMPLE_SIZE
        and single_trace_cases == PHASE0_SAMPLE_SIZE
        and isinstance(missing_case_ids, list)
        and not missing_case_ids
        and isinstance(duplicate_case_ids, list)
        and not duplicate_case_ids
        and isinstance(unknown_case_ids, list)
        and not unknown_case_ids
        and cardinality.get("expected_request_ids_total") == PHASE0_SAMPLE_SIZE
        and cardinality.get("distinct_request_ids_total") == PHASE0_SAMPLE_SIZE
        and cardinality.get("invalid_expected_request_ids_total") == 0
        and cardinality.get("invalid_observed_request_ids_total") == 0
        and cardinality.get("duplicate_request_ids_total") == 0
        and cardinality.get("missing_request_case_pairs_total") == 0
        and cardinality.get("unexpected_request_case_pairs_total") == 0
        and cardinality.get("request_case_pairs_match") is True
        and cardinality.get("cache_hit_true_total") == 0
        and cardinality.get("cache_hit_false_total") == PHASE0_SAMPLE_SIZE
        and cardinality.get("cache_hit_unknown_total") == 0
    )
    return {
        "phase0_run_binding": {"passed": phase0_run_passed},
        "runtime_identity": {"passed": runtime_passed},
        "cost_control": {"passed": cost_control_passed},
        "cost_reservation": {"passed": reservation_passed},
        "estimated_cost": {"passed": estimated_cost_passed},
        "trace_binding": {"passed": trace_binding_passed},
        "query_error": {"passed": query_errors_passed},
        "trace_cardinality": {"passed": cardinality_passed},
    }


def _phase0_billing_reconciliation(
    manifest: Mapping[str, Any],
    ask_report: Mapping[str, Any],
    billing: Mapping[str, Any] | None,
    *,
    expected_cases_sha: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if billing is None:
        return (
            {
                "status": "pending",
                "maximum_relative_discrepancy": (
                    PHASE0_BILLING_MAX_RELATIVE_DISCREPANCY
                ),
            },
            {"status": "pending", "passed": False},
        )

    expected_fields = {
        "schema_version",
        "approval_id",
        "eval_run_id",
        "runtime_git_sha",
        "cases_file_sha256",
        "attribution_scope",
        "provider_currency",
        "provider_reference",
        "window_started_at",
        "window_ended_at",
        "provider_billed_rub",
        "runner_estimated_rub",
        "hard_cap_rub",
        "verified_at",
    }
    fields_exact = set(billing) == expected_fields
    provider_cost = _nonnegative_finite_number(billing.get("provider_billed_rub"))
    runner_cost = _nonnegative_finite_number(billing.get("runner_estimated_rub"))
    report_cost = _nonnegative_finite_number(
        ask_report.get("llm_estimated_cost_rub")
    )
    discrepancy = _relative_billing_discrepancy(provider_cost, runner_cost)
    run_started = _parse_utc_datetime(ask_report.get("run_started_at"))
    run_completed = _parse_utc_datetime(ask_report.get("run_completed_at"))
    window_started = _parse_utc_datetime(billing.get("window_started_at"))
    window_ended = _parse_utc_datetime(billing.get("window_ended_at"))
    verified_at = _parse_utc_datetime(billing.get("verified_at"))
    telemetry_git_sha = str(
        (manifest.get("telemetry") or {}).get("git_sha") or ""
    )
    provider_reference = str(billing.get("provider_reference") or "").strip()
    time_window_bound = bool(
        run_started is not None
        and run_completed is not None
        and window_started is not None
        and window_ended is not None
        and verified_at is not None
        and window_started <= run_started <= run_completed <= window_ended <= verified_at
    )
    identity_bound = bool(
        billing.get("schema_version") == BILLING_RECONCILIATION_SCHEMA_VERSION
        and billing.get("approval_id") == PHASE0_APPROVAL_ID
        and billing.get("eval_run_id") == ask_report.get("eval_run_id")
        and billing.get("runtime_git_sha") == telemetry_git_sha
        and billing.get("cases_file_sha256") == expected_cases_sha
        and billing.get("attribution_scope") == "dedicated_eval_credential"
        and billing.get("provider_currency") == "RUB"
        and 3 <= len(provider_reference) <= 128
        and billing.get("hard_cap_rub") == PHASE0_COST_CAP_RUB
    )
    cost_bound = bool(
        provider_cost is not None
        and runner_cost is not None
        and report_cost is not None
        and math.isclose(runner_cost, report_cost, rel_tol=1e-9, abs_tol=1e-6)
        and provider_cost <= PHASE0_COST_CAP_RUB
        and discrepancy is not None
        and discrepancy <= PHASE0_BILLING_MAX_RELATIVE_DISCREPANCY
    )
    passed = fields_exact and identity_bound and time_window_bound and cost_bound
    status = "verified" if passed else "invalid"
    summary = {
        "status": status,
        "attribution_scope": (
            "dedicated_eval_credential"
            if billing.get("attribution_scope") == "dedicated_eval_credential"
            else "invalid"
        ),
        "provider_billed_rub": provider_cost,
        "runner_estimated_rub": runner_cost,
        "relative_discrepancy": discrepancy,
        "maximum_relative_discrepancy": PHASE0_BILLING_MAX_RELATIVE_DISCREPANCY,
        "hard_cap_rub": PHASE0_COST_CAP_RUB,
        "provider_reference_present": bool(provider_reference),
    }
    return summary, {
        "status": status,
        "fields_exact": fields_exact,
        "identity_bound": identity_bound,
        "time_window_bound": time_window_bound,
        "cost_bound": cost_bound,
        "passed": passed,
    }


def _relative_billing_discrepancy(
    provider_cost: float | None,
    runner_cost: float | None,
) -> float | None:
    if provider_cost is None or runner_cost is None:
        return None
    if provider_cost == 0:
        return 0.0 if runner_cost == 0 else None
    return abs(provider_cost - runner_cost) / provider_cost


def _parse_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        return None
    return parsed.astimezone(UTC)


def _build_phase0_gate(
    rows: Sequence[Mapping[str, Any]],
    report_classification: Any,
    *,
    execution_binding_checks: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    checks = {
        "sample_size": {
            "passed": len(rows) == PHASE0_SAMPLE_SIZE,
        },
        "http_success": _exact_bool_gate_check(rows, "http_success", True),
        "trace_found": _exact_bool_gate_check(rows, "trace_found", True),
        "cache_hit": _exact_bool_gate_check(rows, "cache_hit", False),
        "observed_behavior": _allowed_value_gate_check(
            rows,
            "observed_behavior",
            {"answer", "clarify", "scope_note", "escalate"},
        ),
        "was_escalated": _typed_bool_gate_check(rows, "was_escalated"),
        "llm_estimated_cost_rub": _nonnegative_number_gate_check(
            rows,
            "llm_estimated_cost_rub",
        ),
        "llm_accounting_present": _exact_bool_gate_check(
            rows,
            "llm_accounting_present",
            True,
        ),
        "analyzer_execution_mode": _analyzer_mode_gate_check(rows),
        "metadata_lookup_attempted": _typed_bool_gate_check(
            rows,
            "metadata_lookup_attempted",
        ),
        "metadata_primary_succeeded": _typed_bool_gate_check(
            rows,
            "metadata_primary_succeeded",
        ),
        "reranker_synthetic_high_score_applied": _typed_bool_gate_check(
            rows,
            "reranker_synthetic_high_score_applied",
        ),
        "source_chunk_applied": _typed_bool_gate_check(
            rows,
            "source_chunk_applied",
        ),
        "hybrid_candidates_present": _typed_bool_gate_check(
            rows,
            "hybrid_candidates_present",
        ),
        "reranker_invoked": _typed_bool_gate_check(rows, "reranker_invoked"),
        "reranker_score_origin": _nonempty_string_gate_check(
            rows,
            "reranker_score_origin",
        ),
        "generator_path": _nonempty_string_gate_check(rows, "generator_path"),
        "report_classification": _report_classification_gate_check(
            report_classification
        ),
        **execution_binding_checks,
    }
    invalid_reasons = [
        name for name, check in checks.items() if check["passed"] is not True
    ]
    valid = not invalid_reasons
    private_rate_summary = _rate_summary(rows, "joint_bypass")
    rate_summary = _safe_binary_rate_summary(rows, "joint_bypass")
    joint_bypass = {
        **rate_summary,
        "primary_metric": "post_stratified_rate",
        "primary_rate": rate_summary["post_stratified_rate"],
        "secondary_metric": "unweighted_rate",
        "secondary_unweighted_rate": rate_summary["unweighted_rate"],
    }
    decision = _joint_bypass_decision(
        private_rate_summary["post_stratified_rate"],
        valid=valid,
    )
    return {
        "valid": valid,
        "status": "valid" if valid else "invalid",
        "invalid_reasons": invalid_reasons,
        "checks": checks,
        "joint_bypass": joint_bypass,
        "decision": decision,
        "decision_bands": {
            "confirmed_minimum": PHASE0_JOINT_CONFIRMED_THRESHOLD,
            "partially_confirmed_minimum": PHASE0_JOINT_PARTIAL_THRESHOLD,
            "below_partial": "refuted_stop",
        },
    }


def _exact_bool_gate_check(
    rows: Sequence[Mapping[str, Any]],
    field: str,
    expected: bool,
) -> dict[str, Any]:
    typed = sum(type(row.get(field)) is bool for row in rows)
    matched = sum(
        type(row.get(field)) is bool and row[field] is expected for row in rows
    )
    return {
        "expected": expected,
        "passed": typed == PHASE0_SAMPLE_SIZE and matched == PHASE0_SAMPLE_SIZE,
    }


def _typed_bool_gate_check(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> dict[str, Any]:
    typed = sum(type(row.get(field)) is bool for row in rows)
    return {"passed": typed == PHASE0_SAMPLE_SIZE}


def _nonnegative_number_gate_check(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> dict[str, Any]:
    typed = sum(_nonnegative_finite_number(row.get(field)) is not None for row in rows)
    return {"passed": typed == PHASE0_SAMPLE_SIZE}


def _nonempty_string_gate_check(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> dict[str, Any]:
    typed = sum(
        isinstance(row.get(field), str) and bool(str(row[field]).strip())
        for row in rows
    )
    return {"passed": typed == PHASE0_SAMPLE_SIZE}


def _allowed_value_gate_check(
    rows: Sequence[Mapping[str, Any]],
    field: str,
    allowed: set[str],
) -> dict[str, Any]:
    matched = sum(row.get(field) in allowed for row in rows)
    return {
        "allowed": sorted(allowed),
        "passed": matched == PHASE0_SAMPLE_SIZE,
    }


def _analyzer_mode_gate_check(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    typed = sum(
        row.get("analyzer_execution_mode") in PHASE0_ANALYZER_MODES
        for row in rows
    )
    return {
        "allowed": sorted(PHASE0_ANALYZER_MODES),
        "passed": typed == PHASE0_SAMPLE_SIZE,
    }


def _report_classification_gate_check(value: Any) -> dict[str, Any]:
    classification = value if isinstance(value, Mapping) else {}
    fields_exact = set(classification) == set(PHASE0_REPORT_CLASSIFICATION)
    matched = sum(
        type(classification.get(field)) is type(expected)
        and classification.get(field) == expected
        for field, expected in PHASE0_REPORT_CLASSIFICATION.items()
    )
    required = len(PHASE0_REPORT_CLASSIFICATION)
    return {
        "fields_exact": fields_exact,
        "passed": (
            isinstance(value, Mapping) and fields_exact and matched == required
        ),
    }


def _report_classification_projection(value: Any) -> dict[str, Any]:
    classification = value if isinstance(value, Mapping) else {}
    return {
        field: classification.get(field)
        for field in PHASE0_REPORT_CLASSIFICATION
    }


def _joint_bypass_decision(rate: Any, *, valid: bool) -> str:
    primary_rate = _finite_number(rate)
    if not valid or primary_rate is None or not 0.0 <= primary_rate <= 1.0:
        return "invalid"
    if primary_rate >= PHASE0_JOINT_CONFIRMED_THRESHOLD:
        return "confirmed"
    if primary_rate >= PHASE0_JOINT_PARTIAL_THRESHOLD:
        return "partially_confirmed"
    return "refuted_stop"


def _value_rate(
    rows: Sequence[Mapping[str, Any]],
    field: str,
    expected: str,
) -> float | None:
    scored = [row.get(field) for row in rows if row.get(field) is not None]
    if not scored:
        return None
    matching = sum(str(value) == expected for value in scored)
    nonmatching = len(scored) - matching
    if (
        matching < SAFE_SLICE_MIN_CASES
        or nonmatching < SAFE_SLICE_MIN_CASES
    ):
        return None
    return matching / len(scored)


def _number_summary(values: Iterable[Any]) -> dict[str, float | int | None]:
    numbers = sorted(
        number for value in values if (number := _finite_number(value)) is not None
    )
    if len(numbers) < SAFE_SLICE_MIN_CASES:
        return {"count": None, "avg": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(numbers),
        "avg": sum(numbers) / len(numbers),
        "p50": _percentile(numbers, 50),
        "p95": _percentile(numbers, 95),
        "max": numbers[-1],
    }


def _percentile(values: Sequence[float], percentile: int) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return values[lower] * (1 - fraction) + values[upper] * fraction


def _safe_public_outcome_summaries(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    containment = _safe_binary_rate_summary(rows, "containment")
    return {
        "behavior_counts": {
            "cells": {},
            "suppressed": {
                "minimum_cell_size": SAFE_SLICE_MIN_CASES,
                "cells": None,
                "complementary": True,
                "applied": True,
                "reason": "cross_metric_protection",
            },
        },
        "answer_no_operator": _withheld_top_level_binary_rate_summary(
            rows,
            "answer_no_operator",
        ),
        "clarification": _withheld_top_level_binary_rate_summary(
            rows,
            "clarification",
        ),
        "containment": containment,
        "escalation": _withheld_top_level_binary_rate_summary(
            rows,
            "was_escalated",
        ),
        "suppression": {
            "applied": True,
            "minimum_cell_size": SAFE_SLICE_MIN_CASES,
            "policy": "phase0_public_primary_outcome_only_v1",
        },
    }


def _safe_categorical_counts(
    counts: Mapping[str, int],
    *,
    namespace: str,
) -> dict[str, Any]:
    projected: Counter[str] = Counter()
    for label, count in counts.items():
        projected[_public_categorical_label(namespace, label)] += int(count)
    visible, suppression = _complementary_suppress_counts(projected)
    return {
        "cells": dict(sorted(visible.items())),
        "suppressed": suppression,
    }


def _safe_categorical_matrix(
    matrix: Mapping[str, Mapping[str, int]],
) -> dict[str, Any]:
    projected: Counter[tuple[str, str]] = Counter()
    for row_label, columns in matrix.items():
        safe_row = (
            "source_member" if str(row_label) == "True" else "not_source_member"
        )
        for column_label, count in columns.items():
            safe_column = _public_categorical_label(
                "behavior",
                column_label,
            )
            projected[(safe_row, safe_column)] += int(count)
    flat_visible, suppression = _complementary_suppress_counts(projected)
    visible: dict[str, dict[str, int]] = defaultdict(dict)
    for (row_label, column_label), count in sorted(flat_visible.items()):
        visible[row_label][column_label] = count
    return {
        "cells": dict(visible),
        "suppressed": suppression,
    }


def _safe_slice(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    raw_labels = {
        "<null>" if row.get(field) is None else str(row.get(field))
        for row in rows
    }
    opaque_forum_labels = (
        _opaque_public_labels(raw_labels - {"<null>"}, prefix="forum")
        if field == "source_forum"
        else {}
    )
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        value = row.get(field)
        label = _public_slice_label(
            field,
            value,
            opaque_forum_labels=opaque_forum_labels,
        )
        grouped[label].append(row)
    visible_sizes, suppression = _complementary_suppress_counts(
        {label: len(group) for label, group in grouped.items()}
    )
    if field == "source_forum_presence":
        visible_sizes = {}
        suppression = {
            "minimum_cell_size": SAFE_SLICE_MIN_CASES,
            "cells": len(grouped),
            "complementary": True,
            "applied": True,
            "reason": "overlapping_margin_protection",
        }
    visible: dict[str, Any] = {}
    for label in sorted(visible_sizes):
        group = grouped[label]
        visible[label] = {
            "cases": len(group),
            "answer_no_operator": _withheld_slice_binary_rate_summary(
                group,
                "answer_no_operator",
            ),
            "escalation": _withheld_slice_binary_rate_summary(
                group,
                "was_escalated",
            ),
        }
    return {
        "groups": visible,
        "suppressed": {
            "minimum_group_size": suppression["minimum_cell_size"],
            "groups": suppression["cells"],
            "complementary": suppression["complementary"],
            "applied": suppression["applied"],
            "reason": suppression.get("reason"),
        },
    }


def _public_categorical_label(namespace: str, value: Any) -> str:
    label = str(value)
    allowlists = {
        "behavior": PUBLIC_BEHAVIOR_LABELS,
        "escalation_reason": PUBLIC_ESCALATION_REASON_LABELS,
        "generator_model": PUBLIC_GENERATOR_MODEL_LABELS,
        "generator_path": PUBLIC_GENERATOR_PATH_LABELS,
        "reranker_score_origin": PUBLIC_RERANKER_SCORE_ORIGIN_LABELS,
        "stratum": frozenset(PHASE0_STRATUM_QUOTAS),
    }
    allowed = allowlists.get(namespace)
    if allowed is None:
        raise ValueError("unknown public categorical namespace")
    return label if label in allowed else "other_or_redacted"


def _public_slice_label(
    field: str,
    value: Any,
    *,
    opaque_forum_labels: Mapping[str, str],
) -> str:
    if field == "source_channel":
        return str(value) if value in {"vk", "max"} else "other_or_redacted"
    if field == "source_forum_presence":
        return (
            str(value)
            if value in {"forum", "no_forum"}
            else "other_or_redacted"
        )
    if field == "source_dialogue_length_bucket":
        return (
            str(value)
            if value in {"1", "2", "3_plus"}
            else "other_or_redacted"
        )
    if field == "source_category":
        if value is None:
            return "category_unknown"
        normalized = _WHITESPACE_RE.sub(
            " ",
            str(value).strip().casefold().replace("-", " "),
        )
        return PUBLIC_CATEGORY_ALIASES.get(
            normalized,
            "category_other_or_redacted",
        )
    if field == "source_forum":
        if value is None:
            return "no_forum"
        return opaque_forum_labels.get(str(value), "forum_other_or_redacted")
    raise ValueError("unknown public slice field")


def _opaque_public_labels(
    labels: Iterable[str],
    *,
    prefix: str,
) -> dict[str, str]:
    ordered = sorted(
        set(labels),
        key=lambda label: hashlib.sha256(
            f"phase0-public-{prefix}-v1\0{label}".encode()
        ).hexdigest(),
    )
    return {
        label: f"{prefix}_{index:02d}"
        for index, label in enumerate(ordered, start=1)
    }


def _complementary_suppress_counts(
    counts: Mapping[Any, int],
) -> tuple[dict[Any, int], dict[str, Any]]:
    normalized = {key: int(count) for key, count in counts.items()}
    suppressed = {
        key
        for key, count in normalized.items()
        if count < SAFE_SLICE_MIN_CASES
    }
    complementary = bool(suppressed and set(normalized) - suppressed)
    if suppressed:
        suppressed = set(normalized)
    visible = {
        key: count
        for key, count in normalized.items()
        if key not in suppressed
    }
    return visible, {
        "minimum_cell_size": SAFE_SLICE_MIN_CASES,
        "cells": len(suppressed),
        "complementary": complementary,
        "applied": bool(suppressed),
    }


def _withheld_slice_binary_rate_summary(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> dict[str, Any]:
    scored = sum(type(row.get(field)) is bool for row in rows)
    return {
        "scored": scored if scored >= SAFE_SLICE_MIN_CASES else None,
        "true": None,
        "false": None,
        "unweighted_rate": None,
        "post_stratified_rate": None,
        "suppression": {
            "applied": True,
            "minimum_outcome_cell_size": SAFE_SLICE_MIN_CASES,
            "primary_cell": "true",
            "complementary_cell": "false",
            "policy": "public_slice_outcomes_withheld",
        },
    }


def _withheld_top_level_binary_rate_summary(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> dict[str, Any]:
    scored = sum(type(row.get(field)) is bool for row in rows)
    return {
        "scored": scored if scored >= SAFE_SLICE_MIN_CASES else None,
        "true": None,
        "false": None,
        "unweighted_rate": None,
        "post_stratified_rate": None,
        "suppression": {
            "applied": True,
            "minimum_outcome_cell_size": SAFE_SLICE_MIN_CASES,
            "primary_cell": "true",
            "complementary_cell": "false",
            "policy": "phase0_public_primary_outcome_only_v1",
        },
    }


def _safe_binary_rate_summary(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> dict[str, Any]:
    scored = [row for row in rows if type(row.get(field)) is bool]
    true_count = sum(row[field] is True for row in scored)
    false_count = len(scored) - true_count
    cells_visible = (
        len(scored) >= SAFE_SLICE_MIN_CASES
        and
        true_count >= SAFE_SLICE_MIN_CASES
        and false_count >= SAFE_SLICE_MIN_CASES
    )
    return {
        "scored": (
            len(scored) if len(scored) >= SAFE_SLICE_MIN_CASES else None
        ),
        "true": true_count if cells_visible else None,
        "false": false_count if cells_visible else None,
        "unweighted_rate": (
            _optional_bool_rate(rows, field) if cells_visible else None
        ),
        "post_stratified_rate": (
            _weighted_bool_rate(rows, field) if cells_visible else None
        ),
        "suppression": {
            "applied": not cells_visible,
            "minimum_outcome_cell_size": SAFE_SLICE_MIN_CASES,
            "primary_cell": "true",
            "complementary_cell": "false",
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--source", type=Path, default=DEFAULT_SOURCE_PATH)
    build.add_argument("--owner-no-continuation", type=Path)
    build.add_argument("--owner-id-column", default=OWNER_ID_COLUMN)
    build.add_argument("--owner-sheet", default="")
    build.add_argument("--cases-output", type=Path, required=True)
    build.add_argument("--manifest-output", type=Path, required=True)
    build.add_argument("--telemetry-git-sha", required=True)

    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("--manifest", type=Path, required=True)
    summarize.add_argument("--ask-report", type=Path, required=True)
    summarize.add_argument("--billing-reconciliation", type=Path, required=True)
    summarize.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    if args.command == "build":
        summary = build_phase0_artifacts(
            source_path=args.source,
            cases_output_path=args.cases_output,
            manifest_output_path=args.manifest_output,
            owner_ids_path=args.owner_no_continuation,
            owner_id_column=args.owner_id_column,
            owner_sheet_name=args.owner_sheet or None,
            seed=PHASE0_SELECTION_SEED,
            telemetry_git_sha=args.telemetry_git_sha,
        )
    else:
        summary = write_safe_phase0_metrics(
            manifest_path=args.manifest,
            ask_report_path=args.ask_report,
            billing_reconciliation_path=args.billing_reconciliation,
            output_path=args.output,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
