from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
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
    "answerable_from_snapshot",
    "approved_chunk_ids",
    "approved_kb_seed_sha256",
    "forbidden_profiles",
    "include_in_calibration",
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
    overwrite: bool = False,
) -> dict[str, int]:
    """Build metadata-only review queues from private calibration cases."""

    _validate_options(
        input_path,
        summary_path,
        manifest_path,
        top_n=top_n,
        min_per_stratum=min_per_stratum,
        total=total,
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
    cases = read_calibration_cases(input_path)
    strata = _build_top_strata(cases, top_n=top_n)
    selected = _select_cases(
        strata,
        min_per_stratum=min_per_stratum,
        total=total,
    )
    summary_rows = _build_summary_rows(strata, selected, total_cases=len(cases))
    manifest_rows = _build_manifest_rows(strata, selected)

    _write_csv(summary_path, SUMMARY_FIELDS, summary_rows, overwrite=overwrite)
    _write_csv(manifest_path, MANIFEST_FIELDS, manifest_rows, overwrite=overwrite)

    return {
        "input_cases": len(cases),
        "strata_total": len({case.stratum for case in cases}),
        "top_strata": len(strata),
        "selected_cases": len(selected),
        "unique_clusters": len({case.duplicate_cluster_id for case in cases}),
        "summary_rows": len(summary_rows),
        "manifest_rows": len(manifest_rows),
    }


def read_calibration_cases(path: Path) -> list[ReviewCase]:
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
            case = _case_from_payload(payload, line_number=line_number)
            if case.case_id_hash in seen_case_ids:
                raise ValueError(f"Duplicate ticket_id_hash at line {line_number}")
            seen_case_ids.add(case.case_id_hash)
            cases.append(case)

    if not cases:
        raise ValueError("Calibration input is empty")
    return cases


def _case_from_payload(payload: dict[str, Any], *, line_number: int) -> ReviewCase:
    if payload.get("split") != "calibration":
        raise ValueError(f"Non-calibration case at line {line_number}")
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
            extra_candidates.append(
                (Fraction(stratum.weight, ordinal), stratum.rank, case)
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
                "answerable_from_snapshot": _csv_nullable_bool(
                    case.answerable_from_snapshot
                ),
                "approved_chunk_ids": "|".join(case.approved_chunk_ids),
                "approved_kb_seed_sha256": "",
                "forbidden_profiles": "|".join(case.forbidden_profiles),
                "include_in_calibration": "",
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
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=list(fieldnames),
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    if path.exists() and not overwrite:
        temp_path.unlink(missing_ok=True)
        raise ValueError(f"Review output already exists: {path}")
    temp_path.replace(path)


def _validate_options(
    input_path: Path,
    summary_path: Path,
    manifest_path: Path,
    *,
    top_n: int,
    min_per_stratum: int,
    total: int,
) -> None:
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    if min_per_stratum <= 0:
        raise ValueError("min_per_stratum must be positive")
    if total <= 0:
        raise ValueError("total must be positive")
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
        overwrite=args.overwrite,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
