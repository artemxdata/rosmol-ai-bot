from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import httpx

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.cost_governance import LiveEvalCostReservation, reserve_live_eval_cost
from eval.pre_pilot_cases import (
    ASK_SECTION_FILES,
    DEFAULT_CASES_DIR,
    FOLLOWUP_FILE,
    build_pre_pilot_case_sets,
)
from eval.release_provenance import (
    build_release_provenance,
    validate_release_provenance_attestation,
)
from eval.run_ask import (
    _auth_headers,
    _cost_governance_runtime_git_sha,
    _fetch_trace,
    _file_sha256,
    _guard_large_live_run_budget,
    _is_in_process_mock_transport,
    _llm_cost_accounting_failure,
    _llm_cost_rub_total,
    _local_llm_pricing_preflight_failure,
    _normalize_case,
    _requires_signed_cache_bypass,
    _trace_dsn_candidates,
    _validate_cost_reservation_for_child_run,
    _verify_cache_bypass_runtime,
    score_case,
)
from eval.run_ask import (
    run_eval as run_ask_eval,
)
from src.security import eval_cache_bypass

DEFAULT_OUTPUT_DIR = Path("reports/pre_pilot_quality_suite")
DEFAULT_SECTIONS = (
    "yonote",
    "forums",
    "safety",
    "off_topic",
    "pii",
    "adversarial",
    "followup",
)
DEFAULT_MIN_SECTION_PASS_RATE = 0.9
SAFETY_MIN_PASS_RATE = 1.0
RELEASE_MIN_TRACE_COVERAGE_RATE = 1.0
MAX_PROVENANCE_ATTESTATION_BYTES = 64 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_FOLLOWUP_CONTENT_SHA256_V1 = {
    (PROJECT_ROOT / "eval" / "cases" / "pre_pilot_followup.json").resolve():
        "1f5d53723b134e2fb4a4701d60db2658f9a8e5c49a6d6786e6df60805a5e226a",
    (PROJECT_ROOT / "eval" / "cases" / "dialog_memory_regression.json").resolve():
        "8a74f5acd66fcb7c9bbfeb5a84c5e15a396b4bcaea15402e061118a20d1719f1",
}
LEGACY_FOLLOWUP_SCHEMAS_V1: dict[str, frozenset[str]] = {
    "followup_amur_refusal_after_context": frozenset(
        f"followup_amur_refusal_after_context_t{index}" for index in range(1, 7)
    ),
    "followup_bctp_family_transfer": frozenset(
        f"followup_bctp_family_transfer_t{index}" for index in range(1, 3)
    ),
    "followup_grants_refund_contact": frozenset(
        f"followup_grants_refund_contact_t{index}" for index in range(1, 3)
    ),
    "followup_youth_day_ticket_family_program": frozenset(
        f"followup_youth_day_ticket_family_program_t{index}"
        for index in range(1, 7)
    ),
    "dialog_ticket_event_clarification": frozenset(
        f"dialog_ticket_event_clarification_t{index}" for index in range(1, 3)
    ),
    "dialog_five_followups_keep_event": frozenset(
        f"dialog_five_followups_keep_event_t{index}" for index in range(1, 8)
    ),
    "dialog_long_clarifications_then_resolution": frozenset(
        f"dialog_long_clarifications_then_resolution_t{index}"
        for index in range(1, 8)
    ),
    "dialog_nested_grant_followups": frozenset(
        f"dialog_nested_grant_followups_t{index}" for index in range(1, 4)
    ),
}
_REVIEW_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "label_status",
        "requires_human_review",
        "ticket_id_hash",
        "duplicate_component_id",
        "source_turn_index",
        "role_confidence",
    }
)


async def run_pre_pilot_quality_suite(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cases_dir: Path = DEFAULT_CASES_DIR,
    kb_seed_path: Path = Path("data/knowledge_base_seed.json"),
    target: str = "http://localhost:8001/ask",
    sections: tuple[str, ...] = DEFAULT_SECTIONS,
    followup_cases_path: Path | None = None,
    rebuild_cases: bool = False,
    concurrency: int = 1,
    request_timeout: float = 180.0,
    trace_lookup: bool = True,
    trace_dsn: str | None = None,
    bypass_cache: bool = True,
    max_llm_cost_rub: float | None = 200.0,
    allow_unbounded_llm_cost: bool = False,
    high_cost_approval_id: str | None = None,
    release_run_id: str | None = None,
    expected_git_sha: str | None = None,
    provenance_file: Path | None = None,
) -> dict[str, Any]:
    _validate_sections(sections)
    if not _valid_quality_target(target):
        raise ValueError(
            "Quality target must be an explicit loopback HTTP /ask endpoint "
            "or http://app-ml:8000/ask"
        )
    release_run_id = release_run_id or f"pre-pilot-{uuid4()}"
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(cases_dir.mkdir, parents=True, exist_ok=True)
    if rebuild_cases or not _all_case_files_exist(cases_dir):
        await asyncio.to_thread(
            build_pre_pilot_case_sets,
            kb_seed_path=kb_seed_path,
            output_dir=cases_dir,
        )

    case_paths = _case_paths(
        cases_dir=cases_dir,
        sections=sections,
        followup_cases_path=followup_cases_path,
    )
    if provenance_file is None:
        provenance = await asyncio.to_thread(
            build_release_provenance,
            release_run_id=release_run_id,
            target=target,
            kb_seed_path=kb_seed_path,
            case_paths=case_paths,
            expected_git_sha=expected_git_sha,
        )
    else:
        provenance = await asyncio.to_thread(
            _load_and_validate_provenance_attestation,
            provenance_file,
            release_run_id=release_run_id,
            target=target,
            kb_seed_path=kb_seed_path,
            case_paths=case_paths,
            expected_git_sha=expected_git_sha or "",
        )

    release_bound = expected_git_sha is not None or provenance_file is not None
    if release_bound and provenance.get("complete") is not True:
        summary = _build_summary(
            output_dir=output_dir,
            cases_dir=cases_dir,
            target=target,
            sections=sections,
            section_reports={},
            max_llm_cost_rub=max_llm_cost_rub,
            stopped_by_budget=False,
            release_run_id=release_run_id,
            trace_required=True,
            provenance=provenance,
        )
        await _persist_summary(output_dir, summary)
        return summary

    cost_reservation: LiveEvalCostReservation | None = None
    if high_cost_approval_id:
        if max_llm_cost_rub is None:
            raise ValueError("A pre-pilot cost approval requires a finite suite budget")
        pricing_failure = _local_llm_pricing_preflight_failure()
        if pricing_failure is not None:
            raise ValueError(
                "Live pre-pilot pricing preflight failed: " + pricing_failure
            )
        if trace_lookup is not True:
            raise ValueError("Live pre-pilot cost approval requires trace lookup")
        preflight_trace_pool = await _open_trace_pool(trace_dsn)
        if preflight_trace_pool is None:
            raise RuntimeError(
                "Live pre-pilot requires PostgreSQL trace lookup before cost reservation"
            )
        await preflight_trace_pool.close()
        total_live_cases = _suite_live_case_count(case_paths)
        cost_reservation = reserve_live_eval_cost(
            scope="pre-pilot-quality-suite",
            run_id=release_run_id,
            runtime_git_sha=_cost_governance_runtime_git_sha(
                explicit_sha=str(provenance.get("git_sha") or expected_git_sha or ""),
                evaluation_runtime_git_sha=None,
            ),
            manifest_sha256=_suite_case_manifest_sha256(provenance),
            case_count=total_live_cases,
            approved_cap_rub=max_llm_cost_rub,
            private_full=False,
            high_cost_approval_id=high_cost_approval_id,
        )

    section_reports: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    stopped_by_budget = False
    for section_index, section in enumerate(sections):
        if section == "followup":
            if _budget_exhausted(max_llm_cost_rub, total_cost):
                stopped_by_budget = True
                break
            report = await run_followup_eval(
                cases_path=followup_cases_path or (cases_dir / FOLLOWUP_FILE),
                output_path=output_dir / "followup_eval.json",
                markdown_path=output_dir / "followup_eval.md",
                target=target,
                request_timeout=request_timeout,
                trace_lookup=trace_lookup,
                trace_dsn=trace_dsn,
                bypass_cache=bypass_cache,
                max_llm_cost_rub=_remaining_budget(max_llm_cost_rub, total_cost),
                require_budget_for_large_runs=not allow_unbounded_llm_cost,
                high_cost_approval_id=high_cost_approval_id,
                cost_reservation=cost_reservation,
            )
        else:
            if _budget_exhausted(max_llm_cost_rub, total_cost):
                stopped_by_budget = True
                break
            report = await run_ask_eval(
                cases_path=cases_dir / ASK_SECTION_FILES[section],
                output_path=output_dir / f"{section}_ask_eval.json",
                target=target,
                concurrency=concurrency,
                request_timeout=request_timeout,
                trace_lookup=trace_lookup,
                trace_dsn=trace_dsn,
                kb_seed_path=kb_seed_path,
                markdown_path=output_dir / f"{section}_ask_eval.md",
                bypass_cache=bypass_cache,
                max_llm_cost_rub=_remaining_budget(max_llm_cost_rub, total_cost),
                require_budget_for_large_runs=not allow_unbounded_llm_cost,
                high_cost_approval_id=high_cost_approval_id,
                cost_reservation=cost_reservation,
            )
        section_reports[section] = report
        total_cost += _section_cost(report)
        if (
            report.get("llm_budget_stopped") is True
            or report.get("llm_pricing_stopped") is True
        ):
            stopped_by_budget = True
            break
        if (
            max_llm_cost_rub is not None
            and total_cost >= max_llm_cost_rub
            and not allow_unbounded_llm_cost
            and section_index < len(sections) - 1
        ):
            stopped_by_budget = True
            break

    summary = _build_summary(
        output_dir=output_dir,
        cases_dir=cases_dir,
        target=target,
        sections=sections,
        section_reports=section_reports,
        max_llm_cost_rub=max_llm_cost_rub,
        stopped_by_budget=stopped_by_budget,
        release_run_id=release_run_id,
        trace_required=True,
        provenance=provenance,
    )
    await _persist_summary(output_dir, summary)
    return summary


async def run_followup_eval(
    *,
    cases_path: Path,
    output_path: Path,
    markdown_path: Path | None = None,
    target: str = "http://localhost:8001/ask",
    request_timeout: float = 180.0,
    trace_lookup: bool = True,
    trace_dsn: str | None = None,
    bypass_cache: bool = True,
    max_llm_cost_rub: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    require_budget_for_large_runs: bool = True,
    high_cost_approval_id: str | None = None,
    cost_reservation: LiveEvalCostReservation | None = None,
) -> dict[str, Any]:
    conversations = _load_followup_cases(cases_path)
    turns_total = sum(
        len(conversation.get("turns") or []) for conversation in conversations
    )
    eval_run_id = f"followup-eval-{uuid4()}"
    _guard_large_live_run_budget(
        cases=[{} for _ in range(turns_total)],
        target=target,
        transport=transport,
        max_llm_cost_rub=max_llm_cost_rub,
        require_budget=require_budget_for_large_runs,
        large_run_threshold=20,
        trace_lookup=trace_lookup,
        private_contract_run=False,
        high_cost_approval_id=high_cost_approval_id,
    )
    if not _is_in_process_mock_transport(transport):
        pricing_failure = _local_llm_pricing_preflight_failure()
        if pricing_failure is not None:
            raise ValueError(
                "Live follow-up eval pricing preflight failed: " + pricing_failure
            )
    trace_pool = await _open_trace_pool(trace_dsn) if trace_lookup else None
    if (
        trace_lookup
        and trace_pool is None
        and not _is_in_process_mock_transport(transport)
    ):
        raise RuntimeError(
            "Live follow-up eval requires an available PostgreSQL trace lookup "
            "connection before the first request"
        )
    if not _is_in_process_mock_transport(transport):
        assert max_llm_cost_rub is not None
        try:
            if cost_reservation is None:
                cost_reservation = reserve_live_eval_cost(
                    scope="followup-eval",
                    run_id=eval_run_id,
                    runtime_git_sha=_cost_governance_runtime_git_sha(
                        explicit_sha=None,
                        evaluation_runtime_git_sha=None,
                    ),
                    manifest_sha256=_file_sha256(cases_path),
                    case_count=turns_total,
                    approved_cap_rub=max_llm_cost_rub,
                    private_full=False,
                    high_cost_approval_id=high_cost_approval_id,
                )
            else:
                _validate_cost_reservation_for_child_run(
                    cost_reservation,
                    case_count=turns_total,
                    max_llm_cost_rub=max_llm_cost_rub,
                    private_full=False,
                )
        except ValueError:
            if trace_pool is not None:
                await trace_pool.close()
            raise
    headers = _auth_headers("API_AUTH_TOKEN")
    headers["X-Eval-Run-Id"] = eval_run_id
    cache_bypass_secret = ""
    signed_bypass_required = (
        bypass_cache and _requires_signed_cache_bypass(target)
    )
    if bypass_cache:
        headers[eval_cache_bypass.HEADER_BYPASS] = "1"
        cache_bypass_secret = headers.get("X-API-Key", "").strip()
        if signed_bypass_required and not cache_bypass_secret:
            raise RuntimeError(
                "non-loopback follow-up cache bypass requires API_AUTH_TOKEN"
            )

    results: list[dict[str, Any]] = []
    budget_stopped = False
    llm_pricing_failure: str | None = None
    strict_live_cost_control = (
        not _is_in_process_mock_transport(transport)
        and max_llm_cost_rub is not None
    )
    strict_cost_total = 0.0
    run_namespace = uuid4().hex[:12]
    async with httpx.AsyncClient(
        transport=transport,
        timeout=request_timeout,
        trust_env=False,
    ) as client:
        if signed_bypass_required:
            await _verify_cache_bypass_runtime(
                client=client,
                target=target,
                headers=headers,
                expected_git_sha=None,
                eval_run_id=eval_run_id,
                cache_bypass_secret=cache_bypass_secret,
            )
        for conversation_index, conversation in enumerate(conversations, start=1):
            user_id = f"pre-pilot-followup-{run_namespace}-{conversation_index}"
            turns = conversation.get("turns") or []
            for raw_turn in turns:
                case = _normalize_case({**raw_turn, "user_id": user_id})
                result = await _run_followup_turn(
                    client=client,
                    target=target,
                    headers=headers,
                    case=case,
                    trace_pool=trace_pool,
                    conversation_id=str(conversation.get("id") or conversation_index),
                    cache_bypass_secret=cache_bypass_secret,
                )
                results.append(result)
                if strict_live_cost_control:
                    llm_pricing_failure = _llm_cost_accounting_failure(result)
                    if llm_pricing_failure is not None:
                        break
                    strict_cost_total += float(
                        result.get("llm_estimated_cost_rub") or 0.0
                    )
                    turns_remain = len(results) < turns_total
                    if strict_cost_total > max_llm_cost_rub or (
                        turns_remain
                        and strict_cost_total >= max_llm_cost_rub
                    ):
                        budget_stopped = True
                        break
                elif (
                    max_llm_cost_rub is not None
                    and _llm_cost_rub_total(results) > max_llm_cost_rub
                ):
                    budget_stopped = True
                    break
            if budget_stopped or llm_pricing_failure is not None:
                break

    if trace_pool:
        await trace_pool.close()

    metrics = _summarize_followup_results(
        results,
        cases_path=cases_path,
        target=target,
        conversations_total=len(conversations),
        budget_stopped=budget_stopped,
        max_llm_cost_rub=max_llm_cost_rub,
        eval_run_id=eval_run_id,
    )
    metrics["cost_reservation"] = (
        cost_reservation.path.name if cost_reservation is not None else None
    )
    if llm_pricing_failure is not None:
        metrics["llm_pricing_stopped"] = True
        metrics["llm_pricing_failure"] = llm_pricing_failure
    await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        output_path.write_text,
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if markdown_path:
        await asyncio.to_thread(_write_followup_markdown, markdown_path, metrics)
    return metrics


async def _run_followup_turn(
    *,
    client: httpx.AsyncClient,
    target: str,
    headers: dict[str, str],
    case: dict[str, Any],
    trace_pool: asyncpg.Pool | None,
    conversation_id: str,
    cache_bypass_secret: str = "",
) -> dict[str, Any]:
    started_at = perf_counter()
    request_id = ""
    try:
        request_payload = {
            "user_id": case["user_id"],
            "channel": case["channel"],
            "text": case["query"],
        }
        request_headers = {
            **headers,
            "X-Eval-Case-Id": str(case["id"]),
        }
        if cache_bypass_secret:
            request_headers.update(
                eval_cache_bypass.build_signed_headers(
                    cache_bypass_secret,
                    method="POST",
                    path=urlsplit(target).path or "/",
                    eval_run_id=str(headers["X-Eval-Run-Id"]),
                    eval_case_id=str(case["id"]),
                    payload_sha256=eval_cache_bypass.canonical_payload_sha256(
                        eval_cache_bypass.canonical_ask_payload(
                            request_payload
                        )
                    ),
                )
            )
        response = await client.post(
            target,
            headers=request_headers,
            json=request_payload,
        )
        payload = response.json() if response.content else {}
        request_id = str(payload.get("request_id") or "")
        response_text = str(payload.get("response") or response.text)
        http_result = {
            "http_status": response.status_code,
            "request_id": request_id,
            "response": response_text,
            "latency_ms": int((perf_counter() - started_at) * 1000),
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

    trace = await _fetch_trace(trace_pool, request_id) if trace_pool and request_id else None
    scored = score_case(case, http_result, trace)
    scored["conversation_id"] = conversation_id
    return scored


async def _open_trace_pool(trace_dsn: str | None) -> asyncpg.Pool | None:
    errors: list[str] = []
    for candidate in _trace_dsn_candidates(trace_dsn):
        try:
            return await asyncpg.create_pool(candidate, min_size=1, max_size=1)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    if os.getenv("PRE_PILOT_TRACE_REQUIRED", "").strip() == "1":
        raise RuntimeError("; ".join(errors))
    return None


def _load_followup_cases(path: Path) -> list[dict[str, Any]]:
    raw_payload = path.read_bytes()
    payload = json.loads(raw_payload.decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Follow-up cases must contain a JSON array: {path}")
    expected_legacy_hash = LEGACY_FOLLOWUP_CONTENT_SHA256_V1.get(path.resolve())
    canonical_payload = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    legacy_file_allowed = (
        expected_legacy_hash is not None
        and hashlib.sha256(canonical_payload).hexdigest() == expected_legacy_hash
    )
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("turns"), list):
            raise ValueError("Each follow-up case must be an object with a turns array")
        if legacy_file_allowed and _is_allowlisted_legacy_followup(item):
            continue
        _require_human_reviewed_followup(item, location="Follow-up case")
        for turn in item["turns"]:
            if not isinstance(turn, dict):
                raise ValueError("Each follow-up turn must be an object")
            _require_human_reviewed_followup(turn, location="Follow-up turn")
    return payload


def _is_allowlisted_legacy_followup(item: dict[str, Any]) -> bool:
    conversation_id = str(item.get("id") or "")
    expected_turn_ids = LEGACY_FOLLOWUP_SCHEMAS_V1.get(conversation_id)
    if expected_turn_ids is None or _REVIEW_STATE_FIELDS.intersection(item):
        return False

    turns = item.get("turns")
    if not isinstance(turns, list) or not all(isinstance(turn, dict) for turn in turns):
        return False
    if any(_REVIEW_STATE_FIELDS.intersection(turn) for turn in turns):
        return False
    actual_turn_ids = {str(turn.get("id") or "") for turn in turns}
    return len(actual_turn_ids) == len(turns) and actual_turn_ids == expected_turn_ids


def _require_human_reviewed_followup(
    item: dict[str, Any],
    *,
    location: str,
) -> None:
    if item.get("label_status") != "human_reviewed":
        raise ValueError(
            f"{location} requiring human review must set "
            "label_status='human_reviewed'"
        )
    if item.get("requires_human_review") is not False:
        raise ValueError(
            f"{location} requiring human review must set "
            "requires_human_review=false"
        )


def _summarize_followup_results(
    results: list[dict[str, Any]],
    *,
    cases_path: Path,
    target: str,
    conversations_total: int,
    budget_stopped: bool,
    max_llm_cost_rub: float | None,
    eval_run_id: str,
) -> dict[str, Any]:
    conversations: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        conversations.setdefault(str(result.get("conversation_id")), []).append(result)
    conversation_passed = [
        all(turn.get("passed") is True for turn in turns) for turns in conversations.values()
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "target": target,
        "eval_run_id": eval_run_id,
        "cases_path": str(cases_path),
        "conversations_total": conversations_total,
        "conversations_executed": len(conversations),
        "conversation_pass_rate": _rate(conversation_passed),
        "turns_total": len(results),
        "turn_pass_rate": _rate([item.get("passed") is True for item in results]),
        "http_success_rate": _rate([item.get("http_success") is True for item in results]),
        "trace_coverage_rate": _rate([item.get("trace_found") is True for item in results]),
        "expected_or_equivalent_chunk_hit_rate": _rate(
            [
                item.get("expected_or_equivalent_chunk_hit") is True
                for item in results
                if item.get("expected_chunk_ids")
            ]
        ),
        "llm_estimated_cost_rub": round(_llm_cost_rub_total(results), 6),
        "llm_budget_rub": max_llm_cost_rub,
        "llm_budget_stopped": budget_stopped,
        "generator_model_counts": dict(
            Counter(str(item.get("generator_model") or "unknown") for item in results)
        ),
        "failure_reason_counts": dict(
            Counter(reason for item in results for reason in item.get("failure_reasons") or [])
        ),
        "results": results,
    }


def _valid_quality_target(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"} and port is not None
    internal_runtime = parsed.hostname == "app-ml" and port == 8000
    return (
        parsed.scheme == "http"
        and (loopback or internal_runtime)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/ask"
        and not parsed.query
        and not parsed.fragment
    )


def _load_and_validate_provenance_attestation(
    path: Path,
    *,
    release_run_id: str,
    target: str,
    kb_seed_path: Path,
    case_paths: dict[str, Path],
    expected_git_sha: str,
) -> dict[str, Any]:
    errors: list[str] = []
    attestation: dict[str, Any] = {}
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("attestation must be a regular file")
        if path.stat().st_size > MAX_PROVENANCE_ATTESTATION_BYTES:
            raise ValueError("attestation is too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("attestation must contain a JSON object")
        attestation = payload
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(f"attestation_unavailable:{type(exc).__name__}")

    if errors:
        return {
            "release_run_id": release_run_id,
            "target": target,
            "git_sha": None,
            "expected_git_sha": expected_git_sha,
            "git_worktree_clean": None,
            "kb_seed": None,
            "case_files": {},
            "verification_mode": "host_git_attestation_with_local_hash_verification",
            "complete": False,
            "errors": errors,
        }
    return validate_release_provenance_attestation(
        attestation,
        release_run_id=release_run_id,
        target=target,
        kb_seed_path=kb_seed_path,
        case_paths=case_paths,
        expected_git_sha=expected_git_sha,
    )


async def _persist_summary(output_dir: Path, summary: dict[str, Any]) -> None:
    summary_path = output_dir / "summary.json"
    summary_md_path = output_dir / "summary.md"
    await asyncio.to_thread(
        summary_path.write_text,
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    await asyncio.to_thread(_write_summary_markdown, summary_md_path, summary)


def _build_summary(
    *,
    output_dir: Path,
    cases_dir: Path,
    target: str,
    sections: tuple[str, ...],
    section_reports: dict[str, dict[str, Any]],
    max_llm_cost_rub: float | None,
    stopped_by_budget: bool,
    release_run_id: str,
    trace_required: bool,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    requested_sections = list(sections)
    completed_sections = list(section_reports)
    sections_complete = bool(requested_sections) and completed_sections == requested_sections
    section_passed = {
        name: _section_passed(name, report, trace_required=trace_required)
        for name, report in section_reports.items()
    }
    provenance_complete = provenance.get("complete") is True
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "release_run_id": release_run_id,
        "expected_git_sha": provenance.get("expected_git_sha"),
        "target": target,
        "cases_dir": str(cases_dir),
        "output_dir": str(output_dir),
        "requested_sections": requested_sections,
        "completed_sections": completed_sections,
        "sections_complete": sections_complete,
        "trace_required": trace_required,
        "passed": sections_complete
        and all(section_passed.values())
        and not stopped_by_budget
        and provenance_complete,
        "max_llm_cost_rub": max_llm_cost_rub,
        "llm_estimated_cost_rub": round(sum(_section_cost(r) for r in section_reports.values()), 6),
        "llm_budget_stopped": stopped_by_budget,
        "provenance": provenance,
        "sections": {
            name: {
                **_compact_section_report(name, report),
                "passed": section_passed[name],
            }
            for name, report in section_reports.items()
        },
    }


def _compact_section_report(name: str, report: dict[str, Any]) -> dict[str, Any]:
    if name == "followup":
        return {
            "turns_total": report.get("turns_total"),
            "conversations_total": report.get("conversations_total"),
            "conversations_executed": report.get("conversations_executed"),
            "turn_pass_rate": report.get("turn_pass_rate"),
            "conversation_pass_rate": report.get("conversation_pass_rate"),
            "http_success_rate": report.get("http_success_rate"),
            "trace_coverage_rate": report.get("trace_coverage_rate"),
            "expected_or_equivalent_chunk_hit_rate": report.get(
                "expected_or_equivalent_chunk_hit_rate"
            ),
            "llm_estimated_cost_rub": report.get("llm_estimated_cost_rub"),
            "generator_model_counts": report.get("generator_model_counts"),
            "failure_reason_counts": report.get("failure_reason_counts"),
            "eval_run_id": report.get("eval_run_id"),
            "llm_budget_stopped": report.get("llm_budget_stopped"),
        }
    return {
        "cases_total": report.get("cases_total"),
        "pass_rate": report.get("pass_rate"),
        "http_success_rate": report.get("http_success_rate"),
        "behavior_match_rate": report.get("behavior_match_rate"),
        "trace_coverage_rate": report.get("trace_coverage_rate"),
        "expected_or_equivalent_chunk_hit_rate": report.get(
            "expected_or_equivalent_chunk_hit_rate"
        ),
        "llm_estimated_cost_rub": report.get("llm_estimated_cost_rub"),
        "generator_model_counts": report.get("generator_model_counts"),
        "failure_reason_counts": report.get("failure_reason_counts"),
        "eval_run_id": report.get("eval_run_id"),
        "llm_budget_stopped": report.get("llm_budget_stopped"),
    }


def _section_passed(
    name: str,
    report: dict[str, Any],
    *,
    trace_required: bool = True,
) -> bool:
    if (
        report.get("llm_budget_stopped") is True
        or report.get("llm_pricing_stopped") is True
    ):
        return False
    if trace_required and not _rate_meets(
        report.get("trace_coverage_rate"), RELEASE_MIN_TRACE_COVERAGE_RATE
    ):
        return False
    if "pass_rate" in report:
        if _positive_int(report.get("cases_total")) is None:
            return False
        threshold = SAFETY_MIN_PASS_RATE if name == "safety" else DEFAULT_MIN_SECTION_PASS_RATE
        return _rate_meets(report.get("pass_rate"), threshold)
    if _positive_int(report.get("turns_total")) is None:
        return False
    conversations_total = _positive_int(report.get("conversations_total"))
    conversations_executed = _positive_int(report.get("conversations_executed"))
    if conversations_total is None or conversations_executed != conversations_total:
        return False
    turn_passed = _rate_meets(report.get("turn_pass_rate"), DEFAULT_MIN_SECTION_PASS_RATE)
    conversation_passed = _rate_meets(
        report.get("conversation_pass_rate"), DEFAULT_MIN_SECTION_PASS_RATE
    )
    return turn_passed and conversation_passed


def _rate_meets(value: Any, threshold: float) -> bool:
    try:
        return float(value) >= threshold
    except (TypeError, ValueError):
        return False


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _section_cost(report: dict[str, Any]) -> float:
    try:
        return float(report.get("llm_estimated_cost_rub") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _budget_exhausted(max_llm_cost_rub: float | None, spent: float) -> bool:
    return max_llm_cost_rub is not None and spent >= max_llm_cost_rub


def _remaining_budget(max_llm_cost_rub: float | None, spent: float) -> float | None:
    if max_llm_cost_rub is None:
        return None
    return max(0.0, max_llm_cost_rub - spent)


def _validate_sections(sections: tuple[str, ...]) -> None:
    if not sections:
        raise ValueError("At least one pre-pilot section is required")
    valid = set(DEFAULT_SECTIONS)
    invalid = [section for section in sections if section not in valid]
    if invalid:
        raise ValueError(f"Unknown pre-pilot sections: {', '.join(invalid)}")


def _case_paths(
    *,
    cases_dir: Path,
    sections: tuple[str, ...],
    followup_cases_path: Path | None,
) -> dict[str, Path]:
    return {
        section: (
            followup_cases_path or (cases_dir / FOLLOWUP_FILE)
            if section == "followup"
            else cases_dir / ASK_SECTION_FILES[section]
        )
        for section in sections
    }


def _suite_live_case_count(case_paths: dict[str, Path]) -> int:
    total = 0
    for section, path in case_paths.items():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"Eval case file must contain a JSON array: {path}")
        if section == "followup":
            total += sum(
                len(item.get("turns") or [])
                for item in payload
                if isinstance(item, dict)
            )
        else:
            total += len(payload)
    if total < 1:
        raise ValueError("Pre-pilot suite contains no live requests")
    return total


def _suite_case_manifest_sha256(provenance: dict[str, Any]) -> str:
    raw_case_files = provenance.get("case_files")
    if not isinstance(raw_case_files, dict) or not raw_case_files:
        raise ValueError("Pre-pilot provenance has no case file fingerprints")
    manifest: dict[str, str] = {}
    for name, raw_fingerprint in raw_case_files.items():
        if not isinstance(raw_fingerprint, dict):
            raise ValueError("Pre-pilot case fingerprint is invalid")
        digest = str(raw_fingerprint.get("sha256") or "").lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("Pre-pilot case fingerprint is invalid")
        manifest[str(name)] = digest
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _all_case_files_exist(cases_dir: Path) -> bool:
    return all((cases_dir / filename).exists() for filename in ASK_SECTION_FILES.values()) and (
        cases_dir / FOLLOWUP_FILE
    ).exists()


def _rate(values: list[bool]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value) / len(values)


def _write_followup_markdown(path: Path, metrics: dict[str, Any]) -> None:
    lines = [
        "# Follow-up Eval",
        "",
        f"- Turns: `{metrics.get('turns_total')}`",
        f"- Turn pass rate: `{_format_rate(metrics.get('turn_pass_rate'))}`",
        f"- Conversation pass rate: `{_format_rate(metrics.get('conversation_pass_rate'))}`",
        f"- Trace coverage: `{_format_rate(metrics.get('trace_coverage_rate'))}`",
        f"- Cost, RUB: `{metrics.get('llm_estimated_cost_rub')}`",
        "",
        "## Failures",
        "",
    ]
    failures = metrics.get("failure_reason_counts") or {}
    if failures:
        lines.extend(f"- `{key}`: `{value}`" for key, value in sorted(failures.items()))
    else:
        lines.append("- no failures")
    lines.extend(["", "## Turns", ""])
    for item in metrics.get("results") or []:
        lines.extend(
            [
                f"### `{item.get('id')}`",
                "",
                f"- Passed: `{item.get('passed')}`",
                f"- Conversation: `{item.get('conversation_id')}`",
                f"- Model: `{item.get('generator_model') or '-'}`",
                f"- Sources: `{', '.join(item.get('cited_source_ids') or []) or '-'}`",
                f"- Failures: `{', '.join(item.get('failure_reasons') or []) or '-'}`",
                "",
                f"**Question:** {item.get('query') or '-'}",
                "",
                f"**Answer:** {_clip(item.get('response'))}",
                "",
            ]
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_summary_markdown(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Pre-pilot Quality Suite",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- Release run: `{summary.get('release_run_id')}`",
        f"- Expected Git SHA: `{summary.get('expected_git_sha')}`",
        f"- Git SHA: `{(summary.get('provenance') or {}).get('git_sha')}`",
        f"- Target: `{summary.get('target')}`",
        f"- Passed: `{summary.get('passed')}`",
        f"- Sections complete: `{summary.get('sections_complete')}`",
        f"- Trace required: `{summary.get('trace_required')}`",
        f"- Cost, RUB: `{summary.get('llm_estimated_cost_rub')}`",
        f"- Budget, RUB: `{summary.get('max_llm_cost_rub')}`",
        f"- Budget stopped: `{summary.get('llm_budget_stopped')}`",
        "",
        "| Section | Cases/Turns | Pass | Trace | Sources | Cost RUB |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, report in summary.get("sections", {}).items():
        cases = report.get("cases_total") or report.get("turns_total")
        pass_rate = (
            report.get("pass_rate") if "pass_rate" in report else report.get("turn_pass_rate")
        )
        source_rate = report.get("expected_or_equivalent_chunk_hit_rate")
        lines.append(
            f"| {name} | {cases} | {_format_rate(pass_rate)} | "
            f"{_format_rate(report.get('trace_coverage_rate'))} | "
            f"{_format_rate(source_rate)} | {report.get('llm_estimated_cost_rub')} |"
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _clip(value: Any, *, limit: int = 700) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text or "-"
    return text[: limit - 1].rstrip() + "…"


def _compact_stdout_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "passed": summary.get("passed"),
        "release_run_id": summary.get("release_run_id"),
        "expected_git_sha": summary.get("expected_git_sha"),
        "completed_sections": summary.get("completed_sections"),
        "llm_estimated_cost_rub": summary.get("llm_estimated_cost_rub"),
        "trace_coverage": {
            name: section.get("trace_coverage_rate")
            for name, section in (summary.get("sections") or {}).items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--cases-dir", default=str(DEFAULT_CASES_DIR))
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
    parser.add_argument("--target", default="http://localhost:8001/ask")
    parser.add_argument("--sections", default=",".join(DEFAULT_SECTIONS))
    parser.add_argument("--followup-cases", default="")
    parser.add_argument("--rebuild-cases", action="store_true")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--request-timeout", type=float, default=180.0)
    parser.add_argument("--no-db-traces", action="store_true")
    parser.add_argument("--trace-dsn", default="")
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--max-llm-cost-rub", type=float, default=200.0)
    parser.add_argument("--allow-unbounded-llm-cost", action="store_true")
    parser.add_argument("--high-cost-approval-id", default="")
    parser.add_argument("--release-run-id", default="")
    parser.add_argument("--expected-git-sha", default="")
    parser.add_argument("--provenance-file", type=Path)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    if not _valid_quality_target(args.target):
        parser.error(
            "--target must be an explicit loopback HTTP /ask endpoint "
            "or http://app-ml:8000/ask"
        )

    summary = asyncio.run(
        run_pre_pilot_quality_suite(
            output_dir=Path(args.output_dir),
            cases_dir=Path(args.cases_dir),
            kb_seed_path=Path(args.kb_seed),
            target=args.target,
            sections=tuple(
                section.strip() for section in args.sections.split(",") if section.strip()
            ),
            followup_cases_path=(Path(args.followup_cases) if args.followup_cases else None),
            rebuild_cases=args.rebuild_cases,
            concurrency=args.concurrency,
            request_timeout=args.request_timeout,
            trace_lookup=not args.no_db_traces,
            trace_dsn=args.trace_dsn or None,
            bypass_cache=not args.use_cache,
            max_llm_cost_rub=(
                None if args.allow_unbounded_llm_cost else args.max_llm_cost_rub
            ),
            allow_unbounded_llm_cost=args.allow_unbounded_llm_cost,
            high_cost_approval_id=args.high_cost_approval_id or None,
            release_run_id=args.release_run_id or None,
            expected_git_sha=args.expected_git_sha or None,
            provenance_file=args.provenance_file,
        )
    )
    stdout_payload = _compact_stdout_summary(summary) if args.summary_only else summary
    print(json.dumps(stdout_payload, ensure_ascii=False, indent=2))
    if not summary["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
