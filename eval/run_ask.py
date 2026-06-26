from __future__ import annotations

import argparse
import asyncio
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
from uuid import UUID

import asyncpg
import httpx

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.ask_cases import build_seed_ask_cases
from src.config import get_settings

FALSE_INSUFFICIENT_SOURCE_RE = re.compile(
    r"(ответ[а]?[^.!?]{0,120}\s+в\s+источник(?:е|ах)\s+нет|"
    r"в\s+(?:предоставленн(?:ом|ых)\s+)?источник(?:е|ах)\s+нет\s+информации|"
    r"из\s+(?:представленных|переданных)\s+источников\s+невозможно\s+ответить|"
    r"источники\s+не\s+(?:содержат|подтверждают)|"
    r"информации\s+(?:в\s+источниках\s+)?нет|"
    r"информаци[яи][^.!?]{0,160}отсутств)",
    flags=re.IGNORECASE,
)
NON_ANSWER_RE = re.compile(
    r"(уже\s+был[ао]?\s+предоставлен[ао]?\s+в\s+источник(?:е|ах)|"
    r"смотрите\s+источник|"
    r"обратитесь\s+к\s+источнику)",
    flags=re.IGNORECASE,
)
EXPECTED_BEHAVIORS = {"answer", "clarify", "scope_note", "escalate"}
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


def _normalize_case(raw: dict[str, Any]) -> dict[str, Any]:
    query = raw.get("query") or raw.get("question") or raw.get("text")
    if not query:
        raise ValueError("ask eval case must contain query, question, or text")

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
    expected_cited_chunk_ids = _string_list(
        raw.get("expected_cited_chunk_ids") or raw.get("expected_cited_sources") or []
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

    return {
        "id": str(raw.get("id") or raw.get("case_id") or query),
        "query": str(query),
        "user_id": str(raw.get("user_id") or "ask-eval"),
        "channel": str(raw.get("channel") or "api"),
        "expected_chunk_ids": expected_chunk_ids,
        "expected_cited_chunk_ids": expected_cited_chunk_ids,
        "equivalent_chunk_ids": equivalent_chunk_ids,
        "expected_answer_contains": expected_answer_contains,
        "expected_behavior": expected_behavior,
        "expected_escalated": raw.get("expected_escalated"),
        "expected_escalation_reason": raw.get("expected_escalation_reason"),
        "expected_generator_model": raw.get("expected_generator_model"),
        "tags": _string_list(raw.get("tags") or []),
    }


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
) -> dict[str, Any]:
    cases, generated_smoke_cases = await _load_cases(
        cases_path=cases_path,
        kb_seed_path=kb_seed_path,
        auto_smoke_cases=auto_smoke_cases,
        max_smoke_cases=max_smoke_cases,
        user_prefix=generated_user_prefix or _default_generated_user_prefix("ask-eval"),
    )
    original_cases_total = len(cases)
    if max_cases is not None:
        if max_cases < 1:
            raise ValueError("--max-cases must be greater than zero")
        cases = cases[:max_cases]
    _guard_large_live_run_budget(
        cases=cases,
        target=target,
        transport=transport,
        max_llm_cost_rub=max_llm_cost_rub,
        require_budget=require_budget_for_large_runs,
        large_run_threshold=large_run_threshold,
    )
    if not cases:
        metrics = _empty_metrics(target=target, cases_path=cases_path, auto_smoke_cases=False)
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
                errors.append(f"{type(exc).__name__}: {exc}")
        if trace_pool is None and errors:
            trace_lookup_error = "; ".join(errors)

    headers = _auth_headers(api_key_env)
    if bypass_cache:
        headers["X-Bypass-Cache"] = "1"
    semaphore = asyncio.Semaphore(max(1, concurrency))
    async with httpx.AsyncClient(transport=transport, timeout=request_timeout) as client:
        tasks = [
            _run_case(
                client=client,
                target=target,
                headers=headers,
                case=case,
                semaphore=semaphore,
                trace_pool=trace_pool,
            )
            for case in cases
        ]
        results = await asyncio.gather(*tasks)

    if trace_pool:
        await trace_pool.close()

    metrics = summarize_results(
        results,
        target=target,
        cases_path=cases_path,
        generated_smoke_cases=generated_smoke_cases,
        trace_lookup_error=trace_lookup_error,
    )
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
    if trace_lookup_error:
        metrics["trace_lookup_error"] = trace_lookup_error
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
    expected_behavior = case.get("expected_behavior")
    observed_behavior = _observed_behavior(response_text, trace)
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
    if expected_answer_contains:
        normalized_response = response_text.lower()
        answer_contains_match = all(
            expected.lower() in normalized_response for expected in expected_answer_contains
        )
        checks["answer_contains_match"] = answer_contains_match
        required_checks["answer_contains_match"] = answer_contains_match
    if expected_behavior:
        behavior_match = observed_behavior == expected_behavior
        checks["behavior_match"] = behavior_match
        required_checks["behavior_match"] = behavior_match
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
        "cited_source_types": _cited_source_types(trace, cited_chunk_ids),
        "expected_answer_contains": expected_answer_contains,
        "answer_contains_match": checks.get("answer_contains_match"),
        "expected_behavior": expected_behavior,
        "observed_behavior": observed_behavior,
        "behavior_match": checks.get("behavior_match"),
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
    if required_checks.get("behavior_match") is False:
        reasons.append(f"behavior_mismatch:{expected_behavior}!={observed_behavior}")
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
    if _looks_like_clarification(normalized):
        return "clarify"
    return "answer"


def _looks_like_scope_note(normalized_response: str) -> bool:
    return all(marker in normalized_response for marker in SCOPE_NOTE_MARKERS)


def _looks_like_clarification(normalized_response: str) -> bool:
    return any(marker in normalized_response for marker in CLARIFICATION_MARKERS)


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
) -> tuple[list[dict[str, Any]], bool]:
    raw_cases: list[dict[str, Any]] = []
    generated_smoke_cases = False
    cases_file_exists = await asyncio.to_thread(cases_path.exists)
    if cases_file_exists:
        raw = await asyncio.to_thread(_read_json, cases_path)
        if not isinstance(raw, list):
            raise ValueError("ask eval cases file must be a JSON array")
        raw_cases = raw
    elif not auto_smoke_cases:
        raise FileNotFoundError(f"ask eval cases file not found: {cases_path}")

    cases = _apply_user_prefix(
        [_normalize_case(item) for item in raw_cases],
        user_prefix=user_prefix,
    )
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
    return cases, generated_smoke_cases


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


async def _run_case(
    *,
    client: httpx.AsyncClient,
    target: str,
    headers: dict[str, str],
    case: dict[str, Any],
    semaphore: asyncio.Semaphore,
    trace_pool: asyncpg.Pool | None,
) -> dict[str, Any]:
    async with semaphore:
        started_at = perf_counter()
        request_id: str | None = None
        try:
            response = await client.post(
                target,
                headers=headers,
                json={
                    "user_id": case["user_id"],
                    "channel": case["channel"],
                    "text": case["query"],
                },
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
        if trace_pool and request_id:
            trace = await _fetch_trace(trace_pool, request_id)
        return score_case(case, http_result, trace)


async def _fetch_trace(pool: asyncpg.Pool, request_id: str) -> dict[str, Any] | None:
    try:
        request_uuid = UUID(request_id)
    except ValueError:
        return None

    row = await pool.fetchrow(
        """
        SELECT
            cache_hit, generator_model, cited_sources, was_escalated,
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
    return {key: _json_safe(row[key]) for key in row.keys()}


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
    source_type = str(metadata.get("source_type") or "").strip()
    if source_type:
        return source_type
    if chunk_id.startswith("ticket_answer_bank_"):
        return "ticket_answer_bank"
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


def _write_markdown(path: Path, metrics: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Ask Eval Report",
        "",
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

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
        )
    )
    print(
        json.dumps(
            {key: value for key, value in metrics.items() if key != "results"},
            ensure_ascii=False,
            indent=2,
        )
    )
    if metrics.get("llm_budget_exceeded") is True:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
