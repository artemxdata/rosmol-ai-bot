from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from src.security.pii_masker import PIIMasker, PIIMaskingUnavailable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "eval" / "cases" / "pilot50_balanced_v1.json"
MANIFEST_SCHEMA_VERSION = "1.0.0"
SAFE_SCHEMA_VERSION = "pilot50-safe-result-v1"
DATASET_ID = "pilot50_balanced_v1"
V2_DATASET_ID = "pilot50_balanced_v2"
CLASSIFICATION = "calibration_only"
PILOT50_TARGET = "http://app-ml:8000/ask"
PILOT50_CANDIDATE_TARGET = "http://pilot50-candidate-ml:8000/ask"
EXPECTED_MANIFEST_CANONICAL_SHA256 = (
    "d591a02da2b616c1dc89931371184c762e0c9e1d3b68a50fd9ae33f9a5cf98f4"
)
V2_EXPECTED_MANIFEST_CANONICAL_SHA256 = (
    "13a706f713eef7c54337bd7cf6efdb38e898dde71089ea7e51a2c34fca3fcb91"
)
DISCLAIMER = (
    "Tracked regression calibration only. This is a mechanical first-turn "
    "closure result for the balanced Pilot50 set, not an independent holdout, "
    "a human product verdict, ticket-level conversion, or production traffic conversion."
)
EXPECTED_CASES_TOTAL = 50
EXPECTED_TYPE_COUNTS = {"typical": 25, "atypical": 25}
EXPECTED_BEHAVIOR = "answer"
EXPECTED_ESCALATED = False
MAX_LLM_COST_RUB = 20.0
CANDIDATE_MAX_LLM_COST_RUB = 30.0
PRICING_SOURCE = "eval_repriced"
PRICING_CONTRACT_ID = "pilot50-c38-pricing-v1"
CANDIDATE_CONTRACT_ID = "pilot50-v2-candidate-v1"
CANDIDATE_CASES_SHA256 = (
    "b027e469e062682b6dc341b2dd4c87440edffb1955c2111f38e6c44a92a3a14d"
)
CANDIDATE_PRICING_SOURCE = "target_reported"
CANDIDATE_COST_SCOPE = "pilot50-v2-candidate"
CANDIDATE_QUALITY_GATE_SCHEMA_VERSION = "pilot50-v2-quality-gate-v1"
CANDIDATE_QUALITY_GATE_CRITERIA = (
    "overall_closed",
    "typical_closed",
    "atypical_closed",
    "output_contract_escalations",
    "source_binding_failures",
    "critical_case_failures",
)
CANDIDATE_EXPECTED_QREL_CASES = 38
CANDIDATE_EXPECTED_CRITICAL_CASES = 15
CANDIDATE_CRITICAL_CASE_TAGS = frozenset({"adversarial", "off_aspect_guard"})
CANDIDATE_OUTPUT_CONTRACT_ESCALATION_REASONS = frozenset(
    {
        "empty_generated_response",
        "final_response_empty",
        "final_response_too_long",
        "final_response_too_many_links",
        "final_response_unapproved_emoji",
        "llm_response_contract_failed",
        "llm_response_profile_failed",
        "llm_response_too_long",
        "llm_source_citation_failed",
        "llm_source_coverage_failed",
        "llm_source_fact_binding_failed",
        "source_response_contract_failed",
    }
)
CANDIDATE_SOURCE_BINDING_DEFINITION = (
    "non_escalated_result_with_qrels_failing_any_effective_expected_retrieval_"
    "or_citation_source_check"
)
CANDIDATE_CRITICAL_CASE_DEFINITION = (
    "result_passed_is_not_true_for_case_tagged_adversarial_or_off_aspect_guard"
)
PRICING_RATE_CARD = {
    "complex_input_price_rub_per_million": "569.34",
    "complex_model": "GigaChat/GigaChat-2-Max",
    "complex_official_price_rub_per_million": "569.3374",
    "complex_output_price_rub_per_million": "569.34",
    "complex_price_policy": "conservative_round_up",
    "simple_input_price_rub_per_million": "12.2",
    "simple_model": "ai-sage/GigaChat3-10B-A1.8B",
    "simple_output_price_rub_per_million": "12.2",
}
PRICING_RATE_CARD_SHA256 = hashlib.sha256(
    json.dumps(
        PRICING_RATE_CARD,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()
PRICING_MODELS: dict[str, tuple[Decimal, Decimal]] = {
    "ai-sage/GigaChat3-10B-A1.8B": (Decimal("12.2"), Decimal("12.2")),
    "GigaChat/GigaChat-2-Max": (Decimal("569.34"), Decimal("569.34")),
}
PRICING_PROJECTION = {
    "schema_version": "pilot50-llm-cost-repricing-v1",
    "contract_id": PRICING_CONTRACT_ID,
    "rate_card_sha256": PRICING_RATE_CARD_SHA256,
    "source": PRICING_SOURCE,
    "target_telemetry_preserved": True,
    "target_telemetry_pricing_complete": False,
    "simple_model": "ai-sage/GigaChat3-10B-A1.8B",
    "simple_input_price_rub_per_million": 12.2,
    "simple_output_price_rub_per_million": 12.2,
    "complex_model": "GigaChat/GigaChat-2-Max",
    "complex_input_price_rub_per_million": 569.34,
    "complex_output_price_rub_per_million": 569.34,
    "complex_official_price_rub_per_million": 569.3374,
    "complex_price_policy": "conservative_round_up",
}
PRICING_PROVENANCE_BASE = {
    "schema_version": "pilot50-llm-cost-repricing-v1",
    "contract_id": PRICING_CONTRACT_ID,
    "rate_card_sha256": PRICING_RATE_CARD_SHA256,
    "source": PRICING_SOURCE,
    "target_telemetry_preserved": True,
    "target_telemetry_pricing_complete": False,
}
MAX_MANIFEST_BYTES = 128 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_KB_SEED_BYTES = 16 * 1024 * 1024
MAX_CASES_BYTES = 4 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_SAFE_BYTES = 128 * 1024
MAX_REVIEW_TEXT_LENGTH = 50_000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EVAL_RUN_ID_RE = re.compile(r"^ask-eval-[0-9a-f-]{36}$")
FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_APPROVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
SAFE_TAG_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,160}$")
SAFE_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
OBSERVED_BEHAVIORS = frozenset({"answer", "clarify", "escalate", "scope_note"})
REVIEW_FIELDS = frozenset(
    {
        "ordinal",
        "group",
        "query",
        "response",
        "was_escalated",
        "escalation_reason",
        "passed",
        "observed_behavior",
    }
)
ALLOWED_SOURCE_PATHS = frozenset(
    {
        "eval/cases/pre_pilot_yonote.json",
        "eval/cases/product_calibration_synthetic_pilot_20.json",
        "eval/cases/pre_pilot_forums.json",
        "eval/cases/pre_pilot_adversarial.json",
        "eval/cases/product_date_aspect_regression_v1.json",
    }
)
V2_ALLOWED_SOURCE_PATHS = frozenset(
    {
        "eval/cases/pre_pilot_yonote.json",
        "eval/cases/product_calibration_synthetic_pilot_20.json",
        "eval/cases/pilot50_atypical_yonote_v2.json",
        "eval/cases/pre_pilot_adversarial.json",
        "eval/cases/product_date_aspect_regression_v1.json",
    }
)
DATASET_CONTRACTS: dict[str, dict[str, Any]] = {
    DATASET_ID: {
        "manifest_canonical_sha256": EXPECTED_MANIFEST_CANONICAL_SHA256,
        "source_paths": ALLOWED_SOURCE_PATHS,
        "tag": "pilot50:v1",
        "user_prefix": "pilot50-v1",
        "version_label": "v1",
    },
    V2_DATASET_ID: {
        "manifest_canonical_sha256": V2_EXPECTED_MANIFEST_CANONICAL_SHA256,
        "source_paths": V2_ALLOWED_SOURCE_PATHS,
        "tag": "pilot50:v2",
        "user_prefix": "pilot50-v2",
        "version_label": "v2",
        "requires_published_yonote_qrels": True,
    },
}
FORBIDDEN_CASE_FIELDS = frozenset(
    {
        "ticket_id",
        "ticket_id_hash",
        "upstream_ticket_id",
        "source_ticket_id",
        "operator_answer",
        "operator_response",
        "user_hash",
        "user_id_hash",
        "holdout_contract",
    }
)
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "dataset_id",
        "classification",
        "human_product_verdict",
        "strata_contract",
        "expected_contract",
        "sources",
        "disclaimer",
    }
)
SOURCE_FIELDS = frozenset(
    {"path", "sha256", "type", "selection_rule", "case_ids"}
)
RUNTIME_IDENTITY_FIELDS = frozenset(
    {
        "required",
        "status",
        "expected_runtime_git_sha",
        "preflight_release_git_sha",
        "postflight_release_git_sha",
        "verified_release_git_sha",
        "matched_expected_runtime",
    }
)
SAFE_FIELDS = frozenset(
    {
        "schema_version",
        "dataset_id",
        "eval_run_id",
        "runtime_git_sha",
        "approval_id",
        "run_window_utc",
        "billing_status",
        "status",
        "classification",
        "human_product_verdict",
        "denominator",
        "counts",
        "mechanical_first_turn_closure",
        "policy_pass",
        "trace_coverage",
        "cache_hits",
        "budget",
        "pricing",
        "latency_ms",
        "llm_cost_rub",
        "cases_sha256",
        "report_sha256",
        "disclaimer",
    }
)
CANDIDATE_SAFE_FIELDS = SAFE_FIELDS | {"quality_gate"}


class Pilot50Error(ValueError):
    """Pilot50 input, integrity, or execution evidence is invalid."""


@lru_cache(maxsize=1)
def _pilot50_pii_masker() -> PIIMasker:
    return PIIMasker()


@lru_cache(maxsize=512)
def _query_has_pii(query: str) -> bool:
    try:
        _masked_query, mapping = _pilot50_pii_masker().mask(query)
    except PIIMaskingUnavailable as exc:
        raise Pilot50Error("Pilot50 PII scan is unavailable") from exc
    return bool(mapping)


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Pilot50Error("duplicate JSON keys are not allowed")
        result[key] = value
    return result


def _reject_non_finite_number(value: str) -> Any:
    raise Pilot50Error(f"non-finite JSON number is not allowed: {value}")


def _read_regular_bytes(path: Path, *, max_bytes: int, label: str) -> bytes:
    candidate = path.expanduser()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise Pilot50Error(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise Pilot50Error(f"{label} must be a regular file")
    if metadata.st_size <= 0 or metadata.st_size > max_bytes:
        raise Pilot50Error(f"{label} size is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise Pilot50Error(f"{label} cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise Pilot50Error(f"{label} must remain a regular file")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise Pilot50Error(f"{label} changed before read")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise Pilot50Error(f"{label} is oversized")
        final = os.fstat(descriptor)
        if (final.st_dev, final.st_ino, final.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise Pilot50Error(f"{label} changed during read")
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if not payload:
        raise Pilot50Error(f"{label} is empty")
    return payload


def _load_json_bytes(payload: bytes, *, label: str) -> Any:
    try:
        text = payload.decode("utf-8-sig")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Pilot50Error(f"{label} must be valid UTF-8 JSON") from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _manifest_snapshot(path: Path) -> tuple[dict[str, Any], bytes, str]:
    payload = _read_regular_bytes(path, max_bytes=MAX_MANIFEST_BYTES, label="manifest")
    manifest = _load_json_bytes(payload, label="manifest")
    if not isinstance(manifest, dict):
        raise Pilot50Error("manifest must be a JSON object")
    _validate_manifest(manifest)
    return manifest, payload, _sha256(payload)


def _dataset_contract(dataset_id: Any) -> dict[str, Any]:
    if not isinstance(dataset_id, str) or dataset_id not in DATASET_CONTRACTS:
        raise Pilot50Error("manifest dataset id is invalid")
    return DATASET_CONTRACTS[dataset_id]


def _evidence_contract(
    dataset_id: str,
    *,
    candidate_contract: str | None = None,
) -> dict[str, Any]:
    requested_candidate = str(candidate_contract or "").strip()
    if dataset_id == DATASET_ID:
        if requested_candidate:
            raise Pilot50Error(
                "candidate contract cannot be used with the Pilot50 v1 dataset"
            )
        return {
            "target": PILOT50_TARGET,
            "max_llm_cost_rub": MAX_LLM_COST_RUB,
            "pricing_source": PRICING_SOURCE,
            "pricing_contract_id": PRICING_CONTRACT_ID,
            "target_telemetry_pricing_complete": False,
            "cost_scope": "ask-eval",
            "reservation_private_full": False,
        }
    if dataset_id != V2_DATASET_ID or requested_candidate != CANDIDATE_CONTRACT_ID:
        raise Pilot50Error(
            "Pilot50 v2 evidence requires the exact candidate contract"
        )
    return {
        "target": PILOT50_CANDIDATE_TARGET,
        "max_llm_cost_rub": CANDIDATE_MAX_LLM_COST_RUB,
        "pricing_source": CANDIDATE_PRICING_SOURCE,
        "pricing_contract_id": CANDIDATE_CONTRACT_ID,
        "target_telemetry_pricing_complete": True,
        "cost_scope": CANDIDATE_COST_SCOPE,
        "reservation_private_full": True,
    }


def _safe_result_evidence_contract(dataset_id: str) -> dict[str, Any]:
    return _evidence_contract(
        dataset_id,
        candidate_contract=(
            CANDIDATE_CONTRACT_ID if dataset_id == V2_DATASET_ID else None
        ),
    )


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    if set(manifest) != MANIFEST_FIELDS:
        raise Pilot50Error("manifest fields do not match the Pilot50 schema")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise Pilot50Error("manifest schema version is invalid")
    dataset_contract = _dataset_contract(manifest.get("dataset_id"))
    if manifest.get("classification") != CLASSIFICATION:
        raise Pilot50Error("manifest classification is invalid")
    if manifest.get("human_product_verdict") is not False:
        raise Pilot50Error("manifest must not claim a human product verdict")
    if manifest.get("disclaimer") != (
        "Tracked regression calibration only; not an independent holdout or a "
        "human product-conversion verdict."
    ):
        raise Pilot50Error("manifest disclaimer is invalid")
    strata = manifest.get("strata_contract")
    if not isinstance(strata, dict) or set(strata) != set(EXPECTED_TYPE_COUNTS):
        raise Pilot50Error("manifest strata contract is invalid")
    if not all(isinstance(value, str) and value.strip() for value in strata.values()):
        raise Pilot50Error("manifest strata descriptions are invalid")
    contract = manifest.get("expected_contract")
    expected_contract = {
        "cases_total": EXPECTED_CASES_TOTAL,
        "type_counts": EXPECTED_TYPE_COUNTS,
        "expected_behavior": EXPECTED_BEHAVIOR,
        "expected_escalated": EXPECTED_ESCALATED,
    }
    if contract != expected_contract:
        raise Pilot50Error("manifest expected contract is invalid")
    sources = manifest.get("sources")
    allowed_source_paths = dataset_contract["source_paths"]
    if not isinstance(sources, list) or len(sources) != len(allowed_source_paths):
        raise Pilot50Error("manifest sources are invalid")
    observed_paths: set[str] = set()
    selected_ids: list[str] = []
    type_counts: Counter[str] = Counter()
    for source in sources:
        if not isinstance(source, dict) or set(source) != SOURCE_FIELDS:
            raise Pilot50Error("manifest source fields are invalid")
        source_path = source.get("path")
        source_hash = source.get("sha256")
        group = source.get("type")
        rule = source.get("selection_rule")
        case_ids = source.get("case_ids")
        if source_path not in allowed_source_paths or source_path in observed_paths:
            raise Pilot50Error("manifest source path is invalid")
        if not isinstance(source_hash, str) or SHA256_RE.fullmatch(source_hash) is None:
            raise Pilot50Error("manifest source hash is invalid")
        if group not in EXPECTED_TYPE_COUNTS:
            raise Pilot50Error("manifest source type is invalid")
        if not isinstance(rule, str) or not rule.strip():
            raise Pilot50Error("manifest source selection rule is invalid")
        if (
            not isinstance(case_ids, list)
            or not case_ids
            or any(not isinstance(case_id, str) or not case_id for case_id in case_ids)
            or len(case_ids) != len(set(case_ids))
        ):
            raise Pilot50Error("manifest source case IDs are invalid")
        observed_paths.add(source_path)
        selected_ids.extend(case_ids)
        type_counts[group] += len(case_ids)
    if observed_paths != allowed_source_paths:
        raise Pilot50Error("manifest source membership is invalid")
    if len(selected_ids) != EXPECTED_CASES_TOTAL or len(set(selected_ids)) != len(
        selected_ids
    ):
        raise Pilot50Error("manifest selected case membership is invalid")
    if dict(type_counts) != EXPECTED_TYPE_COUNTS:
        raise Pilot50Error("manifest type counts are invalid")


def _source_path(relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise Pilot50Error("source path escapes the project")
    candidate = PROJECT_ROOT.joinpath(relative)
    project = PROJECT_ROOT.resolve(strict=True)
    resolved_parent = candidate.parent.resolve(strict=True)
    try:
        resolved_parent.relative_to(project)
    except ValueError as exc:
        raise Pilot50Error("source path escapes the project") from exc
    return candidate


def _case_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise Pilot50Error("case tags must be an array")
    tags: list[str] = []
    for tag in value:
        if not isinstance(tag, str) or SAFE_TAG_RE.fullmatch(tag) is None:
            raise Pilot50Error("case contains an invalid tag")
        tags.append(tag)
    return tags


def _qrel_ids_from_case(case: Mapping[str, Any]) -> set[str]:
    qrel_ids: set[str] = set()
    for field in ("expected_chunk_ids", "expected_cited_chunk_ids"):
        value = case.get(field)
        if value is None:
            continue
        if (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item for item in value)
            or len(value) != len(set(value))
        ):
            raise Pilot50Error(f"selected case {field} is invalid")
        qrel_ids.update(value)

    equivalents = case.get("equivalent_chunk_ids")
    if equivalents is None:
        return qrel_ids
    if isinstance(equivalents, dict):
        for expected_id, accepted_ids in equivalents.items():
            if not isinstance(expected_id, str) or not expected_id:
                raise Pilot50Error("selected case equivalent chunk IDs are invalid")
            if isinstance(accepted_ids, str):
                accepted_ids = [accepted_ids]
            if (
                not isinstance(accepted_ids, list)
                or any(not isinstance(item, str) or not item for item in accepted_ids)
                or len(accepted_ids) != len(set(accepted_ids))
            ):
                raise Pilot50Error("selected case equivalent chunk IDs are invalid")
            qrel_ids.add(expected_id)
            qrel_ids.update(accepted_ids)
        return qrel_ids
    if isinstance(equivalents, str):
        equivalents = [equivalents]
    if (
        not isinstance(equivalents, list)
        or any(not isinstance(item, str) or not item for item in equivalents)
        or len(equivalents) != len(set(equivalents))
    ):
        raise Pilot50Error("selected case equivalent chunk IDs are invalid")
    qrel_ids.update(equivalents)
    return qrel_ids


def _validate_published_yonote_qrels(cases: Sequence[Mapping[str, Any]]) -> None:
    seed_path = _source_path("data/knowledge_base_seed.json")
    seed_bytes = _read_regular_bytes(
        seed_path,
        max_bytes=MAX_KB_SEED_BYTES,
        label="frozen knowledge seed",
    )
    seed_rows = _load_json_bytes(seed_bytes, label="frozen knowledge seed")
    if not isinstance(seed_rows, list) or not all(
        isinstance(row, dict) for row in seed_rows
    ):
        raise Pilot50Error("frozen knowledge seed must contain a JSON object array")
    seed_by_id: dict[str, Mapping[str, Any]] = {}
    for row in seed_rows:
        chunk_id = row.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id or chunk_id in seed_by_id:
            raise Pilot50Error("frozen knowledge seed chunk IDs are invalid")
        seed_by_id[chunk_id] = row

    for case in cases:
        for qrel_id in _qrel_ids_from_case(case):
            source = seed_by_id.get(qrel_id)
            if (
                source is None
                or source.get("status") != "published"
                or source.get("source_type") != "yonote"
            ):
                raise Pilot50Error(
                    "Pilot50 v2 qrel is not a published Yonote source"
                )


def build_materialized_cases(manifest_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest, _manifest_bytes, manifest_sha = _manifest_snapshot(manifest_path)
    dataset_id = str(manifest["dataset_id"])
    dataset_contract = _dataset_contract(dataset_id)
    materialized: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    seen_ids: set[str] = set()
    type_counts: Counter[str] = Counter()
    for source in manifest["sources"]:
        source_path = _source_path(source["path"])
        source_bytes = _read_regular_bytes(
            source_path,
            max_bytes=MAX_SOURCE_BYTES,
            label="case source",
        )
        source_rows = _load_json_bytes(source_bytes, label="case source")
        if not isinstance(source_rows, list) or not all(
            isinstance(row, dict) for row in source_rows
        ):
            raise Pilot50Error("case source must contain a JSON object array")
        if _sha256(_canonical_json_bytes(source_rows)) != source["sha256"]:
            raise Pilot50Error("case source canonical hash mismatch")
        by_id: dict[str, dict[str, Any]] = {}
        for row in source_rows:
            case_id = row.get("id")
            if not isinstance(case_id, str) or not case_id or case_id in by_id:
                raise Pilot50Error("case source IDs are invalid")
            by_id[case_id] = row
        selected_ids = source["case_ids"]
        if source["selection_rule"] in {"all_cases", "all_multi_aspect_cases"} and set(
            selected_ids
        ) != set(by_id):
            raise Pilot50Error("all-cases selection rule does not match its source")
        for case_id in selected_ids:
            source_case = by_id.get(case_id)
            if source_case is None:
                raise Pilot50Error("selected case is missing from its source")
            if FORBIDDEN_CASE_FIELDS & set(source_case):
                raise Pilot50Error("selected case contains forbidden identity fields")
            privacy_class = str(source_case.get("privacy_class") or "standard").strip()
            if privacy_class != "standard":
                raise Pilot50Error("selected case is not a standard synthetic regression")
            if source_case.get("expected_behavior") != EXPECTED_BEHAVIOR:
                raise Pilot50Error("selected case is not expected to answer")
            if source_case.get("expected_escalated") is not EXPECTED_ESCALATED:
                raise Pilot50Error("selected case is not expected to stay with the bot")
            query = source_case.get("query")
            if not isinstance(query, str) or not query.strip() or len(query) > 4000:
                raise Pilot50Error("selected case query is invalid")
            if _query_has_pii(query):
                raise Pilot50Error("selected case query failed the PII scan")
            normalized_query = " ".join(query.casefold().split())
            if case_id in seen_ids or normalized_query in seen_queries:
                raise Pilot50Error("selected cases are not unique")
            tags = _case_tags(source_case.get("tags"))
            if any("holdout" in tag.casefold() for tag in tags):
                raise Pilot50Error("holdout-marked case cannot enter Pilot50")
            group = source["type"]
            enriched = dict(source_case)
            enriched["privacy_class"] = "standard"
            enriched["user_id"] = (
                f"{dataset_contract['user_prefix']}-{len(materialized) + 1:02d}"
            )
            enriched["pilot50_group"] = group
            enriched["tags"] = list(
                dict.fromkeys([*tags, dataset_contract["tag"], f"type:{group}"])
            )
            materialized.append(enriched)
            seen_ids.add(case_id)
            seen_queries.add(normalized_query)
            type_counts[group] += 1
    if _sha256(_canonical_json_bytes(manifest)) != dataset_contract[
        "manifest_canonical_sha256"
    ]:
        raise Pilot50Error(
            "manifest differs from the frozen Pilot50 "
            f"{dataset_contract['version_label']} selection"
        )
    if len(materialized) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("materialized case count is invalid")
    if dict(type_counts) != EXPECTED_TYPE_COUNTS:
        raise Pilot50Error("materialized type counts are invalid")
    if dataset_contract.get("requires_published_yonote_qrels") is True:
        _validate_published_yonote_qrels(materialized)
    cases_bytes = _canonical_json_bytes(materialized)
    receipt = {
        "status": "OK",
        "operation": "prepare",
        "dataset_id": dataset_id,
        "cases_total": EXPECTED_CASES_TOTAL,
        "type_counts": EXPECTED_TYPE_COUNTS,
        "expected_behavior": EXPECTED_BEHAVIOR,
        "expected_escalated": EXPECTED_ESCALATED,
        "manifest_sha256": manifest_sha,
        "cases_sha256": _sha256(cases_bytes),
    }
    return materialized, receipt


def _validate_materialized_cases(
    manifest_path: Path,
    cases_path: Path,
) -> tuple[list[dict[str, Any]], bytes, str, dict[str, Any]]:
    expected_cases, receipt = build_materialized_cases(manifest_path)
    expected_bytes = _canonical_json_bytes(expected_cases)
    observed_bytes = _read_regular_bytes(
        cases_path,
        max_bytes=MAX_CASES_BYTES,
        label="materialized cases",
    )
    if observed_bytes != expected_bytes:
        raise Pilot50Error("materialized cases differ from the frozen selection")
    observed = _load_json_bytes(observed_bytes, label="materialized cases")
    if observed != expected_cases:
        raise Pilot50Error("materialized cases payload is invalid")
    return expected_cases, observed_bytes, _sha256(observed_bytes), receipt


def _finite_nonnegative_number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Pilot50Error(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise Pilot50Error(f"{label} must be finite") from exc
    if not math.isfinite(number) or number < 0:
        raise Pilot50Error(f"{label} must be finite and non-negative")
    return number


def _strict_nonnegative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Pilot50Error(f"{label} must be a non-negative integer")
    return value


def _validated_repriced_case_cost(result: Mapping[str, Any]) -> float:
    if result.get("llm_accounting_present") is not True:
        raise Pilot50Error("ask result LLM accounting is missing")
    provenance = result.get("llm_cost_pricing_provenance")
    if (
        not isinstance(provenance, dict)
        or {key: provenance.get(key) for key in PRICING_PROVENANCE_BASE}
        != PRICING_PROVENANCE_BASE
        or set(provenance) != {*PRICING_PROVENANCE_BASE, "status"}
        or provenance.get("status") not in {"repriced", "not_run"}
    ):
        raise Pilot50Error("ask result pricing provenance is invalid")
    target_usage = result.get("target_reported_llm_usage")
    projected_usage = result.get("llm_usage")
    if not isinstance(target_usage, list) or not isinstance(projected_usage, list):
        raise Pilot50Error("ask result pricing evidence is invalid")
    target_prompt = _strict_nonnegative_int(
        result.get("target_reported_llm_prompt_tokens"),
        label="target-reported prompt tokens",
    )
    target_completion = _strict_nonnegative_int(
        result.get("target_reported_llm_completion_tokens"),
        label="target-reported completion tokens",
    )
    target_total = _strict_nonnegative_int(
        result.get("target_reported_llm_total_tokens"),
        label="target-reported total tokens",
    )
    target_cost = _finite_nonnegative_number(
        result.get("target_reported_llm_estimated_cost_rub"),
        label="target-reported case LLM cost",
    )
    projected_prompt = _strict_nonnegative_int(
        result.get("llm_prompt_tokens"), label="projected prompt tokens"
    )
    projected_completion = _strict_nonnegative_int(
        result.get("llm_completion_tokens"), label="projected completion tokens"
    )
    projected_total = _strict_nonnegative_int(
        result.get("llm_total_tokens"), label="projected total tokens"
    )
    projected_cost = _finite_nonnegative_number(
        result.get("llm_estimated_cost_rub"), label="projected case LLM cost"
    )
    if not target_usage:
        if (
            provenance["status"] != "not_run"
            or result.get("generator_model")
            not in {None, "not_run", "source_only", "source_chunk"}
            or result.get("analyzer_execution_mode") != "deterministic"
            or result.get("http_status") != 200
            or result.get("http_success") is not True
            or result.get("error") not in (None, "")
            or result.get("trace_error") not in (None, "")
            or bool(result.get("generate_retry_reasons"))
            or projected_usage
            or any(
                (
                    target_prompt,
                    target_completion,
                    target_total,
                    projected_prompt,
                    projected_completion,
                    projected_total,
                )
            )
            or target_cost != 0
            or projected_cost != 0
        ):
            raise Pilot50Error("not-run pricing evidence is inconsistent")
        return 0.0
    if provenance["status"] != "repriced" or len(projected_usage) != len(target_usage):
        raise Pilot50Error("repriced result usage is inconsistent")

    prompt_sum = 0
    completion_sum = 0
    total_sum = 0
    target_cost_sum = 0.0
    projected_cost_sum = Decimal("0")
    for target_event, projected_event in zip(target_usage, projected_usage, strict=True):
        if not isinstance(target_event, dict) or not isinstance(projected_event, dict):
            raise Pilot50Error("repriced event must be an object")
        model = str(target_event.get("model") or "").strip()
        prices = PRICING_MODELS.get(model)
        if prices is None:
            raise Pilot50Error("repriced event model is not approved")
        prompt_tokens = _strict_nonnegative_int(
            target_event.get("prompt_tokens"), label="target event prompt tokens"
        )
        completion_tokens = _strict_nonnegative_int(
            target_event.get("completion_tokens"),
            label="target event completion tokens",
        )
        total_tokens = _strict_nonnegative_int(
            target_event.get("total_tokens"), label="target event total tokens"
        )
        if total_tokens <= 0 or prompt_tokens + completion_tokens != total_tokens:
            raise Pilot50Error("target event token accounting is inconsistent")
        event_target_cost = _finite_nonnegative_number(
            target_event.get("estimated_cost_rub"), label="target event LLM cost"
        )
        input_price, output_price = prices
        event_projected_cost = (
            (
                Decimal(prompt_tokens) * input_price
                + Decimal(completion_tokens) * output_price
            )
            / Decimal(1_000_000)
        ).quantize(Decimal("0.000001"))
        if event_projected_cost <= 0:
            raise Pilot50Error("projected event LLM cost is zero")
        if target_event.get("priced") is True:
            if not math.isclose(
                event_target_cost,
                float(event_projected_cost),
                rel_tol=1e-9,
                abs_tol=1e-6,
            ):
                raise Pilot50Error("target priced event cost is inconsistent")
        elif target_event.get("priced") is False:
            if event_target_cost != 0:
                raise Pilot50Error("target unpriced event has a nonzero cost")
        else:
            raise Pilot50Error("target event priced flag is invalid")
        expected_projected = dict(target_event)
        expected_projected.update(
            {
                "estimated_cost_rub": float(event_projected_cost),
                "priced": True,
                "pricing_source": PRICING_SOURCE,
                "pricing_contract_id": PRICING_CONTRACT_ID,
                "pricing_rate_card_sha256": PRICING_RATE_CARD_SHA256,
            }
        )
        if projected_event != expected_projected:
            raise Pilot50Error("projected event differs from target-bound repricing")
        prompt_sum += prompt_tokens
        completion_sum += completion_tokens
        total_sum += total_tokens
        target_cost_sum += event_target_cost
        projected_cost_sum += event_projected_cost

    if (
        (target_prompt, target_completion, target_total)
        != (prompt_sum, completion_sum, total_sum)
        or (projected_prompt, projected_completion, projected_total)
        != (prompt_sum, completion_sum, total_sum)
        or not math.isclose(target_cost, target_cost_sum, rel_tol=1e-9, abs_tol=1e-6)
        or not math.isclose(
            projected_cost,
            float(projected_cost_sum),
            rel_tol=1e-9,
            abs_tol=1e-6,
        )
    ):
        raise Pilot50Error("case LLM cost projection is inconsistent")
    return float(projected_cost_sum)


def _validated_target_reported_case_cost(result: Mapping[str, Any]) -> float:
    if result.get("llm_accounting_present") is not True:
        raise Pilot50Error("ask result LLM accounting is missing")
    if "llm_cost_pricing_provenance" in result or any(
        field.startswith("target_reported_llm_") for field in result
    ):
        raise Pilot50Error("candidate result must not contain repriced telemetry")
    usage = result.get("llm_usage")
    if not isinstance(usage, list):
        raise Pilot50Error("candidate target-reported usage is invalid")
    aggregate_prompt = _strict_nonnegative_int(
        result.get("llm_prompt_tokens"), label="target-reported prompt tokens"
    )
    aggregate_completion = _strict_nonnegative_int(
        result.get("llm_completion_tokens"),
        label="target-reported completion tokens",
    )
    aggregate_total = _strict_nonnegative_int(
        result.get("llm_total_tokens"), label="target-reported total tokens"
    )
    aggregate_cost = Decimal(
        str(
            _finite_nonnegative_number(
                result.get("llm_estimated_cost_rub"),
                label="target-reported case LLM cost",
            )
        )
    )
    if aggregate_prompt + aggregate_completion != aggregate_total:
        raise Pilot50Error("candidate aggregate token accounting is inconsistent")
    if not usage:
        if (
            any((aggregate_prompt, aggregate_completion, aggregate_total))
            or aggregate_cost != 0
            or result.get("generator_model")
            not in {None, "not_run", "source_only", "source_chunk"}
            or result.get("analyzer_execution_mode") != "deterministic"
            or result.get("http_status") != 200
            or result.get("http_success") is not True
            or result.get("error") not in (None, "")
            or result.get("trace_error") not in (None, "")
            or bool(result.get("generate_retry_reasons"))
        ):
            raise Pilot50Error("candidate not-run LLM accounting is inconsistent")
        return 0.0

    prompt_sum = 0
    completion_sum = 0
    total_sum = 0
    cost_sum = Decimal("0")
    for event in usage:
        if not isinstance(event, dict):
            raise Pilot50Error("candidate usage event must be an object")
        if any(
            field in event
            for field in (
                "pricing_source",
                "pricing_contract_id",
                "pricing_rate_card_sha256",
            )
        ):
            raise Pilot50Error("candidate usage event contains repricing metadata")
        model = str(event.get("model") or "").strip()
        prices = PRICING_MODELS.get(model)
        if prices is None:
            raise Pilot50Error("candidate usage event model is not approved")
        prompt_tokens = _strict_nonnegative_int(
            event.get("prompt_tokens"), label="candidate event prompt tokens"
        )
        completion_tokens = _strict_nonnegative_int(
            event.get("completion_tokens"),
            label="candidate event completion tokens",
        )
        total_tokens = _strict_nonnegative_int(
            event.get("total_tokens"), label="candidate event total tokens"
        )
        if total_tokens <= 0 or prompt_tokens + completion_tokens != total_tokens:
            raise Pilot50Error("candidate event token accounting is inconsistent")
        event_cost = Decimal(
            str(
                _finite_nonnegative_number(
                    event.get("estimated_cost_rub"),
                    label="candidate event LLM cost",
                )
            )
        )
        input_price, output_price = prices
        expected_cost = (
            (
                Decimal(prompt_tokens) * input_price
                + Decimal(completion_tokens) * output_price
            )
            / Decimal(1_000_000)
        ).quantize(Decimal("0.000001"))
        if (
            event.get("priced") is not True
            or event_cost <= 0
            or event_cost != expected_cost
        ):
            raise Pilot50Error("candidate target-reported event cost is inconsistent")
        prompt_sum += prompt_tokens
        completion_sum += completion_tokens
        total_sum += total_tokens
        cost_sum += event_cost
    if (
        (aggregate_prompt, aggregate_completion, aggregate_total)
        != (prompt_sum, completion_sum, total_sum)
        or aggregate_cost != cost_sum
    ):
        raise Pilot50Error("candidate case LLM cost accounting is inconsistent")
    return float(cost_sum)


def _validated_eval_run_id(value: Any) -> str:
    eval_run_id = str(value or "")
    if EVAL_RUN_ID_RE.fullmatch(eval_run_id) is None:
        raise Pilot50Error("ask report eval run ID is invalid")
    try:
        parsed = str(UUID(eval_run_id.removeprefix("ask-eval-")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise Pilot50Error("ask report eval run ID is invalid") from exc
    if eval_run_id != f"ask-eval-{parsed}":
        raise Pilot50Error("ask report eval run ID is invalid")
    return eval_run_id


def _validated_runtime_git_sha(value: Any) -> str:
    runtime_sha = str(value or "")
    if (
        FULL_GIT_SHA_RE.fullmatch(runtime_sha) is None
        or runtime_sha == "0" * 40
    ):
        raise Pilot50Error("expected runtime Git SHA is invalid")
    return runtime_sha


def _validated_approval_id(value: Any) -> str:
    approval_id = str(value or "")
    if SAFE_APPROVAL_ID_RE.fullmatch(approval_id) is None:
        raise Pilot50Error("expected approval reference is invalid")
    return approval_id


def _parse_utc_timestamp(value: Any, *, label: str) -> tuple[str, datetime]:
    raw = str(value or "")
    if not raw or len(raw) > 40:
        raise Pilot50Error(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Pilot50Error(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise Pilot50Error(f"{label} must be UTC")
    return raw, parsed.astimezone(UTC)


def _validated_run_window(report: Mapping[str, Any]) -> dict[str, str]:
    started_raw, started = _parse_utc_timestamp(
        report.get("run_started_at"),
        label="ask report run start",
    )
    completed_raw, completed = _parse_utc_timestamp(
        report.get("run_completed_at"),
        label="ask report run completion",
    )
    if completed < started or completed - started > timedelta(hours=4):
        raise Pilot50Error("ask report run window is invalid")
    _generated_raw, generated = _parse_utc_timestamp(
        report.get("generated_at"),
        label="ask report generation time",
    )
    if generated < started or generated > completed:
        raise Pilot50Error("ask report generation time is outside the run window")
    return {"started_at": started_raw, "completed_at": completed_raw}


def _rate_row(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "closed": numerator,
        "total": denominator,
        "rate": round(numerator / denominator, 6),
    }


def _pass_row(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "passed": numerator,
        "total": denominator,
        "rate": round(numerator / denominator, 6),
    }


def _validated_candidate_quality_count(
    value: Any,
    *,
    label: str,
    maximum: int,
) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise Pilot50Error(f"candidate quality {label} count is invalid")
    return value


def _build_candidate_quality_gate(
    *,
    typical_closed: int,
    atypical_closed: int,
    output_contract_escalations: int,
    source_binding_failures: int,
    applicable_qrel_cases: int,
    critical_case_failures: int,
    applicable_critical_cases: int,
) -> dict[str, Any]:
    typical_closed = _validated_candidate_quality_count(
        typical_closed,
        label="typical closure",
        maximum=EXPECTED_TYPE_COUNTS["typical"],
    )
    atypical_closed = _validated_candidate_quality_count(
        atypical_closed,
        label="atypical closure",
        maximum=EXPECTED_TYPE_COUNTS["atypical"],
    )
    output_contract_escalations = _validated_candidate_quality_count(
        output_contract_escalations,
        label="output-contract escalation",
        maximum=EXPECTED_CASES_TOTAL,
    )
    applicable_qrel_cases = _validated_candidate_quality_count(
        applicable_qrel_cases,
        label="qrel coverage",
        maximum=EXPECTED_CASES_TOTAL,
    )
    if applicable_qrel_cases != CANDIDATE_EXPECTED_QREL_CASES:
        raise Pilot50Error("candidate quality gate qrel coverage is invalid")
    source_binding_failures = _validated_candidate_quality_count(
        source_binding_failures,
        label="source-binding failure",
        maximum=applicable_qrel_cases,
    )
    applicable_critical_cases = _validated_candidate_quality_count(
        applicable_critical_cases,
        label="critical-case coverage",
        maximum=EXPECTED_CASES_TOTAL,
    )
    if applicable_critical_cases != CANDIDATE_EXPECTED_CRITICAL_CASES:
        raise Pilot50Error("candidate quality gate critical-case coverage is invalid")
    critical_case_failures = _validated_candidate_quality_count(
        critical_case_failures,
        label="critical-case failure",
        maximum=applicable_critical_cases,
    )
    overall_closed = typical_closed + atypical_closed
    criteria = {
        "overall_closed": {
            "actual": overall_closed,
            "minimum": 30,
            "passed": overall_closed >= 30,
        },
        "typical_closed": {
            "actual": typical_closed,
            "minimum": 11,
            "passed": typical_closed >= 11,
        },
        "atypical_closed": {
            "actual": atypical_closed,
            "minimum": 7,
            "passed": atypical_closed >= 7,
        },
        "output_contract_escalations": {
            "actual": output_contract_escalations,
            "maximum": 6,
            "passed": output_contract_escalations <= 6,
        },
        "source_binding_failures": {
            "actual": source_binding_failures,
            "maximum": 0,
            "passed": source_binding_failures == 0,
            "applicable_qrel_cases": applicable_qrel_cases,
            "total_cases": EXPECTED_CASES_TOTAL,
        },
        "critical_case_failures": {
            "actual": critical_case_failures,
            "maximum": 0,
            "passed": critical_case_failures == 0,
            "applicable_critical_cases": applicable_critical_cases,
            "total_cases": EXPECTED_CASES_TOTAL,
        },
    }
    failed_criteria = [
        criterion
        for criterion in CANDIDATE_QUALITY_GATE_CRITERIA
        if criteria[criterion]["passed"] is not True
    ]
    return {
        "schema_version": CANDIDATE_QUALITY_GATE_SCHEMA_VERSION,
        "status": "STOP" if failed_criteria else "GO",
        "criteria": criteria,
        "failed_criteria": failed_criteria,
        "output_contract_reasons": sorted(
            CANDIDATE_OUTPUT_CONTRACT_ESCALATION_REASONS
        ),
        "source_binding_definition": CANDIDATE_SOURCE_BINDING_DEFINITION,
        "critical_case_definition": CANDIDATE_CRITICAL_CASE_DEFINITION,
    }


def _candidate_source_binding_failed(
    expected: Mapping[str, Any],
    result: Mapping[str, Any],
) -> bool:
    """Conservatively fail a non-escalated qrel case on any missing effective check."""

    if result.get("was_escalated") is not False:
        return False
    has_equivalent_chunks = bool(expected.get("equivalent_chunk_ids"))
    effective_checks: list[str] = []
    if expected.get("expected_chunk_ids"):
        effective_checks.append(
            "expected_or_equivalent_chunk_hit"
            if has_equivalent_chunks
            else "expected_chunk_hit"
        )
    if expected.get("expected_cited_chunk_ids"):
        effective_checks.append(
            "expected_cited_or_equivalent_chunk_hit"
            if has_equivalent_chunks
            else "expected_cited_chunk_hit"
        )
    return bool(effective_checks) and any(
        result.get(field) is not True for field in effective_checks
    )


def _candidate_case_is_critical(case: Mapping[str, Any]) -> bool:
    tags = case.get("tags")
    return isinstance(tags, list) and bool(
        CANDIDATE_CRITICAL_CASE_TAGS.intersection(tags)
    )


def _validate_candidate_quality_gate(
    value: Any,
    *,
    typical_closed: int,
    atypical_closed: int,
) -> None:
    if not isinstance(value, dict):
        raise Pilot50Error("candidate quality gate is invalid")
    criteria = value.get("criteria")
    if not isinstance(criteria, dict):
        raise Pilot50Error("candidate quality gate criteria are invalid")
    output_row = criteria.get("output_contract_escalations")
    source_row = criteria.get("source_binding_failures")
    critical_row = criteria.get("critical_case_failures")
    if (
        not isinstance(output_row, dict)
        or not isinstance(source_row, dict)
        or not isinstance(critical_row, dict)
    ):
        raise Pilot50Error("candidate quality gate count rows are invalid")
    expected = _build_candidate_quality_gate(
        typical_closed=typical_closed,
        atypical_closed=atypical_closed,
        output_contract_escalations=output_row.get("actual"),
        source_binding_failures=source_row.get("actual"),
        applicable_qrel_cases=source_row.get("applicable_qrel_cases"),
        critical_case_failures=critical_row.get("actual"),
        applicable_critical_cases=critical_row.get("applicable_critical_cases"),
    )
    if value != expected:
        raise Pilot50Error("candidate quality gate is inconsistent")


def _percentile(values: Sequence[int], percentile: int) -> int:
    ordered = sorted(values)
    if not ordered:
        raise Pilot50Error("latency evidence is empty")
    index = round((len(ordered) - 1) * percentile / 100)
    return ordered[max(0, min(len(ordered) - 1, index))]


def validate_trace_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    eval_run_id: str,
    expected_results: Sequence[Mapping[str, Any]],
) -> None:
    if len(rows) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("database trace cardinality is not exactly 50")
    expected_pairs: set[tuple[str, str]] = set()
    expected_case_ids: set[str] = set()
    for result in expected_results:
        request_id = str(result.get("request_id") or "")
        case_id = str(result.get("id") or "")
        try:
            request_id = str(UUID(request_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise Pilot50Error("report request ID is invalid") from exc
        expected_pairs.add((request_id, case_id))
        expected_case_ids.add(case_id)
    if len(expected_pairs) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("report request/case pairs are not unique")
    observed_pairs: set[tuple[str, str]] = set()
    observed_request_ids: set[str] = set()
    observed_case_ids: list[str] = []
    for row in rows:
        if str(row.get("eval_run_id") or "") != eval_run_id:
            raise Pilot50Error("database trace run ID mismatch")
        try:
            request_id = str(UUID(str(row.get("request_id") or "")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise Pilot50Error("database request ID is invalid") from exc
        case_id = str(row.get("eval_case_id") or "")
        if row.get("cache_hit") is not False:
            raise Pilot50Error("database trace cache invariant failed")
        if row.get("error_present") is not False:
            raise Pilot50Error("database trace contains an execution error")
        observed_pairs.add((request_id, case_id))
        observed_request_ids.add(request_id)
        observed_case_ids.append(case_id)
    if len(observed_pairs) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("database trace request/case pairs are not unique")
    if len(observed_request_ids) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("database trace request IDs are not unique")
    if Counter(observed_case_ids) != Counter({case_id: 1 for case_id in expected_case_ids}):
        raise Pilot50Error("database trace case membership is invalid")
    if observed_pairs != expected_pairs:
        raise Pilot50Error("database trace pairs do not match the report")


async def _fetch_trace_rows(eval_run_id: str) -> list[dict[str, Any]]:
    dsn = str(os.getenv("ASK_EVAL_POSTGRES_DSN") or "").strip()
    if not dsn:
        raise Pilot50Error("trace DSN is unavailable inside acceptance")
    try:
        connection = await asyncpg.connect(dsn, timeout=15, command_timeout=15)
    except Exception as exc:
        raise Pilot50Error("trace database connection failed") from exc
    try:
        async with connection.transaction(readonly=True):
            records = await connection.fetch(
                """
                SELECT
                    eval_run_id,
                    request_id,
                    eval_case_id,
                    cache_hit,
                    (error IS NOT NULL) AS error_present
                FROM request_traces
                WHERE eval_run_id = $1
                ORDER BY eval_case_id NULLS FIRST, request_id
                """,
                eval_run_id,
                timeout=15,
            )
    except Exception as exc:
        raise Pilot50Error("trace database query failed") from exc
    finally:
        await connection.close()
    return [dict(record) for record in records]


def build_safe_result(
    *,
    manifest_path: Path,
    cases_path: Path,
    report_path: Path,
    trace_rows: Sequence[Mapping[str, Any]],
    expected_runtime_git_sha: str,
    expected_approval_id: str,
    candidate_contract: str | None = None,
    report_snapshot: bytes | None = None,
) -> dict[str, Any]:
    runtime_git_sha = _validated_runtime_git_sha(expected_runtime_git_sha)
    approval_id = _validated_approval_id(expected_approval_id)
    cases, cases_bytes, cases_sha, receipt = _validate_materialized_cases(
        manifest_path,
        cases_path,
    )
    dataset_id = str(receipt["dataset_id"])
    evidence_contract = _evidence_contract(
        dataset_id,
        candidate_contract=candidate_contract,
    )
    if dataset_id == V2_DATASET_ID and cases_sha != CANDIDATE_CASES_SHA256:
        raise Pilot50Error("materialized cases differ from the frozen candidate set")
    report_bytes = report_snapshot
    if report_bytes is None:
        report_bytes = _read_regular_bytes(
            report_path,
            max_bytes=MAX_REPORT_BYTES,
            label="ask report",
        )
    elif not report_bytes or len(report_bytes) > MAX_REPORT_BYTES:
        raise Pilot50Error("ask report snapshot size is invalid")
    report = _load_json_bytes(report_bytes, label="ask report")
    if not isinstance(report, dict):
        raise Pilot50Error("ask report must be a JSON object")
    results = report.get("results")
    if not isinstance(results, list) or len(results) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("ask report must contain exactly 50 result rows")
    if report.get("cases_total") != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("ask report case count is invalid")
    eval_run_id = _validated_eval_run_id(report.get("eval_run_id"))
    if report.get("target") != evidence_contract["target"]:
        raise Pilot50Error("ask report target is invalid")
    run_window = _validated_run_window(report)
    runtime_identity = report.get("runtime_identity")
    if (
        not isinstance(runtime_identity, dict)
        or set(runtime_identity) != RUNTIME_IDENTITY_FIELDS
        or runtime_identity.get("required") is not True
        or runtime_identity.get("status") != "verified"
        or runtime_identity.get("expected_runtime_git_sha") != runtime_git_sha
        or runtime_identity.get("preflight_release_git_sha") != runtime_git_sha
        or runtime_identity.get("postflight_release_git_sha") != runtime_git_sha
        or runtime_identity.get("verified_release_git_sha") != runtime_git_sha
        or runtime_identity.get("matched_expected_runtime") is not True
    ):
        raise Pilot50Error("ask report runtime identity does not bind Pilot50")
    expected_by_id = {str(case["id"]): case for case in cases}
    observed_by_id: dict[str, dict[str, Any]] = {}
    latencies: list[int] = []
    group_closed: Counter[str] = Counter()
    group_passed: Counter[str] = Counter()
    group_totals: Counter[str] = Counter()
    output_contract_escalations = 0
    source_binding_failures = 0
    applicable_qrel_cases = 0
    critical_case_failures = 0
    applicable_critical_cases = 0
    total_cost = 0.0
    for result in results:
        if not isinstance(result, dict):
            raise Pilot50Error("ask result row must be an object")
        case_id = str(result.get("id") or "")
        expected = expected_by_id.get(case_id)
        if expected is None or case_id in observed_by_id:
            raise Pilot50Error("ask result membership is invalid")
        observed_by_id[case_id] = result
        group = str(expected.get("pilot50_group") or "")
        if group not in EXPECTED_TYPE_COUNTS:
            raise Pilot50Error("materialized case type is invalid")
        tags = result.get("tags")
        if not isinstance(tags, list) or f"type:{group}" not in tags:
            raise Pilot50Error("ask result type binding is invalid")
        required_bools = {
            "http_success": True,
            "trace_found": True,
            "cache_hit": False,
        }
        for field, expected_value in required_bools.items():
            if result.get(field) is not expected_value:
                raise Pilot50Error(f"ask result {field} invariant failed")
        if result.get("error") is not None or result.get("trace_error") not in (None, ""):
            raise Pilot50Error("ask result contains an execution error")
        if result.get("trace_eval_run_id") != eval_run_id:
            raise Pilot50Error("ask result trace run binding failed")
        if result.get("trace_eval_case_id") != case_id:
            raise Pilot50Error("ask result trace case binding failed")
        if result.get("trace_binding_match") is not True:
            raise Pilot50Error("ask result trace binding is incomplete")
        passed = result.get("passed")
        was_escalated = result.get("was_escalated")
        if type(passed) is not bool or type(was_escalated) is not bool:
            raise Pilot50Error("ask result verdict fields must be typed booleans")
        observed_behavior = result.get("observed_behavior")
        if not isinstance(observed_behavior, str):
            raise Pilot50Error("ask result behavior is missing")
        latency = result.get("trace_total_latency_ms")
        if isinstance(latency, bool) or not isinstance(latency, int) or latency < 0:
            raise Pilot50Error("ask result latency is invalid")
        latencies.append(latency)
        total_cost += (
            _validated_target_reported_case_cost(result)
            if dataset_id == V2_DATASET_ID
            else _validated_repriced_case_cost(result)
        )
        group_totals[group] += 1
        if passed:
            group_passed[group] += 1
        if passed and observed_behavior == "answer" and was_escalated is False:
            group_closed[group] += 1
        if dataset_id == V2_DATASET_ID:
            has_qrels = bool(
                expected.get("expected_chunk_ids")
                or expected.get("expected_cited_chunk_ids")
            )
            if has_qrels:
                applicable_qrel_cases += 1
                if _candidate_source_binding_failed(expected, result):
                    source_binding_failures += 1
            if _candidate_case_is_critical(expected):
                applicable_critical_cases += 1
                if passed is not True:
                    critical_case_failures += 1
            escalation_reason = result.get("escalation_reason")
            if (
                was_escalated is True
                and isinstance(escalation_reason, str)
                and escalation_reason
                in CANDIDATE_OUTPUT_CONTRACT_ESCALATION_REASONS
            ):
                output_contract_escalations += 1
    if set(observed_by_id) != set(expected_by_id):
        raise Pilot50Error("ask report case membership does not match Pilot50")
    if dict(group_totals) != EXPECTED_TYPE_COUNTS:
        raise Pilot50Error("ask report type counts are invalid")
    if dataset_id == V2_DATASET_ID and (
        applicable_qrel_cases != CANDIDATE_EXPECTED_QREL_CASES
        or applicable_critical_cases != CANDIDATE_EXPECTED_CRITICAL_CASES
    ):
        raise Pilot50Error("candidate quality coverage differs from the frozen set")
    validate_trace_rows(
        trace_rows,
        eval_run_id=eval_run_id,
        expected_results=results,
    )
    if report.get("trace_coverage_rate") != 1.0:
        raise Pilot50Error("ask report trace coverage is incomplete")
    if report.get("cache_hit_rate") != 0.0:
        raise Pilot50Error("ask report cache hit rate is not zero")
    if report.get("llm_budget_stopped") is True or report.get("llm_budget_exceeded") is True:
        raise Pilot50Error("ask report stopped on budget")
    if report.get("llm_pricing_stopped") is True:
        raise Pilot50Error("ask report stopped on pricing")
    budget_value = report.get("llm_budget_rub")
    max_llm_cost_rub = float(evidence_contract["max_llm_cost_rub"])
    if (
        _finite_nonnegative_number(budget_value, label="report LLM budget")
        != max_llm_cost_rub
    ):
        raise Pilot50Error("ask report budget differs from the Pilot50 cap")
    reported_cost = _finite_nonnegative_number(
        report.get("llm_estimated_cost_rub"),
        label="report LLM cost",
    )
    if abs(reported_cost - total_cost) > 0.000001 or reported_cost > max_llm_cost_rub:
        raise Pilot50Error("ask report cost accounting is inconsistent")
    cost_control = report.get("cost_control")
    if (
        not isinstance(cost_control, dict)
        or cost_control.get("strict_live") is not True
        or cost_control.get("pricing_complete") is not True
        or cost_control.get("high_cost_approval_id") != approval_id
    ):
        raise Pilot50Error("ask report cost-control evidence is incomplete")
    if dataset_id == V2_DATASET_ID:
        candidate_evidence = cost_control.get("candidate_contract")
        if (
            not isinstance(candidate_evidence, dict)
            or candidate_evidence.get("contract_id") != CANDIDATE_CONTRACT_ID
            or candidate_evidence.get("runtime_git_sha") != runtime_git_sha
            or candidate_evidence.get("cases_file_sha256") != cases_sha
            or candidate_evidence.get("target") != PILOT50_CANDIDATE_TARGET
            or candidate_evidence.get("cases_total") != EXPECTED_CASES_TOTAL
            or candidate_evidence.get("concurrency") != 1
            or candidate_evidence.get("complete_traces_required") is not True
            or candidate_evidence.get("max_llm_cost_rub")
            != CANDIDATE_MAX_LLM_COST_RUB
            or candidate_evidence.get("cost_scope") != CANDIDATE_COST_SCOPE
            or candidate_evidence.get("reservation_private_full") is not True
            or candidate_evidence.get("pricing_source")
            != CANDIDATE_PRICING_SOURCE
            or candidate_evidence.get("pricing_rate_card_sha256")
            != PRICING_RATE_CARD_SHA256
            or candidate_evidence.get("target_telemetry_pricing_complete")
            is not True
            or candidate_evidence.get("repricing_applied") is not False
            or "pricing_projection" in cost_control
        ):
            raise Pilot50Error("ask report candidate contract evidence is invalid")
        candidate_run = report.get("pilot50_candidate")
        if (
            not isinstance(candidate_run, dict)
            or candidate_run.get("status") != "completed"
            or candidate_run.get("completed") is not True
            or candidate_run.get("contract_id") != CANDIDATE_CONTRACT_ID
            or candidate_run.get("expected_cases_total") != EXPECTED_CASES_TOTAL
            or candidate_run.get("executed_cases_total") != EXPECTED_CASES_TOTAL
            or candidate_run.get("cases_file_sha256") != cases_sha
            or candidate_run.get("runtime_git_sha") != runtime_git_sha
            or candidate_run.get("integrity_failures") != []
        ):
            raise Pilot50Error("ask report candidate completion evidence is invalid")
    elif cost_control.get("pricing_projection") != PRICING_PROJECTION:
        raise Pilot50Error("ask report cost-control evidence is incomplete")
    reservation = cost_control.get("reservation")
    if not isinstance(reservation, dict):
        raise Pilot50Error("ask report cost reservation is missing")
    if (
        reservation.get("valid") is not True
        or reservation.get("run_id") != eval_run_id
        or reservation.get("scope") != evidence_contract["cost_scope"]
        or reservation.get("runtime_git_sha") != runtime_git_sha
        or reservation.get("case_count") != EXPECTED_CASES_TOTAL
        or reservation.get("approved_cap_rub") != max_llm_cost_rub
        or reservation.get("approval_required") is not True
        or reservation.get("high_cost_approval_id") != approval_id
        or reservation.get("cases_file_sha256") != cases_sha
        or reservation.get("manifest_sha256") != cases_sha
        or reservation.get("manifest_matches_cases_file") is not True
    ):
        raise Pilot50Error("ask report cost reservation does not bind Pilot50")
    if evidence_contract["reservation_private_full"] is True and (
        reservation.get("private_full") is not True
        or reservation.get("reservation_class") != "private_full"
    ):
        raise Pilot50Error("candidate cost reservation is not private-full")

    closure = {
        group: _rate_row(group_closed[group], EXPECTED_TYPE_COUNTS[group])
        for group in ("typical", "atypical")
    }
    closure["overall"] = _rate_row(sum(group_closed.values()), EXPECTED_CASES_TOTAL)
    policy = {
        group: _pass_row(group_passed[group], EXPECTED_TYPE_COUNTS[group])
        for group in ("typical", "atypical")
    }
    policy["overall"] = _pass_row(sum(group_passed.values()), EXPECTED_CASES_TOTAL)
    safe_result = {
        "schema_version": SAFE_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "eval_run_id": eval_run_id,
        "runtime_git_sha": runtime_git_sha,
        "approval_id": approval_id,
        "run_window_utc": run_window,
        "billing_status": "pending_provider_reconciliation",
        "status": "OK",
        "classification": CLASSIFICATION,
        "human_product_verdict": False,
        "denominator": EXPECTED_CASES_TOTAL,
        "counts": EXPECTED_TYPE_COUNTS,
        "mechanical_first_turn_closure": closure,
        "policy_pass": policy,
        "trace_coverage": {
            "found": EXPECTED_CASES_TOTAL,
            "total": EXPECTED_CASES_TOTAL,
            "rate": 1.0,
        },
        "cache_hits": 0,
        "budget": {
            "max_rub": int(max_llm_cost_rub),
            "exceeded": False,
            "stopped": False,
        },
        "pricing": {
            "complete": True,
            "stopped": False,
            "source": evidence_contract["pricing_source"],
            "contract_id": evidence_contract["pricing_contract_id"],
            "rate_card_sha256": PRICING_RATE_CARD_SHA256,
            "target_telemetry_preserved": True,
            "target_telemetry_pricing_complete": evidence_contract[
                "target_telemetry_pricing_complete"
            ],
        },
        "latency_ms": {"p50": _percentile(latencies, 50), "p95": _percentile(latencies, 95)},
        "llm_cost_rub": round(reported_cost, 6),
        "cases_sha256": _sha256(cases_bytes),
        "report_sha256": _sha256(report_bytes),
        "disclaimer": DISCLAIMER,
    }
    if dataset_id == V2_DATASET_ID:
        safe_result["quality_gate"] = _build_candidate_quality_gate(
            typical_closed=group_closed["typical"],
            atypical_closed=group_closed["atypical"],
            output_contract_escalations=output_contract_escalations,
            source_binding_failures=source_binding_failures,
            applicable_qrel_cases=applicable_qrel_cases,
            critical_case_failures=critical_case_failures,
            applicable_critical_cases=applicable_critical_cases,
        )
    return safe_result


def validate_safe_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Pilot50Error("safe result fields are invalid")
    dataset_id = value.get("dataset_id")
    expected_fields = (
        CANDIDATE_SAFE_FIELDS if dataset_id == V2_DATASET_ID else SAFE_FIELDS
    )
    if set(value) != expected_fields:
        raise Pilot50Error("safe result fields are invalid")
    if value.get("schema_version") != SAFE_SCHEMA_VERSION:
        raise Pilot50Error("safe result schema is invalid")
    if value.get("dataset_id") not in DATASET_CONTRACTS or value.get("status") != "OK":
        raise Pilot50Error("safe result identity is invalid")
    evidence_contract = _safe_result_evidence_contract(str(value["dataset_id"]))
    _validated_eval_run_id(value.get("eval_run_id"))
    _validated_runtime_git_sha(value.get("runtime_git_sha"))
    _validated_approval_id(value.get("approval_id"))
    run_window = value.get("run_window_utc")
    if not isinstance(run_window, dict) or set(run_window) != {
        "started_at",
        "completed_at",
    }:
        raise Pilot50Error("safe result run window is invalid")
    _validated_run_window(
        {
            "run_started_at": run_window.get("started_at"),
            "run_completed_at": run_window.get("completed_at"),
            "generated_at": run_window.get("completed_at"),
        }
    )
    if value.get("billing_status") != "pending_provider_reconciliation":
        raise Pilot50Error("safe result billing status is invalid")
    if value.get("classification") != CLASSIFICATION:
        raise Pilot50Error("safe result classification is invalid")
    if value.get("human_product_verdict") is not False:
        raise Pilot50Error("safe result contains an invalid product verdict")
    if value.get("denominator") != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("safe result denominator is invalid")
    if value.get("counts") != EXPECTED_TYPE_COUNTS:
        raise Pilot50Error("safe result group counts are invalid")
    numerators: dict[str, dict[str, int]] = {}
    for field, count_key in (
        ("mechanical_first_turn_closure", "closed"),
        ("policy_pass", "passed"),
    ):
        table = value.get(field)
        if not isinstance(table, dict) or set(table) != {"typical", "atypical", "overall"}:
            raise Pilot50Error(f"safe result {field} is invalid")
        field_numerators: dict[str, int] = {}
        for group, denominator in {**EXPECTED_TYPE_COUNTS, "overall": 50}.items():
            row = table.get(group)
            if not isinstance(row, dict) or set(row) != {count_key, "total", "rate"}:
                raise Pilot50Error(f"safe result {field} row is invalid")
            numerator = row.get(count_key)
            if type(numerator) is not int or not 0 <= numerator <= denominator:
                raise Pilot50Error(f"safe result {field} numerator is invalid")
            if row.get("total") != denominator:
                raise Pilot50Error(f"safe result {field} total is invalid")
            expected_rate = round(numerator / denominator, 6)
            if row.get("rate") != expected_rate:
                raise Pilot50Error(f"safe result {field} rate is invalid")
            field_numerators[group] = numerator
        if field_numerators["overall"] != (
            field_numerators["typical"] + field_numerators["atypical"]
        ):
            raise Pilot50Error(f"safe result {field} totals are inconsistent")
        numerators[field] = field_numerators
    if numerators["mechanical_first_turn_closure"] != numerators["policy_pass"]:
        raise Pilot50Error("safe closure and policy totals are inconsistent")
    if dataset_id == V2_DATASET_ID:
        closure_numerators = numerators["mechanical_first_turn_closure"]
        _validate_candidate_quality_gate(
            value.get("quality_gate"),
            typical_closed=closure_numerators["typical"],
            atypical_closed=closure_numerators["atypical"],
        )
    if value.get("trace_coverage") != {"found": 50, "total": 50, "rate": 1.0}:
        raise Pilot50Error("safe result trace coverage is invalid")
    if value.get("cache_hits") != 0:
        raise Pilot50Error("safe result cache count is invalid")
    expected_max_rub = int(float(evidence_contract["max_llm_cost_rub"]))
    if value.get("budget") != {
        "max_rub": expected_max_rub,
        "exceeded": False,
        "stopped": False,
    }:
        raise Pilot50Error("safe result budget is invalid")
    if value.get("pricing") != {
        "complete": True,
        "stopped": False,
        "source": evidence_contract["pricing_source"],
        "contract_id": evidence_contract["pricing_contract_id"],
        "rate_card_sha256": PRICING_RATE_CARD_SHA256,
        "target_telemetry_preserved": True,
        "target_telemetry_pricing_complete": evidence_contract[
            "target_telemetry_pricing_complete"
        ],
    }:
        raise Pilot50Error("safe result pricing is invalid")
    latency = value.get("latency_ms")
    if not isinstance(latency, dict) or set(latency) != {"p50", "p95"}:
        raise Pilot50Error("safe result latency is invalid")
    if any(type(item) is not int or item < 0 for item in latency.values()):
        raise Pilot50Error("safe result latency values are invalid")
    if latency["p50"] > latency["p95"]:
        raise Pilot50Error("safe result latency percentiles are inconsistent")
    if (
        _finite_nonnegative_number(value.get("llm_cost_rub"), label="safe LLM cost")
        > float(evidence_contract["max_llm_cost_rub"])
    ):
        raise Pilot50Error("safe LLM cost exceeds the Pilot50 cap")
    for field in ("cases_sha256", "report_sha256"):
        if not isinstance(value.get(field), str) or SHA256_RE.fullmatch(value[field]) is None:
            raise Pilot50Error(f"safe result {field} is invalid")
    if value.get("disclaimer") != DISCLAIMER:
        raise Pilot50Error("safe result disclaimer is invalid")
    return dict(value)


def _validated_review_text(value: Any, *, label: str, allow_empty: bool) -> str:
    if not isinstance(value, str) or len(value) > MAX_REVIEW_TEXT_LENGTH:
        raise Pilot50Error(f"{label} is not a bounded string")
    if not allow_empty and not value.strip():
        raise Pilot50Error(f"{label} is empty")
    return value


def build_review_rows(
    *,
    manifest_path: Path,
    cases_path: Path,
    report_path: Path,
    safe_result_path: Path,
    expected_runtime_git_sha: str,
) -> list[dict[str, Any]]:
    """Build an owner-only JSONL view bound to the already validated Pilot50 run."""

    runtime_git_sha = _validated_runtime_git_sha(expected_runtime_git_sha)
    cases, cases_bytes, cases_sha, receipt = _validate_materialized_cases(
        manifest_path,
        cases_path,
    )
    safe_bytes = _read_regular_bytes(
        safe_result_path,
        max_bytes=MAX_SAFE_BYTES,
        label="safe result",
    )
    safe = validate_safe_result(_load_json_bytes(safe_bytes, label="safe result"))
    if safe["dataset_id"] != receipt["dataset_id"]:
        raise Pilot50Error("safe result dataset differs from the reviewed Pilot50 set")
    evidence_contract = _safe_result_evidence_contract(str(safe["dataset_id"]))
    if safe["runtime_git_sha"] != runtime_git_sha:
        raise Pilot50Error("safe result runtime differs from the expected runtime")
    if safe["cases_sha256"] != cases_sha or cases_sha != _sha256(cases_bytes):
        raise Pilot50Error("safe result cases binding is invalid")

    report_bytes = _read_regular_bytes(
        report_path,
        max_bytes=MAX_REPORT_BYTES,
        label="ask report",
    )
    if safe["report_sha256"] != _sha256(report_bytes):
        raise Pilot50Error("safe result report binding is invalid")
    report = _load_json_bytes(report_bytes, label="ask report")
    if not isinstance(report, dict):
        raise Pilot50Error("ask report must be a JSON object")
    if (
        report.get("target") != evidence_contract["target"]
        or report.get("cases_total") != EXPECTED_CASES_TOTAL
        or report.get("eval_run_id") != safe["eval_run_id"]
        or report.get("cases_file_sha256") != cases_sha
        or _validated_run_window(report) != safe["run_window_utc"]
    ):
        raise Pilot50Error("ask report does not bind the reviewed Pilot50 run")
    runtime_identity = report.get("runtime_identity")
    if (
        not isinstance(runtime_identity, dict)
        or set(runtime_identity) != RUNTIME_IDENTITY_FIELDS
        or runtime_identity.get("required") is not True
        or runtime_identity.get("status") != "verified"
        or runtime_identity.get("expected_runtime_git_sha") != runtime_git_sha
        or runtime_identity.get("preflight_release_git_sha") != runtime_git_sha
        or runtime_identity.get("postflight_release_git_sha") != runtime_git_sha
        or runtime_identity.get("verified_release_git_sha") != runtime_git_sha
        or runtime_identity.get("matched_expected_runtime") is not True
    ):
        raise Pilot50Error("ask report runtime does not bind the reviewed Pilot50 run")
    cost_control = report.get("cost_control")
    reservation = cost_control.get("reservation") if isinstance(cost_control, dict) else None
    if (
        not isinstance(reservation, dict)
        or cost_control.get("high_cost_approval_id") != safe["approval_id"]
        or reservation.get("high_cost_approval_id") != safe["approval_id"]
        or reservation.get("runtime_git_sha") != runtime_git_sha
        or reservation.get("cases_file_sha256") != cases_sha
        or reservation.get("scope") != evidence_contract["cost_scope"]
        or reservation.get("approved_cap_rub")
        != evidence_contract["max_llm_cost_rub"]
    ):
        raise Pilot50Error("ask report approval does not bind the reviewed Pilot50 run")
    if evidence_contract["reservation_private_full"] is True and (
        reservation.get("private_full") is not True
        or reservation.get("reservation_class") != "private_full"
    ):
        raise Pilot50Error("candidate review reservation is not private-full")

    results = report.get("results")
    if not isinstance(results, list) or len(results) != EXPECTED_CASES_TOTAL:
        raise Pilot50Error("ask report must contain exactly 50 result rows")
    by_id: dict[str, Mapping[str, Any]] = {}
    for result in results:
        if not isinstance(result, Mapping):
            raise Pilot50Error("ask result row must be an object")
        case_id = str(result.get("id") or "")
        if not case_id or case_id in by_id:
            raise Pilot50Error("ask result membership is invalid")
        by_id[case_id] = result

    review_rows: list[dict[str, Any]] = []
    closure_counts: Counter[str] = Counter()
    policy_counts: Counter[str] = Counter()
    for ordinal, case in enumerate(cases, start=1):
        case_id = str(case["id"])
        result = by_id.get(case_id)
        if result is None:
            raise Pilot50Error("ask report membership does not match Pilot50")
        group = str(case.get("pilot50_group") or "")
        if group not in EXPECTED_TYPE_COUNTS:
            raise Pilot50Error("materialized case type is invalid")
        query = _validated_review_text(
            result.get("query"),
            label="review query",
            allow_empty=False,
        )
        if query != case.get("query"):
            raise Pilot50Error("ask report query does not match Pilot50")
        response = _validated_review_text(
            result.get("response"),
            label="review response",
            allow_empty=True,
        )
        passed = result.get("passed")
        was_escalated = result.get("was_escalated")
        if type(passed) is not bool or type(was_escalated) is not bool:
            raise Pilot50Error("review verdict fields must be typed booleans")
        observed_behavior = result.get("observed_behavior")
        if observed_behavior not in OBSERVED_BEHAVIORS:
            raise Pilot50Error("review behavior is invalid")
        escalation_reason = result.get("escalation_reason")
        if escalation_reason is not None and (
            not isinstance(escalation_reason, str)
            or SAFE_REASON_RE.fullmatch(escalation_reason) is None
        ):
            raise Pilot50Error("review escalation reason is invalid")
        if result.get("http_success") is not True or result.get("trace_found") is not True:
            raise Pilot50Error("review row lacks successful execution evidence")
        if result.get("cache_hit") is not False:
            raise Pilot50Error("review row violates the cache invariant")
        if result.get("error") is not None or result.get("trace_error") not in (None, ""):
            raise Pilot50Error("review row contains an execution error")
        if passed:
            policy_counts[group] += 1
        if passed and observed_behavior == "answer" and was_escalated is False:
            closure_counts[group] += 1
        row = {
            "ordinal": ordinal,
            "group": group,
            "query": query,
            "response": response,
            "was_escalated": was_escalated,
            "escalation_reason": escalation_reason,
            "passed": passed,
            "observed_behavior": observed_behavior,
        }
        if set(row) != REVIEW_FIELDS:
            raise Pilot50Error("review row fields are invalid")
        review_rows.append(row)
    if set(by_id) != {str(case["id"]) for case in cases}:
        raise Pilot50Error("ask report membership does not match Pilot50")
    for group in ("typical", "atypical"):
        if (
            safe["mechanical_first_turn_closure"][group]["closed"]
            != closure_counts[group]
            or safe["policy_pass"][group]["passed"] != policy_counts[group]
        ):
            raise Pilot50Error("safe result verdicts do not match review rows")
    return review_rows


def _validated_output_parent(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise Pilot50Error("output already exists")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise Pilot50Error("output parent must already exist") from exc
    if not parent.is_dir() or parent.is_symlink():
        raise Pilot50Error("output parent must be a real directory")
    return parent


def _write_exclusive_json(path: Path, value: Any) -> str:
    parent = _validated_output_parent(path)
    payload = _canonical_json_bytes(value)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _validated_output_parent(path)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise Pilot50Error("output already exists") from exc
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return _sha256(payload)


def _prepare(args: argparse.Namespace) -> dict[str, Any]:
    cases, receipt = build_materialized_cases(args.manifest)
    written_sha = _write_exclusive_json(args.output, cases)
    if written_sha != receipt["cases_sha256"]:
        raise Pilot50Error("written case hash mismatch")
    return receipt


async def _summarize(args: argparse.Namespace) -> dict[str, Any]:
    report_bytes = _read_regular_bytes(
        args.report,
        max_bytes=MAX_REPORT_BYTES,
        label="ask report",
    )
    report = _load_json_bytes(report_bytes, label="ask report")
    if not isinstance(report, dict):
        raise Pilot50Error("ask report must be a JSON object")
    eval_run_id = _validated_eval_run_id(report.get("eval_run_id"))
    trace_rows = await _fetch_trace_rows(eval_run_id)
    safe = build_safe_result(
        manifest_path=args.manifest,
        cases_path=args.cases,
        report_path=args.report,
        trace_rows=trace_rows,
        expected_runtime_git_sha=args.expected_runtime_git_sha,
        expected_approval_id=args.expected_approval_id,
        candidate_contract=getattr(args, "candidate_contract", "") or None,
        report_snapshot=report_bytes,
    )
    validate_safe_result(safe)
    _write_exclusive_json(args.output, safe)
    return safe


def _show_safe(args: argparse.Namespace) -> dict[str, Any]:
    payload = _read_regular_bytes(
        args.input,
        max_bytes=MAX_SAFE_BYTES,
        label="safe result",
    )
    value = _load_json_bytes(payload, label="safe result")
    return validate_safe_result(value)


def _show_review(args: argparse.Namespace) -> list[dict[str, Any]]:
    return build_review_rows(
        manifest_path=args.manifest,
        cases_path=args.cases,
        report_path=args.report,
        safe_result_path=args.safe_result,
        expected_runtime_git_sha=args.expected_runtime_git_sha,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare and summarize Pilot50 evidence.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    prepare.add_argument("--output", type=Path, required=True)
    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    summarize.add_argument("--cases", type=Path, required=True)
    summarize.add_argument("--report", type=Path, required=True)
    summarize.add_argument("--output", type=Path, required=True)
    summarize.add_argument("--expected-runtime-git-sha", required=True)
    summarize.add_argument("--expected-approval-id", required=True)
    summarize.add_argument(
        "--candidate-contract",
        choices=(CANDIDATE_CONTRACT_ID,),
        default="",
        help=(
            "Required fixed evidence contract for the Pilot50 v2 candidate run."
        ),
    )
    show_safe = subparsers.add_parser("show-safe")
    show_safe.add_argument("--input", type=Path, required=True)
    show_review = subparsers.add_parser("show-review")
    show_review.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    show_review.add_argument("--cases", type=Path, required=True)
    show_review.add_argument("--report", type=Path, required=True)
    show_review.add_argument("--safe-result", type=Path, required=True)
    show_review.add_argument("--expected-runtime-git-sha", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "prepare":
            result = _prepare(args)
        elif args.command == "summarize":
            result = asyncio.run(_summarize(args))
        elif args.command == "show-review":
            result = _show_review(args)
        else:
            result = _show_safe(args)
    except (Pilot50Error, OSError) as exc:
        reason = "output_exists" if "output already exists" in str(exc) else "validation_failed"
        print(f"pilot50={args.command.upper()} reason={reason}")
        return 2
    if args.command == "show-review":
        for row in result:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
