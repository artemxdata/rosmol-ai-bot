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
from datetime import UTC, datetime
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
from src.config import get_settings
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
HUMAN_REVIEW_MODE = "human_reviewed"
MODEL_ASSISTED_PRERUN_MODE = "model_assisted_prerun"
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
) -> dict[str, Any]:
    label_status = str(raw.get("label_status") or "").strip().casefold()
    review_flag = raw.get("requires_human_review")
    review_required = review_flag is True or (
        isinstance(review_flag, str)
        and review_flag.strip().casefold() in {"1", "true", "yes"}
    )
    model_assisted_prerun = label_status == MODEL_ASSISTED_PRERUN_MODE
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
    query = raw.get("query") or raw.get("question") or raw.get("text")
    if not query:
        raise ValueError("ask eval case must contain query, question, or text")
    privacy_class = str(raw.get("privacy_class") or "standard").strip().casefold()
    split = str(raw.get("split") or "").strip().casefold()
    tags = _string_list(raw.get("tags") or [])
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
    if split:
        normalized["split"] = split
    if holdout_contract is not None:
        normalized["holdout_contract"] = holdout_contract
    return normalized


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
) -> dict[str, Any]:
    eval_run_id = f"ask-eval-{uuid4()}"
    if sealed_holdout and calibration_replay:
        raise ValueError("sealed_holdout and calibration_replay are mutually exclusive")
    private_contract_run = sealed_holdout or calibration_replay
    effective_api_key_env = (
        "API_AUTH_TOKEN"
        if private_contract_run and api_key_env is None
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
    _guard_eval_artifact_aliases(
        cases_path=cases_path,
        output_path=output_path,
        markdown_path=markdown_path,
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
        or expected_cases_file_sha256 is not None
        or holdout_ledger_dir is not None
        or expected_runtime_git_sha is not None
        or calibration_replay_ledger_dir is not None
    ):
        raise ValueError(
            "private contract identity and ledger options require sealed_holdout "
            "or calibration_replay mode"
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
    )
    holdout_ledger: Path | None = None
    holdout_receipt_key: str | None = None
    holdout_receipt_path: Path | None = None
    holdout_completed_receipt_path: Path | None = None
    source_holdout_contract: dict[str, Any] | None = None
    evaluation_runtime_git_sha: str | None = None
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
    original_cases_total = len(cases)
    if max_cases is not None:
        if holdout_contract is not None:
            raise ValueError(f"--max-cases is forbidden for {private_run_label}")
        if max_cases < 1:
            raise ValueError("--max-cases must be greater than zero")
        cases = cases[:max_cases]
    try:
        _guard_large_live_run_budget(
            cases=cases,
            target=target,
            transport=transport,
            max_llm_cost_rub=max_llm_cost_rub,
            require_budget=require_budget_for_large_runs,
            large_run_threshold=large_run_threshold,
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
                failures=["llm_budget_required"],
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
    if holdout_contract is not None and (
        trace_pool is None or trace_lookup_error is not None
    ):
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

    if bypass_cache:
        headers[eval_cache_bypass.HEADER_BYPASS] = "1"
    semaphore = asyncio.Semaphore(max(1, concurrency))
    budget_stopped = False
    holdout_trace_cardinality: dict[str, Any] | None = None
    holdout_trace_cardinality_error: str | None = None
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=request_timeout,
            trust_env=False,
        ) as client:
            if holdout_contract is not None or (
                bypass_cache and _requires_signed_cache_bypass(target)
            ):
                try:
                    await _verify_cache_bypass_runtime(
                        client=client,
                        target=target,
                        headers=headers,
                        expected_git_sha=(
                            evaluation_runtime_git_sha
                            if holdout_contract is not None
                            else None
                        ),
                        eval_run_id=eval_run_id,
                        cache_bypass_secret=cache_bypass_secret,
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
            if max_llm_cost_rub is None:
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
                results = []
                sequential_semaphore = asyncio.Semaphore(1)
                for case in cases:
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
                    results.append(result)
                    if _llm_cost_rub_total(results) > max_llm_cost_rub:
                        budget_stopped = True
                        break
            if holdout_contract is not None:
                try:
                    await _verify_cache_bypass_runtime(
                        client=client,
                        target=target,
                        headers=headers,
                        expected_git_sha=evaluation_runtime_git_sha,
                        eval_run_id=eval_run_id,
                        cache_bypass_secret=cache_bypass_secret,
                    )
                except ValueError as exc:
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
                assert trace_pool is not None
                try:
                    holdout_trace_cardinality = await _fetch_eval_trace_cardinality(
                        trace_pool,
                        eval_run_id=eval_run_id,
                        expected_case_ids=[str(case["id"]) for case in cases],
                    )
                except Exception as exc:
                    holdout_trace_cardinality_error = type(exc).__name__
    finally:
        if trace_pool:
            await trace_pool.close()

    metrics = summarize_results(
        results,
        target=target,
        cases_path=cases_path,
        generated_smoke_cases=generated_smoke_cases,
        trace_lookup_error=trace_lookup_error,
    )
    metrics["eval_run_id"] = eval_run_id
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
    if budget_stopped:
        metrics["cases_original_total"] = original_cases_total
        metrics["cases_limited"] = True
        metrics["llm_budget_stopped"] = True
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
    usage_events = [event for item in results for event in item.get("llm_usage", [])]
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
        "llm_prompt_tokens": sum(int(item.get("llm_prompt_tokens") or 0) for item in results),
        "llm_completion_tokens": sum(
            int(item.get("llm_completion_tokens") or 0) for item in results
        ),
        "llm_total_tokens": sum(int(item.get("llm_total_tokens") or 0) for item in results),
        "llm_estimated_cost_rub": round(
            sum(float(item.get("llm_estimated_cost_rub") or 0.0) for item in results),
            6,
        ),
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


def _guard_large_live_run_budget(
    *,
    cases: list[dict[str, Any]],
    target: str,
    transport: httpx.AsyncBaseTransport | None,
    max_llm_cost_rub: float | None,
    require_budget: bool,
    large_run_threshold: int,
) -> None:
    if not require_budget or transport is not None:
        return
    if large_run_threshold < 1:
        raise ValueError("--large-run-threshold must be greater than zero")
    if max_llm_cost_rub is not None:
        if max_llm_cost_rub < 0:
            raise ValueError("--max-llm-cost-rub must be zero or greater")
        return
    if len(cases) <= large_run_threshold:
        return
    raise ValueError(
        "Refusing to run a large live ask eval without an explicit LLM budget: "
        f"{len(cases)} cases against {target}. "
        "Pass --max-llm-cost-rub <rubles>, --max-cases <n>, or "
        "--allow-unbounded-llm-cost for a deliberate full run."
    )


def _guard_eval_privacy(
    *,
    cases: list[dict[str, Any]],
    cases_path: Path,
    output_path: Path,
    markdown_path: Path | None,
    target: str,
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

    parsed = urlsplit(target)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in PRIVATE_EVAL_HOSTS
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/ask"
    ):
        raise ValueError(
            "private_ticket_derived cases may only use a loopback or app-ml /ask target"
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
) -> None:
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


def _holdout_failure_status(
    failures: list[str],
    *,
    budget_stopped: bool,
) -> str:
    if not failures:
        return "completed"
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
            total += float(item.get("llm_estimated_cost_rub") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def score_case(
    case: dict[str, Any],
    http_result: dict[str, Any],
    trace: dict[str, Any] | None,
) -> dict[str, Any]:
    response_text = str(http_result.get("response") or "")
    status = http_result.get("http_status")
    http_success = isinstance(status, int) and 200 <= status < 300
    trace = trace or {}

    observed_chunk_ids = _collect_trace_chunk_ids(trace)
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
    if expected_escalation_reason:
        escalation_reason_match = (
            trace.get("escalation_reason") == expected_escalation_reason if trace else None
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

    passed = http_success and all(value is True for value in required_checks.values())
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
        "llm_usage": trace.get("llm_usage") or [],
        "llm_prompt_tokens": trace.get("llm_prompt_tokens") or 0,
        "llm_completion_tokens": trace.get("llm_completion_tokens") or 0,
        "llm_total_tokens": trace.get("llm_total_tokens") or 0,
        "llm_estimated_cost_rub": trace.get("llm_estimated_cost_rub") or 0.0,
        "passed": passed,
        "failure_reasons": [] if passed else failure_reasons,
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
        )
        for item in raw_cases
    ]
    if not any(case.get("split") == PRIVATE_HOLDOUT_SPLIT for case in cases):
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
    normalized = str(value or "").casefold().replace("ё", "е")
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
        "--large-run-threshold",
        type=int,
        default=20,
        help="Require an explicit LLM budget above this number of live cases.",
    )
    parser.add_argument(
        "--allow-unbounded-llm-cost",
        action="store_true",
        help="Allow a large live eval without --max-llm-cost-rub.",
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
        help="Externally supplied exact holdout cases file SHA-256.",
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
    if metrics.get("llm_budget_exceeded") is True:
        raise SystemExit(2)
    if quality_gate_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
