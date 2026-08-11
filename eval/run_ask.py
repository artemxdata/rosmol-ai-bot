from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
import httpx

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.ask_cases import build_seed_ask_cases
from eval.cost_governance import (
    ROUTINE_LIVE_EVAL_MAX_CASES,
    ROUTINE_LIVE_EVAL_MAX_COST_RUB,
    CostGovernanceError,
    LiveEvalCostReservation,
    reserve_live_eval_cost,
)
from eval.cost_governance import (
    approval_required as cost_approval_required,
)
from src.config import get_settings
from src.graph.provenance import (
    PROVENANCE_SCHEMA_VERSION as PIPELINE_LINEAGE_SCHEMA_VERSION,
)
from src.response_contract import ResponseProfileName
from src.security import eval_cache_bypass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DATA_ROOT = PROJECT_ROOT / "data" / "private"
CANONICAL_HOLDOUT_LEDGER_DIRNAME = "sealed-holdout-ledger-v1"
CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME = "calibration-replay-ledger-v1"
PRIVATE_TICKET_DERIVED = "private_ticket_derived"
PRIVATE_HOLDOUT_SPLIT = "holdout"
PRIVATE_EVAL_SPLITS = frozenset({"calibration", "validation", PRIVATE_HOLDOUT_SPLIT})
ALLOWED_PRIVACY_CLASSES = {"standard", PRIVATE_TICKET_DERIVED}
PRIVATE_EVAL_HOSTS = {"localhost", "127.0.0.1", "::1", "app-ml"}
SOURCE_DIAGNOSTIC_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
PHASE0_SERVER_LOCAL_ASK_HOSTS = {"app-ml", "rosmol-phase0-ml"}
PHASE0_SERVER_LOCAL_TRACE_HOSTS = {"postgres", "rosmol-postgres"}
PHASE0_SERVER_LOCAL_OWNER_EXCEPTION_ENV = "PHASE0_SERVER_LOCAL_OWNER_EXCEPTION"
HUMAN_REVIEW_MODE = "human_reviewed"
MODEL_ASSISTED_PRERUN_MODE = "model_assisted_prerun"
SOURCE_OBSERVED_DIAGNOSTIC_MODE = "source_observed_diagnostic"
PHASE0_APPROVAL_ID = "RAG-PHASE0-30-20260805"
PHASE0_COST_CAP_RUB = 200.0
PHASE0_CASES_TOTAL = 30
PHASE0_COST_SCOPE = "phase0-social-30"
PHASE0_COST_LEDGER_DIRNAME = "phase0-social-30-cost-ledger-v1"
PILOT50_REPRICING_CONTRACT_ID = "pilot50-c38-pricing-v1"
PILOT50_REPRICING_RUNTIME_SHA = "c38f0e055630fae2af50720fae81acee20ff4f6a"
PILOT50_REPRICING_CASES_SHA256 = (
    "65da11ebc790b37e0b8e5dff2601f6cc2cd3956d17652f7d74ab95eb1c21c6ed"
)
PILOT50_REPRICING_TARGET = "http://app-ml:8000/ask"
PILOT50_REPRICING_CASES_TOTAL = 50
PILOT50_REPRICING_COST_CAP_RUB = 20.0
PILOT50_REPRICING_RATE_CARD = {
    "complex_input_price_rub_per_million": "569.34",
    "complex_model": "GigaChat/GigaChat-2-Max",
    "complex_official_price_rub_per_million": "569.3374",
    "complex_output_price_rub_per_million": "569.34",
    "complex_price_policy": "conservative_round_up",
    "simple_input_price_rub_per_million": "12.2",
    "simple_model": "ai-sage/GigaChat3-10B-A1.8B",
    "simple_output_price_rub_per_million": "12.2",
}
PILOT50_REPRICING_RATE_CARD_SHA256 = hashlib.sha256(
    json.dumps(
        PILOT50_REPRICING_RATE_CARD,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()
PILOT50_REPRICING_MODELS: dict[str, tuple[Decimal, Decimal]] = {
    "ai-sage/GigaChat3-10B-A1.8B": (Decimal("12.2"), Decimal("12.2")),
    "GigaChat/GigaChat-2-Max": (Decimal("569.34"), Decimal("569.34")),
}
PILOT50_CANDIDATE_CONTRACT_ID = "pilot50-v2-candidate-v1"
PILOT50_CANDIDATE_CASES_SHA256 = (
    "b027e469e062682b6dc341b2dd4c87440edffb1955c2111f38e6c44a92a3a14d"
)
PILOT50_CANDIDATE_TARGET = "http://pilot50-candidate-ml:8000/ask"
PILOT50_CANDIDATE_CASES_TOTAL = 50
PILOT50_CANDIDATE_COST_CAP_RUB = 30.0
PILOT50_CANDIDATE_COST_SCOPE = "pilot50-v2-candidate"
PHASE0_RUNNER_CASE_FIELDS = frozenset(
    {
        "id",
        "query",
        "privacy_class",
        "split",
        "label_status",
        "requires_human_review",
        "user_id",
        "channel",
        "tags",
    }
)
PHASE0_RUNNER_TAGS = [
    "benchmark:social_only_v1",
    "measurement:source_observed_diagnostic",
    "split:calibration",
]
HOLDOUT_REVIEW_MODES = frozenset(
    {
        HUMAN_REVIEW_MODE,
        MODEL_ASSISTED_PRERUN_MODE,
    }
)
HOLDOUT_CONTRACT_SCHEMA_VERSION = "1.1.0"
MODEL_ASSISTED_REPORT_STATUS = "provisional_model_assisted_prerun"
CALIBRATION_REPLAY_REPORT_STATUS = "exposed_holdout_calibration_replay"
EXPECTED_HOLDOUT_CASES_TOTAL = 80
HOLDOUT_CONTRACT_FIELDS = frozenset(
    {
        "schema_version",
        "baseline_id",
        "runtime_git_sha",
        "review_mode",
        "product_verdict_eligible",
        "freeze_contract_sha256",
        "review_manifest_sha256",
        "selected_case_ids_sha256",
        "cases_payload_sha256",
        "knowledge_base_seed_sha256",
        "review_workbook_sha256",
        "source_cases_sha256",
        "selection_manifest_sha256",
        "cases_total",
        "execution_allowed",
    }
)
FULL_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SAFE_BASELINE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SAFE_COST_APPROVAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}")
SAFE_GOLD_TICKET_HASH_RE = re.compile(r"[0-9a-f]{12,64}")
SAFE_GOLD_STEP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}")
HOLDOUT_MARKER_RE = re.compile(r"(?:^|[:_./-])holdout(?:$|[:_./-])")

FALSE_INSUFFICIENT_SOURCE_RE = re.compile(
    r"(ответ[а]?[^.!?]{0,120}\s+в\s+источник(?:е|ах)\s+нет|"
    r"в\s+(?:предоставленн(?:ом|ых)\s+)?источник(?:е|ах)\s+нет\s+информации|"
    r"из\s+(?:представленных|переданных)\s+источников\s+невозможно\s+ответить|"
    r"источники\s+не\s+(?:содержат|подтверждают)|"
    r"информации\s+(?:в\s+источниках\s+)?нет|"
    r"информаци[яи][^.!?]{0,160}отсутств)",
    flags=re.IGNORECASE,
)
DATE_SEPARATOR_SPACING_RE = re.compile(r"(?<=\d)\s*([./-])\s*(?=\d)")
NON_ANSWER_RE = re.compile(
    r"(уже\s+был[ао]?\s+предоставлен[ао]?\s+в\s+источник(?:е|ах)|"
    r"смотрите\s+источник|"
    r"обратитесь\s+к\s+источнику)",
    flags=re.IGNORECASE,
)
EXPECTED_BEHAVIORS = {"answer", "clarify", "scope_note", "escalate"}
EXPECTED_RESPONSE_PROFILES = {profile.value for profile in ResponseProfileName}
SOURCE_DIAGNOSTIC_EXPECTATION_FIELDS = frozenset(
    {
        "allowed_cited_source_types",
        "answer_contains",
        "acceptable_chunk_ids",
        "behavior",
        "equivalent_chunk_ids",
        "equivalent_chunks",
        "expected_answer_contains",
        "expected_behavior",
        "expected_chunk_ids",
        "expected_chunks",
        "expected_cited_chunk_ids",
        "expected_cited_sources",
        "expected_escalated",
        "expected_escalation_reason",
        "expected_generator_model",
        "expected_message_masked_contains",
        "expected_profile",
        "expected_response_profile",
        "expected_response_type",
        "forbidden_message_masked_contains",
        "forbidden_profiles",
        "forbidden_response_profiles",
        "qrels",
        "relevant_chunk_ids",
        "response_profile",
    }
)
EVAL_CRITICAL_FORBIDDEN_RESPONSE_PROFILES: dict[str, tuple[str, ...]] = {
    "dates": ("application", "selection_status", "travel"),
    "application": ("dates", "selection_status", "travel"),
    "selection_status": ("application", "dates", "travel"),
    "travel": ("application", "selection_status"),
}
EVAL_RESPONSE_PROFILE_MARKERS: dict[str, tuple[str, ...]] = {
    "application": (
        "подать заяв",
        "подача заяв",
        "заявку пода",
        "прием заяв",
        "регистрац",
        "зарегистр",
        "заполнить анкет",
        "отправить заяв",
        "дедлайн подачи",
    ),
    "selection_status": (
        "статус заяв",
        "результат отбора",
        "результаты отбора",
        "прошел отбор",
        "прошла отбор",
        "одобрен",
        "отклонен",
        "резерв",
        "список участник",
        "списки участник",
        "решение по заяв",
    ),
    "travel": (
        "трансфер",
        "проезд",
        "авиабилет",
        "железнодорожн",
        "ж/д билет",
        "вокзал",
        "аэропорт",
        "точка сбора",
        "точки сбора",
        "довез",
        "отвез",
        "подвез",
        "как добраться",
        "добраться до",
        "как доехать",
        "доехать до",
        "доставят участник",
        "встретят участник",
        "расходы на дорогу",
        "оплатить дорогу",
    ),
}
EVAL_EVENT_DATE_RE = re.compile(
    r"\b(?:форум|мероприятие|смена|фестиваль|слет|съезд|лагерь)\b"
    r"[^.!?]{0,100}\b(?:проход(?:ит|ил|ила|ят)|пройдет|состоится|"
    r"начнется|завершится|закончится)\b"
    r"|"
    r"\b(?:пройдет|состоится|начнется|завершится|закончится)\b"
    r"[^.!?]{0,100}\b(?:форум|мероприятие|смена|фестиваль|слет|съезд|лагерь)\b",
    flags=re.IGNORECASE,
)
EVAL_EVENT_DATE_MARKERS = (
    "даты проведения",
    "дата проведения",
    "период проведения",
    "дни проведения",
    "начало мероприятия",
    "окончание мероприятия",
)
SCOPE_NOTE_MARKERS = (
    "я отвечаю на вопросы по мероприятиям",
    "форумам, фгаис",
    "грантам росмолодежи",
    "задай, пожалуйста, вопрос по этим темам",
)
CLARIFICATION_MARKERS = (
    "уточни",
    "уточните",
    "речь о",
    "название форума",
    "какой форум",
    "каком форуме",
    "тему вопроса",
)


def _normalize_case(
    raw: dict[str, Any],
    *,
    allow_model_assisted_prerun: bool = False,
    allow_source_observed_diagnostic: bool = False,
) -> dict[str, Any]:
    label_status = str(raw.get("label_status") or "").strip().casefold()
    review_flag = raw.get("requires_human_review")
    review_required = review_flag is True or (
        isinstance(review_flag, str)
        and review_flag.strip().casefold() in {"1", "true", "yes"}
    )
    model_assisted_prerun = label_status == MODEL_ASSISTED_PRERUN_MODE
    source_observed_diagnostic = (
        label_status == SOURCE_OBSERVED_DIAGNOSTIC_MODE
    )
    if label_status.startswith("weak_") or (
        review_required and not model_assisted_prerun
    ):
        raise ValueError(
            "ask eval cases requiring human review cannot be executed"
        )
    if model_assisted_prerun and not allow_model_assisted_prerun:
        raise ValueError(
            "model_assisted_prerun requires an explicit runner opt-in"
        )
    if source_observed_diagnostic and not allow_source_observed_diagnostic:
        raise ValueError(
            "source_observed_diagnostic requires an explicit runner opt-in"
        )
    query = raw.get("query") or raw.get("question") or raw.get("text")
    if not query:
        raise ValueError("ask eval case must contain query, question, or text")
    privacy_class = str(raw.get("privacy_class") or "standard").strip().casefold()
    split = str(raw.get("split") or "").strip().casefold()
    tags = _string_list(raw.get("tags") or [])
    gold_ticket_case = "gold_ticket:v1" in {tag.casefold() for tag in tags}
    gold_ticket_id_hash = str(raw.get("ticket_id_hash") or "").strip()
    gold_step_id = str(raw.get("step_id") or "").strip()
    if gold_ticket_case:
        if SAFE_GOLD_TICKET_HASH_RE.fullmatch(gold_ticket_id_hash) is None:
            raise ValueError("GoldTicket eval case requires a safe ticket_id_hash")
        if SAFE_GOLD_STEP_ID_RE.fullmatch(gold_step_id) is None:
            raise ValueError("GoldTicket eval case requires a safe step_id")
    user_id = str(raw.get("user_id") or "ask-eval")
    if privacy_class not in ALLOWED_PRIVACY_CLASSES:
        raise ValueError(
            "privacy_class must be one of: "
            f"{', '.join(sorted(ALLOWED_PRIVACY_CLASSES))}"
        )
    if privacy_class == PRIVATE_TICKET_DERIVED:
        if label_status == HUMAN_REVIEW_MODE:
            if review_flag is not False:
                raise ValueError(
                    "human-reviewed private_ticket_derived cases require "
                    "requires_human_review=false"
                )
        elif label_status == MODEL_ASSISTED_PRERUN_MODE:
            if split != PRIVATE_HOLDOUT_SPLIT or review_flag is not True:
                raise ValueError(
                    "model_assisted_prerun is only valid for a sealed holdout "
                    "with requires_human_review=true"
                )
        elif source_observed_diagnostic:
            if split != "calibration" or review_flag is not False:
                raise ValueError(
                    "source_observed_diagnostic is only valid for private "
                    "calibration with requires_human_review=false"
                )
            if _has_explicit_case_expectation(raw):
                raise ValueError(
                    "source_observed_diagnostic cannot contain expected "
                    "behavior, qrels, citations, or verdict fields"
                )
        else:
            raise ValueError(
                "private_ticket_derived cases require an explicit "
                "human-reviewed or model_assisted_prerun verdict"
            )
        if split not in PRIVATE_EVAL_SPLITS:
            raise ValueError(
                "private_ticket_derived cases require an explicit split: "
                f"{', '.join(sorted(PRIVATE_EVAL_SPLITS))}"
            )
    elif model_assisted_prerun:
        raise ValueError(
            "model_assisted_prerun requires privacy_class=private_ticket_derived"
        )
    elif source_observed_diagnostic:
        raise ValueError(
            "source_observed_diagnostic requires privacy_class=private_ticket_derived"
        )

    split_holdout = split == PRIVATE_HOLDOUT_SPLIT
    tag_holdout = any(_has_holdout_marker(tag) for tag in tags)
    user_holdout = _has_holdout_marker(user_id)
    contract_holdout = "holdout_contract" in raw
    holdout_markers = {
        "split": split_holdout,
        "tag": tag_holdout,
        "user_id": user_holdout,
        "contract": contract_holdout,
    }
    if any(holdout_markers.values()) and (
        privacy_class != PRIVATE_TICKET_DERIVED
        or not all(holdout_markers.values())
    ):
        raise ValueError(
            "holdout markers in split, tags, user_id, and holdout_contract "
            f"must be present and consistent: {holdout_markers}"
        )
    private_split_tags = {
        tag.split(":", maxsplit=1)[1].strip().casefold()
        for tag in tags
        if tag.casefold().startswith("split:") and ":" in tag
    }
    if privacy_class == PRIVATE_TICKET_DERIVED and private_split_tags not in (
        set(),
        {split},
    ):
        raise ValueError("private_ticket_derived split tags must match split")

    holdout_contract: dict[str, Any] | None = None
    if split_holdout:
        holdout_contract = _normalize_holdout_contract(raw.get("holdout_contract"))
        if holdout_contract["review_mode"] != label_status:
            raise ValueError(
                "holdout_contract review_mode must match case label_status"
            )

    expected_behavior = None
    if not source_observed_diagnostic:
        expected_behavior = _normalize_expected_behavior(
            raw.get("expected_behavior")
            or raw.get("expected_response_type")
            or raw.get("behavior")
        ) or _infer_expected_behavior(raw, str(query))
    expected_chunk_ids = _string_list(
        raw.get("expected_chunk_ids")
        or raw.get("expected_chunks")
        or raw.get("relevant_chunk_ids")
        or []
    )
    expected_answer_contains = _string_list(
        raw.get("expected_answer_contains") or raw.get("answer_contains") or []
    )
    expected_message_masked_contains = _string_list(
        raw.get("expected_message_masked_contains") or []
    )
    forbidden_message_masked_contains = _string_list(
        raw.get("forbidden_message_masked_contains") or []
    )
    expected_cited_chunk_ids = _string_list(
        raw.get("expected_cited_chunk_ids") or raw.get("expected_cited_sources") or []
    )
    allowed_cited_source_types = sorted(
        {
            source_type.strip().casefold()
            for source_type in _string_list(
                raw.get("allowed_cited_source_types") or []
            )
            if source_type.strip()
        }
    )
    equivalent_chunk_ids = _equivalent_chunk_id_map(
        raw.get("equivalent_chunk_ids")
        or raw.get("equivalent_chunks")
        or raw.get("acceptable_chunk_ids")
        or {},
        expected_chunk_ids,
    )
    if expected_behavior and expected_behavior != "answer":
        expected_chunk_ids = []
        expected_cited_chunk_ids = []
        equivalent_chunk_ids = {}

    expected_response_profile = _normalize_expected_response_profile(
        raw.get("expected_response_profile")
        or raw.get("expected_profile")
        or raw.get("response_profile")
    )
    forbidden_response_profiles = set(
        _normalize_response_profile_list(
            raw.get("forbidden_response_profiles")
            or raw.get("forbidden_profiles")
            or []
        )
    )
    forbidden_response_profiles.update(
        EVAL_CRITICAL_FORBIDDEN_RESPONSE_PROFILES.get(
            expected_response_profile or "",
            (),
        )
    )

    normalized = {
        "id": str(raw.get("id") or raw.get("case_id") or query),
        "query": str(query),
        "privacy_class": privacy_class,
        "user_id": user_id,
        "channel": str(raw.get("channel") or "api"),
        "forum_context": str(raw.get("forum_context") or "").strip() or None,
        "expected_chunk_ids": expected_chunk_ids,
        "expected_cited_chunk_ids": expected_cited_chunk_ids,
        "allowed_cited_source_types": allowed_cited_source_types,
        "equivalent_chunk_ids": equivalent_chunk_ids,
        "expected_answer_contains": expected_answer_contains,
        "expected_message_masked_contains": expected_message_masked_contains,
        "forbidden_message_masked_contains": forbidden_message_masked_contains,
        "expected_behavior": expected_behavior,
        "expected_response_profile": expected_response_profile,
        "forbidden_response_profiles": sorted(forbidden_response_profiles),
        "expected_escalated": raw.get("expected_escalated"),
        "expected_escalation_reason": raw.get("expected_escalation_reason"),
        "expected_generator_model": raw.get("expected_generator_model"),
        "tags": tags,
    }
    if label_status:
        normalized["label_status"] = label_status
    if split:
        normalized["split"] = split
    if gold_ticket_case:
        normalized["ticket_id_hash"] = gold_ticket_id_hash
        normalized["step_id"] = gold_step_id
    if holdout_contract is not None:
        normalized["holdout_contract"] = holdout_contract
    return normalized


def _has_explicit_case_expectation(raw: Mapping[str, Any]) -> bool:
    for field in SOURCE_DIAGNOSTIC_EXPECTATION_FIELDS:
        if field not in raw:
            continue
        value = raw[field]
        if value not in (None, "", [], {}):
            return True
    return False


def _normalize_holdout_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(
            "private_ticket_derived holdout cases require holdout_contract"
        )
    fields = set(value)
    if fields != HOLDOUT_CONTRACT_FIELDS:
        missing = sorted(HOLDOUT_CONTRACT_FIELDS - fields)
        unexpected = sorted(fields - HOLDOUT_CONTRACT_FIELDS)
        raise ValueError(
            "holdout_contract fields must match schema exactly: "
            f"missing={missing}, unexpected={unexpected}"
        )

    schema_version = str(value["schema_version"] or "").strip()
    if schema_version != HOLDOUT_CONTRACT_SCHEMA_VERSION:
        raise ValueError(
            "holdout_contract schema_version must be "
            f"{HOLDOUT_CONTRACT_SCHEMA_VERSION}"
        )
    raw_baseline_id = str(value["baseline_id"] or "")
    baseline_id = raw_baseline_id.strip()
    if (
        baseline_id != raw_baseline_id
        or SAFE_BASELINE_ID_RE.fullmatch(baseline_id) is None
    ):
        raise ValueError(
            "holdout_contract baseline_id must be 1-128 safe ASCII characters "
            "using only letters, digits, dot, underscore, or hyphen"
        )
    runtime_git_sha = str(value["runtime_git_sha"] or "").strip()
    if FULL_GIT_SHA_RE.fullmatch(runtime_git_sha) is None:
        raise ValueError(
            "holdout_contract runtime_git_sha must be a full lowercase 40-hex SHA"
        )
    review_mode = str(value["review_mode"] or "").strip().casefold()
    if review_mode not in HOLDOUT_REVIEW_MODES:
        raise ValueError(
            "holdout_contract review_mode must be one of: "
            + ", ".join(sorted(HOLDOUT_REVIEW_MODES))
        )
    product_verdict_eligible = value["product_verdict_eligible"]
    if not isinstance(product_verdict_eligible, bool):
        raise ValueError(
            "holdout_contract product_verdict_eligible must be boolean"
        )
    if product_verdict_eligible is not (
        review_mode == HUMAN_REVIEW_MODE
    ):
        raise ValueError(
            "holdout_contract product_verdict_eligible conflicts with review_mode"
        )

    hashes: dict[str, str] = {}
    for field in (
        "freeze_contract_sha256",
        "review_manifest_sha256",
        "selected_case_ids_sha256",
        "cases_payload_sha256",
        "knowledge_base_seed_sha256",
        "review_workbook_sha256",
        "source_cases_sha256",
        "selection_manifest_sha256",
    ):
        digest = str(value[field] or "").strip()
        if SHA256_RE.fullmatch(digest) is None:
            raise ValueError(
                f"holdout_contract {field} must be a lowercase SHA-256 digest"
            )
        hashes[field] = digest

    cases_total = value["cases_total"]
    if (
        not isinstance(cases_total, int)
        or isinstance(cases_total, bool)
        or cases_total != EXPECTED_HOLDOUT_CASES_TOTAL
    ):
        raise ValueError(
            "holdout_contract cases_total must be exactly "
            f"{EXPECTED_HOLDOUT_CASES_TOTAL}"
        )
    if value["execution_allowed"] is not True:
        raise ValueError("holdout_contract execution_allowed must be true")

    return {
        "schema_version": schema_version,
        "baseline_id": baseline_id,
        "runtime_git_sha": runtime_git_sha,
        "review_mode": review_mode,
        "product_verdict_eligible": product_verdict_eligible,
        **hashes,
        "cases_total": cases_total,
        "execution_allowed": True,
    }


def _validate_holdout_run_contract(
    cases: list[dict[str, Any]],
    *,
    raw_cases_payload_sha256: str | None,
) -> dict[str, Any] | None:
    holdout_cases = [
        case
        for case in cases
        if case.get("privacy_class") == PRIVATE_TICKET_DERIVED
        and case.get("split") == PRIVATE_HOLDOUT_SPLIT
    ]
    if not holdout_cases:
        return None
    if len(holdout_cases) != len(cases):
        raise ValueError(
            "private_ticket_derived holdout cases cannot be mixed with other eval cases"
        )

    contract = holdout_cases[0]["holdout_contract"]
    if any(case.get("holdout_contract") != contract for case in holdout_cases[1:]):
        raise ValueError("all private holdout cases must have an identical holdout_contract")

    case_ids = [str(case["id"]) for case in holdout_cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("private holdout case ids must be unique")
    if contract["cases_total"] != len(case_ids):
        raise ValueError(
            "holdout_contract cases_total does not match the loaded case count"
        )
    selected_case_ids_sha256 = hashlib.sha256(
        ("\n".join(sorted(case_ids)) + "\n").encode("utf-8")
    ).hexdigest()
    if contract["selected_case_ids_sha256"] != selected_case_ids_sha256:
        raise ValueError(
            "holdout_contract selected_case_ids_sha256 does not match loaded case ids"
        )
    if (
        raw_cases_payload_sha256 is None
        or contract["cases_payload_sha256"] != raw_cases_payload_sha256
    ):
        raise ValueError(
            "holdout_contract cases_payload_sha256 does not match raw exported cases"
        )
    return dict(contract)


def holdout_cases_payload_sha256(cases: list[dict[str, Any]]) -> str:
    """Hash raw exported cases, excluding only their repeated contract."""

    payload: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("holdout cases payload must contain only JSON objects")
        raw_without_contract = {
            str(key): value
            for key, value in case.items()
            if key != "holdout_contract"
        }
        payload.append(raw_without_contract)
    payload.sort(
        key=lambda case: (
            str(case.get("id") or ""),
            _canonical_json(case),
        )
    )
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


async def run_eval(
    cases_path: Path,
    output_path: Path,
    target: str = "http://localhost:8001/ask",
    *,
    concurrency: int = 1,
    request_timeout: float = 120.0,
    api_key_env: str | None = "API_AUTH_TOKEN",
    trace_lookup: bool = True,
    trace_dsn: str | None = None,
    kb_seed_path: Path = Path("data/knowledge_base_seed.json"),
    auto_smoke_cases: bool = False,
    max_smoke_cases: int = 50,
    markdown_path: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    bypass_cache: bool = False,
    generated_user_prefix: str | None = None,
    max_cases: int | None = None,
    max_llm_cost_rub: float | None = None,
    require_budget_for_large_runs: bool = True,
    large_run_threshold: int = 20,
    sealed_holdout: bool = False,
    expected_holdout_freeze_sha256: str | None = None,
    expected_cases_payload_sha256: str | None = None,
    expected_cases_file_sha256: str | None = None,
    holdout_ledger_dir: Path | None = None,
    allow_model_assisted_prerun: bool = False,
    calibration_replay: bool = False,
    expected_runtime_git_sha: str | None = None,
    calibration_replay_ledger_dir: Path | None = None,
    high_cost_approval_id: str | None = None,
    cost_ledger_dir: Path | None = None,
    cost_runtime_git_sha: str | None = None,
    cost_reservation: LiveEvalCostReservation | None = None,
    llm_cost_repricing_contract: str | None = None,
    pilot50_candidate_contract: str | None = None,
    require_complete_traces: bool = False,
    allow_source_observed_diagnostic: bool = False,
    phase0_manifest_path: Path | None = None,
    phase0_server_local: bool = False,
    phase0_builder_source: Path | None = None,
) -> dict[str, Any]:
    run_started_at = datetime.now(UTC)
    eval_run_id = f"ask-eval-{uuid4()}"
    if sealed_holdout and calibration_replay:
        raise ValueError("sealed_holdout and calibration_replay are mutually exclusive")
    if str(llm_cost_repricing_contract or "").strip() and str(
        pilot50_candidate_contract or ""
    ).strip():
        raise ValueError(
            "Pilot50 repricing and candidate contracts are mutually exclusive"
        )
    private_contract_run = sealed_holdout or calibration_replay
    source_diagnostic_requested = allow_source_observed_diagnostic
    strict_live = not _is_in_process_mock_transport(transport)
    effective_api_key_env = (
        "API_AUTH_TOKEN"
        if (private_contract_run or source_diagnostic_requested)
        and api_key_env is None
        else api_key_env
    )
    headers = _auth_headers(effective_api_key_env)
    cache_bypass_secret = (
        headers.get("X-API-Key", "").strip()
        if bypass_cache
        else ""
    )
    if allow_model_assisted_prerun and not private_contract_run:
        raise ValueError(
            "allow_model_assisted_prerun requires sealed_holdout or "
            "calibration_replay mode"
        )
    if allow_source_observed_diagnostic and private_contract_run:
        raise ValueError(
            "source_observed_diagnostic cannot be combined with holdout modes"
        )
    _guard_eval_artifact_aliases(
        cases_path=cases_path,
        output_path=output_path,
        markdown_path=markdown_path,
        extra_paths=[phase0_manifest_path] if phase0_manifest_path is not None else None,
    )
    (
        cases,
        generated_smoke_cases,
        raw_cases_payload_sha256,
        cases_file_sha256,
    ) = await _load_cases(
        cases_path=cases_path,
        kb_seed_path=kb_seed_path,
        auto_smoke_cases=auto_smoke_cases,
        max_smoke_cases=max_smoke_cases,
        user_prefix=generated_user_prefix or _default_generated_user_prefix("ask-eval"),
        allow_model_assisted_prerun=allow_model_assisted_prerun,
        allow_source_observed_diagnostic=allow_source_observed_diagnostic,
    )
    source_diagnostic_cases = [
        case
        for case in cases
        if case.get("label_status") == SOURCE_OBSERVED_DIAGNOSTIC_MODE
    ]
    if source_diagnostic_cases and len(source_diagnostic_cases) != len(cases):
        raise ValueError(
            "source_observed_diagnostic cases cannot be mixed with scored cases"
        )
    if bool(source_diagnostic_cases) != allow_source_observed_diagnostic:
        raise ValueError(
            "source_observed_diagnostic cases and explicit runner opt-in "
            "must be used together"
        )
    phase0_contract: dict[str, Any] | None = None
    if phase0_server_local and phase0_manifest_path is None:
        raise ValueError("--phase0-server-local requires --phase0-manifest")
    if phase0_builder_source is not None and phase0_manifest_path is None:
        raise ValueError("--phase0-builder-source requires --phase0-manifest")
    if high_cost_approval_id == PHASE0_APPROVAL_ID and phase0_manifest_path is None:
        raise ValueError(
            f"{PHASE0_APPROVAL_ID} requires --phase0-manifest before any live request"
        )
    if phase0_manifest_path is not None:
        if not source_diagnostic_cases:
            raise ValueError(
                "--phase0-manifest requires source_observed_diagnostic cases"
            )
        phase0_contract = _validated_phase0_execution_contract(
            manifest_path=phase0_manifest_path,
            cases_path=cases_path,
            cases=cases,
            cases_file_sha256=cases_file_sha256,
            expected_cases_file_sha256=expected_cases_file_sha256,
            expected_runtime_git_sha=expected_runtime_git_sha,
            high_cost_approval_id=high_cost_approval_id,
            max_llm_cost_rub=max_llm_cost_rub,
            bypass_cache=bypass_cache,
            trace_lookup=trace_lookup,
            max_cases=max_cases,
            auto_smoke_cases=auto_smoke_cases,
            generated_user_prefix=generated_user_prefix,
            concurrency=concurrency,
            target=target,
            trace_dsn=trace_dsn,
            strict_live=strict_live,
            cost_runtime_git_sha=cost_runtime_git_sha,
            cost_ledger_dir=cost_ledger_dir,
            cost_reservation=cost_reservation,
            transport=transport,
            markdown_path=markdown_path,
            server_local=phase0_server_local,
            builder_source=phase0_builder_source,
        )
        canonical_existing = [
            str(path)
            for path in (output_path, markdown_path)
            if path is not None and _path_lexists(path)
        ]
        if canonical_existing:
            raise FileExistsError(
                "Phase 0 canonical output and markdown must be absent: "
                + ", ".join(canonical_existing)
            )
    holdout_contract = _validate_holdout_run_contract(
        cases,
        raw_cases_payload_sha256=raw_cases_payload_sha256,
    )
    if (holdout_contract is not None) != private_contract_run:
        raise ValueError(
            "holdout contract cases and exactly one explicit private run mode "
            "are required together"
        )
    if holdout_contract is not None:
        model_assisted_contract = (
            holdout_contract["review_mode"]
            == MODEL_ASSISTED_PRERUN_MODE
        )
        if model_assisted_contract != allow_model_assisted_prerun:
            raise ValueError(
                "model_assisted_prerun contract and explicit runner opt-in "
                "must be used together"
            )
    if not private_contract_run and (
        expected_holdout_freeze_sha256 is not None
        or expected_cases_payload_sha256 is not None
        or holdout_ledger_dir is not None
        or calibration_replay_ledger_dir is not None
    ):
        raise ValueError(
            "private contract identity and ledger options require sealed_holdout "
            "or calibration_replay mode"
        )
    if expected_cases_file_sha256 is not None and not (
        private_contract_run or phase0_contract is not None
    ):
        raise ValueError(
            "expected_cases_file_sha256 requires a private holdout/replay or "
            "an approved Phase 0 manifest"
        )
    if expected_runtime_git_sha is not None and not (
        calibration_replay
        or source_diagnostic_cases
        or bool(str(llm_cost_repricing_contract or "").strip())
        or bool(str(pilot50_candidate_contract or "").strip())
    ):
        raise ValueError(
            "expected_runtime_git_sha is only valid for calibration_replay "
            "or source_observed_diagnostic"
        )
    if sealed_holdout and (
        expected_runtime_git_sha is not None
        or calibration_replay_ledger_dir is not None
    ):
        raise ValueError(
            "calibration runtime identity and ledger options require "
            "calibration_replay mode"
        )
    if calibration_replay and holdout_ledger_dir is not None:
        raise ValueError(
            "calibration_replay must not use or modify the sealed holdout ledger"
        )
    _guard_eval_privacy(
        cases=cases,
        cases_path=cases_path,
        output_path=output_path,
        markdown_path=markdown_path,
        target=target,
        phase0_server_local=bool(
            phase0_contract
            and phase0_contract.get("transport_mode") == "server_local"
        ),
    )
    holdout_ledger: Path | None = None
    holdout_receipt_key: str | None = None
    holdout_receipt_path: Path | None = None
    holdout_completed_receipt_path: Path | None = None
    source_holdout_contract: dict[str, Any] | None = None
    evaluation_runtime_git_sha: str | None = None
    verified_runtime_git_sha_preflight: str | None = None
    verified_runtime_git_sha_postflight: str | None = None
    prior_sealed_exposure_receipts: list[str] = []
    private_report_context: dict[str, Any] = {}
    private_run_label = (
        "calibration replay" if calibration_replay else "sealed private holdout"
    )
    if holdout_contract is not None:
        source_holdout_contract = dict(holdout_contract)
        if calibration_replay:
            evaluation_runtime_git_sha = str(
                expected_runtime_git_sha or ""
            ).strip()
            if (
                FULL_GIT_SHA_RE.fullmatch(evaluation_runtime_git_sha) is None
                or evaluation_runtime_git_sha == "0" * 40
            ):
                raise ValueError(
                    "calibration_replay requires expected_runtime_git_sha as a "
                    "non-zero full lowercase Git SHA"
                )
        expected_freeze_sha256 = str(
            expected_holdout_freeze_sha256 or ""
        ).strip()
        if SHA256_RE.fullmatch(expected_freeze_sha256) is None:
            raise ValueError(
                "private contract run requires "
                "expected_holdout_freeze_sha256 as lowercase SHA-256"
            )
        if expected_freeze_sha256 != holdout_contract["freeze_contract_sha256"]:
            raise ValueError(
                "expected_holdout_freeze_sha256 does not match holdout_contract"
            )
        expected_payload_sha256 = str(
            expected_cases_payload_sha256 or ""
        ).strip()
        if SHA256_RE.fullmatch(expected_payload_sha256) is None:
            raise ValueError(
                "private contract run requires "
                "expected_cases_payload_sha256 as lowercase SHA-256"
            )
        if (
            expected_payload_sha256
            != holdout_contract["cases_payload_sha256"]
            or expected_payload_sha256 != raw_cases_payload_sha256
        ):
            raise ValueError(
                "expected_cases_payload_sha256 does not match both "
                "holdout_contract and recomputed raw cases"
            )
        expected_file_sha256 = str(
            expected_cases_file_sha256 or ""
        ).strip()
        if SHA256_RE.fullmatch(expected_file_sha256) is None:
            raise ValueError(
                "private contract run requires "
                "expected_cases_file_sha256 as lowercase SHA-256"
            )
        if expected_file_sha256 != cases_file_sha256:
            raise ValueError(
                "expected_cases_file_sha256 does not match the exact cases file"
            )
        if calibration_replay:
            assert evaluation_runtime_git_sha is not None
            assert source_holdout_contract is not None
            prior_sealed_exposure_receipts = (
                _require_prior_sealed_exposure_receipts(
                    contract=source_holdout_contract,
                    cases_file_sha256=expected_file_sha256,
                )
            )
            holdout_ledger = _validated_calibration_replay_ledger_dir(
                calibration_replay_ledger_dir,
                cases_path=cases_path,
            )
            holdout_receipt_key = _derive_calibration_replay_receipt_key(
                selected_case_ids_sha256=holdout_contract[
                    "selected_case_ids_sha256"
                ],
                cases_file_sha256=expected_file_sha256,
                runtime_git_sha=evaluation_runtime_git_sha,
            )
        else:
            evaluation_runtime_git_sha = holdout_contract["runtime_git_sha"]
            calibration_receipts = _calibration_replay_exposure_receipts(
                selected_case_ids_sha256=holdout_contract[
                    "selected_case_ids_sha256"
                ]
            )
            if calibration_receipts:
                raise ValueError(
                    "sealed private holdout selection already has calibration "
                    "replay exposure evidence; independent execution is "
                    "forbidden"
                )
            holdout_ledger = _validated_holdout_ledger_dir(
                holdout_ledger_dir,
                cases_path=cases_path,
            )
            holdout_receipt_key = _derive_holdout_receipt_key(
                holdout_contract["selected_case_ids_sha256"]
            )
        private_report_context = {
            "calibration_replay": calibration_replay,
            "evaluation_runtime_git_sha": evaluation_runtime_git_sha,
            "prior_sealed_exposure_receipts": (
                prior_sealed_exposure_receipts
            ),
        }
        holdout_receipt_path = holdout_ledger / (
            f"{holdout_receipt_key}.started.json"
        )
        holdout_completed_receipt_path = holdout_ledger / (
            f"{holdout_receipt_key}.completed.json"
        )
        assert holdout_receipt_key is not None
        _guard_eval_artifact_aliases(
            cases_path=cases_path,
            output_path=output_path,
            markdown_path=markdown_path,
            extra_paths=[
                holdout_receipt_path,
                holdout_completed_receipt_path,
            ],
        )
        canonical_existing = [
            str(path)
            for path in (output_path, markdown_path)
            if path is not None and _path_lexists(path)
        ]
        if canonical_existing:
            await _write_holdout_rejection_report(
                ledger_dir=holdout_ledger,
                receipt_key=holdout_receipt_key,
                target=target,
                cases_path=cases_path,
                eval_run_id=eval_run_id,
                contract=holdout_contract,
                expected_cases_file_sha256=expected_file_sha256,
                status="canonical_output_exists",
                failures=["canonical_output_must_be_absent"],
                executed_cases_total=0,
                receipt_path=holdout_receipt_path,
                detail=", ".join(canonical_existing),
                **private_report_context,
            )
            raise FileExistsError(
                f"{private_run_label} canonical output and markdown must be absent"
            )
        existing_receipts = [
            path.name
            for path in (
                holdout_receipt_path,
                holdout_completed_receipt_path,
            )
            if _path_lexists(path)
        ]
        if existing_receipts:
            await _write_holdout_rejection_report(
                ledger_dir=holdout_ledger,
                receipt_key=holdout_receipt_key,
                target=target,
                cases_path=cases_path,
                eval_run_id=eval_run_id,
                contract=holdout_contract,
                expected_cases_file_sha256=expected_file_sha256,
                status="rerun_rejected",
                failures=["holdout_receipt_exists"],
                executed_cases_total=0,
                receipt_path=holdout_receipt_path,
                detail=", ".join(existing_receipts),
                **private_report_context,
            )
            raise ValueError(
                f"{private_run_label} already has a started or completed "
                "receipt; rerun is forbidden"
            )
        preflight_failures: list[str] = []
        if generated_user_prefix:
            preflight_failures.append("user_prefix_forbidden")
        if bypass_cache is not True:
            preflight_failures.append("bypass_cache_required")
        if not cache_bypass_secret:
            preflight_failures.append("cache_bypass_signature_secret_required")
        if trace_lookup is not True:
            preflight_failures.append("trace_lookup_required")
        if max_cases is not None:
            preflight_failures.append("max_cases_forbidden")
        if preflight_failures:
            await _write_holdout_rejection_report(
                ledger_dir=holdout_ledger,
                receipt_key=holdout_receipt_key,
                target=target,
                cases_path=cases_path,
                eval_run_id=eval_run_id,
                contract=holdout_contract,
                expected_cases_file_sha256=expected_file_sha256,
                status="preflight_rejected",
                failures=preflight_failures,
                executed_cases_total=0,
                receipt_path=holdout_receipt_path,
                **private_report_context,
            )
            raise ValueError(
                f"{private_run_label} preflight failed: "
                + ", ".join(preflight_failures)
            )
    elif source_diagnostic_cases:
        evaluation_runtime_git_sha = _validated_source_diagnostic_runtime_sha(
            expected_runtime_git_sha,
            required=strict_live,
        )
        if strict_live and not bypass_cache:
            raise ValueError(
                "strict-live source_observed_diagnostic requires signed cache bypass"
            )
    validated_repricing_contract = _validated_pilot50_repricing_contract(
        llm_cost_repricing_contract,
        cases=cases,
        cases_file_sha256=cases_file_sha256,
        target=target,
        concurrency=concurrency,
        trace_lookup=trace_lookup,
        bypass_cache=bypass_cache,
        max_llm_cost_rub=max_llm_cost_rub,
        max_cases=max_cases,
        auto_smoke_cases=auto_smoke_cases,
        generated_user_prefix=generated_user_prefix,
        private_contract_run=private_contract_run,
        source_diagnostic_cases=bool(source_diagnostic_cases),
        phase0_contract=phase0_contract,
        strict_live=strict_live,
        high_cost_approval_id=high_cost_approval_id,
        expected_runtime_git_sha=expected_runtime_git_sha,
    )
    if validated_repricing_contract is not None:
        evaluation_runtime_git_sha = PILOT50_REPRICING_RUNTIME_SHA
    validated_candidate_contract = _validated_pilot50_candidate_contract(
        pilot50_candidate_contract,
        cases=cases,
        cases_file_sha256=cases_file_sha256,
        target=target,
        concurrency=concurrency,
        trace_lookup=trace_lookup,
        bypass_cache=bypass_cache,
        max_llm_cost_rub=max_llm_cost_rub,
        max_cases=max_cases,
        auto_smoke_cases=auto_smoke_cases,
        generated_user_prefix=generated_user_prefix,
        private_contract_run=private_contract_run,
        source_diagnostic_cases=bool(source_diagnostic_cases),
        phase0_contract=phase0_contract,
        strict_live=strict_live,
        high_cost_approval_id=high_cost_approval_id,
        expected_runtime_git_sha=expected_runtime_git_sha,
        require_budget_for_large_runs=require_budget_for_large_runs,
        require_complete_traces=require_complete_traces,
    )
    candidate_contract_run = validated_candidate_contract is not None
    if validated_candidate_contract is not None:
        evaluation_runtime_git_sha = validated_candidate_contract["runtime_git_sha"]
    original_cases_total = len(cases)
    if max_cases is not None:
        if holdout_contract is not None:
            raise ValueError(f"--max-cases is forbidden for {private_run_label}")
        if max_cases < 1:
            raise ValueError("--max-cases must be greater than zero")
        cases = cases[:max_cases]
    validated_high_cost_approval_id: str | None = None
    validated_cost_reservation = cost_reservation
    try:
        validated_high_cost_approval_id = _guard_large_live_run_budget(
            cases=cases,
            target=target,
            transport=transport,
            max_llm_cost_rub=max_llm_cost_rub,
            require_budget=require_budget_for_large_runs,
            large_run_threshold=large_run_threshold,
            trace_lookup=trace_lookup,
            private_contract_run=(private_contract_run or candidate_contract_run),
            high_cost_approval_id=high_cost_approval_id,
        )
        if not _is_in_process_mock_transport(transport):
            pricing_failure = _local_llm_pricing_preflight_failure()
            if pricing_failure is not None:
                raise ValueError(
                    "Live ask eval pricing preflight failed: " + pricing_failure
                )
    except ValueError as exc:
        if holdout_contract is not None:
            assert holdout_ledger is not None
            assert holdout_receipt_key is not None
            await _write_holdout_rejection_report(
                ledger_dir=holdout_ledger,
                receipt_key=holdout_receipt_key,
                target=target,
                cases_path=cases_path,
                eval_run_id=eval_run_id,
                contract=holdout_contract,
                expected_cases_file_sha256=expected_file_sha256,
                status="budget_preflight_rejected",
                failures=[_cost_preflight_failure_code(exc)],
                executed_cases_total=0,
                receipt_path=holdout_receipt_path,
                detail=str(exc),
                **private_report_context,
            )
        raise
    if not cases:
        metrics = _empty_metrics(target=target, cases_path=cases_path, auto_smoke_cases=False)
        metrics["eval_run_id"] = eval_run_id
        _apply_run_limits(
            metrics,
            original_cases_total=original_cases_total,
            max_cases=max_cases,
            max_llm_cost_rub=max_llm_cost_rub,
        )
        await asyncio.to_thread(_write_json, output_path, metrics)
        if markdown_path:
            await asyncio.to_thread(_write_markdown, markdown_path, metrics)
        return metrics

    trace_pool: asyncpg.Pool | None = None
    trace_lookup_error: str | None = None
    if trace_lookup:
        errors: list[str] = []
        for candidate in _trace_dsn_candidates(trace_dsn):
            try:
                trace_pool = await asyncpg.create_pool(
                    candidate,
                    min_size=1,
                    max_size=max(1, min(concurrency, 5)),
                )
                break
            except Exception as exc:
                errors.append(type(exc).__name__)
        if trace_pool is None and errors:
            trace_lookup_error = "; ".join(errors)
    trace_unavailable = trace_pool is None or trace_lookup_error is not None
    if trace_lookup and trace_unavailable and (
        holdout_contract is not None or not _is_in_process_mock_transport(transport)
    ):
        if holdout_contract is None:
            raise RuntimeError(
                "Live ask eval requires an available PostgreSQL trace lookup "
                "connection before the first request"
            )
        failures = ["trace_lookup_unavailable"]
        assert holdout_ledger is not None
        assert holdout_receipt_key is not None
        await _write_holdout_rejection_report(
            ledger_dir=holdout_ledger,
            receipt_key=holdout_receipt_key,
            target=target,
            cases_path=cases_path,
            eval_run_id=eval_run_id,
            contract=holdout_contract,
            expected_cases_file_sha256=expected_file_sha256,
            status="trace_unavailable",
            failures=failures,
            executed_cases_total=0,
            receipt_path=holdout_receipt_path,
            trace_lookup_error=trace_lookup_error or "trace pool was not created",
            **private_report_context,
        )
        raise RuntimeError(
            f"{private_run_label} requires an available trace lookup connection"
        )

    if (
        not _is_in_process_mock_transport(transport)
        and cases
        and phase0_contract is None
        and validated_candidate_contract is None
    ):
        try:
            if validated_cost_reservation is None:
                assert max_llm_cost_rub is not None
                validated_cost_reservation = reserve_live_eval_cost(
                    scope=(
                        "calibration-replay"
                        if calibration_replay
                        else "sealed-holdout" if sealed_holdout else "ask-eval"
                    ),
                    run_id=eval_run_id,
                    runtime_git_sha=_cost_governance_runtime_git_sha(
                        explicit_sha=cost_runtime_git_sha,
                        evaluation_runtime_git_sha=evaluation_runtime_git_sha,
                    ),
                    manifest_sha256=_file_sha256(cases_path),
                    case_count=len(cases),
                    approved_cap_rub=max_llm_cost_rub,
                    private_full=private_contract_run,
                    high_cost_approval_id=validated_high_cost_approval_id,
                    ledger_dir=cost_ledger_dir,
                )
            else:
                _validate_cost_reservation_for_child_run(
                    validated_cost_reservation,
                    case_count=len(cases),
                    max_llm_cost_rub=max_llm_cost_rub,
                    private_full=private_contract_run,
                )
            if source_diagnostic_cases and evaluation_runtime_git_sha is not None:
                reservation_runtime_git_sha = str(
                    validated_cost_reservation.record.get("runtime_git_sha") or ""
                )
                if reservation_runtime_git_sha != evaluation_runtime_git_sha:
                    raise CostGovernanceError(
                        "source_observed_diagnostic cost reservation runtime Git "
                        "SHA differs from the expected evaluation runtime"
                    )
            if private_contract_run:
                assert holdout_contract is not None
                assert holdout_ledger is not None
                assert validated_high_cost_approval_id is not None
                _reserve_private_full_eval(
                    ledger_dir=holdout_ledger,
                    eval_run_id=eval_run_id,
                    run_mode=(
                        "calibration_replay"
                        if calibration_replay
                        else "sealed_holdout"
                    ),
                    contract=holdout_contract,
                    evaluation_runtime_git_sha=evaluation_runtime_git_sha,
                    high_cost_approval_id=validated_high_cost_approval_id,
                    max_llm_cost_rub=max_llm_cost_rub,
                )
        except (OSError, ValueError) as exc:
            if trace_pool is not None:
                await trace_pool.close()
                trace_pool = None
            if holdout_contract is not None:
                assert holdout_ledger is not None
                assert holdout_receipt_key is not None
                await _write_holdout_rejection_report(
                    ledger_dir=holdout_ledger,
                    receipt_key=holdout_receipt_key,
                    target=target,
                    cases_path=cases_path,
                    eval_run_id=eval_run_id,
                    contract=holdout_contract,
                    expected_cases_file_sha256=expected_file_sha256,
                    status="cost_governance_rejected",
                    failures=[_cost_preflight_failure_code(exc)],
                    executed_cases_total=0,
                    receipt_path=holdout_receipt_path,
                    detail=f"{type(exc).__name__}: {exc}",
                    **private_report_context,
                )
                raise ValueError(
                    f"{private_run_label} cost-governance preflight failed: {exc}"
                ) from exc
            raise

    if bypass_cache:
        headers[eval_cache_bypass.HEADER_BYPASS] = "1"
    semaphore = asyncio.Semaphore(max(1, concurrency))
    budget_stopped = False
    llm_pricing_failure: str | None = None
    holdout_trace_cardinality: dict[str, Any] | None = None
    holdout_trace_cardinality_error: str | None = None
    results: list[dict[str, Any]] = []
    phase0_execution_stage = "http_client_setup"
    phase0_reservation_started = False
    phase0_rejection_path: Path | None = None
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=request_timeout,
            trust_env=False,
        ) as client:
            if source_diagnostic_cases or holdout_contract is not None or (
                bypass_cache and _requires_signed_cache_bypass(target)
            ):
                phase0_execution_stage = "runtime_preflight"
                try:
                    verified_runtime_git_sha_preflight = (
                        await _verify_cache_bypass_runtime(
                            client=client,
                            target=target,
                            headers=headers,
                            expected_git_sha=evaluation_runtime_git_sha,
                            eval_run_id=eval_run_id,
                            cache_bypass_secret=cache_bypass_secret,
                        )
                    )
                except ValueError as exc:
                    if holdout_contract is not None:
                        assert holdout_ledger is not None
                        assert holdout_receipt_key is not None
                        await _write_holdout_rejection_report(
                            ledger_dir=holdout_ledger,
                            receipt_key=holdout_receipt_key,
                            target=target,
                            cases_path=cases_path,
                            eval_run_id=eval_run_id,
                            contract=holdout_contract,
                            expected_cases_file_sha256=expected_file_sha256,
                            status="runtime_rejected",
                            failures=["runtime_ready_check_failed"],
                            executed_cases_total=0,
                            receipt_path=holdout_receipt_path,
                            detail=str(exc),
                            **private_report_context,
                        )
                    raise
                if holdout_contract is not None:
                    assert holdout_receipt_path is not None
                    try:
                        await asyncio.to_thread(
                            _create_holdout_started_receipt,
                            holdout_receipt_path,
                            contract=holdout_contract,
                            expected_cases_file_sha256=expected_file_sha256,
                            eval_run_id=eval_run_id,
                            cases_path=cases_path,
                            output_path=output_path,
                            receipt_key=holdout_receipt_key,
                            **private_report_context,
                        )
                    except FileExistsError:
                        await _write_holdout_rejection_report(
                            ledger_dir=holdout_ledger,
                            receipt_key=holdout_receipt_key,
                            target=target,
                            cases_path=cases_path,
                            eval_run_id=eval_run_id,
                            contract=holdout_contract,
                            expected_cases_file_sha256=expected_file_sha256,
                            status="rerun_rejected",
                            failures=["started_receipt_exists"],
                            executed_cases_total=0,
                            receipt_path=holdout_receipt_path,
                            **private_report_context,
                        )
                        raise ValueError(
                            f"{private_run_label} already has a started receipt; "
                            "rerun is forbidden"
                        ) from None
                    except OSError as exc:
                        await _write_holdout_rejection_report(
                            ledger_dir=holdout_ledger,
                            receipt_key=holdout_receipt_key,
                            target=target,
                            cases_path=cases_path,
                            eval_run_id=eval_run_id,
                            contract=holdout_contract,
                            expected_cases_file_sha256=expected_file_sha256,
                            status="receipt_rejected",
                            failures=["started_receipt_create_failed"],
                            executed_cases_total=0,
                            receipt_path=holdout_receipt_path,
                            detail=f"{type(exc).__name__}: {exc}",
                            **private_report_context,
                        )
                        raise RuntimeError(
                            f"{private_run_label} started receipt could not be created"
                        ) from exc
                if validated_candidate_contract is not None:
                    phase0_execution_stage = "candidate_cost_reservation"
                    try:
                        if validated_cost_reservation is None:
                            validated_cost_reservation = reserve_live_eval_cost(
                                scope=PILOT50_CANDIDATE_COST_SCOPE,
                                run_id=eval_run_id,
                                runtime_git_sha=validated_candidate_contract[
                                    "runtime_git_sha"
                                ],
                                manifest_sha256=PILOT50_CANDIDATE_CASES_SHA256,
                                case_count=PILOT50_CANDIDATE_CASES_TOTAL,
                                approved_cap_rub=PILOT50_CANDIDATE_COST_CAP_RUB,
                                private_full=True,
                                high_cost_approval_id=(
                                    validated_high_cost_approval_id
                                ),
                                ledger_dir=cost_ledger_dir,
                            )
                        else:
                            _validate_cost_reservation_for_child_run(
                                validated_cost_reservation,
                                case_count=PILOT50_CANDIDATE_CASES_TOTAL,
                                max_llm_cost_rub=PILOT50_CANDIDATE_COST_CAP_RUB,
                                private_full=True,
                            )
                        _validate_pilot50_candidate_cost_reservation(
                            validated_cost_reservation,
                            eval_run_id=eval_run_id,
                            contract=validated_candidate_contract,
                            high_cost_approval_id=(
                                validated_high_cost_approval_id
                            ),
                        )
                    except (OSError, ValueError) as exc:
                        raise ValueError(
                            "Pilot50 candidate cost-governance preflight failed "
                            f"after runtime verification: {exc}"
                        ) from exc
                if phase0_contract is not None:
                    phase0_execution_stage = "cost_reservation"
                    phase0_reservation_started = True
                    try:
                        if validated_cost_reservation is None:
                            validated_cost_reservation = reserve_live_eval_cost(
                                scope=PHASE0_COST_SCOPE,
                                run_id=eval_run_id,
                                runtime_git_sha=phase0_contract["runtime_git_sha"],
                                manifest_sha256=phase0_contract["cases_file_sha256"],
                                case_count=PHASE0_CASES_TOTAL,
                                approved_cap_rub=PHASE0_COST_CAP_RUB,
                                private_full=False,
                                high_cost_approval_id=PHASE0_APPROVAL_ID,
                                ledger_dir=phase0_contract["cost_ledger_dir"],
                            )
                        else:
                            _validate_cost_reservation_for_child_run(
                                validated_cost_reservation,
                                case_count=PHASE0_CASES_TOTAL,
                                max_llm_cost_rub=PHASE0_COST_CAP_RUB,
                                private_full=False,
                            )
                        _validate_phase0_cost_reservation(
                            validated_cost_reservation,
                            eval_run_id=eval_run_id,
                            contract=phase0_contract,
                        )
                    except (OSError, ValueError) as exc:
                        raise ValueError(
                            "Phase 0 cost-governance preflight failed after "
                            f"runtime verification: {exc}"
                        ) from exc
            strict_live_cost_control = (
                not _is_in_process_mock_transport(transport)
                and max_llm_cost_rub is not None
            )
            if max_llm_cost_rub is None:
                phase0_execution_stage = "case_execution"
                tasks = [
                    _run_case(
                        client=client,
                        target=target,
                        headers=headers,
                        eval_run_id=eval_run_id,
                        case=case,
                        semaphore=semaphore,
                        trace_pool=trace_pool,
                        sealed_holdout=private_contract_run,
                        cache_bypass_secret=cache_bypass_secret,
                    )
                    for case in cases
                ]
                results = await asyncio.gather(*tasks)
            else:
                # Budget enforcement needs trace usage after each response, so run cases
                # sequentially.
                phase0_execution_stage = "case_execution"
                strict_cost_total = 0.0
                sequential_semaphore = asyncio.Semaphore(1)
                for case_index, case in enumerate(cases):
                    result = await _run_case(
                        client=client,
                        target=target,
                        headers=headers,
                        eval_run_id=eval_run_id,
                        case=case,
                        semaphore=sequential_semaphore,
                        trace_pool=trace_pool,
                        sealed_holdout=private_contract_run,
                        cache_bypass_secret=cache_bypass_secret,
                    )
                    repricing_failure: str | None = None
                    if validated_repricing_contract is not None:
                        result, repricing_failure = _pilot50_reprice_result(
                            result,
                            contract=validated_repricing_contract,
                        )
                    results.append(result)
                    if strict_live_cost_control:
                        llm_pricing_failure = (
                            repricing_failure
                            or (
                                _pilot50_candidate_accounting_failure(result)
                                if validated_candidate_contract is not None
                                else _llm_cost_accounting_failure(result)
                            )
                        )
                        if llm_pricing_failure is not None:
                            break
                        strict_cost_total += float(
                            result.get("llm_estimated_cost_rub") or 0.0
                        )
                        cases_remain = case_index < len(cases) - 1
                        if strict_cost_total > max_llm_cost_rub or (
                            cases_remain
                            and strict_cost_total >= max_llm_cost_rub
                        ):
                            budget_stopped = True
                            break
                    elif _llm_cost_rub_total(results) > max_llm_cost_rub:
                        budget_stopped = True
                        break
            if (
                holdout_contract is not None
                or source_diagnostic_cases
                or validated_repricing_contract is not None
                or validated_candidate_contract is not None
            ):
                phase0_execution_stage = "runtime_postflight"
                try:
                    verified_runtime_git_sha_postflight = (
                        await _verify_cache_bypass_runtime(
                            client=client,
                            target=target,
                            headers=headers,
                            expected_git_sha=evaluation_runtime_git_sha,
                            eval_run_id=eval_run_id,
                            cache_bypass_secret=cache_bypass_secret,
                        )
                    )
                except ValueError as exc:
                    if holdout_contract is not None:
                        assert holdout_ledger is not None
                        assert holdout_receipt_key is not None
                        await _write_holdout_rejection_report(
                            ledger_dir=holdout_ledger,
                            receipt_key=holdout_receipt_key,
                            target=target,
                            cases_path=cases_path,
                            eval_run_id=eval_run_id,
                            contract=holdout_contract,
                            expected_cases_file_sha256=expected_file_sha256,
                            status="post_runtime_rejected",
                            failures=["post_runtime_ready_check_failed"],
                            executed_cases_total=len(results),
                            receipt_path=holdout_receipt_path,
                            detail=str(exc),
                            base_metrics=summarize_results(
                                results,
                                target=target,
                                cases_path=cases_path,
                                generated_smoke_cases=generated_smoke_cases,
                                trace_lookup_error=trace_lookup_error,
                            ),
                            **private_report_context,
                        )
                    raise
                if (
                    holdout_contract is not None
                    or phase0_contract is not None
                    or validated_candidate_contract is not None
                ):
                    assert trace_pool is not None
                    phase0_execution_stage = "trace_cardinality"
                    try:
                        if phase0_contract is not None:
                            holdout_trace_cardinality = (
                                await _fetch_phase0_trace_cardinality(
                                    trace_pool,
                                    eval_run_id=eval_run_id,
                                    expected_request_case_pairs=[
                                        (
                                            str(result.get("request_id") or ""),
                                            str(result.get("id") or ""),
                                        )
                                        for result in results
                                    ],
                                )
                            )
                        else:
                            holdout_trace_cardinality = (
                                await _fetch_eval_trace_cardinality(
                                    trace_pool,
                                    eval_run_id=eval_run_id,
                                    expected_case_ids=[
                                        str(case["id"]) for case in cases
                                    ],
                                )
                            )
                    except Exception as exc:
                        holdout_trace_cardinality_error = type(exc).__name__
            phase0_execution_stage = "completed"
    except Exception as exc:
        if phase0_contract is not None and phase0_reservation_started:
            try:
                phase0_rejection_path = await _write_phase0_execution_rejection(
                    output_path=output_path,
                    eval_run_id=eval_run_id,
                    run_started_at=run_started_at,
                    target=target,
                    cases_path=cases_path,
                    cases_file_sha256=cases_file_sha256,
                    phase0_contract=phase0_contract,
                    results=results,
                    trace_lookup_error=trace_lookup_error,
                    trace_cardinality=holdout_trace_cardinality,
                    trace_cardinality_error=holdout_trace_cardinality_error,
                    reservation=validated_cost_reservation,
                    stage=phase0_execution_stage,
                    error=exc,
                )
            except Exception as rejection_exc:
                raise RuntimeError(
                    "Phase 0 failed after cost reservation and rejection "
                    "evidence could not be written"
                ) from rejection_exc
            raise RuntimeError(
                "Phase 0 failed after cost reservation; private rejection "
                f"evidence was written to {phase0_rejection_path.name}"
            ) from exc
        raise
    finally:
        if trace_pool:
            try:
                await trace_pool.close()
            except Exception as close_exc:
                if phase0_contract is not None and phase0_reservation_started:
                    if phase0_rejection_path is None:
                        try:
                            phase0_rejection_path = (
                                await _write_phase0_execution_rejection(
                                    output_path=output_path,
                                    eval_run_id=eval_run_id,
                                    run_started_at=run_started_at,
                                    target=target,
                                    cases_path=cases_path,
                                    cases_file_sha256=cases_file_sha256,
                                    phase0_contract=phase0_contract,
                                    results=results,
                                    trace_lookup_error=trace_lookup_error,
                                    trace_cardinality=holdout_trace_cardinality,
                                    trace_cardinality_error=(
                                        holdout_trace_cardinality_error
                                    ),
                                    reservation=validated_cost_reservation,
                                    stage="trace_pool_close",
                                    error=close_exc,
                                )
                            )
                        except Exception as rejection_exc:
                            raise RuntimeError(
                                "Phase 0 trace pool close failed and rejection "
                                "evidence could not be written"
                            ) from rejection_exc
                    raise RuntimeError(
                        "Phase 0 trace pool close failed; private rejection "
                        "evidence was written to "
                        f"{phase0_rejection_path.name}"
                    ) from close_exc
                raise

    metrics = summarize_results(
        results,
        target=target,
        cases_path=cases_path,
        generated_smoke_cases=generated_smoke_cases,
        trace_lookup_error=trace_lookup_error,
    )
    metrics["run_started_at"] = run_started_at.isoformat()
    metrics["run_completed_at"] = datetime.now(UTC).isoformat()
    metrics["eval_run_id"] = eval_run_id
    metrics["cases_file_sha256"] = cases_file_sha256
    if source_diagnostic_cases:
        metrics["report_classification"] = {
            "evaluation_classification": SOURCE_OBSERVED_DIAGNOSTIC_MODE,
            "provisional": True,
            "calibration_only": True,
            "independent_evaluation": False,
            "previously_exposed": True,
            "product_verdict_eligible": False,
            "human_product_verdict": False,
        }
        metrics["cases_passed"] = None
        metrics["pass_rate"] = None
    if phase0_contract is not None:
        metrics["trace_cardinality"] = holdout_trace_cardinality
        if holdout_trace_cardinality_error is not None:
            metrics["trace_cardinality_error"] = holdout_trace_cardinality_error
    private_run_key = "calibration_replay" if calibration_replay else "holdout_run"
    if holdout_contract is not None:
        if calibration_replay:
            assert source_holdout_contract is not None
            metrics["source_holdout_contract"] = source_holdout_contract
        else:
            metrics["holdout_contract"] = holdout_contract
        metrics["report_classification"] = _holdout_report_classification(
            holdout_contract,
            calibration_replay=calibration_replay,
        )
        metrics["trace_cardinality"] = holdout_trace_cardinality
        if holdout_trace_cardinality_error is not None:
            metrics["trace_cardinality_error"] = holdout_trace_cardinality_error
    _apply_run_limits(
        metrics,
        original_cases_total=original_cases_total,
        max_cases=max_cases,
        max_llm_cost_rub=max_llm_cost_rub,
    )
    runtime_identity = _runtime_identity_report(
        expected_runtime_git_sha=evaluation_runtime_git_sha,
        preflight_release_git_sha=verified_runtime_git_sha_preflight,
        postflight_release_git_sha=verified_runtime_git_sha_postflight,
        required=(
            holdout_contract is not None
            or bool(source_diagnostic_cases)
            and (strict_live or evaluation_runtime_git_sha is not None)
            or validated_repricing_contract is not None
            or validated_candidate_contract is not None
        ),
    )
    if (
        source_diagnostic_cases
        and strict_live
        and runtime_identity["status"] != "verified"
    ):
        raise RuntimeError(
            "strict-live source_observed_diagnostic runtime identity was not verified"
        )
    if (
        validated_repricing_contract is not None
        and runtime_identity["status"] != "verified"
    ):
        raise RuntimeError("Pilot50 repricing runtime identity was not verified")
    if (
        validated_candidate_contract is not None
        and runtime_identity["status"] != "verified"
    ):
        raise RuntimeError("Pilot50 candidate runtime identity was not verified")
    metrics["runtime_identity"] = runtime_identity
    metrics["cost_control"] = {
        "strict_live": strict_live,
        "routine_run_cap_rub": ROUTINE_LIVE_EVAL_MAX_COST_RUB,
        "routine_run_max_cases": ROUTINE_LIVE_EVAL_MAX_CASES,
        "high_cost_approval_id": validated_high_cost_approval_id,
        "pricing_complete": llm_pricing_failure is None,
        "reservation": _safe_cost_reservation_report(
            validated_cost_reservation,
            cases_file_sha256=cases_file_sha256,
            include_private_full=validated_candidate_contract is not None,
        ),
    }
    if validated_repricing_contract is not None:
        metrics["cost_control"]["pricing_projection"] = validated_repricing_contract
    if validated_candidate_contract is not None:
        metrics["cost_control"]["candidate_contract"] = (
            validated_candidate_contract
        )
    if budget_stopped:
        metrics["cases_original_total"] = original_cases_total
        metrics["cases_limited"] = True
        metrics["llm_budget_stopped"] = True
    if llm_pricing_failure is not None:
        metrics["cases_original_total"] = original_cases_total
        metrics["cases_limited"] = True
        metrics["llm_pricing_stopped"] = True
        metrics["llm_pricing_failure"] = llm_pricing_failure
    candidate_failures: list[str] = []
    if validated_candidate_contract is not None:
        candidate_failures = _holdout_integrity_failures(
            metrics,
            results=results,
            expected_cases_total=PILOT50_CANDIDATE_CASES_TOTAL,
            executed_cases_total=len(results),
            trace_cardinality=holdout_trace_cardinality,
            trace_cardinality_error=holdout_trace_cardinality_error,
        )
        if budget_stopped:
            candidate_failures.append("llm_budget_stopped")
        if llm_pricing_failure is not None:
            candidate_failures.append("llm_pricing_unavailable")
        if runtime_identity["status"] != "verified":
            candidate_failures.append("runtime_identity_invalid")
        reservation_report = metrics["cost_control"].get("reservation")
        if not isinstance(reservation_report, Mapping) or any(
            (
                reservation_report.get("valid") is not True,
                reservation_report.get("scope")
                != PILOT50_CANDIDATE_COST_SCOPE,
                reservation_report.get("case_count")
                != PILOT50_CANDIDATE_CASES_TOTAL,
                reservation_report.get("approved_cap_rub")
                != PILOT50_CANDIDATE_COST_CAP_RUB,
                reservation_report.get("private_full") is not True,
                reservation_report.get("reservation_class") != "private_full",
                reservation_report.get("high_cost_approval_id")
                != validated_high_cost_approval_id,
            )
        ):
            candidate_failures.append("cost_reservation_invalid")
        candidate_failures = list(dict.fromkeys(candidate_failures))
        metrics["trace_cardinality"] = holdout_trace_cardinality
        if holdout_trace_cardinality_error is not None:
            metrics["trace_cardinality_error"] = holdout_trace_cardinality_error
        metrics["pilot50_candidate"] = {
            "status": (
                "completed" if not candidate_failures else "integrity_rejected"
            ),
            "completed": not candidate_failures,
            "contract_id": PILOT50_CANDIDATE_CONTRACT_ID,
            "expected_cases_total": PILOT50_CANDIDATE_CASES_TOTAL,
            "executed_cases_total": len(results),
            "cases_file_sha256": PILOT50_CANDIDATE_CASES_SHA256,
            "runtime_git_sha": validated_candidate_contract["runtime_git_sha"],
            "integrity_failures": candidate_failures,
            "selective_reruns_forbidden": True,
        }
    phase0_failures: list[str] = []
    if phase0_contract is not None:
        phase0_failures = _phase0_integrity_failures(
            metrics,
            results=results,
            trace_cardinality=holdout_trace_cardinality,
            trace_cardinality_error=holdout_trace_cardinality_error,
        )
        if budget_stopped:
            phase0_failures.append("llm_budget_stopped")
        if llm_pricing_failure is not None:
            phase0_failures.append("llm_pricing_unavailable")
        reservation_report = metrics["cost_control"].get("reservation")
        if not isinstance(reservation_report, Mapping) or (
            reservation_report.get("valid") is not True
        ):
            phase0_failures.append("cost_reservation_invalid")
        phase0_failures = list(dict.fromkeys(phase0_failures))
        metrics["phase0_run"] = {
            "status": "completed" if not phase0_failures else "integrity_rejected",
            "completed": not phase0_failures,
            "expected_cases_total": PHASE0_CASES_TOTAL,
            "executed_cases_total": len(results),
            "cases_file_sha256": phase0_contract["cases_file_sha256"],
            "manifest_file_sha256": phase0_contract["manifest_file_sha256"],
            "manifest_binding_sha256": phase0_contract[
                "manifest_binding_sha256"
            ],
            "ordered_selection_sha256": phase0_contract[
                "ordered_selection_sha256"
            ],
            "runtime_git_sha": phase0_contract["runtime_git_sha"],
            "transport_mode": phase0_contract["transport_mode"],
            "builder_snapshot": phase0_contract["builder_snapshot"],
            "approval_id": PHASE0_APPROVAL_ID,
            "cost_scope": PHASE0_COST_SCOPE,
            "integrity_failures": phase0_failures,
            "selective_reruns_forbidden": True,
        }
    if phase0_failures:
        rejection_path = output_path.with_name(
            f"{output_path.stem}.{eval_run_id}.rejected.json"
        )
        metrics["phase0_run"]["rejection_evidence"] = rejection_path.name
        await asyncio.to_thread(_write_json_exclusive, rejection_path, metrics)
        raise RuntimeError(
            "Phase 0 failed run-integrity checks; private rejection evidence was "
            "written and the canonical report was not created: "
            + ", ".join(phase0_failures)
        )
    if candidate_failures:
        rejection_path = output_path.with_name(
            f"{output_path.stem}.{eval_run_id}.rejected.json"
        )
        metrics["pilot50_candidate"]["rejection_evidence"] = rejection_path.name
        await asyncio.to_thread(_write_json_exclusive, rejection_path, metrics)
        raise RuntimeError(
            "Pilot50 candidate failed run-integrity checks; private rejection "
            "evidence was written and the canonical report was not created: "
            + ", ".join(candidate_failures)
        )
    holdout_failures: list[str] = []
    if holdout_contract is not None:
        executed_cases_total = len(results)
        holdout_failures = _holdout_integrity_failures(
            metrics,
            results=results,
            expected_cases_total=holdout_contract["cases_total"],
            executed_cases_total=executed_cases_total,
            trace_cardinality=holdout_trace_cardinality,
            trace_cardinality_error=holdout_trace_cardinality_error,
        )
        if llm_pricing_failure is not None:
            holdout_failures.append("llm_pricing_unavailable")
        status = _holdout_failure_status(
            holdout_failures,
            budget_stopped=budget_stopped,
        )
        metrics[private_run_key] = {
            "status": status,
            "completed": not holdout_failures,
            **_holdout_report_classification(
                holdout_contract,
                calibration_replay=calibration_replay,
            ),
            "expected_cases_total": holdout_contract["cases_total"],
            "executed_cases_total": executed_cases_total,
            "expected_cases_file_sha256": expected_file_sha256,
            "integrity_failures": holdout_failures,
            "started_receipt": holdout_receipt_path.name
            if holdout_receipt_path
            else None,
            "completed_receipt": holdout_completed_receipt_path.name
            if holdout_completed_receipt_path
            else None,
            "knowledge_base_identity_gate": (
                "manual_pre_run_verification_required"
            ),
            "one_shot_scope": (
                "one_calibration_replay_per_source_selection_file_and_runtime"
                if calibration_replay
                else "enforced_while_canonical_persistent_ledger_is_preserved"
            ),
        }
        if calibration_replay:
            assert source_holdout_contract is not None
            assert evaluation_runtime_git_sha is not None
            metrics[private_run_key]["source_runtime_git_sha"] = (
                source_holdout_contract["runtime_git_sha"]
            )
            metrics[private_run_key]["evaluation_runtime_git_sha"] = (
                evaluation_runtime_git_sha
            )
            metrics[private_run_key]["prior_sealed_exposure_receipts"] = (
                prior_sealed_exposure_receipts
            )
    if holdout_failures:
        assert holdout_ledger is not None
        assert holdout_receipt_key is not None
        await _write_holdout_rejection_report(
            ledger_dir=holdout_ledger,
            receipt_key=holdout_receipt_key,
            target=target,
            cases_path=cases_path,
            eval_run_id=eval_run_id,
            contract=holdout_contract,
            expected_cases_file_sha256=expected_file_sha256,
            status=str(metrics[private_run_key]["status"]),
            failures=holdout_failures,
            executed_cases_total=len(results),
            receipt_path=holdout_receipt_path,
            trace_lookup_error=metrics.get("trace_lookup_error"),
            base_metrics=metrics,
            **private_report_context,
        )
        raise RuntimeError(
            f"{private_run_label} failed run-integrity checks; rejection evidence "
            "was written and the canonical report was not created: "
            + ", ".join(holdout_failures)
        )
    if holdout_contract is not None:
        assert holdout_completed_receipt_path is not None
        assert holdout_receipt_key is not None
        try:
            await asyncio.to_thread(_write_json_exclusive, output_path, metrics)
            if markdown_path:
                await asyncio.to_thread(
                    _write_markdown_exclusive,
                    markdown_path,
                    metrics,
                )
            output_sha256 = await asyncio.to_thread(_file_sha256, output_path)
            await asyncio.to_thread(
                _create_holdout_completed_receipt,
                holdout_completed_receipt_path,
                contract=holdout_contract,
                expected_cases_file_sha256=expected_file_sha256,
                eval_run_id=eval_run_id,
                receipt_key=holdout_receipt_key,
                output_path=output_path,
                output_sha256=output_sha256,
                **private_report_context,
            )
        except OSError as exc:
            assert holdout_ledger is not None
            await _write_holdout_rejection_report(
                ledger_dir=holdout_ledger,
                receipt_key=holdout_receipt_key,
                target=target,
                cases_path=cases_path,
                eval_run_id=eval_run_id,
                contract=holdout_contract,
                expected_cases_file_sha256=expected_file_sha256,
                status="finalization_failed",
                failures=["exclusive_finalization_failed"],
                executed_cases_total=len(results),
                receipt_path=holdout_receipt_path,
                detail=type(exc).__name__,
                base_metrics=metrics,
                **private_report_context,
            )
            raise RuntimeError(
                f"{private_run_label} final report or completed receipt "
                "could not be created exclusively"
            ) from exc
    elif phase0_contract is not None:
        await _finalize_phase0_report(
            output_path=output_path,
            metrics=metrics,
            eval_run_id=eval_run_id,
        )
    elif validated_candidate_contract is not None:
        await asyncio.to_thread(_write_json_exclusive, output_path, metrics)
        if markdown_path:
            await asyncio.to_thread(
                _write_markdown_exclusive,
                markdown_path,
                metrics,
            )
    else:
        await asyncio.to_thread(_write_json, output_path, metrics)
        if markdown_path:
            await asyncio.to_thread(_write_markdown, markdown_path, metrics)
    return metrics


def summarize_results(
    results: list[dict[str, Any]],
    *,
    target: str,
    cases_path: Path,
    generated_smoke_cases: bool = False,
    trace_lookup_error: str | None = None,
) -> dict[str, Any]:
    latencies = [int(item["latency_ms"]) for item in results if item.get("latency_ms") is not None]
    trace_latencies = [
        int(item["trace_total_latency_ms"])
        for item in results
        if item.get("trace_total_latency_ms") is not None
    ]
    chunk_scored = [item for item in results if item.get("expected_chunk_ids")]
    cited_scored = [item for item in results if item.get("expected_cited_chunk_ids")]
    answer_scored = [item for item in results if item.get("expected_answer_contains")]
    behavior_scored = [item for item in results if item.get("expected_behavior")]
    escalation_reason_scored = [
        item
        for item in results
        if item.get("escalation_reason_match") is not None
    ]
    routing_profile_scored = [
        item
        for item in results
        if item.get("expected_response_profile")
    ]
    forbidden_profile_scored = [
        item
        for item in results
        if item.get("forbidden_response_profiles")
    ]
    cited_source_policy_scored = [
        item
        for item in results
        if item.get("allowed_cited_source_types")
    ]
    trace_scored = [item for item in results if item.get("trace_found")]
    usage_events = [
        event
        for item in results
        if isinstance(item.get("llm_usage"), list)
        for event in item["llm_usage"]
        if isinstance(event, dict)
    ]
    reranker_scores = _numeric_values(results, "max_reranker_score")
    low_confidence_chunk_hits = [
        item
        for item in chunk_scored
        if item.get("expected_chunk_hit") is True
        and item.get("escalation_reason") == "low_confidence"
    ]

    metrics: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "target": target,
        "cases_path": str(cases_path),
        "generated_smoke_cases": generated_smoke_cases,
        "cases_total": len(results),
        "cases_passed": sum(1 for item in results if item.get("passed") is True),
        "pass_rate": _bool_rate(results, "passed"),
        "http_success_rate": _bool_rate(results, "http_success"),
        "expected_chunk_hit_rate": _bool_rate(chunk_scored, "expected_chunk_hit"),
        "expected_or_equivalent_chunk_hit_rate": _bool_rate(
            chunk_scored,
            "expected_or_equivalent_chunk_hit",
        ),
        "expected_cited_chunk_hit_rate": _bool_rate(
            cited_scored,
            "expected_cited_chunk_hit",
        ),
        "expected_cited_or_equivalent_chunk_hit_rate": _bool_rate(
            cited_scored,
            "expected_cited_or_equivalent_chunk_hit",
        ),
        "answer_contains_rate": _bool_rate(answer_scored, "answer_contains_match"),
        "behavior_match_rate": _bool_rate(behavior_scored, "behavior_match"),
        "behavior_confusion_matrix": _behavior_confusion_matrix(behavior_scored),
        "escalation_reason_match_rate": _bool_rate(
            escalation_reason_scored,
            "escalation_reason_match",
        ),
        "routing_response_profile_match_rate": _bool_rate(
            routing_profile_scored,
            "routing_response_profile_match",
        ),
        "forbidden_response_profile_absence_rate": _bool_rate(
            forbidden_profile_scored,
            "forbidden_response_profiles_absent",
        ),
        "cited_source_type_policy_rate": _bool_rate(
            cited_source_policy_scored,
            "cited_source_types_allowed",
        ),
        "trace_coverage_rate": len(trace_scored) / len(results) if results else None,
        "escalation_rate": _bool_rate(trace_scored, "was_escalated"),
        "cache_hit_rate": _bool_rate(trace_scored, "cache_hit"),
        "source_chunk_rate": _value_rate(trace_scored, "generator_model", "source_chunk"),
        "reranker_score": _number_summary(reranker_scores),
        "low_confidence_expected_chunk_hits": len(low_confidence_chunk_hits),
        "low_confidence_expected_chunk_hit_rate": (
            len(low_confidence_chunk_hits) / len(chunk_scored) if chunk_scored else None
        ),
        "latency_ms": {
            "avg": _average(latencies),
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "max": max(latencies) if latencies else None,
        },
        "trace_total_latency_ms": {
            "avg": _average(trace_latencies),
            "p50": _percentile(trace_latencies, 50),
            "p95": _percentile(trace_latencies, 95),
            "max": max(trace_latencies) if trace_latencies else None,
        },
        "http_status_counts": dict(Counter(str(item.get("http_status")) for item in results)),
        "generator_model_counts": dict(
            Counter(str(item.get("generator_model") or "unknown") for item in results)
        ),
        "escalation_reason_counts": dict(
            Counter(str(item.get("escalation_reason") or "none") for item in results)
        ),
        "generate_retry_reason_counts": dict(
            Counter(
                reason
                for item in results
                for reason in item.get("generate_retry_reasons") or []
            )
        ),
        "failure_reason_counts": _failure_reason_counts(results),
        "expected_behavior_counts": dict(
            Counter(str(item.get("expected_behavior") or "unscored") for item in results)
        ),
        "observed_behavior_counts": dict(
            Counter(str(item.get("observed_behavior") or "unknown") for item in results)
        ),
        "expected_response_profile_counts": dict(
            Counter(
                str(item.get("expected_response_profile") or "unscored")
                for item in results
            )
        ),
        "observed_routing_response_profile_counts": dict(
            Counter(
                str(item.get("observed_routing_response_profile") or "unknown")
                for item in results
            )
        ),
        "detected_response_profile_counts": dict(
            Counter(
                profile
                for item in results
                for profile in item.get("detected_response_profiles") or []
            )
        ),
        "likely_infrastructure_failure": _likely_infrastructure_failure(results),
        "llm_prompt_tokens": sum(
            _report_nonnegative_int(item.get("llm_prompt_tokens")) for item in results
        ),
        "llm_completion_tokens": sum(
            _report_nonnegative_int(item.get("llm_completion_tokens"))
            for item in results
        ),
        "llm_total_tokens": sum(
            _report_nonnegative_int(item.get("llm_total_tokens")) for item in results
        ),
        "llm_estimated_cost_rub": round(_llm_cost_rub_total(results), 6),
        "llm_usage_events": usage_events,
        "results": results,
    }
    trace_lookup_errors = [
        str(item["trace_lookup_error"])
        for item in results
        if item.get("trace_lookup_error")
    ]
    if trace_lookup_error:
        trace_lookup_errors.insert(0, trace_lookup_error)
    if trace_lookup_errors:
        metrics["trace_lookup_error"] = "; ".join(
            dict.fromkeys(trace_lookup_errors)
        )
    return metrics


def _validated_pilot50_repricing_contract(
    value: str | None,
    *,
    cases: list[dict[str, Any]],
    cases_file_sha256: str,
    target: str,
    concurrency: int,
    trace_lookup: bool,
    bypass_cache: bool,
    max_llm_cost_rub: float | None,
    max_cases: int | None,
    auto_smoke_cases: bool,
    generated_user_prefix: str | None,
    private_contract_run: bool,
    source_diagnostic_cases: bool,
    phase0_contract: Mapping[str, Any] | None,
    strict_live: bool,
    high_cost_approval_id: str | None,
    expected_runtime_git_sha: str | None,
) -> dict[str, Any] | None:
    contract_id = str(value or "").strip()
    if not contract_id:
        return None
    runtime_sha = str(os.getenv("RELEASE_GIT_SHA") or "").strip()
    failures: list[str] = []
    if contract_id != PILOT50_REPRICING_CONTRACT_ID:
        failures.append("contract_id")
    if runtime_sha != PILOT50_REPRICING_RUNTIME_SHA:
        failures.append("runtime_sha")
    if expected_runtime_git_sha != PILOT50_REPRICING_RUNTIME_SHA:
        failures.append("expected_runtime_sha")
    if cases_file_sha256 != PILOT50_REPRICING_CASES_SHA256:
        failures.append("cases_sha")
    if len(cases) != PILOT50_REPRICING_CASES_TOTAL:
        failures.append("case_count")
    if target != PILOT50_REPRICING_TARGET:
        failures.append("target")
    if concurrency != 1:
        failures.append("concurrency")
    if trace_lookup is not True:
        failures.append("trace_lookup")
    if bypass_cache is not True:
        failures.append("bypass_cache")
    if max_llm_cost_rub != PILOT50_REPRICING_COST_CAP_RUB:
        failures.append("cost_cap")
    if max_cases is not None or auto_smoke_cases:
        failures.append("case_selection")
    if generated_user_prefix is not None:
        failures.append("user_prefix")
    if private_contract_run or source_diagnostic_cases or phase0_contract is not None:
        failures.append("run_class")
    if not strict_live:
        failures.append("strict_live")
    if not str(high_cost_approval_id or "").strip():
        failures.append("approval")
    if failures:
        raise ValueError(
            "Pilot50 LLM cost repricing contract rejected: " + ", ".join(failures)
        )
    return {
        "schema_version": "pilot50-llm-cost-repricing-v1",
        "contract_id": PILOT50_REPRICING_CONTRACT_ID,
        "rate_card_sha256": PILOT50_REPRICING_RATE_CARD_SHA256,
        "source": "eval_repriced",
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


def _validated_pilot50_candidate_contract(
    value: str | None,
    *,
    cases: list[dict[str, Any]],
    cases_file_sha256: str,
    target: str,
    concurrency: int,
    trace_lookup: bool,
    bypass_cache: bool,
    max_llm_cost_rub: float | None,
    max_cases: int | None,
    auto_smoke_cases: bool,
    generated_user_prefix: str | None,
    private_contract_run: bool,
    source_diagnostic_cases: bool,
    phase0_contract: Mapping[str, Any] | None,
    strict_live: bool,
    high_cost_approval_id: str | None,
    expected_runtime_git_sha: str | None,
    require_budget_for_large_runs: bool,
    require_complete_traces: bool,
) -> dict[str, Any] | None:
    contract_id = str(value or "").strip()
    if not contract_id:
        return None
    runtime_sha = str(os.getenv("RELEASE_GIT_SHA") or "").strip()
    approval_id = str(high_cost_approval_id or "").strip()
    failures: list[str] = []
    if contract_id != PILOT50_CANDIDATE_CONTRACT_ID:
        failures.append("contract_id")
    if (
        FULL_GIT_SHA_RE.fullmatch(runtime_sha) is None
        or runtime_sha == "0" * 40
        or expected_runtime_git_sha != runtime_sha
    ):
        failures.append("runtime_sha")
    if cases_file_sha256 != PILOT50_CANDIDATE_CASES_SHA256:
        failures.append("cases_sha")
    if len(cases) != PILOT50_CANDIDATE_CASES_TOTAL:
        failures.append("case_count")
    if target != PILOT50_CANDIDATE_TARGET:
        failures.append("target")
    if concurrency != 1:
        failures.append("concurrency")
    if trace_lookup is not True or require_complete_traces is not True:
        failures.append("complete_traces")
    if bypass_cache is not True:
        failures.append("bypass_cache")
    if max_llm_cost_rub != PILOT50_CANDIDATE_COST_CAP_RUB:
        failures.append("cost_cap")
    if require_budget_for_large_runs is not True:
        failures.append("bounded_cost")
    if max_cases is not None or auto_smoke_cases:
        failures.append("case_selection")
    if generated_user_prefix is not None:
        failures.append("user_prefix")
    if private_contract_run or source_diagnostic_cases or phase0_contract is not None:
        failures.append("run_class")
    if not strict_live:
        failures.append("strict_live")
    if SAFE_COST_APPROVAL_ID_RE.fullmatch(approval_id) is None:
        failures.append("approval")
    if failures:
        raise ValueError(
            "Pilot50 candidate contract rejected: " + ", ".join(failures)
        )
    return {
        "schema_version": "pilot50-candidate-eval-v1",
        "contract_id": PILOT50_CANDIDATE_CONTRACT_ID,
        "runtime_git_sha": runtime_sha,
        "cases_file_sha256": PILOT50_CANDIDATE_CASES_SHA256,
        "cases_total": PILOT50_CANDIDATE_CASES_TOTAL,
        "target": PILOT50_CANDIDATE_TARGET,
        "concurrency": 1,
        "cache_bypass": "signed_pre_and_per_request",
        "runtime_ready_checks": "signed_pre_and_post",
        "complete_traces_required": True,
        "max_llm_cost_rub": PILOT50_CANDIDATE_COST_CAP_RUB,
        "cost_scope": PILOT50_CANDIDATE_COST_SCOPE,
        "reservation_private_full": True,
        "pricing_source": "target_reported",
        "pricing_rate_card_sha256": PILOT50_REPRICING_RATE_CARD_SHA256,
        "target_telemetry_pricing_complete": True,
        "repricing_applied": False,
    }


def _pilot50_reprice_result(
    result: dict[str, Any],
    *,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], str | None]:
    projected = dict(result)
    usage = result.get("llm_usage")
    original_cost = result.get("llm_estimated_cost_rub")
    projected["target_reported_llm_usage"] = usage
    projected["target_reported_llm_prompt_tokens"] = result.get(
        "llm_prompt_tokens"
    )
    projected["target_reported_llm_completion_tokens"] = result.get(
        "llm_completion_tokens"
    )
    projected["target_reported_llm_total_tokens"] = result.get("llm_total_tokens")
    projected["target_reported_llm_estimated_cost_rub"] = original_cost
    provenance = {
        "schema_version": contract["schema_version"],
        "contract_id": contract["contract_id"],
        "rate_card_sha256": contract["rate_card_sha256"],
        "source": contract["source"],
        "target_telemetry_preserved": True,
        "target_telemetry_pricing_complete": False,
        "status": "failed",
    }
    projected["llm_cost_pricing_provenance"] = provenance
    if result.get("trace_found") is not True:
        return projected, "llm_repricing_trace_missing"
    if result.get("llm_accounting_present") is not True or not isinstance(usage, list):
        return projected, "llm_repricing_usage_missing"
    try:
        aggregate_prompt = _strict_nonnegative_int(result.get("llm_prompt_tokens"))
        aggregate_completion = _strict_nonnegative_int(
            result.get("llm_completion_tokens")
        )
        aggregate_total = _strict_nonnegative_int(result.get("llm_total_tokens"))
        target_cost = float(original_cost)
    except (TypeError, ValueError):
        return projected, "llm_repricing_aggregate_invalid"
    if not math.isfinite(target_cost) or target_cost < 0:
        return projected, "llm_repricing_aggregate_invalid"
    if not usage:
        if any((aggregate_prompt, aggregate_completion, aggregate_total)) or target_cost != 0:
            return projected, "llm_repricing_empty_usage_mismatch"
        if result.get("generator_model") not in {
            None,
            "not_run",
            "source_only",
            "source_chunk",
        }:
            return projected, "llm_repricing_zero_usage_model_ambiguous"
        if result.get("analyzer_execution_mode") != "deterministic":
            return projected, "llm_repricing_zero_usage_ambiguous"
        if (
            result.get("http_status") != 200
            or result.get("http_success") is not True
            or result.get("error") not in (None, "")
            or result.get("trace_error") not in (None, "")
            or bool(result.get("generate_retry_reasons"))
        ):
            return projected, "llm_repricing_zero_usage_ambiguous"
        provenance["status"] = "not_run"
        projected["llm_usage"] = []
        projected["llm_estimated_cost_rub"] = 0.0
        return projected, None

    repriced_usage: list[dict[str, Any]] = []
    prompt_total = 0
    completion_total = 0
    token_total = 0
    cost_total = Decimal("0")
    target_cost_total = 0.0
    for event in usage:
        if not isinstance(event, dict):
            return projected, "llm_repricing_event_invalid"
        model = str(event.get("model") or "").strip()
        prices = PILOT50_REPRICING_MODELS.get(model)
        if prices is None:
            return projected, "llm_repricing_unknown_model"
        try:
            prompt_tokens = _strict_nonnegative_int(event.get("prompt_tokens"))
            completion_tokens = _strict_nonnegative_int(event.get("completion_tokens"))
            total_tokens = _strict_nonnegative_int(event.get("total_tokens"))
            target_event_cost = float(event.get("estimated_cost_rub"))
        except (TypeError, ValueError):
            return projected, "llm_repricing_event_tokens_invalid"
        if not math.isfinite(target_event_cost) or target_event_cost < 0:
            return projected, "llm_repricing_target_event_cost_invalid"
        if total_tokens <= 0:
            return projected, "llm_repricing_event_tokens_zero"
        if prompt_tokens + completion_tokens != total_tokens:
            return projected, "llm_repricing_event_token_mismatch"
        input_price, output_price = prices
        event_cost = (
            (
                Decimal(prompt_tokens) * input_price
                + Decimal(completion_tokens) * output_price
            )
            / Decimal(1_000_000)
        ).quantize(Decimal("0.000001"))
        if event_cost <= 0:
            return projected, "llm_repricing_event_cost_zero"
        if event.get("priced") is True:
            if not math.isclose(
                target_event_cost,
                float(event_cost),
                rel_tol=1e-9,
                abs_tol=1e-6,
            ):
                return projected, "llm_repricing_target_priced_cost_mismatch"
        elif event.get("priced") is False:
            if target_event_cost != 0:
                return projected, "llm_repricing_target_unpriced_cost_nonzero"
        else:
            return projected, "llm_repricing_target_priced_flag_invalid"
        repriced_event = dict(event)
        repriced_event["estimated_cost_rub"] = float(event_cost)
        repriced_event["priced"] = True
        repriced_event["pricing_source"] = "eval_repriced"
        repriced_event["pricing_contract_id"] = PILOT50_REPRICING_CONTRACT_ID
        repriced_event["pricing_rate_card_sha256"] = (
            PILOT50_REPRICING_RATE_CARD_SHA256
        )
        repriced_usage.append(repriced_event)
        prompt_total += prompt_tokens
        completion_total += completion_tokens
        token_total += total_tokens
        cost_total += event_cost
        target_cost_total += target_event_cost
    if (
        prompt_total != aggregate_prompt
        or completion_total != aggregate_completion
        or token_total != aggregate_total
    ):
        return projected, "llm_repricing_aggregate_token_mismatch"
    if not math.isclose(
        target_cost,
        target_cost_total,
        rel_tol=1e-9,
        abs_tol=1e-6,
    ):
        return projected, "llm_repricing_target_aggregate_cost_mismatch"
    provenance["status"] = "repriced"
    projected["llm_usage"] = repriced_usage
    projected["llm_estimated_cost_rub"] = float(cost_total)
    return projected, None


def _pilot50_candidate_accounting_failure(result: Mapping[str, Any]) -> str | None:
    if result.get("trace_found") is not True:
        return "pilot50_candidate_trace_missing"
    usage = result.get("llm_usage")
    if result.get("llm_accounting_present") is not True or not isinstance(usage, list):
        return "pilot50_candidate_usage_missing"
    try:
        aggregate_prompt = _strict_nonnegative_int(result.get("llm_prompt_tokens"))
        aggregate_completion = _strict_nonnegative_int(
            result.get("llm_completion_tokens")
        )
        aggregate_total = _strict_nonnegative_int(result.get("llm_total_tokens"))
        aggregate_cost = Decimal(str(result.get("llm_estimated_cost_rub")))
    except (ArithmeticError, TypeError, ValueError):
        return "pilot50_candidate_aggregate_invalid"
    if not aggregate_cost.is_finite() or aggregate_cost < 0:
        return "pilot50_candidate_aggregate_invalid"
    if aggregate_prompt + aggregate_completion != aggregate_total:
        return "pilot50_candidate_aggregate_token_mismatch"

    if not usage:
        if any((aggregate_prompt, aggregate_completion, aggregate_total)):
            return "pilot50_candidate_empty_usage_mismatch"
        if aggregate_cost != 0:
            return "pilot50_candidate_empty_usage_mismatch"
        if result.get("generator_model") not in {
            None,
            "not_run",
            "source_only",
            "source_chunk",
        }:
            return "pilot50_candidate_zero_usage_model_ambiguous"
        if result.get("analyzer_execution_mode") != "deterministic":
            return "pilot50_candidate_zero_usage_ambiguous"
        if (
            result.get("http_status") != 200
            or result.get("http_success") is not True
            or result.get("error") not in (None, "")
            or result.get("trace_error") not in (None, "")
            or bool(result.get("generate_retry_reasons"))
        ):
            return "pilot50_candidate_zero_usage_ambiguous"
        return None

    prompt_total = 0
    completion_total = 0
    token_total = 0
    cost_total = Decimal("0")
    for event in usage:
        if not isinstance(event, Mapping):
            return "pilot50_candidate_event_invalid"
        model = str(event.get("model") or "").strip()
        prices = PILOT50_REPRICING_MODELS.get(model)
        if prices is None:
            return "pilot50_candidate_unknown_model"
        try:
            prompt_tokens = _strict_nonnegative_int(event.get("prompt_tokens"))
            completion_tokens = _strict_nonnegative_int(
                event.get("completion_tokens")
            )
            total_tokens = _strict_nonnegative_int(event.get("total_tokens"))
            event_cost = Decimal(str(event.get("estimated_cost_rub")))
        except (ArithmeticError, TypeError, ValueError):
            return "pilot50_candidate_event_invalid"
        if (
            total_tokens <= 0
            or prompt_tokens + completion_tokens != total_tokens
        ):
            return "pilot50_candidate_event_token_mismatch"
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
            or not event_cost.is_finite()
            or event_cost <= 0
            or event_cost != expected_cost
        ):
            return "pilot50_candidate_event_cost_mismatch"
        prompt_total += prompt_tokens
        completion_total += completion_tokens
        token_total += total_tokens
        cost_total += event_cost
    if (
        prompt_total != aggregate_prompt
        or completion_total != aggregate_completion
        or token_total != aggregate_total
    ):
        return "pilot50_candidate_aggregate_token_mismatch"
    if aggregate_cost != cost_total:
        return "pilot50_candidate_aggregate_cost_mismatch"
    return None


def _guard_large_live_run_budget(
    *,
    cases: list[dict[str, Any]],
    target: str,
    transport: httpx.AsyncBaseTransport | None,
    max_llm_cost_rub: float | None,
    require_budget: bool,
    large_run_threshold: int,
    trace_lookup: bool,
    private_contract_run: bool,
    high_cost_approval_id: str | None,
) -> str | None:
    approval_id = str(high_cost_approval_id or "").strip()
    if _is_in_process_mock_transport(transport):
        return approval_id or None
    if large_run_threshold < 1:
        raise ValueError("--large-run-threshold must be greater than zero")
    if not require_budget:
        raise ValueError(
            "Unbounded live ask eval is forbidden; --allow-unbounded-llm-cost "
            "may only be used with an in-process mock transport."
        )
    if max_llm_cost_rub is None:
        raise ValueError(
            "Refusing to run a live ask eval without an explicit LLM budget: "
            f"{len(cases)} cases against {target}. Pass "
            "--max-llm-cost-rub <rubles>."
        )
    if not math.isfinite(max_llm_cost_rub) or max_llm_cost_rub <= 0:
        raise ValueError(
            "--max-llm-cost-rub must be a finite value greater than zero"
        )
    if trace_lookup is not True:
        raise ValueError(
            "Live ask eval cost enforcement requires PostgreSQL trace lookup."
        )
    approval_required = bool(cases) and cost_approval_required(
        case_count=len(cases),
        budget_rub=max_llm_cost_rub,
        private_full=private_contract_run,
    )
    if approval_id and SAFE_COST_APPROVAL_ID_RE.fullmatch(approval_id) is None:
        raise ValueError(
            "--high-cost-approval-id must contain 3-128 safe ASCII characters"
        )
    if approval_required and not approval_id:
        reason = (
            "a sealed/calibration full run"
            if private_contract_run
            else (
                f"more than {ROUTINE_LIVE_EVAL_MAX_CASES} live cases"
                if len(cases) > ROUTINE_LIVE_EVAL_MAX_CASES
                else f"a budget above {ROUTINE_LIVE_EVAL_MAX_COST_RUB:.0f} RUB"
            )
        )
        raise ValueError(
            f"Refusing {reason} without a separate one-time owner approval. "
            "Pass --high-cost-approval-id <non-secret-reference> only after "
            "the runtime SHA, case set, forecast and hard cap are approved."
        )
    return approval_id or None


def _cost_governance_runtime_git_sha(
    *,
    explicit_sha: str | None,
    evaluation_runtime_git_sha: str | None,
) -> str:
    settings_sha = ""
    try:
        settings_sha = str(get_settings().release_git_sha or "").strip()
    except Exception:
        pass
    for candidate in (
        explicit_sha,
        evaluation_runtime_git_sha,
        os.getenv("RELEASE_GIT_SHA"),
        settings_sha,
    ):
        value = str(candidate or "").strip().lower()
        if FULL_GIT_SHA_RE.fullmatch(value) is not None and value != "0" * 40:
            return value
    raise CostGovernanceError(
        "live eval cost reservation requires the target runtime Git SHA"
    )


def _validated_source_diagnostic_runtime_sha(
    value: str | None,
    *,
    required: bool,
) -> str | None:
    runtime_git_sha = str(value or "")
    if not runtime_git_sha and not required:
        return None
    if (
        FULL_GIT_SHA_RE.fullmatch(runtime_git_sha) is None
        or runtime_git_sha == "0" * 40
    ):
        raise ValueError(
            "source_observed_diagnostic requires expected_runtime_git_sha as "
            "a non-zero full lowercase 40-hex Git SHA for strict-live runs"
        )
    return runtime_git_sha


def _validated_phase0_execution_contract(
    *,
    manifest_path: Path,
    cases_path: Path,
    cases: list[dict[str, Any]],
    cases_file_sha256: str,
    expected_cases_file_sha256: str | None,
    expected_runtime_git_sha: str | None,
    high_cost_approval_id: str | None,
    max_llm_cost_rub: float | None,
    bypass_cache: bool,
    trace_lookup: bool,
    max_cases: int | None,
    auto_smoke_cases: bool,
    generated_user_prefix: str | None,
    concurrency: int,
    target: str,
    trace_dsn: str | None,
    strict_live: bool,
    cost_runtime_git_sha: str | None,
    cost_ledger_dir: Path | None,
    cost_reservation: LiveEvalCostReservation | None,
    transport: httpx.AsyncBaseTransport | None,
    markdown_path: Path | None,
    server_local: bool,
    builder_source: Path | None,
) -> dict[str, Any]:
    """Bind the paid Phase 0 run to its exact approved manifest before /ask."""

    if not strict_live:
        raise ValueError("Phase 0 requires a strict-live loopback transport")
    if transport is not None:
        raise ValueError("Phase 0 forbids injected HTTP transports")
    if cost_reservation is not None:
        raise ValueError("Phase 0 forbids injected cost reservations")
    if markdown_path is not None:
        raise ValueError("Phase 0 requires --no-markdown for atomic finalization")
    if server_local:
        if os.getenv(PHASE0_SERVER_LOCAL_OWNER_EXCEPTION_ENV) != PHASE0_APPROVAL_ID:
            raise ValueError(
                "Phase 0 server-local owner exception is missing or invalid"
            )
        if builder_source is None:
            raise ValueError("Phase 0 server-local requires --phase0-builder-source")
    elif builder_source is not None:
        raise ValueError(
            "--phase0-builder-source is allowed only with --phase0-server-local"
        )

    parsed = urlsplit(target)
    allowed_ask_hosts = (
        PHASE0_SERVER_LOCAL_ASK_HOSTS
        if server_local
        else SOURCE_DIAGNOSTIC_LOOPBACK_HOSTS
    )
    valid_ask_scheme = parsed.scheme == "http" if server_local else (
        parsed.scheme in {"http", "https"}
    )
    if (
        not valid_ask_scheme
        or parsed.hostname not in allowed_ask_hosts
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/ask"
        or (server_local and parsed.port != 8000)
    ):
        transport_name = "server-local Docker" if server_local else "local SSH-forwarded"
        raise ValueError(f"Phase 0 /ask must use the approved {transport_name} target")
    effective_trace_dsn = _trace_dsn_candidates(trace_dsn)[0]
    try:
        parsed_trace_dsn = urlsplit(effective_trace_dsn)
    except ValueError as exc:
        raise ValueError("Phase 0 trace DSN is invalid") from exc
    allowed_trace_hosts = (
        PHASE0_SERVER_LOCAL_TRACE_HOSTS
        if server_local
        else SOURCE_DIAGNOSTIC_LOOPBACK_HOSTS
    )
    if (
        parsed_trace_dsn.scheme not in {"postgres", "postgresql"}
        or parsed_trace_dsn.hostname not in allowed_trace_hosts
        or "," in parsed_trace_dsn.netloc
        or "%2c" in parsed_trace_dsn.netloc.casefold()
        or parsed_trace_dsn.query
        or parsed_trace_dsn.fragment
        or (server_local and parsed_trace_dsn.port != 5432)
    ):
        transport_name = "server-local Docker" if server_local else "local SSH-forwarded loopback"
        raise ValueError(f"Phase 0 trace lookup must use the approved {transport_name} DSN")
    argument_failures: list[str] = []
    if bypass_cache is not True:
        argument_failures.append("bypass_cache_required")
    if trace_lookup is not True:
        argument_failures.append("trace_lookup_required")
    if max_cases is not None:
        argument_failures.append("max_cases_forbidden")
    if auto_smoke_cases:
        argument_failures.append("auto_smoke_cases_forbidden")
    if generated_user_prefix:
        argument_failures.append("generated_user_prefix_forbidden")
    if concurrency != 1:
        argument_failures.append("concurrency_must_equal_one")
    if high_cost_approval_id != PHASE0_APPROVAL_ID:
        argument_failures.append("approval_id_mismatch")
    if (
        isinstance(max_llm_cost_rub, bool)
        or not isinstance(max_llm_cost_rub, (int, float))
        or not math.isfinite(float(max_llm_cost_rub))
        or float(max_llm_cost_rub) != PHASE0_COST_CAP_RUB
    ):
        argument_failures.append("cost_cap_mismatch")
    if argument_failures:
        raise ValueError(
            "Phase 0 preflight failed: " + ", ".join(argument_failures)
        )

    try:
        private_root = PRIVATE_DATA_ROOT.resolve(strict=True)
        manifest_resolved = manifest_path.resolve(strict=True)
        cases_resolved = cases_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Phase 0 manifest and cases must exist locally") from exc
    if (
        not manifest_resolved.is_file()
        or not manifest_resolved.is_relative_to(private_root)
        or not cases_resolved.is_relative_to(private_root)
    ):
        raise ValueError(
            "Phase 0 manifest and cases must remain under data/private"
        )
    if manifest_resolved.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Phase 0 manifest exceeds the bounded size limit")
    try:
        manifest = _read_json(manifest_resolved)
        raw_cases = _read_json(cases_resolved)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Phase 0 manifest and cases must be valid UTF-8 JSON"
        ) from exc
    if not isinstance(manifest, dict):
        raise ValueError("Phase 0 manifest must be a JSON object")
    if not isinstance(raw_cases, list):
        raise ValueError("Phase 0 cases must be a JSON array")

    canonical_cost_ledger = private_root / PHASE0_COST_LEDGER_DIRNAME
    if cost_ledger_dir is not None:
        candidate_cost_ledger = cost_ledger_dir
        if not candidate_cost_ledger.is_absolute():
            candidate_cost_ledger = PROJECT_ROOT / candidate_cost_ledger
        candidate_cost_ledger = Path(os.path.abspath(candidate_cost_ledger))
        if candidate_cost_ledger != canonical_cost_ledger:
            raise ValueError("Phase 0 cost ledger must use the canonical private path")

    # Import lazily so the generic runner does not load spreadsheet dependencies.
    from eval import social_ticket_benchmark as phase0_benchmark

    phase0_benchmark._validate_manifest_integrity(manifest)
    telemetry = manifest.get("telemetry")
    approval = manifest.get("approval")
    integrity = manifest.get("integrity")
    manifest_cases = manifest.get("cases")
    if not all(
        isinstance(value, Mapping)
        for value in (telemetry, approval, integrity)
    ) or not isinstance(manifest_cases, list):
        raise ValueError("Phase 0 manifest execution fields are invalid")

    runtime_git_sha = str(telemetry.get("git_sha") or "")
    builder_snapshot: dict[str, object] | None = None
    if server_local:
        from eval.phase0_server_provenance import (
            validate_phase0_builder_snapshot,
        )

        assert builder_source is not None
        builder_snapshot = validate_phase0_builder_snapshot(
            builder_source,
            telemetry_git_sha=runtime_git_sha,
        )
    else:
        phase0_benchmark._validate_builder_git_provenance(runtime_git_sha)
    manifest_cases_sha = str(integrity.get("cases_file_sha256") or "")
    ordered_selection_sha256 = str(
        integrity.get("ordered_selection_sha256") or ""
    )
    expected_external_sha = str(expected_cases_file_sha256 or "")
    if (
        SHA256_RE.fullmatch(expected_external_sha) is None
        or expected_external_sha != cases_file_sha256
        or manifest_cases_sha != cases_file_sha256
        or cases_file_sha256
        != phase0_benchmark.EXPECTED_PHASE0_CASES_FILE_SHA256
    ):
        raise ValueError(
            "Phase 0 expected_cases_file_sha256 must match the exact approved cases file"
        )
    if expected_runtime_git_sha != runtime_git_sha:
        raise ValueError(
            "Phase 0 expected_runtime_git_sha differs from the approved telemetry SHA"
        )
    if cost_runtime_git_sha is not None and cost_runtime_git_sha != runtime_git_sha:
        raise ValueError(
            "Phase 0 cost runtime Git SHA differs from the approved telemetry SHA"
        )
    if any(
        (
            approval.get("id") != PHASE0_APPROVAL_ID,
            approval.get("hard_cap_rub") != PHASE0_COST_CAP_RUB,
            approval.get("case_count") != PHASE0_CASES_TOTAL,
            approval.get("telemetry_git_sha") != runtime_git_sha,
            approval.get("ordered_selection_sha256")
            != ordered_selection_sha256,
        )
    ):
        raise ValueError("Phase 0 manifest approval tuple is invalid")

    case_ids = [str(case.get("id") or "") for case in cases]
    manifest_ids = [
        str(row.get("id") or "") if isinstance(row, Mapping) else ""
        for row in manifest_cases
    ]
    if (
        len(cases) != PHASE0_CASES_TOTAL
        or len(manifest_ids) != PHASE0_CASES_TOTAL
        or len(set(case_ids)) != PHASE0_CASES_TOTAL
        or any(not case_id for case_id in case_ids)
        or case_ids != manifest_ids
        or phase0_benchmark._canonical_sha256(case_ids)
        != ordered_selection_sha256
    ):
        raise ValueError(
            "Phase 0 cases must match the exact ordered 30-case manifest"
        )
    for raw_case, case, manifest_case in zip(
        raw_cases,
        cases,
        manifest_cases,
        strict=True,
    ):
        if not isinstance(raw_case, dict) or set(raw_case) != PHASE0_RUNNER_CASE_FIELDS:
            raise ValueError("Phase 0 runner cases must use the exact private schema")
        assert isinstance(manifest_case, Mapping)
        query_sha256 = hashlib.sha256(case["query"].encode("utf-8")).hexdigest()
        runner_case_sha256 = phase0_benchmark._canonical_sha256(raw_case)
        exact_case = bool(
            raw_case.get("id") == case.get("id")
            and raw_case.get("query") == case.get("query")
            and raw_case.get("privacy_class") == PRIVATE_TICKET_DERIVED
            and raw_case.get("split") == "calibration"
            and raw_case.get("label_status") == SOURCE_OBSERVED_DIAGNOSTIC_MODE
            and raw_case.get("requires_human_review") is False
            and raw_case.get("user_id") == raw_case.get("id")
            and raw_case.get("channel") == manifest_case.get("source_channel")
            and raw_case.get("tags") == PHASE0_RUNNER_TAGS
            and case.get("user_id") == case.get("id")
            and case.get("forum_context") is None
            and case.get("privacy_class") == PRIVATE_TICKET_DERIVED
            and case.get("split") == "calibration"
            and case.get("label_status") == SOURCE_OBSERVED_DIAGNOSTIC_MODE
            and case.get("tags") == PHASE0_RUNNER_TAGS
        )
        if not exact_case or any(
            (
                query_sha256 != manifest_case.get("deidentified_query_sha256"),
                runner_case_sha256 != manifest_case.get("runner_case_sha256"),
                case.get("channel") != manifest_case.get("source_channel"),
            )
        ):
            raise ValueError(
                "Phase 0 case payload differs from its exact single-turn manifest binding"
            )

    return {
        "manifest_path": manifest_resolved,
        "manifest_file_sha256": _file_sha256(manifest_resolved),
        "manifest_binding_sha256": hashlib.sha256(
            _canonical_json(manifest).encode("utf-8")
        ).hexdigest(),
        "cases_file_sha256": cases_file_sha256,
        "ordered_selection_sha256": ordered_selection_sha256,
        "runtime_git_sha": runtime_git_sha,
        "approval_id": PHASE0_APPROVAL_ID,
        "cost_cap_rub": PHASE0_COST_CAP_RUB,
        "case_count": PHASE0_CASES_TOTAL,
        "cost_scope": PHASE0_COST_SCOPE,
        "cost_ledger_dir": canonical_cost_ledger,
        "transport_mode": "server_local" if server_local else "ssh_loopback",
        "builder_snapshot": builder_snapshot,
    }


def _runtime_identity_report(
    *,
    expected_runtime_git_sha: str | None,
    preflight_release_git_sha: str | None,
    postflight_release_git_sha: str | None,
    required: bool,
) -> dict[str, Any]:
    expected = str(expected_runtime_git_sha or "") or None
    preflight = str(preflight_release_git_sha or "") or None
    postflight = str(postflight_release_git_sha or "") or None
    verified = bool(
        expected
        and preflight == expected
        and postflight == expected
    )
    if verified:
        status = "verified"
    elif required:
        status = "invalid"
    elif preflight or postflight:
        status = "observed_unbound"
    else:
        status = "not_checked"
    return {
        "required": required,
        "status": status,
        "expected_runtime_git_sha": expected,
        "preflight_release_git_sha": preflight,
        "postflight_release_git_sha": postflight,
        "verified_release_git_sha": (
            expected if verified else postflight or preflight
        ),
        "matched_expected_runtime": verified if expected else None,
    }


def _safe_cost_reservation_report(
    reservation: LiveEvalCostReservation | None,
    *,
    cases_file_sha256: str | None,
    include_private_full: bool = False,
) -> dict[str, Any] | None:
    if reservation is None:
        return None
    record = reservation.record
    runtime_git_sha = str(record.get("runtime_git_sha") or "")
    manifest_sha256 = str(record.get("manifest_sha256") or "")
    approval_id = str(record.get("high_cost_approval_id") or "") or None
    run_id = str(record.get("run_id") or "") or None
    scope = str(record.get("scope") or "") or None
    approval_required = record.get("approval_required")
    private_full = record.get("private_full")
    reservation_class_value = record.get("reservation_class")
    reservation_class = (
        reservation_class_value
        if reservation_class_value in {"routine", "private_full"}
        else None
    )
    try:
        case_count = _strict_nonnegative_int(record.get("case_count"))
    except ValueError:
        case_count = None
    try:
        approved_cap_rub = float(record.get("approved_cap_rub"))
    except (TypeError, ValueError, OverflowError):
        approved_cap_rub = math.nan
    if not math.isfinite(approved_cap_rub) or approved_cap_rub <= 0:
        approved_cap: float | None = None
    else:
        approved_cap = approved_cap_rub
    safe_cases_sha = str(cases_file_sha256 or "")
    runtime_valid = (
        FULL_GIT_SHA_RE.fullmatch(runtime_git_sha) is not None
        and runtime_git_sha != "0" * 40
    )
    manifest_valid = SHA256_RE.fullmatch(manifest_sha256) is not None
    cases_sha_valid = SHA256_RE.fullmatch(safe_cases_sha) is not None
    approval_valid = approval_id is None or (
        SAFE_COST_APPROVAL_ID_RE.fullmatch(approval_id) is not None
    )
    private_shape_valid = (
        private_full is None and reservation_class_value is None
    ) or (
        type(private_full) is bool
        and reservation_class
        == ("private_full" if private_full else "routine")
    )
    report = {
        "valid": all(
            (
                runtime_valid,
                manifest_valid,
                cases_sha_valid,
                case_count is not None,
                approved_cap is not None,
                approval_valid,
                bool(run_id),
                bool(scope),
                type(approval_required) is bool,
                manifest_sha256 == safe_cases_sha,
                private_shape_valid,
            )
        ),
        "run_id": run_id,
        "scope": scope,
        "runtime_git_sha": runtime_git_sha if runtime_valid else None,
        "manifest_sha256": manifest_sha256 if manifest_valid else None,
        "cases_file_sha256": safe_cases_sha if cases_sha_valid else None,
        "manifest_matches_cases_file": (
            manifest_sha256 == safe_cases_sha
            if manifest_valid and cases_sha_valid
            else None
        ),
        "case_count": case_count,
        "approved_cap_rub": approved_cap,
        "approval_required": (
            approval_required if type(approval_required) is bool else None
        ),
        "high_cost_approval_id": approval_id if approval_valid else None,
    }
    if (
        include_private_full
        and private_full is True
        and reservation_class == "private_full"
    ):
        report["private_full"] = True
        report["reservation_class"] = "private_full"
    return report


def _validate_cost_reservation_for_child_run(
    reservation: LiveEvalCostReservation,
    *,
    case_count: int,
    max_llm_cost_rub: float | None,
    private_full: bool,
) -> None:
    if max_llm_cost_rub is None:
        raise CostGovernanceError("child live eval requires a finite reserved budget")
    record = reservation.record
    try:
        reserved_cap = float(record["approved_cap_rub"])
        reserved_cases = int(record["case_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CostGovernanceError("parent cost reservation is invalid") from exc
    if not reservation.path.is_file():
        raise CostGovernanceError("parent cost reservation evidence is missing")
    if reserved_cap < max_llm_cost_rub or reserved_cases < case_count:
        raise CostGovernanceError("child live eval exceeds its parent cost reservation")
    if private_full and record.get("private_full") is not True:
        raise CostGovernanceError("private full eval requires a private reservation")


def _validate_phase0_cost_reservation(
    reservation: LiveEvalCostReservation,
    *,
    eval_run_id: str,
    contract: Mapping[str, Any],
) -> None:
    record = reservation.record
    expected = {
        "scope": PHASE0_COST_SCOPE,
        "run_id": eval_run_id,
        "runtime_git_sha": contract["runtime_git_sha"],
        "manifest_sha256": contract["cases_file_sha256"],
        "case_count": PHASE0_CASES_TOTAL,
        "approved_cap_rub": PHASE0_COST_CAP_RUB,
        "private_full": False,
        "approval_required": True,
        "high_cost_approval_id": PHASE0_APPROVAL_ID,
    }
    if any(record.get(field) != value for field, value in expected.items()):
        raise CostGovernanceError(
            "Phase 0 cost reservation differs from the approved execution tuple"
        )


def _validate_pilot50_candidate_cost_reservation(
    reservation: LiveEvalCostReservation,
    *,
    eval_run_id: str,
    contract: Mapping[str, Any],
    high_cost_approval_id: str | None,
) -> None:
    record = reservation.record
    expected = {
        "reservation_class": "private_full",
        "scope": PILOT50_CANDIDATE_COST_SCOPE,
        "run_id": eval_run_id,
        "runtime_git_sha": contract["runtime_git_sha"],
        "manifest_sha256": PILOT50_CANDIDATE_CASES_SHA256,
        "case_count": PILOT50_CANDIDATE_CASES_TOTAL,
        "approved_cap_rub": PILOT50_CANDIDATE_COST_CAP_RUB,
        "private_full": True,
        "approval_required": True,
        "high_cost_approval_id": high_cost_approval_id,
    }
    if any(record.get(field) != value for field, value in expected.items()):
        raise CostGovernanceError(
            "Pilot50 candidate cost reservation differs from the fixed execution tuple"
        )


def _is_in_process_mock_transport(
    transport: httpx.AsyncBaseTransport | None,
) -> bool:
    return isinstance(transport, httpx.MockTransport)


def _cost_preflight_failure_code(exc: ValueError) -> str:
    message = str(exc).casefold()
    if "cost ledger" in message or "cost reservation" in message:
        return "eval_cost_ledger_rejected"
    if "already been consumed" in message or "placeholder token" in message:
        return "high_cost_approval_rejected"
    if "pricing preflight" in message:
        return "llm_pricing_config_required"
    if "unbounded" in message:
        return "unbounded_llm_cost_forbidden"
    if "trace lookup" in message:
        return "llm_cost_trace_lookup_required"
    if "approval" in message:
        return "high_cost_approval_required"
    if "finite" in message:
        return "llm_budget_invalid"
    return "llm_budget_required"


def _local_llm_pricing_preflight_failure() -> str | None:
    try:
        settings = get_settings()
    except Exception as exc:
        return f"settings unavailable ({type(exc).__name__})"

    price_fields = {
        "simple_input": settings.cloud_ru_model_simple_input_price_rub_per_million,
        "simple_output": settings.cloud_ru_model_simple_output_price_rub_per_million,
        "complex_input": settings.cloud_ru_model_complex_input_price_rub_per_million,
        "complex_output": settings.cloud_ru_model_complex_output_price_rub_per_million,
    }
    invalid_prices = []
    for name, raw_value in price_fields.items():
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            invalid_prices.append(name)
            continue
        if not math.isfinite(value) or value <= 0:
            invalid_prices.append(name)
    if invalid_prices:
        return "positive model prices are required: " + ", ".join(invalid_prices)

    recognized_models = {
        str(settings.cloud_ru_model_simple or "").strip(),
        str(settings.cloud_ru_model_complex or "").strip(),
        str(settings.cloud_ru_model or "").strip(),
    }
    recognized_models.discard("")
    unknown_role_models = [
        model
        for model in (
            str(settings.cloud_ru_model_analyzer or "").strip(),
            str(settings.cloud_ru_model_judge or "").strip(),
        )
        if model and model not in recognized_models
    ]
    if unknown_role_models:
        return "configured analyzer/judge model has no price mapping"
    return None


def _guard_eval_privacy(
    *,
    cases: list[dict[str, Any]],
    cases_path: Path,
    output_path: Path,
    markdown_path: Path | None,
    target: str,
    phase0_server_local: bool = False,
) -> None:
    private_root = PRIVATE_DATA_ROOT.resolve()
    cases_resolved = cases_path.resolve()
    if cases_resolved.is_relative_to(private_root) and any(
        case.get("privacy_class") != PRIVATE_TICKET_DERIVED for case in cases
    ):
        raise ValueError(
            "eval cases stored under data/private cannot use privacy_class=standard"
        )
    if not any(
        case.get("privacy_class") == PRIVATE_TICKET_DERIVED for case in cases
    ):
        return

    source_diagnostic = any(
        case.get("label_status") == SOURCE_OBSERVED_DIAGNOSTIC_MODE
        for case in cases
    )
    if phase0_server_local and not source_diagnostic:
        raise ValueError(
            "Phase 0 server-local privacy exception requires source diagnostics"
        )
    if phase0_server_local:
        allowed_hosts = PHASE0_SERVER_LOCAL_ASK_HOSTS
    else:
        allowed_hosts = (
            SOURCE_DIAGNOSTIC_LOOPBACK_HOSTS
            if source_diagnostic
            else PRIVATE_EVAL_HOSTS
        )
    parsed = urlsplit(target)
    valid_scheme = parsed.scheme == "http" if phase0_server_local else (
        parsed.scheme in {"http", "https"}
    )
    if (
        not valid_scheme
        or parsed.hostname not in allowed_hosts
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/ask"
        or (phase0_server_local and parsed.port != 8000)
    ):
        target_contract = (
            "the approved server-local Docker /ask target"
            if phase0_server_local
            else "a local SSH-forwarded loopback /ask target"
            if source_diagnostic
            else "a loopback or app-ml /ask target"
        )
        raise ValueError(
            "private_ticket_derived cases may only use " + target_contract
        )

    artifact_paths = [cases_resolved, output_path.resolve()]
    if markdown_path is not None:
        artifact_paths.append(markdown_path.resolve())
    if not all(path.is_relative_to(private_root) for path in artifact_paths):
        raise ValueError(
            f"private_ticket_derived eval artifacts must stay under {private_root}"
        )


def _guard_eval_artifact_aliases(
    *,
    cases_path: Path,
    output_path: Path,
    markdown_path: Path | None,
    extra_paths: list[Path] | None = None,
) -> None:
    named_paths: list[tuple[str, Path]] = [
        ("cases", cases_path),
        ("output", output_path),
    ]
    if markdown_path is not None:
        named_paths.append(("markdown", markdown_path))
    for index, path in enumerate(extra_paths or [], start=1):
        named_paths.append((f"extra_{index}", path))

    for left_index, (left_name, left_path) in enumerate(named_paths):
        for right_name, right_path in named_paths[left_index + 1 :]:
            if _paths_alias(left_path, right_path):
                raise ValueError(
                    "eval artifact paths must not alias each other: "
                    f"{left_name} and {right_name}"
                )


def _paths_alias(left: Path, right: Path) -> bool:
    left_resolved = left.expanduser().resolve()
    right_resolved = right.expanduser().resolve()
    if os.path.normcase(str(left_resolved)) == os.path.normcase(
        str(right_resolved)
    ):
        return True
    if left_resolved.exists() and right_resolved.exists():
        try:
            return os.path.samefile(left_resolved, right_resolved)
        except OSError:
            return False
    return False


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


async def _verify_cache_bypass_runtime(
    *,
    client: httpx.AsyncClient,
    target: str,
    headers: dict[str, str],
    expected_git_sha: str | None,
    eval_run_id: str,
    cache_bypass_secret: str,
) -> str:
    ready_url = urlsplit(target)._replace(path="/ready", query="", fragment="").geturl()
    ready_headers = {
        **headers,
        eval_cache_bypass.HEADER_BYPASS: "1",
        eval_cache_bypass.HEADER_CAPABILITY_PROBE: "1",
        **eval_cache_bypass.build_signed_headers(
            cache_bypass_secret,
            method="GET",
            path="/ready",
            eval_run_id=eval_run_id,
            eval_case_id=eval_cache_bypass.CAPABILITY_PROBE_CASE_ID,
            payload_sha256=eval_cache_bypass.EMPTY_PAYLOAD_SHA256,
        ),
    }
    try:
        response = await client.get(ready_url, headers=ready_headers)
    except httpx.HTTPError:
        raise ValueError("signed cache-bypass runtime /ready check failed") from None
    if response.status_code != 200:
        raise ValueError(
            "signed cache-bypass runtime /ready check returned "
            f"HTTP {response.status_code}"
        )
    payload = _safe_response_json(response)
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        raise ValueError(
            "signed cache-bypass runtime /ready payload is not ready"
        )
    release_git_sha = str(payload.get("release_git_sha") or "").strip()
    if expected_git_sha is not None and release_git_sha != expected_git_sha:
        raise ValueError(
            "signed cache-bypass runtime release_git_sha does not match the "
            "expected evaluation runtime"
        )
    capability = payload.get("eval_cache_bypass")
    if (
        not isinstance(capability, dict)
        or capability.get("scheme") != eval_cache_bypass.SCHEME
        or capability.get("authorized") is not True
    ):
        raise ValueError(
            "runtime did not authorize signed "
            "cache bypass capability"
        )
    return release_git_sha


def _validated_holdout_ledger_dir(
    value: Path | None,
    *,
    cases_path: Path,
) -> Path:
    if value is None:
        raise ValueError("sealed private holdout requires holdout_ledger_dir")
    ledger = value.expanduser().resolve()
    private_root = PRIVATE_DATA_ROOT.resolve()
    cases_dir = cases_path.expanduser().resolve().parent
    if not ledger.is_relative_to(private_root):
        raise ValueError("holdout ledger must stay under data/private")
    if ledger.is_relative_to(cases_dir) or cases_dir.is_relative_to(ledger):
        raise ValueError(
            "holdout ledger must be in a separate private directory tree "
            "from the cases file"
        )
    canonical_ledger = (
        private_root / CANONICAL_HOLDOUT_LEDGER_DIRNAME
    ).resolve()
    if ledger != canonical_ledger:
        raise ValueError(
            "holdout ledger must use the canonical persistent private "
            f"directory: {canonical_ledger}"
        )
    ledger.mkdir(parents=True, exist_ok=True)
    if not ledger.is_dir():
        raise ValueError("holdout ledger path must be a directory")
    return ledger


def _validated_calibration_replay_ledger_dir(
    value: Path | None,
    *,
    cases_path: Path,
) -> Path:
    if value is None:
        raise ValueError(
            "calibration_replay requires calibration_replay_ledger_dir"
        )
    ledger = value.expanduser().resolve()
    private_root = PRIVATE_DATA_ROOT.resolve()
    cases_dir = cases_path.expanduser().resolve().parent
    if not ledger.is_relative_to(private_root):
        raise ValueError("calibration replay ledger must stay under data/private")
    if ledger.is_relative_to(cases_dir) or cases_dir.is_relative_to(ledger):
        raise ValueError(
            "calibration replay ledger must be in a separate private directory "
            "tree from the cases file"
        )
    canonical_ledger = (
        private_root / CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME
    ).resolve()
    if ledger != canonical_ledger:
        raise ValueError(
            "calibration replay ledger must use the canonical persistent private "
            f"directory: {canonical_ledger}"
        )
    ledger.mkdir(parents=True, exist_ok=True)
    if not ledger.is_dir():
        raise ValueError("calibration replay ledger path must be a directory")
    return ledger


def _parse_receipt_timestamp(value: Any, *, label: str) -> datetime:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must contain a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _recent_private_full_eval_receipts(*, now: datetime) -> list[str]:
    cutoff = now - timedelta(hours=24)
    recent: list[str] = []
    for dirname, label in (
        (CANONICAL_HOLDOUT_LEDGER_DIRNAME, "sealed holdout started receipt"),
        (
            CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME,
            "calibration replay started receipt",
        ),
    ):
        ledger = PRIVATE_DATA_ROOT.resolve() / dirname
        if not _path_lexists(ledger):
            continue
        if ledger.is_symlink() or not ledger.is_dir():
            raise ValueError(f"{label} ledger must be a real directory")
        for path in sorted(ledger.glob("*.started.json")):
            payload = _validated_receipt_payload(path, label=label)
            started_at = _parse_receipt_timestamp(
                payload.get("started_at"),
                label=label,
            )
            if started_at > now + timedelta(minutes=5):
                raise ValueError(f"{label} timestamp is in the future")
            if started_at >= cutoff:
                recent.append(str(path))
    return recent


def _reserve_private_full_eval(
    *,
    ledger_dir: Path,
    eval_run_id: str,
    run_mode: str,
    contract: dict[str, Any],
    evaluation_runtime_git_sha: str | None,
    high_cost_approval_id: str,
    max_llm_cost_rub: float | None,
    now: datetime | None = None,
) -> Path:
    reservation_time = (now or datetime.now(UTC)).astimezone(UTC)
    if max_llm_cost_rub is None or not math.isfinite(max_llm_cost_rub):
        raise ValueError("private full eval requires a finite hard cost cap")
    if SAFE_COST_APPROVAL_ID_RE.fullmatch(high_cost_approval_id) is None:
        raise ValueError("private full eval requires a valid cost approval id")
    recent_receipts = _recent_private_full_eval_receipts(now=reservation_time)
    if recent_receipts:
        raise ValueError(
            "another private full eval started within the previous 24 hours"
        )

    cutoff = reservation_time - timedelta(hours=24)
    reservation_ledgers: list[Path] = []
    for dirname in (
        CANONICAL_HOLDOUT_LEDGER_DIRNAME,
        CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME,
    ):
        candidate = PRIVATE_DATA_ROOT.resolve() / dirname
        if not _path_lexists(candidate):
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("private eval ledger must be a real directory")
        reservation_ledgers.append(candidate)
    if ledger_dir.resolve() not in {path.resolve() for path in reservation_ledgers}:
        raise ValueError("cost reservation requires a canonical private eval ledger")
    for ledger in reservation_ledgers:
        for path in sorted(ledger.glob("full-eval-*.reserved.json")):
            payload = _validated_receipt_payload(path, label="eval cost reservation")
            if payload.get("schema_version") != "1.0.0":
                raise ValueError("eval cost reservation has an invalid schema")
            if payload.get("high_cost_approval_id") == high_cost_approval_id:
                raise ValueError("high-cost approval id has already been consumed")
            reserved_at = _parse_receipt_timestamp(
                payload.get("reserved_at"),
                label="eval cost reservation",
            )
            if reserved_at > reservation_time + timedelta(minutes=5):
                raise ValueError("eval cost reservation timestamp is in the future")
            if reserved_at >= cutoff:
                raise ValueError(
                    "another private full eval was reserved within the previous 24 hours"
                )

    marker_path = ledger_dir / (
        f"full-eval-{reservation_time.strftime('%Y%m%d')}.reserved.json"
    )
    runtime_git_sha = str(
        evaluation_runtime_git_sha or contract.get("runtime_git_sha") or ""
    )
    payload = {
        "schema_version": "1.0.0",
        "reservation_type": "private_full_eval_cost",
        "reserved_at": reservation_time.isoformat(),
        "eval_run_id": eval_run_id,
        "run_mode": run_mode,
        "baseline_id": contract.get("baseline_id"),
        "runtime_git_sha": runtime_git_sha,
        "selected_case_ids_sha256": contract.get("selected_case_ids_sha256"),
        "high_cost_approval_id": high_cost_approval_id,
        "hard_cost_cap_rub": max_llm_cost_rub,
        "policy": "one_private_full_eval_per_rolling_24_hours",
    }
    try:
        _write_json_exclusive(marker_path, payload)
    except FileExistsError as exc:
        raise ValueError(
            "a private full eval reservation already exists for this UTC day"
        ) from exc
    return marker_path


def _require_prior_sealed_exposure_receipts(
    *,
    contract: dict[str, Any],
    cases_file_sha256: str,
) -> list[str]:
    ledger = (
        PRIVATE_DATA_ROOT.resolve() / CANONICAL_HOLDOUT_LEDGER_DIRNAME
    )
    if ledger.is_symlink() or (
        _path_lexists(ledger) and not ledger.is_dir()
    ):
        raise ValueError("canonical sealed exposure ledger must be a real directory")
    receipt_key = _derive_holdout_receipt_key(
        str(contract["selected_case_ids_sha256"])
    )
    expected_fields = {
        "schema_version": "1.0.0",
        "baseline_id": contract["baseline_id"],
        "receipt_key": receipt_key,
        "runtime_git_sha": contract["runtime_git_sha"],
        "freeze_contract_sha256": contract["freeze_contract_sha256"],
        "selected_case_ids_sha256": contract["selected_case_ids_sha256"],
        "cases_payload_sha256": contract["cases_payload_sha256"],
        "cases_file_sha256": cases_file_sha256,
    }
    evidence: list[str] = []
    for status in ("started", "completed"):
        path = ledger / f"{receipt_key}.{status}.json"
        if not _path_lexists(path):
            continue
        payload = _validated_receipt_payload(
            path,
            label="sealed exposure receipt",
        )
        if payload.get("status") != status or any(
            payload.get(field) != expected
            for field, expected in expected_fields.items()
        ):
            raise ValueError(
                "calibration_replay prior sealed exposure receipt does not "
                "match the exact source contract and cases file"
            )
        evidence.append(path.name)
    if not evidence:
        raise ValueError(
            "calibration_replay requires an existing canonical sealed started "
            "or completed receipt for the exact exposed source file"
        )
    return evidence


def _calibration_replay_exposure_receipts(
    *,
    selected_case_ids_sha256: str,
) -> list[str]:
    ledger = (
        PRIVATE_DATA_ROOT.resolve()
        / CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME
    )
    if not _path_lexists(ledger):
        return []
    if ledger.is_symlink() or not ledger.is_dir():
        raise ValueError(
            "canonical calibration replay ledger must be a real directory"
        )
    receipt_paths = sorted(
        {
            *ledger.glob("*.started.json"),
            *ledger.glob("*.completed.json"),
        }
    )
    matching: list[str] = []
    for path in receipt_paths:
        status = "started" if path.name.endswith(".started.json") else "completed"
        payload = _validated_receipt_payload(
            path,
            label="calibration replay exposure receipt",
        )
        receipt_selection = str(
            payload.get("selected_case_ids_sha256") or ""
        )
        cases_file_sha256 = str(payload.get("cases_file_sha256") or "")
        evaluation_runtime_git_sha = str(
            payload.get("evaluation_runtime_git_sha") or ""
        )
        receipt_key = str(payload.get("receipt_key") or "")
        valid = (
            payload.get("schema_version") == "1.0.0"
            and payload.get("receipt_type")
            == "exposed_holdout_calibration_replay"
            and payload.get("run_mode") == "calibration_replay"
            and payload.get("status") == status
            and SHA256_RE.fullmatch(receipt_selection) is not None
            and SHA256_RE.fullmatch(cases_file_sha256) is not None
            and FULL_GIT_SHA_RE.fullmatch(evaluation_runtime_git_sha)
            is not None
            and evaluation_runtime_git_sha != "0" * 40
        )
        if valid:
            expected_key = _derive_calibration_replay_receipt_key(
                selected_case_ids_sha256=receipt_selection,
                cases_file_sha256=cases_file_sha256,
                runtime_git_sha=evaluation_runtime_git_sha,
            )
            valid = (
                receipt_key == expected_key
                and path.name == f"{expected_key}.{status}.json"
            )
        if not valid:
            raise ValueError(
                "canonical calibration replay ledger contains an invalid "
                "started or completed receipt"
            )
        if receipt_selection == selected_case_ids_sha256:
            matching.append(path.name)
    return matching


def _validated_receipt_payload(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    try:
        payload = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable or invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return payload


def _derive_holdout_receipt_key(selected_case_ids_sha256: str) -> str:
    immutable_identity = {
        "domain": "rosmol-private-holdout-selection-v1",
        "selected_case_ids_sha256": selected_case_ids_sha256,
    }
    return hashlib.sha256(
        _canonical_json(immutable_identity).encode("utf-8")
    ).hexdigest()


def _derive_calibration_replay_receipt_key(
    *,
    selected_case_ids_sha256: str,
    cases_file_sha256: str,
    runtime_git_sha: str,
) -> str:
    immutable_identity = {
        "domain": "rosmol-exposed-holdout-calibration-replay-v1",
        "selected_case_ids_sha256": selected_case_ids_sha256,
        "cases_file_sha256": cases_file_sha256,
        "runtime_git_sha": runtime_git_sha,
    }
    return hashlib.sha256(
        _canonical_json(immutable_identity).encode("utf-8")
    ).hexdigest()


def _holdout_report_classification(
    contract: dict[str, Any],
    *,
    calibration_replay: bool = False,
) -> dict[str, Any]:
    if calibration_replay:
        return {
            "review_mode": str(contract["review_mode"]),
            "report_status": CALIBRATION_REPLAY_REPORT_STATUS,
            "provisional": True,
            "calibration_only": True,
            "independent_evaluation": False,
            "previously_exposed": True,
            "product_verdict_eligible": False,
            "human_product_verdict": False,
            "measurement_disclaimer": (
                "Calibration replay only. This previously exposed Product80 set "
                "is not an independent holdout and cannot support a product "
                "conversion claim."
            ),
        }
    review_mode = str(contract["review_mode"])
    provisional = review_mode == MODEL_ASSISTED_PRERUN_MODE
    return {
        "review_mode": review_mode,
        "report_status": (
            MODEL_ASSISTED_REPORT_STATUS
            if provisional
            else "sealed_human_reviewed_prerun_diagnostic"
        ),
        "provisional": provisional,
        "product_verdict_eligible": bool(
            contract["product_verdict_eligible"]
        ),
        "human_product_verdict": False,
        "measurement_disclaimer": (
            "Diagnostic only. Model-assisted pre-run labels were not "
            "human-reviewed and cannot be reported as product conversion "
            "or a human product verdict."
            if provisional
            else "Machine diagnostics require a separate human post-run "
            "product verdict."
        ),
    }


def _create_holdout_started_receipt(
    path: Path,
    *,
    contract: dict[str, Any],
    expected_cases_file_sha256: str,
    eval_run_id: str,
    cases_path: Path,
    output_path: Path,
    receipt_key: str,
    calibration_replay: bool = False,
    evaluation_runtime_git_sha: str | None = None,
    prior_sealed_exposure_receipts: list[str] | None = None,
) -> None:
    if calibration_replay:
        if evaluation_runtime_git_sha is None:
            raise ValueError(
                "calibration replay receipt requires evaluation runtime SHA"
            )
        payload = {
            "schema_version": "1.0.0",
            "receipt_type": "exposed_holdout_calibration_replay",
            "status": "started",
            "started_at": datetime.now(UTC).isoformat(),
            "run_mode": "calibration_replay",
            "baseline_id": contract["baseline_id"],
            "eval_run_id": eval_run_id,
            "source_runtime_git_sha": contract["runtime_git_sha"],
            "evaluation_runtime_git_sha": evaluation_runtime_git_sha,
            **_holdout_report_classification(
                contract,
                calibration_replay=True,
            ),
            "freeze_contract_sha256": contract["freeze_contract_sha256"],
            "selected_case_ids_sha256": contract[
                "selected_case_ids_sha256"
            ],
            "cases_payload_sha256": contract["cases_payload_sha256"],
            "expected_cases_payload_sha256": contract[
                "cases_payload_sha256"
            ],
            "cases_file_sha256": expected_cases_file_sha256,
            "expected_cases_file_sha256": expected_cases_file_sha256,
            "receipt_key": receipt_key,
            "cases_path": str(cases_path.expanduser().resolve()),
            "output_path": str(output_path.expanduser().resolve()),
            "source_holdout_contract": contract,
            "prior_sealed_exposure_receipts": list(
                prior_sealed_exposure_receipts or []
            ),
            "knowledge_base_identity_gate": (
                "manual_pre_run_verification_required"
            ),
            "one_shot_scope": (
                "one_calibration_replay_per_source_selection_file_and_runtime"
            ),
        }
    else:
        # Keep the sealed receipt v1 schema and fields byte-compatible.
        payload = {
            "schema_version": "1.0.0",
            "status": "started",
            "started_at": datetime.now(UTC).isoformat(),
            "baseline_id": contract["baseline_id"],
            "eval_run_id": eval_run_id,
            "runtime_git_sha": contract["runtime_git_sha"],
            **_holdout_report_classification(contract),
            "freeze_contract_sha256": contract["freeze_contract_sha256"],
            "selected_case_ids_sha256": contract[
                "selected_case_ids_sha256"
            ],
            "cases_payload_sha256": contract["cases_payload_sha256"],
            "expected_cases_payload_sha256": contract[
                "cases_payload_sha256"
            ],
            "cases_file_sha256": expected_cases_file_sha256,
            "expected_cases_file_sha256": expected_cases_file_sha256,
            "receipt_key": receipt_key,
            "cases_path": str(cases_path.expanduser().resolve()),
            "output_path": str(output_path.expanduser().resolve()),
            "knowledge_base_identity_gate": (
                "manual_pre_run_verification_required"
            ),
            "one_shot_scope": (
                "enforced_while_canonical_persistent_ledger_is_preserved"
            ),
        }
    _write_json_exclusive(path, payload)


def _create_holdout_completed_receipt(
    path: Path,
    *,
    contract: dict[str, Any],
    expected_cases_file_sha256: str,
    eval_run_id: str,
    receipt_key: str,
    output_path: Path,
    output_sha256: str,
    calibration_replay: bool = False,
    evaluation_runtime_git_sha: str | None = None,
    prior_sealed_exposure_receipts: list[str] | None = None,
) -> None:
    if calibration_replay:
        if evaluation_runtime_git_sha is None:
            raise ValueError(
                "calibration replay receipt requires evaluation runtime SHA"
            )
        payload = {
            "schema_version": "1.0.0",
            "receipt_type": "exposed_holdout_calibration_replay",
            "status": "completed",
            "completed_at": datetime.now(UTC).isoformat(),
            "run_mode": "calibration_replay",
            "baseline_id": contract["baseline_id"],
            "eval_run_id": eval_run_id,
            "receipt_key": receipt_key,
            "source_runtime_git_sha": contract["runtime_git_sha"],
            "evaluation_runtime_git_sha": evaluation_runtime_git_sha,
            **_holdout_report_classification(
                contract,
                calibration_replay=True,
            ),
            "freeze_contract_sha256": contract["freeze_contract_sha256"],
            "selected_case_ids_sha256": contract[
                "selected_case_ids_sha256"
            ],
            "cases_payload_sha256": contract["cases_payload_sha256"],
            "expected_cases_payload_sha256": contract[
                "cases_payload_sha256"
            ],
            "cases_file_sha256": expected_cases_file_sha256,
            "expected_cases_file_sha256": expected_cases_file_sha256,
            "output_path": str(output_path.expanduser().resolve()),
            "output_sha256": output_sha256,
            "cases_total": contract["cases_total"],
            "source_holdout_contract": contract,
            "prior_sealed_exposure_receipts": list(
                prior_sealed_exposure_receipts or []
            ),
            "one_shot_scope": (
                "one_calibration_replay_per_source_selection_file_and_runtime"
            ),
        }
    else:
        # Keep the sealed receipt v1 schema and fields byte-compatible.
        payload = {
            "schema_version": "1.0.0",
            "status": "completed",
            "completed_at": datetime.now(UTC).isoformat(),
            "baseline_id": contract["baseline_id"],
            "eval_run_id": eval_run_id,
            "receipt_key": receipt_key,
            "runtime_git_sha": contract["runtime_git_sha"],
            **_holdout_report_classification(contract),
            "freeze_contract_sha256": contract["freeze_contract_sha256"],
            "selected_case_ids_sha256": contract[
                "selected_case_ids_sha256"
            ],
            "cases_payload_sha256": contract["cases_payload_sha256"],
            "expected_cases_payload_sha256": contract[
                "cases_payload_sha256"
            ],
            "cases_file_sha256": expected_cases_file_sha256,
            "expected_cases_file_sha256": expected_cases_file_sha256,
            "output_path": str(output_path.expanduser().resolve()),
            "output_sha256": output_sha256,
            "cases_total": contract["cases_total"],
            "one_shot_scope": (
                "enforced_while_canonical_persistent_ledger_is_preserved"
            ),
        }
    _write_json_exclusive(path, payload)


async def _write_holdout_rejection_report(
    *,
    ledger_dir: Path,
    receipt_key: str,
    target: str,
    cases_path: Path,
    eval_run_id: str,
    contract: dict[str, Any],
    expected_cases_file_sha256: str,
    status: str,
    failures: list[str],
    executed_cases_total: int,
    receipt_path: Path | None,
    trace_lookup_error: str | None = None,
    detail: str | None = None,
    base_metrics: dict[str, Any] | None = None,
    calibration_replay: bool = False,
    evaluation_runtime_git_sha: str | None = None,
    prior_sealed_exposure_receipts: list[str] | None = None,
) -> dict[str, Any]:
    metrics = (
        dict(base_metrics)
        if base_metrics is not None
        else _empty_metrics(
            target=target,
            cases_path=cases_path,
            auto_smoke_cases=False,
        )
    )
    metrics["eval_run_id"] = eval_run_id
    metrics["cases_total"] = executed_cases_total
    metrics["message"] = (
        "calibration replay rejected before valid completion"
        if calibration_replay
        else "sealed holdout rejected before valid completion"
    )
    if calibration_replay:
        metrics["source_holdout_contract"] = contract
    else:
        metrics["holdout_contract"] = contract
    metrics["report_classification"] = _holdout_report_classification(
        contract,
        calibration_replay=calibration_replay,
    )
    if trace_lookup_error:
        metrics["trace_lookup_error"] = trace_lookup_error
    if detail:
        metrics[
            "calibration_replay_rejection_detail"
            if calibration_replay
            else "holdout_rejection_detail"
        ] = detail
    rejection_path = ledger_dir / (
        f"{receipt_key}.{eval_run_id}.rejected.json"
    )
    run_key = "calibration_replay" if calibration_replay else "holdout_run"
    metrics[run_key] = {
        "status": status,
        "completed": False,
        **_holdout_report_classification(
            contract,
            calibration_replay=calibration_replay,
        ),
        "expected_cases_total": contract["cases_total"],
        "executed_cases_total": executed_cases_total,
        "cases_file_sha256": expected_cases_file_sha256,
        "expected_cases_file_sha256": expected_cases_file_sha256,
        "integrity_failures": list(failures),
        "started_receipt": receipt_path.name if receipt_path else None,
        "knowledge_base_identity_gate": "manual_pre_run_verification_required",
        "one_shot_scope": (
            "one_calibration_replay_per_source_selection_file_and_runtime"
            if calibration_replay
            else "enforced_while_canonical_persistent_ledger_is_preserved"
        ),
        "rejection_evidence": rejection_path.name,
    }
    if calibration_replay:
        metrics[run_key]["source_runtime_git_sha"] = contract[
            "runtime_git_sha"
        ]
        metrics[run_key]["evaluation_runtime_git_sha"] = (
            evaluation_runtime_git_sha
        )
        metrics[run_key]["prior_sealed_exposure_receipts"] = list(
            prior_sealed_exposure_receipts or []
        )
    await asyncio.to_thread(_write_json_exclusive, rejection_path, metrics)
    return metrics


def _holdout_integrity_failures(
    metrics: dict[str, Any],
    *,
    results: list[dict[str, Any]],
    expected_cases_total: int,
    executed_cases_total: int,
    trace_cardinality: dict[str, Any] | None,
    trace_cardinality_error: str | None,
) -> list[str]:
    failures: list[str] = []
    if executed_cases_total != expected_cases_total:
        failures.append("case_count_incomplete")
    if metrics.get("http_success_rate") != 1.0:
        failures.append("http_success_below_100_percent")
    if metrics.get("trace_coverage_rate") != 1.0:
        failures.append("trace_coverage_below_100_percent")
    request_ids = [
        str(item.get("request_id") or "").strip()
        for item in results
    ]
    if (
        len(request_ids) != expected_cases_total
        or any(not request_id for request_id in request_ids)
    ):
        failures.append("request_ids_missing")
    if len(set(request_ids)) != expected_cases_total:
        failures.append("request_ids_not_unique")
    if any(item.get("cache_hit") is not False for item in results):
        failures.append("cache_hit_not_exactly_false")
    if any(item.get("trace_binding_match") is not True for item in results):
        failures.append("trace_binding_mismatch")
    if any(item.get("trace_lookup_error") for item in results) or metrics.get(
        "trace_lookup_error"
    ):
        failures.append("trace_lookup_error")
    if any(item.get("trace_error") not in (None, "") for item in results):
        failures.append("trace_error_present")
    if trace_cardinality_error is not None or trace_cardinality is None:
        failures.append("trace_cardinality_lookup_error")
    else:
        if trace_cardinality.get("traces_total") != expected_cases_total:
            failures.append("trace_cardinality_total_mismatch")
        if trace_cardinality.get("missing_case_ids"):
            failures.append("trace_cardinality_missing_case_ids")
        if trace_cardinality.get("duplicate_case_ids"):
            failures.append("trace_cardinality_duplicate_case_ids")
        if trace_cardinality.get("unknown_case_ids"):
            failures.append("trace_cardinality_unknown_case_ids")
    return failures


def _phase0_integrity_failures(
    metrics: Mapping[str, Any],
    *,
    results: list[dict[str, Any]],
    trace_cardinality: Mapping[str, Any] | None,
    trace_cardinality_error: str | None,
) -> list[str]:
    failures: list[str] = []
    if len(results) != PHASE0_CASES_TOTAL:
        failures.append("case_count_incomplete")
    if metrics.get("http_success_rate") != 1.0:
        failures.append("http_success_below_100_percent")
    if metrics.get("trace_coverage_rate") != 1.0:
        failures.append("trace_coverage_below_100_percent")

    request_ids = [str(result.get("request_id") or "") for result in results]
    valid_request_ids: list[str] = []
    for request_id in request_ids:
        try:
            valid_request_ids.append(str(UUID(request_id)))
        except (TypeError, ValueError, AttributeError):
            failures.append("request_id_invalid")
            break
    if len(valid_request_ids) != PHASE0_CASES_TOTAL:
        if "request_id_invalid" not in failures:
            failures.append("request_ids_missing")
    elif len(set(valid_request_ids)) != PHASE0_CASES_TOTAL:
        failures.append("request_ids_not_unique")

    if any(result.get("cache_hit") is not False for result in results):
        failures.append("cache_hit_not_exactly_false")
    if any(result.get("trace_found") is not True for result in results):
        failures.append("trace_coverage_below_100_percent")
    if any(result.get("trace_binding_match") is not True for result in results):
        failures.append("trace_binding_mismatch")
    if any(
        result.get("trace_eval_run_id") != metrics.get("eval_run_id")
        or result.get("trace_eval_case_id") != result.get("id")
        for result in results
    ):
        failures.append("trace_identity_mismatch")
    if any(result.get("trace_lookup_error") for result in results) or metrics.get(
        "trace_lookup_error"
    ):
        failures.append("trace_lookup_error")
    if any(result.get("trace_error") not in (None, "") for result in results):
        failures.append("trace_error_present")
    if any(result.get("error") not in (None, "") for result in results):
        failures.append("case_error_present")

    if trace_cardinality_error is not None or trace_cardinality is None:
        failures.append("trace_cardinality_lookup_error")
    else:
        exact_cardinality = bool(
            trace_cardinality.get("eval_run_id") == metrics.get("eval_run_id")
            and trace_cardinality.get("expected_cases_total")
            == PHASE0_CASES_TOTAL
            and trace_cardinality.get("traces_total") == PHASE0_CASES_TOTAL
            and trace_cardinality.get("request_case_pairs_match") is True
            and trace_cardinality.get("distinct_request_ids_total")
            == PHASE0_CASES_TOTAL
            and trace_cardinality.get("invalid_expected_request_ids_total") == 0
            and trace_cardinality.get("invalid_observed_request_ids_total") == 0
            and trace_cardinality.get("duplicate_request_ids_total") == 0
            and trace_cardinality.get("missing_request_case_pairs_total") == 0
            and trace_cardinality.get("unexpected_request_case_pairs_total") == 0
            and trace_cardinality.get("cache_hit_true_total") == 0
            and trace_cardinality.get("cache_hit_false_total")
            == PHASE0_CASES_TOTAL
            and trace_cardinality.get("cache_hit_unknown_total") == 0
            and not trace_cardinality.get("missing_case_ids")
            and not trace_cardinality.get("duplicate_case_ids")
            and not trace_cardinality.get("unknown_case_ids")
        )
        case_counts = trace_cardinality.get("case_counts")
        exact_cardinality = bool(
            exact_cardinality
            and isinstance(case_counts, Mapping)
            and len(case_counts) == PHASE0_CASES_TOTAL
            and all(type(count) is int and count == 1 for count in case_counts.values())
        )
        if not exact_cardinality:
            failures.append("trace_cardinality_mismatch")
    return list(dict.fromkeys(failures))


def _holdout_failure_status(
    failures: list[str],
    *,
    budget_stopped: bool,
) -> str:
    if not failures:
        return "completed"
    if "llm_pricing_unavailable" in failures:
        return "cost_accounting_failed"
    if "case_count_incomplete" in failures:
        return "incomplete_budget_stop" if budget_stopped else "incomplete"
    if "http_success_below_100_percent" in failures:
        return "http_failed"
    if (
        "trace_coverage_below_100_percent" in failures
        or "trace_lookup_error" in failures
        or "trace_binding_mismatch" in failures
        or "trace_error_present" in failures
        or any(failure.startswith("trace_cardinality_") for failure in failures)
    ):
        return "trace_failed"
    if "cache_hit_not_exactly_false" in failures:
        return "cache_contaminated"
    return "integrity_failed"


def _apply_run_limits(
    metrics: dict[str, Any],
    *,
    original_cases_total: int,
    max_cases: int | None,
    max_llm_cost_rub: float | None,
) -> None:
    if max_cases is not None:
        metrics["cases_original_total"] = original_cases_total
        metrics["cases_limit"] = max_cases
        metrics["cases_limited"] = original_cases_total > metrics.get("cases_total", 0)

    if max_llm_cost_rub is None:
        metrics["llm_budget_rub"] = None
        metrics["llm_budget_exceeded"] = None
        return

    actual_cost = float(metrics.get("llm_estimated_cost_rub") or 0.0)
    metrics["llm_budget_rub"] = max_llm_cost_rub
    metrics["llm_budget_exceeded"] = actual_cost > max_llm_cost_rub


def _llm_cost_rub_total(results: list[dict[str, Any]]) -> float:
    total = 0.0
    for item in results:
        try:
            value = float(item.get("llm_estimated_cost_rub") or 0.0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            total += value
    return total


def _llm_cost_accounting_failure(result: dict[str, Any]) -> str | None:
    if result.get("trace_found") is not True:
        return "llm_cost_trace_missing"
    if result.get("llm_accounting_present") is False:
        return "llm_cost_accounting_missing"
    try:
        aggregate_cost = float(result.get("llm_estimated_cost_rub"))
    except (TypeError, ValueError):
        return "llm_cost_invalid"
    if not math.isfinite(aggregate_cost) or aggregate_cost < 0:
        return "llm_cost_invalid"

    usage = result.get("llm_usage")
    if not isinstance(usage, list):
        return "llm_usage_invalid"
    try:
        aggregate_tokens = _strict_nonnegative_int(result.get("llm_total_tokens"))
    except ValueError:
        return "llm_tokens_invalid"
    if aggregate_tokens > 0 and not usage:
        return "llm_usage_missing"
    if not usage:
        return None if aggregate_cost == 0 else "llm_usage_cost_mismatch"

    event_cost_total = 0.0
    event_tokens_total = 0
    for event in usage:
        if not isinstance(event, dict):
            return "llm_usage_event_invalid"
        try:
            event_tokens = _strict_nonnegative_int(event.get("total_tokens"))
            event_cost = float(event.get("estimated_cost_rub"))
        except (TypeError, ValueError):
            return "llm_usage_event_invalid"
        if not math.isfinite(event_cost) or event_cost < 0:
            return "llm_usage_event_invalid"
        if event_tokens > 0 and (event.get("priced") is not True or event_cost <= 0):
            return "llm_pricing_unavailable"
        event_tokens_total += event_tokens
        event_cost_total += event_cost

    if aggregate_tokens != event_tokens_total:
        return "llm_usage_token_mismatch"
    if not math.isclose(
        aggregate_cost,
        event_cost_total,
        rel_tol=1e-9,
        abs_tol=1e-6,
    ):
        return "llm_usage_cost_mismatch"
    return None


def _strict_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a token count")
    try:
        parsed = int(value)
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("token count must be numeric") from exc
    if not math.isfinite(numeric) or parsed < 0 or parsed != numeric:
        raise ValueError("token count must be a non-negative integer")
    return parsed


def _report_nonnegative_int(value: Any) -> int:
    try:
        return _strict_nonnegative_int(value or 0)
    except ValueError:
        return 0


def score_case(
    case: dict[str, Any],
    http_result: dict[str, Any],
    trace: dict[str, Any] | None,
) -> dict[str, Any]:
    source_observed_diagnostic = (
        case.get("label_status") == SOURCE_OBSERVED_DIAGNOSTIC_MODE
    )
    response_text = str(http_result.get("response") or "")
    status = http_result.get("http_status")
    http_success = isinstance(status, int) and 200 <= status < 300
    trace = trace or {}
    llm_accounting_fields = (
        "llm_usage",
        "llm_prompt_tokens",
        "llm_completion_tokens",
        "llm_total_tokens",
        "llm_estimated_cost_rub",
    )
    llm_accounting_present = bool(trace) and all(
        field in trace and trace[field] is not None for field in llm_accounting_fields
    )

    observed_chunk_ids = _collect_trace_chunk_ids(trace)
    pipeline_lineage = _pipeline_lineage(trace)
    expected_chunk_ids = case.get("expected_chunk_ids") or []
    expected_cited_chunk_ids = case.get("expected_cited_chunk_ids") or []
    equivalent_chunk_ids = case.get("equivalent_chunk_ids") or {}
    expected_answer_contains = case.get("expected_answer_contains") or []
    expected_message_masked_contains = case.get("expected_message_masked_contains") or []
    forbidden_message_masked_contains = case.get("forbidden_message_masked_contains") or []
    expected_behavior = case.get("expected_behavior")
    observed_behavior = _observed_behavior(response_text, trace)
    expected_response_profile = case.get("expected_response_profile")
    observed_routing_profile = _observed_response_profile(trace)
    detected_response_profiles = sorted(
        _detect_eval_response_profiles(response_text)
    )
    forbidden_response_profiles = case.get("forbidden_response_profiles") or []
    forbidden_response_profile_hits = sorted(
        set(forbidden_response_profiles) & set(detected_response_profiles)
    )
    allowed_cited_source_types = case.get("allowed_cited_source_types") or []
    expected_escalated = case.get("expected_escalated")
    expected_escalation_reason = case.get("expected_escalation_reason")
    expected_generator_model = case.get("expected_generator_model")

    checks: dict[str, bool | None] = {}
    required_checks: dict[str, bool | None] = {}
    missing_expected_chunk_ids: list[str] = []
    missing_expected_or_equivalent_chunk_ids: list[str] = []
    if expected_chunk_ids:
        expected_chunk_set = set(expected_chunk_ids)
        missing_expected_chunk_ids = sorted(expected_chunk_set - observed_chunk_ids)
        exact_hit = (
            not missing_expected_chunk_ids
            if len(expected_chunk_ids) > 1
            else bool(expected_chunk_set & observed_chunk_ids)
        )
        missing_expected_or_equivalent_chunk_ids = _missing_expected_or_equivalent_ids(
            expected_chunk_ids,
            equivalent_chunk_ids,
            observed_chunk_ids,
        )
        equivalent_hit = not missing_expected_or_equivalent_chunk_ids
        checks["expected_chunk_hit"] = exact_hit
        checks["expected_or_equivalent_chunk_hit"] = equivalent_hit
        required_checks["expected_chunk_hit"] = (
            equivalent_hit if equivalent_chunk_ids else exact_hit
        )
    missing_expected_cited_chunk_ids: list[str] = []
    missing_expected_cited_or_equivalent_chunk_ids: list[str] = []
    cited_chunk_ids = _collect_trace_cited_chunk_ids(trace)
    cited_source_types = _cited_source_types(trace, cited_chunk_ids)
    if expected_cited_chunk_ids:
        expected_cited_set = set(expected_cited_chunk_ids)
        missing_expected_cited_chunk_ids = sorted(expected_cited_set - cited_chunk_ids)
        exact_cited_hit = not missing_expected_cited_chunk_ids
        missing_expected_cited_or_equivalent_chunk_ids = (
            _missing_expected_or_equivalent_ids(
                expected_cited_chunk_ids,
                equivalent_chunk_ids,
                cited_chunk_ids,
            )
        )
        equivalent_cited_hit = not missing_expected_cited_or_equivalent_chunk_ids
        checks["expected_cited_chunk_hit"] = exact_cited_hit
        checks["expected_cited_or_equivalent_chunk_hit"] = equivalent_cited_hit
        required_checks["expected_cited_chunk_hit"] = (
            equivalent_cited_hit if equivalent_chunk_ids else exact_cited_hit
        )
    unexpected_cited_source_types: list[str] = []
    if allowed_cited_source_types:
        unexpected_cited_source_types = sorted(
            set(cited_source_types) - set(allowed_cited_source_types)
        )
        cited_source_types_allowed = not unexpected_cited_source_types
        checks["cited_source_types_allowed"] = cited_source_types_allowed
        required_checks["cited_source_types_allowed"] = cited_source_types_allowed
    if expected_answer_contains:
        normalized_response = _normalize_answer_contains_text(response_text)
        answer_contains_match = all(
            _normalize_answer_contains_text(expected) in normalized_response
            for expected in expected_answer_contains
        )
        checks["answer_contains_match"] = answer_contains_match
        required_checks["answer_contains_match"] = answer_contains_match
    if expected_message_masked_contains:
        message_masked = str(trace.get("message_masked") or "")
        masked_contains_match = all(
            expected in message_masked for expected in expected_message_masked_contains
        )
        checks["message_masked_contains_match"] = masked_contains_match
        required_checks["message_masked_contains_match"] = masked_contains_match
    if forbidden_message_masked_contains:
        message_masked = str(trace.get("message_masked") or "")
        masked_forbidden_absent_match = all(
            forbidden not in message_masked for forbidden in forbidden_message_masked_contains
        )
        checks["message_masked_forbidden_absent_match"] = masked_forbidden_absent_match
        required_checks["message_masked_forbidden_absent_match"] = (
            masked_forbidden_absent_match
        )
    if expected_behavior:
        behavior_match = observed_behavior == expected_behavior
        checks["behavior_match"] = behavior_match
        required_checks["behavior_match"] = behavior_match
    if expected_response_profile:
        routing_profile_match = (
            observed_routing_profile == expected_response_profile
            if trace
            else None
        )
        checks["routing_response_profile_match"] = routing_profile_match
        required_checks["routing_response_profile_match"] = routing_profile_match
    if forbidden_response_profiles:
        forbidden_profiles_absent = not forbidden_response_profile_hits
        checks["forbidden_response_profiles_absent"] = forbidden_profiles_absent
        required_checks["forbidden_response_profiles_absent"] = (
            forbidden_profiles_absent
        )
    if expected_escalated is not None:
        escalation_match = (
            bool(trace.get("was_escalated")) == bool(expected_escalated)
            if trace
            else None
        )
        checks["escalation_match"] = escalation_match
        required_checks["escalation_match"] = escalation_match
    if expected_escalation_reason and trace and trace.get("was_escalated") is True:
        escalation_reason_match = (
            trace.get("escalation_reason") == expected_escalation_reason
        )
        checks["escalation_reason_match"] = escalation_reason_match
        required_checks["escalation_reason_match"] = escalation_reason_match
    if expected_generator_model:
        generator_model_match = (
            trace.get("generator_model") == expected_generator_model if trace else None
        )
        checks["generator_model_match"] = generator_model_match
        required_checks["generator_model_match"] = generator_model_match
    if expected_chunk_ids and expected_escalated is not True:
        no_false_insufficient = not _looks_like_insufficient_source(
            response_text
        )
        no_non_answer = not _looks_like_non_answer(response_text)
        checks["no_false_insufficient_source_response"] = no_false_insufficient
        checks["no_non_answer_response"] = no_non_answer
        required_checks["no_false_insufficient_source_response"] = no_false_insufficient
        required_checks["no_non_answer_response"] = no_non_answer

    passed: bool | None
    if source_observed_diagnostic:
        passed = None
    else:
        passed = http_success and all(
            value is True for value in required_checks.values()
        )
        if not required_checks:
            passed = http_success
    failure_reasons = _failure_reasons(
        http_success=http_success,
        trace_found=bool(trace),
        checks=checks,
        required_checks=required_checks,
        has_equivalent_chunks=bool(equivalent_chunk_ids),
        expected_chunk_ids=expected_chunk_ids,
        expected_cited_chunk_ids=expected_cited_chunk_ids,
        missing_expected_chunk_ids=missing_expected_chunk_ids,
        missing_expected_or_equivalent_chunk_ids=missing_expected_or_equivalent_chunk_ids,
        missing_expected_cited_chunk_ids=missing_expected_cited_chunk_ids,
        missing_expected_cited_or_equivalent_chunk_ids=(
            missing_expected_cited_or_equivalent_chunk_ids
        ),
        expected_behavior=expected_behavior,
        observed_behavior=observed_behavior,
        expected_response_profile=expected_response_profile,
        observed_routing_profile=observed_routing_profile,
        forbidden_response_profile_hits=forbidden_response_profile_hits,
        expected_escalated=expected_escalated,
        was_escalated=trace.get("was_escalated"),
        error=http_result.get("error") or trace.get("error"),
    )

    return {
        "id": case["id"],
        "ticket_id_hash": case.get("ticket_id_hash"),
        "step_id": case.get("step_id"),
        "query": case["query"],
        "tags": case.get("tags", []),
        "request_id": http_result.get("request_id"),
        "http_status": status,
        "http_success": http_success,
        "latency_ms": http_result.get("latency_ms"),
        "response": response_text,
        "error": http_result.get("error") or trace.get("error"),
        "trace_found": bool(trace),
        "expected_chunk_ids": expected_chunk_ids,
        "equivalent_chunk_ids": equivalent_chunk_ids,
        "observed_chunk_ids": sorted(observed_chunk_ids),
        "observed_chunk_ids_scope": "union_retrieved_reranked_cited_legacy",
        **pipeline_lineage,
        "expected_chunk_hit": checks.get("expected_chunk_hit"),
        "expected_or_equivalent_chunk_hit": checks.get("expected_or_equivalent_chunk_hit"),
        "missing_expected_chunk_ids": missing_expected_chunk_ids,
        "missing_expected_or_equivalent_chunk_ids": (
            missing_expected_or_equivalent_chunk_ids
        ),
        "expected_cited_chunk_ids": expected_cited_chunk_ids,
        "expected_cited_chunk_hit": checks.get("expected_cited_chunk_hit"),
        "expected_cited_or_equivalent_chunk_hit": checks.get(
            "expected_cited_or_equivalent_chunk_hit"
        ),
        "missing_expected_cited_chunk_ids": missing_expected_cited_chunk_ids,
        "missing_expected_cited_or_equivalent_chunk_ids": (
            missing_expected_cited_or_equivalent_chunk_ids
        ),
        "cited_source_ids": sorted(cited_chunk_ids),
        "cited_source_types": cited_source_types,
        "allowed_cited_source_types": allowed_cited_source_types,
        "unexpected_cited_source_types": unexpected_cited_source_types,
        "cited_source_types_allowed": checks.get("cited_source_types_allowed"),
        "expected_answer_contains": expected_answer_contains,
        "answer_contains_match": checks.get("answer_contains_match"),
        "message_masked": trace.get("message_masked"),
        "expected_message_masked_contains": expected_message_masked_contains,
        "message_masked_contains_match": checks.get("message_masked_contains_match"),
        "forbidden_message_masked_contains": forbidden_message_masked_contains,
        "message_masked_forbidden_absent_match": checks.get(
            "message_masked_forbidden_absent_match"
        ),
        "expected_behavior": expected_behavior,
        "observed_behavior": observed_behavior,
        "behavior_match": checks.get("behavior_match"),
        "expected_response_profile": expected_response_profile,
        "observed_routing_response_profile": observed_routing_profile,
        "routing_response_profile_match": checks.get(
            "routing_response_profile_match"
        ),
        "detected_response_profiles": detected_response_profiles,
        "forbidden_response_profiles": forbidden_response_profiles,
        "forbidden_response_profile_hits": forbidden_response_profile_hits,
        "forbidden_response_profiles_absent": checks.get(
            "forbidden_response_profiles_absent"
        ),
        "expected_escalated": expected_escalated,
        "was_escalated": trace.get("was_escalated"),
        "escalation_match": checks.get("escalation_match"),
        "expected_escalation_reason": expected_escalation_reason,
        "escalation_reason": trace.get("escalation_reason"),
        "escalation_reason_match": checks.get("escalation_reason_match"),
        "expected_generator_model": expected_generator_model,
        "generator_model": trace.get("generator_model"),
        "generator_model_match": checks.get("generator_model_match"),
        "no_false_insufficient_source_response": checks.get(
            "no_false_insufficient_source_response"
        ),
        "no_non_answer_response": checks.get("no_non_answer_response"),
        "cache_hit": trace.get("cache_hit"),
        "max_reranker_score": trace.get("max_reranker_score"),
        "trace_total_latency_ms": trace.get("total_latency_ms"),
        "llm_usage": trace.get("llm_usage"),
        "generate_retry_reasons": _generate_retry_reasons(trace),
        "llm_prompt_tokens": trace.get("llm_prompt_tokens"),
        "llm_completion_tokens": trace.get("llm_completion_tokens"),
        "llm_total_tokens": trace.get("llm_total_tokens"),
        "llm_estimated_cost_rub": trace.get("llm_estimated_cost_rub"),
        "llm_accounting_present": llm_accounting_present,
        "passed": passed,
        "failure_reasons": [] if passed is True else failure_reasons,
    }


def _failure_reasons(
    *,
    http_success: bool,
    trace_found: bool,
    checks: dict[str, bool | None],
    required_checks: dict[str, bool | None],
    has_equivalent_chunks: bool,
    expected_chunk_ids: list[str],
    expected_cited_chunk_ids: list[str],
    missing_expected_chunk_ids: list[str],
    missing_expected_or_equivalent_chunk_ids: list[str],
    missing_expected_cited_chunk_ids: list[str],
    missing_expected_cited_or_equivalent_chunk_ids: list[str],
    expected_behavior: object,
    observed_behavior: object,
    expected_response_profile: object,
    observed_routing_profile: object,
    forbidden_response_profile_hits: list[str],
    expected_escalated: object,
    was_escalated: object,
    error: object,
) -> list[str]:
    reasons: list[str] = []
    if not http_success:
        reasons.append("http_error")
    if http_success and not trace_found:
        reasons.append("trace_missing")
    if expected_chunk_ids and required_checks.get("expected_chunk_hit") is False:
        reasons.append(
            "expected_or_equivalent_chunk_not_observed"
            if has_equivalent_chunks
            else "expected_chunk_not_observed"
        )
    if expected_cited_chunk_ids and required_checks.get("expected_cited_chunk_hit") is False:
        if has_equivalent_chunks:
            if (
                missing_expected_or_equivalent_chunk_ids
                == missing_expected_cited_or_equivalent_chunk_ids
            ):
                reasons.append("expected_or_equivalent_chunk_not_retrieved")
            else:
                reasons.append("expected_or_equivalent_chunk_not_cited")
        elif missing_expected_chunk_ids == missing_expected_cited_chunk_ids:
            reasons.append("expected_chunk_not_retrieved")
        else:
            reasons.append("expected_chunk_not_cited")
    if required_checks.get("answer_contains_match") is False:
        reasons.append("answer_contains_mismatch")
    if required_checks.get("cited_source_types_allowed") is False:
        reasons.append("forbidden_cited_source_type")
    if required_checks.get("message_masked_contains_match") is False:
        reasons.append("message_masked_contains_mismatch")
    if required_checks.get("message_masked_forbidden_absent_match") is False:
        reasons.append("message_masked_forbidden_contains_raw_pii")
    if required_checks.get("behavior_match") is False:
        reasons.append(f"behavior_mismatch:{expected_behavior}!={observed_behavior}")
    if required_checks.get("routing_response_profile_match") is False:
        reasons.append(
            "routing_response_profile_mismatch:"
            f"{expected_response_profile}!={observed_routing_profile}"
        )
    if required_checks.get("forbidden_response_profiles_absent") is False:
        reasons.append(
            "forbidden_response_profile_detected:"
            + ",".join(forbidden_response_profile_hits)
        )
    if required_checks.get("escalation_match") is False:
        if expected_escalated is False and was_escalated is True:
            reasons.append("unexpected_escalation")
        elif expected_escalated is True and was_escalated is False:
            reasons.append("missing_escalation")
        else:
            reasons.append("escalation_mismatch")
    if required_checks.get("escalation_reason_match") is False:
        reasons.append("escalation_reason_mismatch")
    if required_checks.get("generator_model_match") is False:
        reasons.append("generator_model_mismatch")
    if required_checks.get("no_false_insufficient_source_response") is False:
        reasons.append("false_insufficient_source_response")
    if required_checks.get("no_non_answer_response") is False:
        reasons.append("non_answer_response")
    if error and not reasons:
        reasons.append("error")
    return reasons or ["quality_check_failed"]


def _looks_like_insufficient_source(response_text: str) -> bool:
    normalized = response_text.casefold().replace("ё", "е")
    return bool(FALSE_INSUFFICIENT_SOURCE_RE.search(normalized))


def _looks_like_non_answer(response_text: str) -> bool:
    normalized = response_text.casefold().replace("ё", "е")
    return bool(NON_ANSWER_RE.search(normalized))


def _normalize_expected_behavior(value: Any) -> str | None:
    if value is None or value == "":
        return None
    normalized = str(value).casefold().strip().replace("-", "_")
    aliases = {
        "offtopic": "scope_note",
        "scope": "scope_note",
        "scope-note": "scope_note",
        "clarification": "clarify",
        "escalation": "escalate",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in EXPECTED_BEHAVIORS:
        raise ValueError(
            "expected_behavior must be one of: "
            f"{', '.join(sorted(EXPECTED_BEHAVIORS))}"
        )
    return normalized


def _normalize_expected_response_profile(value: Any) -> str | None:
    if value is None or value == "":
        return None
    normalized = str(value).casefold().strip().replace("-", "_")
    if normalized not in EXPECTED_RESPONSE_PROFILES:
        raise ValueError(
            "expected_response_profile must be one of: "
            f"{', '.join(sorted(EXPECTED_RESPONSE_PROFILES))}"
        )
    return normalized


def _normalize_response_profile_list(value: Any) -> list[str]:
    normalized: set[str] = set()
    for item in _string_list(value):
        profile = _normalize_expected_response_profile(item)
        if profile:
            normalized.add(profile)
    return sorted(normalized)


def _detect_eval_response_profiles(response_text: str) -> set[str]:
    """Detect critical answer aspects independently from the production verifier."""

    normalized = " ".join(
        str(response_text or "").casefold().replace("ё", "е").split()
    )
    detected = {
        profile
        for profile, markers in EVAL_RESPONSE_PROFILE_MARKERS.items()
        if any(marker in normalized for marker in markers)
    }
    if (
        any(marker in normalized for marker in EVAL_EVENT_DATE_MARKERS)
        or EVAL_EVENT_DATE_RE.search(normalized)
    ):
        detected.add("dates")
    return detected


def _infer_expected_behavior(raw: dict[str, Any], query: str) -> str | None:
    tags = " ".join(_string_list(raw.get("tags") or []))
    identity = f"{raw.get('id') or raw.get('case_id') or ''} {tags}".casefold()
    if "topic:offtop_ne_po_rosmolodezhi" in identity:
        return "scope_note"
    if "topic:pereklyuchit_na_operatora" in identity:
        return "escalate"

    normalized_query = " ".join(query.casefold().replace("ё", "е").split())
    if normalized_query in {
        "подать заявку на участие",
        "как подать заявку",
        "хочу подать заявку",
    }:
        return "clarify"
    return None


def _observed_behavior(response_text: str, trace: dict[str, Any]) -> str:
    if trace.get("was_escalated") is True:
        return "escalate"
    normalized = response_text.casefold().replace("ё", "е")
    if _looks_like_scope_note(normalized):
        return "scope_note"
    # The final verifier may replace a generated answer with a clarification while
    # retrieval citations remain in the trace. User-visible behavior wins here.
    if _looks_like_clarification(normalized):
        return "clarify"
    return "answer"


def _observed_response_profile(trace: dict[str, Any]) -> str | None:
    analysis = _json_safe(trace.get("query_analysis"))
    if not isinstance(analysis, dict):
        return None
    value = analysis.get("response_profile")
    return str(value).strip() or None


def _looks_like_scope_note(normalized_response: str) -> bool:
    return all(marker in normalized_response for marker in SCOPE_NOTE_MARKERS)


def _looks_like_clarification(normalized_response: str) -> bool:
    if re.search(r"\bуточни(?:те)?\b", normalized_response):
        return True
    return any(
        marker in normalized_response
        for marker in CLARIFICATION_MARKERS
        if marker not in {"уточни", "уточните"}
    )


def _failure_reason_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in results:
        for reason in item.get("failure_reasons") or []:
            counter[str(reason)] += 1
    return dict(counter)


def _behavior_confusion_matrix(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    matrix: dict[str, Counter[str]] = {}
    for item in results:
        expected = str(item.get("expected_behavior") or "unscored")
        observed = str(item.get("observed_behavior") or "unknown")
        matrix.setdefault(expected, Counter())[observed] += 1
    return {
        expected: dict(sorted(observed.items()))
        for expected, observed in sorted(matrix.items())
    }


def _generate_retry_reasons(trace: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for event in trace.get("trace_events") or []:
        if not isinstance(event, dict) or event.get("node") != "generate_retry":
            continue
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        reason = str(metadata.get("reason") or "").strip()
        if reason:
            reasons.append(reason)
    return reasons


def _likely_infrastructure_failure(results: list[dict[str, Any]]) -> bool:
    if not results:
        return False
    if any(item.get("http_success") for item in results):
        return False
    return all("http_error" in (item.get("failure_reasons") or []) for item in results)


async def _load_cases(
    *,
    cases_path: Path,
    kb_seed_path: Path,
    auto_smoke_cases: bool,
    max_smoke_cases: int,
    user_prefix: str,
    allow_model_assisted_prerun: bool = False,
    allow_source_observed_diagnostic: bool = False,
) -> tuple[list[dict[str, Any]], bool, str | None, str | None]:
    raw_cases: list[dict[str, Any]] = []
    generated_smoke_cases = False
    cases_file_sha256: str | None = None
    cases_file_exists = await asyncio.to_thread(cases_path.exists)
    if cases_file_exists:
        raw_bytes = await asyncio.to_thread(cases_path.read_bytes)
        cases_file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        raw = json.loads(raw_bytes.decode("utf-8-sig"))
        if not isinstance(raw, list):
            raise ValueError("ask eval cases file must be a JSON array")
        raw_cases = raw
    elif not auto_smoke_cases:
        raise FileNotFoundError(f"ask eval cases file not found: {cases_path}")

    raw_cases_payload_sha256 = (
        holdout_cases_payload_sha256(raw_cases)
        if any(
            isinstance(item, dict) and "holdout_contract" in item
            for item in raw_cases
        )
        else None
    )
    cases = [
        _normalize_case(
            item,
            allow_model_assisted_prerun=allow_model_assisted_prerun,
            allow_source_observed_diagnostic=allow_source_observed_diagnostic,
        )
        for item in raw_cases
    ]
    if not any(
        case.get("split") == PRIVATE_HOLDOUT_SPLIT
        or case.get("label_status") == SOURCE_OBSERVED_DIAGNOSTIC_MODE
        for case in cases
    ):
        cases = _apply_user_prefix(cases, user_prefix=user_prefix)
    if not cases and auto_smoke_cases:
        records = await asyncio.to_thread(_read_json, kb_seed_path)
        if not isinstance(records, list):
            raise ValueError("KB seed must be a JSON array")
        cases = build_seed_ask_cases(
            records,
            max_cases=max_smoke_cases,
            user_prefix=user_prefix,
        )
        generated_smoke_cases = True
    return (
        cases,
        generated_smoke_cases,
        raw_cases_payload_sha256,
        cases_file_sha256,
    )


def _apply_user_prefix(cases: list[dict[str, Any]], *, user_prefix: str) -> list[dict[str, Any]]:
    if not user_prefix:
        return cases
    isolated: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        item = dict(case)
        item["user_id"] = f"{user_prefix}-{index}"
        isolated.append(item)
    return isolated


def _default_generated_user_prefix(base: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    return f"{base}-{stamp}"


def _sealed_holdout_user_id(eval_run_id: str, case_id: str) -> str:
    run_digest = hashlib.sha256(eval_run_id.encode("utf-8")).hexdigest()
    case_digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    return f"runner-holdout-{run_digest}-{case_digest}"


async def _run_case(
    *,
    client: httpx.AsyncClient,
    target: str,
    headers: dict[str, str],
    eval_run_id: str,
    case: dict[str, Any],
    semaphore: asyncio.Semaphore,
    trace_pool: asyncpg.Pool | None,
    sealed_holdout: bool = False,
    cache_bypass_secret: str = "",
) -> dict[str, Any]:
    async with semaphore:
        started_at = perf_counter()
        request_id: str | None = None
        try:
            request_user_id = (
                _sealed_holdout_user_id(eval_run_id, str(case["id"]))
                if sealed_holdout
                else case["user_id"]
            )
            request_payload: dict[str, Any] = {
                "user_id": request_user_id,
                "channel": case["channel"],
                "text": case["query"],
            }
            if case.get("forum_context"):
                request_payload["forum_context"] = case["forum_context"]
            request_headers = {
                **headers,
                "X-Eval-Run-Id": eval_run_id,
                "X-Eval-Case-Id": str(case["id"]),
            }
            if cache_bypass_secret:
                request_headers.update(
                    eval_cache_bypass.build_signed_headers(
                        cache_bypass_secret,
                        method="POST",
                        path=urlsplit(target).path or "/",
                        eval_run_id=eval_run_id,
                        eval_case_id=str(case["id"]),
                        payload_sha256=(
                            eval_cache_bypass.canonical_payload_sha256(
                                eval_cache_bypass.canonical_ask_payload(
                                    request_payload
                                )
                            )
                        ),
                    )
                )
            response = await client.post(
                target,
                headers=request_headers,
                json=request_payload,
            )
            latency_ms = int((perf_counter() - started_at) * 1000)
            payload = _safe_response_json(response)
            if isinstance(payload, dict):
                request_id = str(payload.get("request_id") or "")
                response_text = str(payload.get("response") or "")
            else:
                response_text = response.text
            http_result = {
                "http_status": response.status_code,
                "request_id": request_id,
                "response": response_text,
                "latency_ms": latency_ms,
                "error": None if response.is_success else response.text[:500],
            }
        except Exception as exc:
            http_result = {
                "http_status": None,
                "request_id": request_id,
                "response": "",
                "latency_ms": int((perf_counter() - started_at) * 1000),
                "error": f"{type(exc).__name__}: {exc}",
            }

        trace: dict[str, Any] | None = None
        case_trace_lookup_error: str | None = None
        if trace_pool and request_id:
            try:
                trace = await _fetch_trace(
                    trace_pool,
                    request_id,
                    expected_eval_run_id=eval_run_id,
                    expected_eval_case_id=str(case["id"]),
                )
            except Exception as exc:
                case_trace_lookup_error = type(exc).__name__
        result = score_case(case, http_result, trace)
        result["trace_eval_run_id"] = (
            str(trace.get("eval_run_id") or "") if trace else ""
        )
        result["trace_eval_case_id"] = (
            str(trace.get("eval_case_id") or "") if trace else ""
        )
        result["trace_binding_match"] = bool(trace) and (
            result["trace_eval_run_id"] == eval_run_id
            and result["trace_eval_case_id"] == str(case["id"])
        )
        result["trace_error"] = trace.get("error") if trace else None
        if case_trace_lookup_error:
            result["trace_lookup_error"] = case_trace_lookup_error
        return result


async def _fetch_eval_trace_cardinality(
    pool: asyncpg.Pool,
    *,
    eval_run_id: str,
    expected_case_ids: list[str],
) -> dict[str, Any]:
    rows = await pool.fetch(
        """
        SELECT eval_case_id, COUNT(*) AS trace_count
        FROM request_traces
        WHERE eval_run_id = $1
        GROUP BY eval_case_id
        ORDER BY eval_case_id NULLS FIRST
        """,
        eval_run_id,
    )
    case_counts: dict[str, int] = {}
    for row in rows:
        raw_case_id = row["eval_case_id"]
        case_id = str(raw_case_id) if raw_case_id is not None else "<null>"
        case_counts[case_id] = int(row["trace_count"])

    expected = set(expected_case_ids)
    observed = set(case_counts)
    return {
        "eval_run_id": eval_run_id,
        "expected_cases_total": len(expected_case_ids),
        "traces_total": sum(case_counts.values()),
        "case_counts": dict(sorted(case_counts.items())),
        "missing_case_ids": sorted(expected - observed),
        "duplicate_case_ids": sorted(
            case_id
            for case_id, trace_count in case_counts.items()
            if trace_count != 1
        ),
        "unknown_case_ids": sorted(observed - expected),
    }


async def _fetch_phase0_trace_cardinality(
    pool: asyncpg.Pool,
    *,
    eval_run_id: str,
    expected_request_case_pairs: list[tuple[str, str]],
) -> dict[str, Any]:
    rows = await pool.fetch(
        """
        SELECT request_id, eval_case_id, cache_hit
        FROM request_traces
        WHERE eval_run_id = $1
        ORDER BY eval_case_id NULLS FIRST, request_id
        """,
        eval_run_id,
    )
    expected_pairs: list[tuple[str, str]] = []
    invalid_expected_request_ids = 0
    for request_id, case_id in expected_request_case_pairs:
        try:
            normalized_request_id = str(UUID(request_id))
        except (TypeError, ValueError, AttributeError):
            invalid_expected_request_ids += 1
            normalized_request_id = "<invalid>"
        expected_pairs.append((normalized_request_id, case_id))

    observed_pairs: list[tuple[str, str]] = []
    case_counts: Counter[str] = Counter()
    observed_request_ids: list[str] = []
    invalid_observed_request_ids = 0
    cache_hit_true_total = 0
    cache_hit_false_total = 0
    cache_hit_unknown_total = 0
    for row in rows:
        raw_request_id = row["request_id"]
        try:
            request_id = str(UUID(str(raw_request_id)))
        except (TypeError, ValueError, AttributeError):
            invalid_observed_request_ids += 1
            request_id = "<invalid>"
        raw_case_id = row["eval_case_id"]
        case_id = str(raw_case_id) if raw_case_id is not None else "<null>"
        observed_request_ids.append(request_id)
        observed_pairs.append((request_id, case_id))
        case_counts[case_id] += 1
        cache_hit = row["cache_hit"]
        if cache_hit is True:
            cache_hit_true_total += 1
        elif cache_hit is False:
            cache_hit_false_total += 1
        else:
            cache_hit_unknown_total += 1

    expected_case_ids = [case_id for _request_id, case_id in expected_pairs]
    expected_case_set = set(expected_case_ids)
    observed_case_set = set(case_counts)
    expected_pair_set = set(expected_pairs)
    observed_pair_set = set(observed_pairs)
    duplicate_request_ids_total = len(observed_request_ids) - len(
        set(observed_request_ids)
    )
    request_case_pairs_match = bool(
        invalid_expected_request_ids == 0
        and invalid_observed_request_ids == 0
        and len(expected_pairs) == PHASE0_CASES_TOTAL
        and len(expected_pair_set) == PHASE0_CASES_TOTAL
        and len(observed_pairs) == PHASE0_CASES_TOTAL
        and len(observed_pair_set) == PHASE0_CASES_TOTAL
        and expected_pair_set == observed_pair_set
    )
    return {
        "eval_run_id": eval_run_id,
        "expected_cases_total": len(expected_pairs),
        "traces_total": len(observed_pairs),
        "case_counts": dict(sorted(case_counts.items())),
        "missing_case_ids": sorted(expected_case_set - observed_case_set),
        "duplicate_case_ids": sorted(
            case_id
            for case_id, trace_count in case_counts.items()
            if trace_count != 1
        ),
        "unknown_case_ids": sorted(observed_case_set - expected_case_set),
        "expected_request_ids_total": len(expected_pairs),
        "distinct_request_ids_total": len(set(observed_request_ids)),
        "invalid_expected_request_ids_total": invalid_expected_request_ids,
        "invalid_observed_request_ids_total": invalid_observed_request_ids,
        "duplicate_request_ids_total": duplicate_request_ids_total,
        "missing_request_case_pairs_total": len(
            expected_pair_set - observed_pair_set
        ),
        "unexpected_request_case_pairs_total": len(
            observed_pair_set - expected_pair_set
        ),
        "request_case_pairs_match": request_case_pairs_match,
        "cache_hit_true_total": cache_hit_true_total,
        "cache_hit_false_total": cache_hit_false_total,
        "cache_hit_unknown_total": cache_hit_unknown_total,
    }


async def _fetch_trace(
    pool: asyncpg.Pool,
    request_id: str,
    *,
    expected_eval_run_id: str = "",
    expected_eval_case_id: str = "",
) -> dict[str, Any] | None:
    try:
        request_uuid = UUID(request_id)
    except ValueError:
        return None

    row = await pool.fetchrow(
        """
        SELECT
            message_masked, query_analysis, cache_hit, generator_model,
            eval_run_id, eval_case_id,
            cited_sources, was_escalated,
            escalation_reason, max_reranker_score, total_latency_ms,
            retrieved_chunks, reranker_scores, trace_events, llm_usage,
            llm_prompt_tokens, llm_completion_tokens, llm_total_tokens,
            llm_estimated_cost_rub, error
        FROM request_traces
        WHERE request_id = $1
        """,
        request_uuid,
    )
    if not row:
        return None
    trace = {key: _json_safe(row[key]) for key in row.keys()}
    # Expected values are deliberately not used to populate trace fields: callers
    # compare them against the independently stored SQL values.
    _ = expected_eval_run_id, expected_eval_case_id
    return trace


def _collect_trace_chunk_ids(trace: dict[str, Any]) -> set[str]:
    chunk_ids = {str(item) for item in trace.get("cited_sources") or [] if item}
    for field in ("retrieved_chunks", "reranker_scores"):
        for item in trace.get(field) or []:
            if not isinstance(item, dict):
                continue
            chunk_id = item.get("chunk_id")
            if not chunk_id and isinstance(item.get("metadata"), dict):
                chunk_id = item["metadata"].get("chunk_id")
            if chunk_id:
                chunk_ids.add(str(chunk_id))
    return chunk_ids


def _pipeline_lineage(trace: dict[str, Any]) -> dict[str, Any]:
    analyze_event = _latest_trace_event_metadata(trace, "analyze")
    retrieve_event = _latest_trace_event_metadata(trace, "retrieve")
    rerank_event = _latest_trace_event_metadata(trace, "rerank")
    generate_event = _latest_trace_event_metadata(trace, "generate_selection")
    verify_event = _latest_trace_event_metadata(trace, "verify_decision")
    metadata_primary = _metadata_primary_diagnostics(retrieve_event)
    retrieve_available = _valid_question_provenance_event(
        retrieve_event,
        stage="retrieve",
    )
    rerank_available = _valid_question_provenance_event(
        rerank_event,
        stage="rerank",
    )
    generate_available = _valid_generate_provenance_event(generate_event)
    verify_available = _valid_verify_provenance_event(verify_event)
    stage_available = {
        "retrieve": retrieve_available,
        "rerank": rerank_available,
        "source_selection": generate_available,
        "citation": _valid_citation_trace(trace),
        "verify": verify_available,
    }
    provenance_available = (
        retrieve_available,
        rerank_available,
        generate_available,
        verify_available,
    )
    if all(stage_available.values()):
        attribution = "exact"
    elif any(provenance_available):
        attribution = "partial"
    else:
        attribution = "legacy_coarse"
    schema_version = (
        PIPELINE_LINEAGE_SCHEMA_VERSION
        if any(provenance_available)
        else "legacy-union-v1"
    )
    return {
        "lineage_schema_version": schema_version,
        "lineage_attribution": attribution,
        "lineage_stage_available": stage_available,
        "retrieved_chunk_ids": _ordered_trace_chunk_ids(trace, "retrieved_chunks"),
        "reranked_chunk_ids": _ordered_trace_chunk_ids(trace, "reranker_scores"),
        "selected_source_ids": _ordered_string_ids(
            generate_event.get("selected_source_ids") if generate_available else None
        ),
        "ordered_cited_source_ids": _ordered_string_ids(trace.get("cited_sources")),
        "verification_source_ids": _ordered_string_ids(
            verify_event.get("referenced_source_ids") if verify_available else None
        ),
        "question_lineage": _question_lineage(
            retrieve_event=retrieve_event if retrieve_available else {},
            rerank_event=rerank_event if rerank_available else {},
            generate_event=generate_event if generate_available else {},
        ),
        "generation_mode": _safe_trace_label(generate_event.get("mode"))
        if generate_available
        else None,
        "generation_contract_status": _safe_trace_label(
            generate_event.get("contract_status")
        )
        if generate_available
        else None,
        "generation_contract_reason": _safe_trace_label(
            generate_event.get("reason")
        )
        if generate_available
        else None,
        "verification_decision": _safe_trace_label(verify_event.get("decision"))
        if verify_available
        else None,
        "verification_reason": _safe_trace_label(verify_event.get("reason"))
        if verify_available
        else None,
        "rerank_confidence_source": _safe_trace_label(
            rerank_event.get("confidence_source")
        ),
        "rerank_confidence_components": _numeric_trace_mapping(
            rerank_event.get("confidence_components")
        ),
        "analyzer_execution_mode": _analyzer_execution_mode(analyze_event),
        "retrieval_method": _safe_trace_label(
            retrieve_event.get("retrieval_method")
        ),
        "metadata_lookup_attempted": _trace_bool(
            retrieve_event.get("metadata_lookup_attempted")
        ),
        "metadata_lookup_succeeded": _trace_bool(
            retrieve_event.get("metadata_lookup_succeeded")
        ),
        "metadata_lookup_result_count": _trace_nonnegative_int(
            retrieve_event.get("metadata_lookup_result_count")
        ),
        "hybrid_candidates_present": _trace_bool(
            retrieve_event.get("hybrid_candidates_present")
        ),
        "reranker_invoked": _trace_bool(rerank_event.get("reranker_invoked")),
        "reranker_raw_max": _finite_trace_number(
            rerank_event.get("raw_reranker_max")
        ),
        "reranker_score_origin": _safe_trace_label(
            rerank_event.get("score_origin")
        ),
        "reranker_synthetic_score_applied": _trace_bool(
            rerank_event.get("synthetic_score_applied")
        ),
        "reranker_floor_applied": _trace_bool(
            rerank_event.get("floor_applied")
        ),
        "reranker_synthetic_high_score_applied": _trace_bool(
            rerank_event.get("synthetic_high_score_applied")
        ),
        "generator_path": _safe_trace_label(generate_event.get("generator_path")),
        "source_chunk_applied": _trace_bool(
            generate_event.get("source_chunk_applied")
        ),
        **metadata_primary,
        "pipeline_node_sequence": _trace_node_sequence(trace),
    }


def _analyzer_execution_mode(metadata: Mapping[str, Any]) -> str | None:
    if metadata.get("mode") == "deterministic":
        return "deterministic"
    if metadata.get("mode") == "fallback" or metadata.get("fallback") is True:
        return "fallback"
    if str(metadata.get("model") or "").strip():
        return "llm"
    return None


def _metadata_primary_diagnostics(
    retrieve_event: Mapping[str, Any],
) -> dict[str, bool | int]:
    attempts_total = 0
    successes_total = 0
    rows = retrieve_event.get("question_provenance")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            attempts = row.get("attempts")
            if not isinstance(attempts, list) or not attempts:
                continue
            first = attempts[0]
            if not isinstance(first, dict):
                continue
            if (
                first.get("retrieval_method") != "metadata"
                or first.get("metadata_lookup_attempted") is not True
            ):
                continue
            attempts_total += 1
            successes_total += int(first.get("metadata_lookup_succeeded") is True)
    return {
        "metadata_primary_attempted": attempts_total > 0,
        "metadata_primary_succeeded": successes_total > 0,
        "metadata_primary_all_succeeded": (
            attempts_total > 0 and successes_total == attempts_total
        ),
        "metadata_primary_attempts_total": attempts_total,
        "metadata_primary_successes_total": successes_total,
    }


def _valid_question_provenance_event(
    metadata: dict[str, Any],
    *,
    stage: str,
) -> bool:
    rows = metadata.get("question_provenance")
    if not isinstance(rows, list):
        return False
    if not rows:
        return metadata.get("schema_version") == PIPELINE_LINEAGE_SCHEMA_VERSION
    for row in rows:
        if not isinstance(row, dict):
            return False
        if row.get("schema_version") != PIPELINE_LINEAGE_SCHEMA_VERSION:
            return False
        question_id = str(row.get("question_id") or "")
        if not (question_id == "shared" or re.fullmatch(r"q[1-9][0-9]*", question_id)):
            return False
        if stage == "retrieve":
            if not _is_ordered_id_array(row.get("retrieved_chunk_ids")):
                return False
        elif stage == "rerank":
            output_chunks = row.get("output_chunks")
            if not isinstance(output_chunks, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("chunk_id"), str)
                or not item["chunk_id"].strip()
                for item in output_chunks
            ):
                return False
        else:
            return False
    return True


def _valid_generate_provenance_event(metadata: dict[str, Any]) -> bool:
    if metadata.get("schema_version") != PIPELINE_LINEAGE_SCHEMA_VERSION:
        return False
    if not _is_ordered_id_array(metadata.get("selected_source_ids")):
        return False
    return _safe_trace_label(metadata.get("contract_status")) in {
        "passed",
        "partial",
        "failed",
    }


def _valid_verify_provenance_event(metadata: dict[str, Any]) -> bool:
    if metadata.get("schema_version") != PIPELINE_LINEAGE_SCHEMA_VERSION:
        return False
    if not _is_ordered_id_array(metadata.get("referenced_source_ids")):
        return False
    return _safe_trace_label(metadata.get("decision")) in {
        "pass",
        "partial",
        "escalate",
        "reject",
    }


def _valid_citation_trace(trace: dict[str, Any]) -> bool:
    return "cited_sources" in trace and _is_ordered_id_array(trace.get("cited_sources"))


def _is_ordered_id_array(value: object) -> bool:
    return isinstance(value, (list, tuple)) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def _question_lineage(
    *,
    retrieve_event: dict[str, Any],
    rerank_event: dict[str, Any],
    generate_event: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}

    def ensure(question_id: str) -> dict[str, Any] | None:
        if not (question_id == "shared" or re.fullmatch(r"q[1-9][0-9]*", question_id)):
            return None
        return rows.setdefault(
            question_id,
            {
                "question_id": question_id,
                "retrieved_chunk_ids": [],
                "reranked_chunk_ids": [],
                "candidate_overlap_source_ids": [],
                "selection_binding_scope": None,
            },
        )

    for raw in retrieve_event.get("question_provenance") or []:
        if not isinstance(raw, dict):
            continue
        row = ensure(str(raw.get("question_id") or ""))
        if row is not None:
            row["retrieved_chunk_ids"] = _ordered_string_ids(
                raw.get("retrieved_chunk_ids")
            )
    for raw in rerank_event.get("question_provenance") or []:
        if not isinstance(raw, dict):
            continue
        row = ensure(str(raw.get("question_id") or ""))
        if row is None:
            continue
        output_ids: list[str] = []
        for item in raw.get("output_chunks") or []:
            if isinstance(item, dict) and item.get("chunk_id"):
                output_ids.append(str(item["chunk_id"]))
        row["reranked_chunk_ids"] = _ordered_string_ids(output_ids)
    for raw in generate_event.get("question_source_overlaps") or []:
        if not isinstance(raw, dict):
            continue
        row = ensure(str(raw.get("question_id") or ""))
        if (
            row is not None
            and raw.get("binding_scope")
            == "candidate_overlap_coarse_unattributed"
        ):
            row["candidate_overlap_source_ids"] = _ordered_string_ids(
                raw.get("candidate_overlap_source_ids")
            )
            row["selection_binding_scope"] = str(raw["binding_scope"])
    missing = set(
        _ordered_string_ids(generate_event.get("candidate_uncovered_question_ids"))
    )
    for question_id, row in rows.items():
        row["source_missing"] = question_id in missing
    return [rows[key] for key in sorted(rows, key=_question_id_sort_key)]


def _question_id_sort_key(value: str) -> tuple[int, int]:
    if value == "shared":
        return (1, 0)
    return (0, int(value[1:]))


def _latest_trace_event_metadata(trace: dict[str, Any], node: str) -> dict[str, Any]:
    for event in reversed(trace.get("trace_events") or []):
        if not isinstance(event, dict) or event.get("node") != node:
            continue
        metadata = event.get("metadata")
        return metadata if isinstance(metadata, dict) else {}
    return {}


def _trace_node_sequence(trace: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for event in trace.get("trace_events") or []:
        if not isinstance(event, dict):
            continue
        node = _safe_trace_label(event.get("node"))
        if node:
            result.append(node)
    return result


def _ordered_trace_chunk_ids(trace: dict[str, Any], field: str) -> list[str]:
    values: list[str] = []
    for item in trace.get(field) or []:
        if not isinstance(item, dict):
            continue
        chunk_id = item.get("chunk_id")
        if not chunk_id and isinstance(item.get("metadata"), dict):
            chunk_id = item["metadata"].get("chunk_id")
        if chunk_id:
            values.append(str(chunk_id))
    return _ordered_string_ids(values)


def _ordered_string_ids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _safe_trace_label(value: object) -> str | None:
    normalized = str(value or "").strip().casefold()
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", normalized):
        return normalized
    return None


def _trace_bool(value: object) -> bool | None:
    return value if type(value) is bool else None


def _trace_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _finite_trace_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _numeric_trace_mapping(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, item in value.items():
        label = _safe_trace_label(key)
        if not label or isinstance(item, bool):
            continue
        try:
            number = float(item)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number):
            result[label] = number
    return result


def _collect_trace_cited_chunk_ids(trace: dict[str, Any]) -> set[str]:
    return {str(item) for item in trace.get("cited_sources") or [] if item}


def _cited_source_types(trace: dict[str, Any], cited_chunk_ids: set[str]) -> list[str]:
    if not cited_chunk_ids:
        return []
    metadata_by_id = _trace_metadata_by_chunk_id(trace)
    source_types = {
        _source_type_for_chunk(chunk_id, metadata_by_id.get(chunk_id))
        for chunk_id in cited_chunk_ids
    }
    return sorted(source_type for source_type in source_types if source_type)


def _trace_metadata_by_chunk_id(trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metadata_by_id: dict[str, dict[str, Any]] = {}
    for field in ("retrieved_chunks", "reranker_scores"):
        for item in trace.get(field) or []:
            if not isinstance(item, dict):
                continue
            chunk_id = item.get("chunk_id")
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if not chunk_id and metadata:
                chunk_id = metadata.get("chunk_id")
            if chunk_id:
                metadata_by_id[str(chunk_id)] = metadata
    return metadata_by_id


def _source_type_for_chunk(chunk_id: str, metadata: dict[str, Any] | None) -> str:
    metadata = metadata or {}
    source_type = str(metadata.get("source_type") or "").strip().casefold()
    if source_type:
        return source_type
    if chunk_id.startswith("ticket_answer_bank_"):
        return "ticket_answer_bank"
    if chunk_id.startswith("yonote_api_"):
        return "yonote"
    if chunk_id.startswith("xlsx_"):
        return "xlsx"
    if chunk_id.startswith("docx_"):
        return "docx"
    return "unknown"


def _auth_headers(api_key_env: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if not api_key_env:
        return headers
    token = (os.getenv(api_key_env) or "").strip()
    if not token and api_key_env == "API_AUTH_TOKEN":
        token = get_settings().api_auth_token.strip()
    if token:
        headers["X-API-Key"] = token
    return headers


def _requires_signed_cache_bypass(target: str) -> bool:
    hostname = (urlsplit(target).hostname or "").strip().casefold()
    return hostname not in {"localhost", "127.0.0.1", "::1"}


def _trace_dsn_candidates(trace_dsn: str | None = None) -> list[str]:
    primary = (
        (trace_dsn or "").strip()
        or (os.getenv("ASK_EVAL_POSTGRES_DSN") or "").strip()
        or get_settings().postgres_dsn
    )
    candidates = [primary]
    fallback = _docker_postgres_host_to_localhost(primary)
    if fallback and fallback not in candidates:
        candidates.append(fallback)
    return candidates


def _docker_postgres_host_to_localhost(dsn: str) -> str | None:
    try:
        parsed = urlsplit(dsn)
    except ValueError:
        return None
    if parsed.hostname != "postgres":
        return None
    replaced = dsn.replace("@postgres:", "@localhost:", 1)
    replaced = replaced.replace("@postgres/", "@localhost/", 1)
    replaced = replaced.replace("//postgres:", "//localhost:", 1)
    replaced = replaced.replace("//postgres/", "//localhost/", 1)
    return replaced if replaced != dsn else None


def _safe_response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _has_holdout_marker(value: Any) -> bool:
    return HOLDOUT_MARKER_RE.search(str(value or "").strip().casefold()) is not None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _equivalent_chunk_id_map(value: Any, expected_chunk_ids: list[str]) -> dict[str, list[str]]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        result: dict[str, list[str]] = {}
        for chunk_id, equivalents in value.items():
            normalized = _string_list(equivalents)
            if normalized:
                result[str(chunk_id)] = normalized
        return result

    equivalents = _string_list(value)
    if not equivalents:
        return {}
    return {chunk_id: equivalents for chunk_id in expected_chunk_ids}


def _normalize_answer_contains_text(value: Any) -> str:
    normalized = (
        str(value or "")
        .casefold()
        .replace("ё", "е")
        .translate(str.maketrans({"–": "-", "—": "-", "−": "-"}))
    )
    normalized = DATE_SEPARATOR_SPACING_RE.sub(r"\1", normalized)
    return " ".join(normalized.split())


def _missing_expected_or_equivalent_ids(
    expected_chunk_ids: list[str],
    equivalent_chunk_ids: dict[str, list[str]],
    observed_chunk_ids: set[str],
) -> list[str]:
    missing: list[str] = []
    for expected_id in expected_chunk_ids:
        accepted_ids = {expected_id, *equivalent_chunk_ids.get(expected_id, [])}
        if not accepted_ids & observed_chunk_ids:
            missing.append(expected_id)
    return missing


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    _write_bytes_exclusive(path, encoded)


async def _write_phase0_execution_rejection(
    *,
    output_path: Path,
    eval_run_id: str,
    run_started_at: datetime,
    target: str,
    cases_path: Path,
    cases_file_sha256: str,
    phase0_contract: Mapping[str, Any],
    results: list[dict[str, Any]],
    trace_lookup_error: str | None,
    trace_cardinality: Mapping[str, Any] | None,
    trace_cardinality_error: str | None,
    reservation: LiveEvalCostReservation | None,
    stage: str,
    error: Exception,
) -> Path:
    try:
        metrics = summarize_results(
            results,
            target=target,
            cases_path=cases_path,
            generated_smoke_cases=False,
            trace_lookup_error=trace_lookup_error,
        )
    except Exception as summary_error:
        metrics = {
            "target": target,
            "cases_file": str(cases_path),
            "cases_total": len(results),
            "results": results,
            "trace_lookup_error": trace_lookup_error,
            "rejection_summary_error": type(summary_error).__name__,
        }
    metrics.update(
        {
            "run_started_at": run_started_at.isoformat(),
            "run_completed_at": datetime.now(UTC).isoformat(),
            "eval_run_id": eval_run_id,
            "cases_file_sha256": cases_file_sha256,
            "report_classification": {
                "evaluation_classification": SOURCE_OBSERVED_DIAGNOSTIC_MODE,
                "provisional": True,
                "calibration_only": True,
                "independent_evaluation": False,
                "previously_exposed": True,
                "product_verdict_eligible": False,
                "human_product_verdict": False,
            },
            "trace_cardinality": trace_cardinality,
            "cost_control": {
                "strict_live": True,
                "high_cost_approval_id": PHASE0_APPROVAL_ID,
                "pricing_complete": False,
                "reservation": _safe_cost_reservation_report(
                    reservation,
                    cases_file_sha256=cases_file_sha256,
                ),
            },
            "phase0_run": {
                "status": "execution_rejected",
                "completed": False,
                "expected_cases_total": PHASE0_CASES_TOTAL,
                "executed_cases_total": len(results),
                "cases_file_sha256": phase0_contract["cases_file_sha256"],
                "manifest_file_sha256": phase0_contract[
                    "manifest_file_sha256"
                ],
                "manifest_binding_sha256": phase0_contract[
                    "manifest_binding_sha256"
                ],
                "ordered_selection_sha256": phase0_contract[
                    "ordered_selection_sha256"
                ],
                "runtime_git_sha": phase0_contract["runtime_git_sha"],
                "transport_mode": phase0_contract["transport_mode"],
                "builder_snapshot": phase0_contract["builder_snapshot"],
                "approval_id": PHASE0_APPROVAL_ID,
                "cost_scope": PHASE0_COST_SCOPE,
                "integrity_failures": [f"{stage}_failed"],
                "failure_stage": stage,
                "failure_error": type(error).__name__,
                "trace_cardinality_error": trace_cardinality_error,
                "selective_reruns_forbidden": True,
            },
        }
    )
    rejection_path = output_path.with_name(
        f"{output_path.stem}.{eval_run_id}.execution-rejected.json"
    )
    metrics["phase0_run"]["rejection_evidence"] = rejection_path.name
    await asyncio.to_thread(_write_json_exclusive, rejection_path, metrics)
    return rejection_path


async def _finalize_phase0_report(
    *,
    output_path: Path,
    metrics: dict[str, Any],
    eval_run_id: str,
) -> None:
    try:
        await asyncio.to_thread(_write_json_exclusive, output_path, metrics)
    except Exception as exc:
        phase0_run = metrics.get("phase0_run")
        if not isinstance(phase0_run, dict):
            phase0_run = {}
            metrics["phase0_run"] = phase0_run
        phase0_run["status"] = "finalization_failed"
        phase0_run["completed"] = False
        phase0_run["integrity_failures"] = ["exclusive_finalization_failed"]
        phase0_run["finalization_error"] = type(exc).__name__
        rejection_path = output_path.with_name(
            f"{output_path.stem}.{eval_run_id}.finalization-rejected.json"
        )
        phase0_run["rejection_evidence"] = rejection_path.name
        try:
            await asyncio.to_thread(
                _write_json_exclusive,
                rejection_path,
                metrics,
            )
        except Exception as rejection_exc:
            raise RuntimeError(
                "Phase 0 canonical report and rejection evidence could not "
                "be finalized exclusively"
            ) from rejection_exc
        raise RuntimeError(
            "Phase 0 canonical report could not be finalized; private "
            "rejection evidence was written"
        ) from exc


def _write_markdown(path: Path, metrics: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_markdown_text(metrics), encoding="utf-8")


def _write_markdown_exclusive(path: Path, metrics: dict[str, Any]) -> None:
    _write_bytes_exclusive(path, _markdown_text(metrics).encode("utf-8"))


def _markdown_text(metrics: dict[str, Any]) -> str:
    lines = [
        "# Ask Eval Report",
        "",
    ]
    report_classification = metrics.get("report_classification") or {}
    if report_classification.get("calibration_only") is True:
        lines.extend(
            [
                "> [!WARNING]",
                "> CALIBRATION REPLAY OF A PREVIOUSLY EXPOSED HOLDOUT.",
                "> This report is not an independent evaluation.",
                "> It must not be reported as product conversion or a human "
                "product verdict.",
                "",
            ]
        )
    elif report_classification.get("provisional") is True:
        lines.extend(
            [
                "> [!WARNING]",
                "> PROVISIONAL MODEL-ASSISTED PRE-RUN DIAGNOSTIC.",
                "> The pre-run labels were produced with model assistance, "
                "not human review.",
                "> This report must not be reported as product conversion or "
                "a human product verdict.",
                "",
            ]
        )
    lines.extend(
        [
        f"- Generated: `{metrics.get('generated_at')}`",
        f"- Target: `{metrics.get('target')}`",
        f"- Cases: `{metrics.get('cases_total')}`",
        f"- Pass rate: `{_format_rate(metrics.get('pass_rate'))}`",
        f"- HTTP success rate: `{_format_rate(metrics.get('http_success_rate'))}`",
        f"- Expected chunk hit rate: `{_format_rate(metrics.get('expected_chunk_hit_rate'))}`",
        "- Expected or equivalent chunk hit rate: "
        f"`{_format_rate(metrics.get('expected_or_equivalent_chunk_hit_rate'))}`",
        "- Expected cited chunk hit rate: "
        f"`{_format_rate(metrics.get('expected_cited_chunk_hit_rate'))}`",
        "- Expected cited or equivalent chunk hit rate: "
        f"`{_format_rate(metrics.get('expected_cited_or_equivalent_chunk_hit_rate'))}`",
        f"- Escalation rate: `{_format_rate(metrics.get('escalation_rate'))}`",
        f"- Behavior match rate: `{_format_rate(metrics.get('behavior_match_rate'))}`",
        "- Routing response profile match rate: "
        f"`{_format_rate(metrics.get('routing_response_profile_match_rate'))}`",
        "- Forbidden answer profile absence rate: "
        f"`{_format_rate(metrics.get('forbidden_response_profile_absence_rate'))}`",
        "- Cited source type policy rate: "
        f"`{_format_rate(metrics.get('cited_source_type_policy_rate'))}`",
        f"- Cache hit rate: `{_format_rate(metrics.get('cache_hit_rate'))}`",
        f"- Source chunk rate: `{_format_rate(metrics.get('source_chunk_rate'))}`",
        "- Low-confidence chunk hits: "
        f"`{metrics.get('low_confidence_expected_chunk_hits')}` "
        f"(`{_format_rate(metrics.get('low_confidence_expected_chunk_hit_rate'))}`)",
        f"- Likely infrastructure failure: `{metrics.get('likely_infrastructure_failure')}`",
        f"- LLM cost, RUB: `{metrics.get('llm_estimated_cost_rub')}`",
        f"- LLM budget, RUB: `{metrics.get('llm_budget_rub')}`",
        f"- LLM budget exceeded: `{metrics.get('llm_budget_exceeded')}`",
        "",
        "## Latency",
        "",
        "| Metric | HTTP ms | Trace ms |",
        "|---|---:|---:|",
        ]
    )
    failure_counts = metrics.get("failure_reason_counts") or {}
    if failure_counts:
        lines.extend(["", "## Failure Reasons", ""])
        for reason, count in sorted(failure_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{reason}`: `{count}`")

    latency = metrics.get("latency_ms") or {}
    trace_latency = metrics.get("trace_total_latency_ms") or {}
    for key in ("avg", "p50", "p95", "max"):
        lines.append(f"| {key} | {latency.get(key)} | {trace_latency.get(key)} |")

    scores = metrics.get("reranker_score") or {}
    lines.extend(
        [
            "",
            "## Reranker Score",
            "",
            "| Metric | Score |",
            "|---|---:|",
        ]
    )
    for key in ("avg", "p50", "p95", "max"):
        lines.append(f"| {key} | {scores.get(key)} |")

    failed = [item for item in metrics.get("results", []) if not item.get("passed")]
    if failed:
        lines.extend(["", "## Failed Cases", ""])
        for item in failed[:20]:
            reasons = ", ".join(item.get("failure_reasons") or [])
            reason = item.get("error") or item.get("escalation_reason") or reasons
            reason = reason or "quality check failed"
            source_types = ",".join(item.get("cited_source_types") or [])
            source_note = f" sources={source_types}" if source_types else ""
            lines.append(
                f"- `{item.get('id')}` status={item.get('http_status')}"
                f"{source_note} reason={reason}"
            )

    return "\n".join(lines) + "\n"


def _write_bytes_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        # A partial exclusive artifact remains fail-closed and is never overwritten.
        raise


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _empty_metrics(target: str, cases_path: Path, auto_smoke_cases: bool) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "target": target,
        "cases_path": str(cases_path),
        "generated_smoke_cases": auto_smoke_cases,
        "cases_total": 0,
        "cases_passed": 0,
        "pass_rate": None,
        "message": "ask eval case set is empty",
        "results": [],
    }


def _bool_rate(items: list[dict[str, Any]], key: str) -> float | None:
    scored = [item for item in items if item.get(key) is not None]
    if not scored:
        return None
    return sum(1 for item in scored if item.get(key) is True) / len(scored)


def _value_rate(items: list[dict[str, Any]], key: str, expected: Any) -> float | None:
    if not items:
        return None
    return sum(1 for item in items if item.get(key) == expected) / len(items)


def _average(values: list[int]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _numeric_values(items: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for item in items:
        value = item.get(key)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric):
            values.append(numeric)
    return values


def _number_summary(values: list[float]) -> dict[str, float | None]:
    return {
        "avg": _rounded_average(values),
        "p50": _rounded_percentile(values, 50),
        "p95": _rounded_percentile(values, 95),
        "max": round(max(values), 6) if values else None,
    }


def _rounded_average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _rounded_percentile(values: list[float], percentile: int) -> float | None:
    value = _percentile_number(values, percentile)
    return round(value, 6) if value is not None else None


def _percentile(values: list[int], percentile: int) -> int | None:
    value = _percentile_number(values, percentile)
    return int(value) if value is not None else None


def _percentile_number(values: list[int] | list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile / 100) - 1))
    return ordered[index]


def _format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _quality_gate_failures(
    metrics: dict[str, Any],
    *,
    fail_on_any_case: bool,
    require_complete_traces: bool,
) -> list[str]:
    failures: list[str] = []
    if fail_on_any_case and metrics.get("pass_rate") != 1.0:
        failures.append("case_pass_rate_below_100_percent")
    if (
        require_complete_traces
        and metrics.get("trace_coverage_rate") != 1.0
    ):
        failures.append("trace_coverage_below_100_percent")
    if metrics.get("llm_pricing_stopped") is True:
        failures.append("llm_cost_accounting_incomplete")
    if metrics.get("llm_budget_stopped") is True:
        failures.append("llm_budget_stopped")
    return failures


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("[", "{")):
            try:
                return _json_safe(json.loads(stripped))
            except json.JSONDecodeError:
                return value
        return value
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="data/ask_eval_set.json")
    parser.add_argument("--output", default="reports/ask_eval.json")
    parser.add_argument("--markdown", default="")
    parser.add_argument("--no-markdown", action="store_true")
    parser.add_argument("--target", default="http://localhost:8001/ask")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--api-key-env", default="API_AUTH_TOKEN")
    parser.add_argument("--no-db-traces", action="store_true")
    parser.add_argument("--trace-dsn", default="")
    parser.add_argument("--auto-smoke-cases", action="store_true")
    parser.add_argument("--max-smoke-cases", type=int, default=50)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--max-llm-cost-rub", type=float, default=None)
    parser.add_argument(
        "--llm-cost-repricing-contract",
        default="",
        help=(
            "Exact fail-closed eval-only cost projection contract. It does not "
            "modify target runtime telemetry or routing."
        ),
    )
    parser.add_argument(
        "--pilot50-candidate-contract",
        choices=(PILOT50_CANDIDATE_CONTRACT_ID,),
        default="",
        help=(
            "Exact fail-closed Pilot50 v2 candidate contract. It fixes the "
            "target, 50-case payload, signed cache bypass, trace coverage, "
            "runtime identity, target-reported pricing, and 30 RUB cap."
        ),
    )
    parser.add_argument(
        "--large-run-threshold",
        type=int,
        default=20,
        help="Require an explicit LLM budget above this number of live cases.",
    )
    parser.add_argument(
        "--allow-unbounded-llm-cost",
        action="store_true",
        help=(
            "Test-only compatibility flag; live evals reject unbounded cost."
        ),
    )
    parser.add_argument(
        "--high-cost-approval-id",
        default="",
        help=(
            "Non-secret one-time owner approval reference required for private "
            "full runs or a live budget above 100 RUB."
        ),
    )
    parser.add_argument("--user-prefix", default="")
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
    parser.add_argument("--bypass-cache", action="store_true")
    private_run_group = parser.add_mutually_exclusive_group()
    private_run_group.add_argument(
        "--sealed-holdout",
        action="store_true",
        help="Enable the one-shot sealed 80-case private holdout protocol.",
    )
    private_run_group.add_argument(
        "--calibration-replay",
        action="store_true",
        help=(
            "Replay an already exposed exact holdout as calibration-only "
            "evidence without touching its sealed ledger."
        ),
    )
    parser.add_argument(
        "--allow-model-assisted-prerun",
        action="store_true",
        help=(
            "Allow a sealed holdout or calibration replay whose pre-run "
            "labels were model-assisted. The report remains provisional and "
            "cannot be used as a human product verdict."
        ),
    )
    parser.add_argument(
        "--allow-source-observed-diagnostic",
        action="store_true",
        help=(
            "Allow private unscored source-observed calibration cases. "
            "This mode never infers expected behavior or qrels."
        ),
    )
    parser.add_argument(
        "--phase0-manifest",
        default="",
        help=(
            "Private approved Phase 0 manifest. Required with approval "
            f"{PHASE0_APPROVAL_ID}; binds the exact 30 cases before /ask."
        ),
    )
    parser.add_argument(
        "--phase0-server-local",
        action="store_true",
        help=(
            "Run approved Phase 0 entirely inside the isolated server Docker "
            "network. Requires the explicit server owner-exception env flag."
        ),
    )
    parser.add_argument(
        "--phase0-builder-source",
        default="",
        help=(
            "Read-only source snapshot of the approval-bound telemetry commit; "
            "required for server-local Phase 0 provenance validation."
        ),
    )
    parser.add_argument(
        "--expected-holdout-freeze-sha256",
        default="",
        help="Externally supplied frozen holdout contract SHA-256.",
    )
    parser.add_argument(
        "--expected-cases-payload-sha256",
        default="",
        help="Externally supplied canonical raw holdout cases SHA-256.",
    )
    parser.add_argument(
        "--expected-cases-file-sha256",
        default="",
        help="Externally supplied exact private cases file SHA-256.",
    )
    parser.add_argument(
        "--holdout-ledger-dir",
        default="",
        help=(
            "Canonical persistent private directory for one-shot holdout "
            "receipts; it must be preserved after the run."
        ),
    )
    parser.add_argument(
        "--expected-runtime-git-sha",
        default="",
        help=(
            "Exact target runtime SHA required for an exposed holdout "
            "calibration replay."
        ),
    )
    parser.add_argument(
        "--calibration-replay-ledger-dir",
        default="",
        help=(
            "Canonical persistent private calibration replay receipt "
            "directory; separate from the sealed holdout ledger."
        ),
    )
    parser.add_argument(
        "--fail-on-any-case",
        action="store_true",
        help="Exit with status 1 unless every evaluated case passes.",
    )
    parser.add_argument(
        "--require-complete-traces",
        action="store_true",
        help="Exit with status 1 unless trace coverage is exactly 100%%.",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    markdown_path = None
    if not args.no_markdown:
        markdown_path = Path(args.markdown) if args.markdown else output_path.with_suffix(".md")

    metrics = asyncio.run(
        run_eval(
            cases_path=Path(args.cases),
            output_path=output_path,
            target=args.target,
            concurrency=args.concurrency,
            request_timeout=args.timeout,
            api_key_env=args.api_key_env,
            trace_lookup=not args.no_db_traces,
            trace_dsn=args.trace_dsn or None,
            kb_seed_path=Path(args.kb_seed),
            auto_smoke_cases=args.auto_smoke_cases,
            max_smoke_cases=args.max_smoke_cases,
            markdown_path=markdown_path,
            bypass_cache=args.bypass_cache,
            generated_user_prefix=args.user_prefix or None,
            max_cases=args.max_cases,
            max_llm_cost_rub=args.max_llm_cost_rub,
            llm_cost_repricing_contract=(
                args.llm_cost_repricing_contract or None
            ),
            pilot50_candidate_contract=(
                args.pilot50_candidate_contract or None
            ),
            require_complete_traces=args.require_complete_traces,
            require_budget_for_large_runs=not args.allow_unbounded_llm_cost,
            large_run_threshold=args.large_run_threshold,
            sealed_holdout=args.sealed_holdout,
            expected_holdout_freeze_sha256=(
                args.expected_holdout_freeze_sha256 or None
            ),
            expected_cases_payload_sha256=(
                args.expected_cases_payload_sha256 or None
            ),
            expected_cases_file_sha256=(
                args.expected_cases_file_sha256 or None
            ),
            holdout_ledger_dir=(
                Path(args.holdout_ledger_dir)
                if args.holdout_ledger_dir
                else None
            ),
            allow_model_assisted_prerun=args.allow_model_assisted_prerun,
            calibration_replay=args.calibration_replay,
            expected_runtime_git_sha=(
                args.expected_runtime_git_sha or None
            ),
            calibration_replay_ledger_dir=(
                Path(args.calibration_replay_ledger_dir)
                if args.calibration_replay_ledger_dir
                else None
            ),
            high_cost_approval_id=args.high_cost_approval_id or None,
            allow_source_observed_diagnostic=(
                args.allow_source_observed_diagnostic
            ),
            phase0_manifest_path=(
                Path(args.phase0_manifest) if args.phase0_manifest else None
            ),
            phase0_server_local=args.phase0_server_local,
            phase0_builder_source=(
                Path(args.phase0_builder_source)
                if args.phase0_builder_source
                else None
            ),
        )
    )
    quality_gate_failures = _quality_gate_failures(
        metrics,
        fail_on_any_case=args.fail_on_any_case,
        require_complete_traces=args.require_complete_traces,
    )
    stdout_metrics = {
        key: value for key, value in metrics.items() if key != "results"
    }
    stdout_metrics["quality_gate_failures"] = quality_gate_failures
    print(
        json.dumps(
            stdout_metrics,
            ensure_ascii=False,
            indent=2,
        )
    )
    if (
        metrics.get("llm_budget_exceeded") is True
        or metrics.get("llm_budget_stopped") is True
        or metrics.get("llm_pricing_stopped") is True
    ):
        raise SystemExit(2)
    if quality_gate_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
