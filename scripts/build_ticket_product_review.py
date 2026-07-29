from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DATA_ROOT = PROJECT_ROOT / "data" / "private"

DEFAULT_INPUT = Path(
    "data/private/tickets/product_baseline_20260728/product_calibration_cases.jsonl"
)
DEFAULT_SUMMARY_NAME = "top20_review_summary.csv"
DEFAULT_MANIFEST_NAME = "top20_review_manifest.csv"
DEFAULT_TOP_N = 20
DEFAULT_MIN_PER_STRATUM = 10
DEFAULT_TOTAL = 300
DEFAULT_SPLIT = "calibration"
DEFAULT_SELECTION_MODE = "frequency_risk"

SUMMARY_FIELDS = (
    "rank",
    "intent",
    "aspect",
    "entity_class",
    "cases",
    "share",
    "unique_clusters",
    "largest_cluster",
    "channels_count",
    "answer_count",
    "clarify_count",
    "escalate_count",
    "time_sensitive_count",
    "review_quota",
    "relabel_risk",
)
MANIFEST_FIELDS = (
    "case_id_hash",
    "duplicate_cluster_id",
    "source_schema_version",
    "source_case_fingerprint",
    "stratum_rank",
    "intent",
    "aspect",
    "entity_class",
    "channel",
    "time_bucket",
    "expected_route",
    "expected_escalation_reason",
    "time_sensitive",
    "difficulty",
    "role_reconstruction_status",
    "multiturn_status",
    "reviewer",
    "reviewed_at",
    "role_verdict",
    "label_verdict",
    "corrected_intent",
    "corrected_aspect",
    "corrected_entity_class",
    "corrected_route",
    "corrected_escalation_reason",
    "deidentified_query",
    "privacy_verdict",
    "date_privacy_verdict",
    "review_workbook_sha256",
    "review_source_sha256",
    "review_selection_sha256",
    "review_freeze_contract_sha256",
    "review_payload_sha256",
    "answerable_from_snapshot",
    "approved_chunk_ids",
    "approved_kb_seed_sha256",
    "forbidden_profiles",
    "include_in_calibration",
    "include_in_validation",
    "include_in_holdout",
)

_HASH_RE = re.compile(r"^[0-9a-f]{12,64}$")
_LABEL_RE = re.compile(r"^[\w-]{1,80}$", re.UNICODE)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
_MONTH_RE = re.compile(r"^(20\d{2})-(0[1-9]|1[0-2])")
_SAFE_CHANNELS = {
    "api",
    "hde",
    "vk",
    "web",
    "telegram",
    "whatsapp",
    "Вконтакте",
    "ВК Умный Бот",
    "ЕСЗ Текстовая линия",
    "MAX Бот",
}
_SAFE_ROUTES = {"answer", "clarify", "escalate"}
_SAFE_DIFFICULTIES = {"simple", "medium", "complex"}
_SAFE_ROLE_STATUSES = {
    "complete",
    "partial",
    "unresolved",
    "ambiguous",
    "unknown",
    "not_available",
}
_SAFE_MULTITURN_STATUSES = {"single_turn", "multi_turn", "unknown", "not_available"}
_SAFE_SPLITS = {"calibration", "validation", "holdout"}
_SAFE_SELECTION_MODES = {
    "frequency",
    "frequency_risk",
    "profile_route_frequency",
}


@dataclass(frozen=True, order=True, slots=True)
class StratumKey:
    intent: str
    aspect: str
    entity_class: str


@dataclass(frozen=True, slots=True)
class ReviewCase:
    case_id_hash: str
    duplicate_cluster_id: str
    source_schema_version: str
    source_case_fingerprint: str
    stratum: StratumKey
    channel: str
    time_bucket: str
    expected_route: str
    expected_escalation_reason: str
    time_sensitive: bool
    difficulty: str
    role_reconstruction_status: str
    multiturn_status: str
    answerable_from_snapshot: bool | None
    approved_chunk_ids: tuple[str, ...]
    forbidden_profiles: tuple[str, ...]
    label_status: str

    @property
    def risk_score(self) -> int:
        score = {"answer": 0, "clarify": 2, "escalate": 4}[self.expected_route]
        score += 3 if self.time_sensitive else 0
        category, _, topic = self.stratum.intent.partition(".")
        score += 3 if category == "другое" else 0
        score += 2 if topic == "прочее" else 0
        score += 1 if self.stratum.aspect == "generic" else 0
        score += 2 if self.stratum.entity_class == "forum:unspecified" else 0
        score += 1 if self.label_status != "human_reviewed" else 0
        score += int(self.role_reconstruction_status != "complete")
        return score


@dataclass(frozen=True, slots=True)
class Stratum:
    rank: int
    key: StratumKey
    cases: tuple[ReviewCase, ...]
    representatives: tuple[ReviewCase, ...]

    @property
    def weight(self) -> int:
        return len(self.cases) * 10 + sum(case.risk_score for case in self.cases)


def build_review_exports(
    input_path: Path,
    summary_path: Path,
    manifest_path: Path,
    *,
    top_n: int = DEFAULT_TOP_N,
    min_per_stratum: int = DEFAULT_MIN_PER_STRATUM,
    total: int = DEFAULT_TOTAL,
    split: str = DEFAULT_SPLIT,
    selection_mode: str = DEFAULT_SELECTION_MODE,
    multiturn_status: str | None = None,
    overwrite: bool = False,
) -> dict[str, int]:
    """Build metadata-only review queues from one private product split."""

    _validate_options(
        input_path,
        summary_path,
        manifest_path,
        top_n=top_n,
        min_per_stratum=min_per_stratum,
        total=total,
        split=split,
        selection_mode=selection_mode,
        multiturn_status=multiturn_status,
    )
    if not overwrite:
        existing = [
            path
            for path in (summary_path, manifest_path)
            if path.exists()
        ]
        if existing:
            raise ValueError(
                "Review output already exists; use explicit overwrite: "
                + ", ".join(str(path) for path in existing)
            )
    source_cases = read_calibration_cases(input_path, expected_split=split)
    cases = [
        case
        for case in source_cases
        if multiturn_status is None or case.multiturn_status == multiturn_status
    ]
    if not cases:
        raise ValueError("No cases remain after the multiturn_status filter")
    all_strata = _build_top_strata(cases, top_n=len(cases))
    strata = all_strata[:top_n]
    if selection_mode == "profile_route_frequency":
        if min_per_stratum != 0:
            raise ValueError(
                "profile_route_frequency requires min_per_stratum=0"
            )
        selected = _select_cases_by_profile_route(cases, total=total)
        manifest_strata = all_strata
        summary_strata = all_strata
    else:
        selected = _select_cases(
            strata,
            min_per_stratum=min_per_stratum,
            total=total,
            selection_mode=selection_mode,
        )
        manifest_strata = strata
        summary_strata = strata
    summary_rows = _build_summary_rows(
        summary_strata,
        selected,
        total_cases=len(cases),
    )
    manifest_rows = _build_manifest_rows(manifest_strata, selected)

    _write_csv(summary_path, SUMMARY_FIELDS, summary_rows, overwrite=overwrite)
    _write_csv(manifest_path, MANIFEST_FIELDS, manifest_rows, overwrite=overwrite)

    return {
        "input_cases": len(source_cases),
        "eligible_cases": len(cases),
        "strata_total": len({case.stratum for case in cases}),
        "top_strata": len(strata),
        "selected_cases": len(selected),
        "unique_clusters": len({case.duplicate_cluster_id for case in cases}),
        "summary_rows": len(summary_rows),
        "manifest_rows": len(manifest_rows),
    }


def read_calibration_cases(
    path: Path,
    *,
    expected_split: str = DEFAULT_SPLIT,
) -> list[ReviewCase]:
    cases: list[ReviewCase] = []
    seen_case_ids: set[str] = set()
    try:
        file = path.open("r", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"Could not read calibration input: {path}") from exc

    with file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"Invalid JSON object at line {line_number}") from None
            if not isinstance(payload, dict):
                raise ValueError(f"Line {line_number} must contain a JSON object")
            case = _case_from_payload(
                payload,
                line_number=line_number,
                expected_split=expected_split,
            )
            if case.case_id_hash in seen_case_ids:
                raise ValueError(f"Duplicate ticket_id_hash at line {line_number}")
            seen_case_ids.add(case.case_id_hash)
            cases.append(case)

    if not cases:
        raise ValueError("Calibration input is empty")
    return cases


def _case_from_payload(
    payload: dict[str, Any],
    *,
    line_number: int,
    expected_split: str = DEFAULT_SPLIT,
) -> ReviewCase:
    if payload.get("split") != expected_split:
        raise ValueError(
            f"Case at line {line_number} does not belong to split {expected_split!r}"
        )
    for flag in ("operator_answer_included", "operator_answer_used_as_fact"):
        if flag not in payload:
            raise ValueError(f"Missing required safety flag {flag!r} at line {line_number}")
        value = payload[flag]
        if not isinstance(value, bool):
            raise ValueError(f"Safety flag {flag!r} must be boolean at line {line_number}")
        if value:
            raise ValueError(f"Unsafe operator-answer flag {flag!r} at line {line_number}")

    case_id_hash = _required_hash(payload.get("ticket_id_hash"), "ticket_id_hash", line_number)
    cluster_id = _required_hash(
        payload.get("duplicate_cluster_id"),
        "duplicate_cluster_id",
        line_number,
    )
    category = _required_label(payload.get("category"), "category", line_number)
    topic = _required_label(payload.get("topic"), "topic", line_number)
    aspect = _required_label(
        payload.get("expected_response_profile"),
        "expected_response_profile",
        line_number,
    )
    entity_present = _entity_is_present(payload.get("entity"), line_number=line_number)
    entity_class = _entity_class(category, entity_present=entity_present)
    expected_route = str(payload.get("expected_route") or "")
    if expected_route not in _SAFE_ROUTES:
        raise ValueError(f"Invalid expected_route at line {line_number}")

    return ReviewCase(
        case_id_hash=case_id_hash,
        duplicate_cluster_id=cluster_id,
        source_schema_version=_source_schema_version(
            payload.get("schema_version"),
            line_number=line_number,
        ),
        source_case_fingerprint=source_case_fingerprint(payload),
        stratum=StratumKey(
            intent=f"{category}.{topic}",
            aspect=aspect,
            entity_class=entity_class,
        ),
        channel=_safe_channel(payload.get("channel")),
        time_bucket=_time_bucket(
            payload.get("available_at") or payload.get("first_timestamp")
        ),
        expected_route=expected_route,
        expected_escalation_reason=_optional_label(
            payload.get("expected_escalation_reason"),
            field="expected_escalation_reason",
            line_number=line_number,
        ),
        time_sensitive=_optional_bool(
            payload.get("time_sensitive"),
            field="time_sensitive",
            line_number=line_number,
            default=False,
        ),
        difficulty=_safe_enum(
            payload.get("difficulty"),
            allowed=_SAFE_DIFFICULTIES,
            default="not_available",
        ),
        role_reconstruction_status=_safe_enum(
            payload.get("role_reconstruction_status"),
            allowed=_SAFE_ROLE_STATUSES,
            default="not_available",
        ),
        multiturn_status=_safe_enum(
            payload.get("multiturn_status"),
            allowed=_SAFE_MULTITURN_STATUSES,
            default="not_available",
        ),
        answerable_from_snapshot=_nullable_bool(
            payload.get("answerable_from_snapshot"),
            field="answerable_from_snapshot",
            line_number=line_number,
        ),
        approved_chunk_ids=_safe_id_list(
            payload.get("approved_chunk_ids"),
            field="approved_chunk_ids",
            line_number=line_number,
        ),
        forbidden_profiles=_safe_label_list(
            payload.get("forbidden_response_profiles"),
            field="forbidden_response_profiles",
            line_number=line_number,
        ),
        label_status=_safe_label_status(payload.get("label_status")),
    )


def _build_top_strata(cases: list[ReviewCase], *, top_n: int) -> list[Stratum]:
    grouped: dict[StratumKey, list[ReviewCase]] = defaultdict(list)
    for case in cases:
        grouped[case.stratum].append(case)

    ranked = sorted(
        grouped.items(),
        key=lambda item: (
            -len(item[1]),
            item[0].intent,
            item[0].aspect,
            item[0].entity_class,
        ),
    )[:top_n]
    strata: list[Stratum] = []
    for rank, (key, group_cases) in enumerate(ranked, start=1):
        ordered_cases = tuple(sorted(group_cases, key=_case_sort_key))
        representative_by_cluster: dict[str, ReviewCase] = {}
        for case in ordered_cases:
            representative_by_cluster.setdefault(case.duplicate_cluster_id, case)
        representatives = tuple(
            sorted(representative_by_cluster.values(), key=_case_sort_key)
        )
        strata.append(
            Stratum(
                rank=rank,
                key=key,
                cases=ordered_cases,
                representatives=representatives,
            )
        )
    return strata


def _select_cases(
    strata: list[Stratum],
    *,
    min_per_stratum: int,
    total: int,
    selection_mode: str = DEFAULT_SELECTION_MODE,
) -> list[ReviewCase]:
    minimum_by_key = {
        stratum.key: min(min_per_stratum, len(stratum.representatives))
        for stratum in strata
    }
    required = sum(minimum_by_key.values())
    if total < required:
        raise ValueError(
            f"total={total} is smaller than the required stratum minimum {required}"
        )

    selected: list[ReviewCase] = []
    selected_keys: set[tuple[StratumKey, str]] = set()

    # Find a deterministic bipartite matching between per-stratum quota slots and
    # globally unique duplicate clusters. A greedy scarce-first pass can still
    # fail even when a valid assignment exists.
    minimum_order = sorted(
        strata,
        key=lambda stratum: (len(stratum.representatives), stratum.rank),
    )
    slots = [
        (stratum.rank, ordinal)
        for stratum in minimum_order
        for ordinal in range(minimum_by_key[stratum.key])
    ]
    stratum_by_rank = {stratum.rank: stratum for stratum in strata}
    cluster_to_slot: dict[str, tuple[int, int]] = {}
    slot_to_case: dict[tuple[int, int], ReviewCase] = {}

    def augment(
        slot: tuple[int, int],
        *,
        visited_clusters: set[str],
    ) -> bool:
        stratum = stratum_by_rank[slot[0]]
        for case in stratum.representatives:
            cluster_id = case.duplicate_cluster_id
            if cluster_id in visited_clusters:
                continue
            visited_clusters.add(cluster_id)
            previous_slot = cluster_to_slot.get(cluster_id)
            if previous_slot is not None and not augment(
                previous_slot,
                visited_clusters=visited_clusters,
            ):
                continue
            cluster_to_slot[cluster_id] = slot
            slot_to_case[slot] = case
            return True
        return False

    for slot in slots:
        if not augment(slot, visited_clusters=set()):
            raise ValueError(
                f"Could not satisfy globally unique minimum for stratum {slot[0]}"
            )

    for slot in slots:
        case = slot_to_case[slot]
        selected.append(case)
        selected_keys.add((case.stratum, case.duplicate_cluster_id))
    used_clusters = {case.duplicate_cluster_id for case in selected}

    available_unique = {
        case.duplicate_cluster_id
        for stratum in strata
        for case in stratum.representatives
    }
    target = min(total, len(available_unique))
    extra_needed = target - len(selected)
    if extra_needed <= 0:
        return sorted(selected, key=lambda case: _selected_sort_key(case, strata))

    extra_candidates: list[tuple[Fraction, int, ReviewCase]] = []
    for stratum in strata:
        remaining = [
            case
            for case in stratum.representatives
            if (stratum.key, case.duplicate_cluster_id) not in selected_keys
        ]
        for ordinal, case in enumerate(remaining, start=1):
            weight = (
                len(stratum.cases)
                if selection_mode == "frequency"
                else stratum.weight
            )
            extra_candidates.append(
                (Fraction(weight, ordinal), stratum.rank, case)
            )
    extra_candidates.sort(
        key=lambda item: (
            -item[0],
            item[1],
            -item[2].risk_score,
            item[2].duplicate_cluster_id,
            item[2].case_id_hash,
        )
    )

    for _, _, case in extra_candidates:
        if case.duplicate_cluster_id in used_clusters:
            continue
        selected.append(case)
        used_clusters.add(case.duplicate_cluster_id)
        if len(selected) == target:
            break

    if len(selected) != target:
        raise ValueError("Could not reach target with globally unique duplicate clusters")
    return sorted(selected, key=lambda case: _selected_sort_key(case, strata))


def _select_cases_by_profile_route(
    cases: list[ReviewCase],
    *,
    total: int,
) -> list[ReviewCase]:
    """Select a deterministic traffic-weighted sample without risk oversampling.

    Exact Hamilton margins are fixed independently for profile and route.
    A bounded dynamic program then finds the closest joint allocation that
    satisfies both margins and every observed profile-route cell capacity.
    """

    grouped: dict[tuple[str, str], list[ReviewCase]] = defaultdict(list)
    for case in cases:
        grouped[(case.stratum.aspect, case.expected_route)].append(case)

    available_unique = {case.duplicate_cluster_id for case in cases}
    if len(available_unique) != len(cases):
        raise ValueError(
            "profile_route_frequency requires one case per duplicate cluster"
        )
    target = min(total, len(cases))
    representatives: dict[tuple[str, str], list[ReviewCase]] = {}
    for key, group_cases in sorted(grouped.items()):
        representatives_by_cluster: dict[str, ReviewCase] = {}
        for case in sorted(
            group_cases,
            key=lambda item: (
                item.case_id_hash,
                item.duplicate_cluster_id,
            ),
        ):
            representatives_by_cluster.setdefault(case.duplicate_cluster_id, case)
        representatives[key] = list(representatives_by_cluster.values())

    quotas = _balanced_profile_route_quotas(
        representatives,
        target=target,
        population=len(cases),
    )

    selected = [
        case
        for key in sorted(representatives)
        for case in representatives[key][: quotas.get(key, 0)]
    ]
    if len(selected) != target:
        raise ValueError("Profile-route quota produced an incomplete selection")
    return sorted(
        selected,
        key=lambda case: (
            case.stratum.aspect,
            case.expected_route,
            case.case_id_hash,
        ),
    )


def _balanced_profile_route_quotas(
    representatives: dict[tuple[str, str], list[ReviewCase]],
    *,
    target: int,
    population: int,
) -> dict[tuple[str, str], int]:
    """Round a profile-route matrix while preserving both exact margins."""

    cell_sizes = {
        key: len(group_cases)
        for key, group_cases in representatives.items()
    }
    profiles = sorted({profile for profile, _ in cell_sizes})
    routes = sorted({route for _, route in cell_sizes})
    profile_sizes = {
        profile: sum(
            cell_sizes.get((profile, route), 0)
            for route in routes
        )
        for profile in profiles
    }
    route_sizes = {
        route: sum(
            cell_sizes.get((profile, route), 0)
            for profile in profiles
        )
        for route in routes
    }
    if sum(cell_sizes.values()) != population:
        raise ValueError("Profile-route cells do not cover the full population")

    profile_quotas = _hamilton_quotas(profile_sizes, target=target)
    route_quotas = _hamilton_quotas(route_sizes, target=target)
    zero_state = tuple(0 for _ in routes)
    # State value is the scaled squared joint-cell error plus the flattened
    # allocation path. The path is the deterministic final tie-break.
    states: dict[tuple[int, ...], tuple[int, tuple[int, ...]]] = {
        zero_state: (0, ())
    }
    for profile in profiles:
        capacities = tuple(
            cell_sizes.get((profile, route), 0)
            for route in routes
        )
        allocations = tuple(
            _bounded_row_allocations(
                capacities,
                total=profile_quotas[profile],
            )
        )
        if not allocations:
            raise ValueError(
                f"Profile quota is infeasible for {profile!r}"
            )
        next_states: dict[tuple[int, ...], tuple[int, tuple[int, ...]]] = {}
        for state, (score, path) in states.items():
            for allocation in allocations:
                next_state = tuple(
                    current + increment
                    for current, increment in zip(
                        state,
                        allocation,
                        strict=True,
                    )
                )
                if any(
                    value > route_quotas[route]
                    for value, route in zip(
                        next_state,
                        routes,
                        strict=True,
                    )
                ):
                    continue
                row_score = sum(
                    (
                        allocated * population
                        - cell_sizes.get((profile, route), 0) * target
                    )
                    ** 2
                    for allocated, route in zip(
                        allocation,
                        routes,
                        strict=True,
                    )
                )
                candidate = (score + row_score, path + allocation)
                current = next_states.get(next_state)
                if current is None or candidate < current:
                    next_states[next_state] = candidate
        states = next_states
        if not states:
            raise ValueError("Profile-route margins are jointly infeasible")

    final_state = tuple(route_quotas[route] for route in routes)
    final = states.get(final_state)
    if final is None:
        raise ValueError("Could not satisfy exact profile and route margins")

    path = final[1]
    quotas: dict[tuple[str, str], int] = {}
    offset = 0
    for profile in profiles:
        for route in routes:
            quotas[(profile, route)] = path[offset]
            offset += 1
    return quotas


def _bounded_row_allocations(
    capacities: tuple[int, ...],
    *,
    total: int,
) -> list[tuple[int, ...]]:
    """Enumerate deterministic bounded integer rows with an exact sum."""

    allocations: list[tuple[int, ...]] = []

    def visit(index: int, remaining: int, prefix: tuple[int, ...]) -> None:
        if index == len(capacities):
            if remaining == 0:
                allocations.append(prefix)
            return
        capacity = capacities[index]
        later_capacity = sum(capacities[index + 1 :])
        lower = max(0, remaining - later_capacity)
        upper = min(capacity, remaining)
        for value in range(lower, upper + 1):
            visit(index + 1, remaining - value, prefix + (value,))

    visit(0, total, ())
    return allocations


def _hamilton_quotas(
    group_sizes: dict[str, int] | Counter[str],
    *,
    target: int,
) -> dict[str, int]:
    """Allocate an exact integer target by largest proportional remainders."""

    population = sum(group_sizes.values())
    if target < 0 or target > population:
        raise ValueError("Hamilton target must fit the available population")
    if population == 0:
        if target:
            raise ValueError("Cannot allocate a non-zero empty population")
        return {}

    quotas: dict[str, int] = {}
    remainders: list[tuple[Fraction, str]] = []
    for key, size in sorted(group_sizes.items()):
        exact = Fraction(size * target, population)
        base = exact.numerator // exact.denominator
        quotas[key] = base
        remainders.append((exact - base, key))

    remaining = target - sum(quotas.values())
    remainders.sort(key=lambda item: (-item[0], item[1]))
    for _, key in remainders:
        if remaining <= 0:
            break
        if quotas[key] >= group_sizes[key]:
            continue
        quotas[key] += 1
        remaining -= 1
    if remaining:
        raise ValueError("Could not allocate complete Hamilton quota")
    return quotas


def _build_summary_rows(
    strata: list[Stratum],
    selected: list[ReviewCase],
    *,
    total_cases: int,
) -> list[dict[str, str | int]]:
    quota = Counter(case.stratum for case in selected)
    rows: list[dict[str, str | int]] = []
    for stratum in strata:
        cluster_sizes = Counter(case.duplicate_cluster_id for case in stratum.cases)
        route_counts = Counter(case.expected_route for case in stratum.cases)
        rows.append(
            {
                "rank": stratum.rank,
                "intent": stratum.key.intent,
                "aspect": stratum.key.aspect,
                "entity_class": stratum.key.entity_class,
                "cases": len(stratum.cases),
                "share": f"{len(stratum.cases) / total_cases:.6f}",
                "unique_clusters": len(cluster_sizes),
                "largest_cluster": max(cluster_sizes.values(), default=0),
                "channels_count": len({case.channel for case in stratum.cases}),
                "answer_count": route_counts["answer"],
                "clarify_count": route_counts["clarify"],
                "escalate_count": route_counts["escalate"],
                "time_sensitive_count": sum(
                    case.time_sensitive for case in stratum.cases
                ),
                "review_quota": quota[stratum.key],
                "relabel_risk": _relabel_risk(stratum),
            }
        )
    return rows


def _build_manifest_rows(
    strata: list[Stratum],
    selected: list[ReviewCase],
) -> list[dict[str, str | int]]:
    rank_by_key = {stratum.key: stratum.rank for stratum in strata}
    rows: list[dict[str, str | int]] = []
    for case in selected:
        rows.append(
            {
                "case_id_hash": case.case_id_hash,
                "duplicate_cluster_id": case.duplicate_cluster_id,
                "source_schema_version": case.source_schema_version,
                "source_case_fingerprint": case.source_case_fingerprint,
                "stratum_rank": rank_by_key[case.stratum],
                "intent": case.stratum.intent,
                "aspect": case.stratum.aspect,
                "entity_class": case.stratum.entity_class,
                "channel": case.channel,
                "time_bucket": case.time_bucket,
                "expected_route": case.expected_route,
                "expected_escalation_reason": case.expected_escalation_reason,
                "time_sensitive": _csv_bool(case.time_sensitive),
                "difficulty": case.difficulty,
                "role_reconstruction_status": case.role_reconstruction_status,
                "multiturn_status": case.multiturn_status,
                "reviewer": "",
                "reviewed_at": "",
                "role_verdict": "",
                "label_verdict": "",
                "corrected_intent": "",
                "corrected_aspect": "",
                "corrected_entity_class": "",
                "corrected_route": "",
                "corrected_escalation_reason": "",
                "deidentified_query": "",
                "privacy_verdict": "",
                "date_privacy_verdict": "",
                "review_workbook_sha256": "",
                "review_source_sha256": "",
                "review_selection_sha256": "",
                "review_freeze_contract_sha256": "",
                "review_payload_sha256": "",
                "answerable_from_snapshot": _csv_nullable_bool(
                    case.answerable_from_snapshot
                ),
                "approved_chunk_ids": "|".join(case.approved_chunk_ids),
                "approved_kb_seed_sha256": "",
                "forbidden_profiles": "|".join(case.forbidden_profiles),
                "include_in_calibration": "",
                "include_in_validation": "",
                "include_in_holdout": "",
            }
        )
    return rows


def _case_sort_key(case: ReviewCase) -> tuple[Any, ...]:
    return (
        -case.risk_score,
        case.expected_route,
        not case.time_sensitive,
        case.channel,
        case.time_bucket,
        case.duplicate_cluster_id,
        case.case_id_hash,
    )


def _selected_sort_key(
    case: ReviewCase,
    strata: list[Stratum],
) -> tuple[Any, ...]:
    rank_by_key = {stratum.key: stratum.rank for stratum in strata}
    return (
        rank_by_key[case.stratum],
        -case.risk_score,
        case.duplicate_cluster_id,
        case.case_id_hash,
    )


def _relabel_risk(stratum: Stratum) -> str:
    category, _, topic = stratum.key.intent.partition(".")
    average_risk = sum(case.risk_score for case in stratum.cases) / len(stratum.cases)
    if category == "другое" or topic == "прочее" or average_risk >= 7:
        return "high"
    if stratum.key.aspect == "generic" or average_risk >= 4:
        return "medium"
    return "low"


def _entity_class(category: str, *, entity_present: bool) -> str:
    if category == "форумы":
        return "forum:named" if entity_present else "forum:unspecified"
    if category == "платформа_фгаис":
        return "platform:event-scoped" if entity_present else "platform:generic"
    if category == "гранты":
        return "grant:event-scoped" if entity_present else "grant:generic"
    if category == "техподдержка":
        return "technical:event-scoped" if entity_present else "technical:generic"
    if category == "навигация":
        return "navigation:event-scoped" if entity_present else "navigation:generic"
    return "other:event-scoped" if entity_present else "other:unknown"


def _entity_is_present(value: Any, *, line_number: int) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        raise ValueError(f"Entity must be a string or null at line {line_number}")
    return bool(value.strip())


def _required_hash(value: Any, field: str, line_number: int) -> str:
    text = str(value or "")
    if not _HASH_RE.fullmatch(text):
        raise ValueError(f"{field} must contain a safe hash at line {line_number}")
    return text


def _required_label(value: Any, field: str, line_number: int) -> str:
    text = str(value or "")
    if not _LABEL_RE.fullmatch(text):
        raise ValueError(f"{field} must contain a safe taxonomy label at line {line_number}")
    return text


def _optional_bool(
    value: Any,
    *,
    field: str,
    line_number: int,
    default: bool,
) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean at line {line_number}")
    return value


def _nullable_bool(
    value: Any,
    *,
    field: str,
    line_number: int,
) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean or null at line {line_number}")
    return value


def _safe_enum(value: Any, *, allowed: set[str], default: str) -> str:
    text = str(value or "")
    return text if text in allowed else default


def _safe_channel(value: Any) -> str:
    text = str(value or "")
    return text if text in _SAFE_CHANNELS else "other"


def _time_bucket(value: Any) -> str:
    match = _MONTH_RE.match(str(value or ""))
    return f"{match.group(1)}-{match.group(2)}" if match else ""


def _safe_id_list(value: Any, *, field: str, line_number: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list at line {line_number}")
    result: set[str] = set()
    for item in value:
        text = str(item or "")
        if not _SAFE_ID_RE.fullmatch(text):
            raise ValueError(f"{field} contains an unsafe identifier at line {line_number}")
        result.add(text)
    return tuple(sorted(result))


def _safe_label_list(value: Any, *, field: str, line_number: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list at line {line_number}")
    result: set[str] = set()
    for item in value:
        result.add(_required_label(item, field, line_number))
    return tuple(sorted(result))


def _safe_label_status(value: Any) -> str:
    text = str(value or "")
    return text if _LABEL_RE.fullmatch(text) else "unknown"


def _source_schema_version(value: Any, *, line_number: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 40 or any(ord(character) < 32 for character in text):
        raise ValueError(f"Invalid schema_version at line {line_number}")
    return text


def source_case_fingerprint(payload: Mapping[str, Any]) -> str:
    """Bind a review decision to the exact private source case semantics."""

    query = str(payload.get("query") or "").strip()
    if not query:
        raise ValueError("Source case fingerprint requires a non-empty query")
    forbidden_profiles = sorted(
        {
            str(item).strip()
            for item in _iter_fingerprint_values(
                payload.get("forbidden_response_profiles")
            )
            if str(item).strip()
        }
    )
    canonical = {
        "schema_version": str(payload.get("schema_version") or "").strip(),
        "ticket_id_hash": str(payload.get("ticket_id_hash") or "").strip(),
        "query": query,
        "category": str(payload.get("category") or "").strip(),
        "topic": str(payload.get("topic") or "").strip(),
        "entity": str(payload.get("entity") or "").strip(),
        "expected_response_profile": str(
            payload.get("expected_response_profile") or ""
        ).strip(),
        "expected_route": str(payload.get("expected_route") or "").strip(),
        "expected_escalation_reason": str(
            payload.get("expected_escalation_reason") or ""
        ).strip(),
        "forbidden_response_profiles": forbidden_profiles,
        "role_reconstruction_status": str(
            payload.get("role_reconstruction_status") or ""
        ).strip(),
        "multiturn_status": str(payload.get("multiturn_status") or "").strip(),
        "time_sensitive": _fingerprint_bool(payload.get("time_sensitive")),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _iter_fingerprint_values(value: Any) -> Iterable[Any]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return value.split("|")
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        return value
    return (value,)


def _fingerprint_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def _optional_label(value: Any, *, field: str, line_number: int) -> str:
    text = str(value or "")
    if text and not _LABEL_RE.fullmatch(text):
        raise ValueError(f"{field} must contain a safe taxonomy label at line {line_number}")
    return text


def _csv_bool(value: bool) -> str:
    return "true" if value else "false"


def _csv_nullable_bool(value: bool | None) -> str:
    if value is None:
        return ""
    return _csv_bool(value)


def _write_csv(
    path: Path,
    fieldnames: Iterable[str],
    rows: list[dict[str, str | int]],
    *,
    overwrite: bool,
) -> None:
    if path.exists() and not overwrite:
        raise ValueError(f"Review output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8-sig",
            newline="",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=list(fieldnames),
                extrasaction="raise",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
            file.flush()
            os.fsync(file.fileno())
        if path.exists() and not overwrite:
            raise ValueError(f"Review output already exists: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_options(
    input_path: Path,
    summary_path: Path,
    manifest_path: Path,
    *,
    top_n: int,
    min_per_stratum: int,
    total: int,
    split: str,
    selection_mode: str,
    multiturn_status: str | None,
) -> None:
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    if min_per_stratum < 0:
        raise ValueError("min_per_stratum must be non-negative")
    if total <= 0:
        raise ValueError("total must be positive")
    if split not in _SAFE_SPLITS:
        raise ValueError(f"Unsupported product split: {split!r}")
    if selection_mode not in _SAFE_SELECTION_MODES:
        raise ValueError(f"Unsupported selection mode: {selection_mode!r}")
    if (
        multiturn_status is not None
        and multiturn_status not in {"single_turn", "multi_turn"}
    ):
        raise ValueError(
            "multiturn_status must be 'single_turn', 'multi_turn', or None"
        )
    resolved = {
        input_path.resolve(),
        summary_path.resolve(),
        manifest_path.resolve(),
    }
    if len(resolved) != 3:
        raise ValueError("Input, summary output, and manifest output must be different files")
    private_root = PRIVATE_DATA_ROOT.resolve()
    if not all(path.is_relative_to(private_root) for path in resolved):
        raise ValueError(
            f"Review input and outputs must stay under {private_root}"
        )
    input_parent = input_path.resolve().parent
    for output_path in (summary_path, manifest_path):
        if not output_path.resolve().is_relative_to(input_parent):
            raise ValueError("Review outputs must stay under the private input directory")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build metadata-only top-strata review CSVs from private calibration cases."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--manifest-output", type=Path)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument(
        "--min-per-stratum",
        type=int,
        default=DEFAULT_MIN_PER_STRATUM,
    )
    parser.add_argument("--total", type=int, default=DEFAULT_TOTAL)
    parser.add_argument(
        "--split",
        choices=sorted(_SAFE_SPLITS),
        default=DEFAULT_SPLIT,
    )
    parser.add_argument(
        "--selection-mode",
        choices=sorted(_SAFE_SELECTION_MODES),
        default=DEFAULT_SELECTION_MODE,
    )
    parser.add_argument(
        "--multiturn-status",
        choices=("single_turn", "multi_turn"),
        help="Optionally restrict the eligible population before sampling.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary_path = args.summary_output or args.input.with_name(DEFAULT_SUMMARY_NAME)
    manifest_path = args.manifest_output or args.input.with_name(DEFAULT_MANIFEST_NAME)
    stats = build_review_exports(
        args.input,
        summary_path,
        manifest_path,
        top_n=args.top_n,
        min_per_stratum=args.min_per_stratum,
        total=args.total,
        split=args.split,
        selection_mode=args.selection_mode,
        multiturn_status=args.multiturn_status,
        overwrite=args.overwrite,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
