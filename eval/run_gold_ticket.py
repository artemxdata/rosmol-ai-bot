from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
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
from eval.gold_ticket import DatasetSplit, DialogueRole, GoldTicketV1
from eval.run_ask import (
    _auth_headers,
    _cost_governance_runtime_git_sha,
    _fetch_trace,
    _file_sha256,
    _is_in_process_mock_transport,
    _llm_cost_accounting_failure,
    _local_llm_pricing_preflight_failure,
    _requires_signed_cache_bypass,
    _trace_dsn_candidates,
    _verify_cache_bypass_runtime,
    score_case,
)
from src.security import eval_cache_bypass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DATA_ROOT = (PROJECT_ROOT / "data" / "private").resolve()
DEFAULT_KB_SEED = PROJECT_ROOT / "data" / "knowledge_base_seed.json"
PRIVATE_REPORT_SCHEMA = "gold-ticket-run-private-v1"
SAFE_REPORT_SCHEMA = "gold-ticket-run-safe-v1"


@dataclass(frozen=True, slots=True)
class ReplayCall:
    source_turn_id: str
    source_turn_index: int
    text: str
    step_id: str | None

    @property
    def scored(self) -> bool:
        return self.step_id is not None


def load_gold_tickets(path: Path) -> list[GoldTicketV1]:
    records: list[GoldTicketV1] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"GoldTicket JSONL line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(f"GoldTicket JSONL line {line_number} must be an object")
        try:
            ticket = GoldTicketV1.model_validate(payload)
        except ValueError as exc:
            raise ValueError(
                f"GoldTicket JSONL line {line_number} is not a sealed GoldTicket"
            ) from exc
        if ticket.split != DatasetSplit.HOLDOUT:
            raise ValueError("ordered product run accepts only sealed holdout GoldTickets")
        records.append(ticket)

    if not records:
        raise ValueError("ordered product run requires at least one GoldTicket")
    if len({ticket.ticket_id_hash for ticket in records}) != len(records):
        raise ValueError("GoldTicket run contains duplicate ticket identifiers")
    if len({ticket.duplicate_component_id for ticket in records}) != len(records):
        raise ValueError("GoldTicket run contains duplicate components")
    if len({ticket.dataset_id for ticket in records}) != 1:
        raise ValueError("GoldTicket run must contain exactly one dataset_id")
    if len(
        {ticket.knowledge_snapshot.canonical_seed_sha256 for ticket in records}
    ) != 1:
        raise ValueError("GoldTicket run contains multiple knowledge snapshots")
    return records


def build_replay_plan(ticket: GoldTicketV1) -> list[ReplayCall]:
    turns_by_id = {turn.turn_id: turn for turn in ticket.turns}
    replayable = sorted(
        (
            turn
            for turn in ticket.turns
            if turn.reviewed_role == DialogueRole.USER and turn.include_in_replay
        ),
        key=lambda turn: turn.source_turn_index,
    )
    if not replayable:
        raise ValueError("GoldTicket has no reviewed user turns to replay")
    replayable_by_id = {turn.turn_id: turn for turn in replayable}

    step_by_anchor: dict[int, str] = {}
    current_turn_ids: set[str] = set()
    history_turn_ids: set[str] = set()
    previous_anchor = -1
    for step in ticket.evaluation_steps:
        current_turns = [turns_by_id[turn_id] for turn_id in step.user_turn_ids]
        if any(
            turn.reviewed_role != DialogueRole.USER or not turn.include_in_replay
            for turn in current_turns
        ):
            raise ValueError("evaluation steps must target replayable reviewed user turns")
        if current_turn_ids.intersection(step.user_turn_ids):
            raise ValueError("a reviewed user turn cannot belong to multiple evaluation steps")
        current_turn_ids.update(step.user_turn_ids)

        first_current_index = min(turn.source_turn_index for turn in current_turns)
        anchor_index = max(turn.source_turn_index for turn in current_turns)
        required_user_history = {
            turn.turn_id
            for turn in replayable
            if turn.source_turn_index < first_current_index
        }
        if not required_user_history.issubset(step.history_turn_ids):
            raise ValueError(
                "evaluation history must include every earlier replayed user turn"
            )
        if anchor_index <= previous_anchor:
            raise ValueError("evaluation step anchors must be strictly chronological")
        if anchor_index in step_by_anchor:
            raise ValueError("evaluation steps cannot share a replay anchor")
        previous_anchor = anchor_index
        step_by_anchor[anchor_index] = step.step_id

        for history_turn_id in step.history_turn_ids:
            history_turn = turns_by_id[history_turn_id]
            if history_turn.source_turn_index >= first_current_index:
                raise ValueError("evaluation history must precede every current user turn")
            history_turn_ids.add(history_turn_id)

    replayable_ids = set(replayable_by_id)
    if not current_turn_ids.issubset(replayable_ids):
        raise ValueError("evaluation step contains a non-replayable user turn")
    unexplained = replayable_ids - current_turn_ids - history_turn_ids
    if unexplained:
        raise ValueError("replayed user turns must be evaluated or used by later history")

    plan = [
        ReplayCall(
            source_turn_id=turn.turn_id,
            source_turn_index=turn.source_turn_index,
            text=turn.text_deidentified,
            step_id=step_by_anchor.get(turn.source_turn_index),
        )
        for turn in replayable
    ]
    if sum(call.scored for call in plan) != len(ticket.evaluation_steps):
        raise ValueError("every GoldTicket evaluation step requires one replay anchor")
    return plan


def validate_knowledge_snapshot(
    tickets: list[GoldTicketV1],
    kb_seed_path: Path,
) -> dict[str, Any]:
    try:
        payload = json.loads(kb_seed_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("KB seed is unavailable or invalid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("KB seed must contain a JSON array")
    canonical_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    expected = tickets[0].knowledge_snapshot
    if canonical_sha256 != expected.canonical_seed_sha256:
        raise ValueError("KB seed differs from the sealed GoldTicket knowledge snapshot")
    published_yonote = sum(
        isinstance(item, dict)
        and item.get("source_type") == "yonote"
        and item.get("published") is True
        for item in payload
    )
    if published_yonote != expected.published_yonote_chunks:
        raise ValueError("published Yonote count differs from the sealed knowledge snapshot")
    return {
        "canonical_seed_sha256": canonical_sha256,
        "published_yonote_chunks": published_yonote,
    }


async def run_gold_ticket_eval(
    *,
    tickets_path: Path,
    private_output_path: Path,
    safe_output_path: Path,
    target: str = "http://localhost:8001/ask",
    kb_seed_path: Path = DEFAULT_KB_SEED,
    expected_runtime_git_sha: str | None = None,
    request_timeout: float = 180.0,
    trace_lookup: bool = True,
    trace_dsn: str | None = None,
    bypass_cache: bool = True,
    max_llm_cost_rub: float | None = 200.0,
    high_cost_approval_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    if not _valid_target(target):
        raise ValueError("GoldTicket target must be the explicit local /ask runtime")
    if not bypass_cache:
        raise ValueError("independent GoldTicket run requires cache bypass")
    await asyncio.to_thread(
        _validate_output_paths,
        private_output_path,
        safe_output_path,
        overwrite,
    )

    tickets = await asyncio.to_thread(load_gold_tickets, tickets_path)
    knowledge_snapshot = await asyncio.to_thread(
        validate_knowledge_snapshot,
        tickets,
        kb_seed_path,
    )
    plans = [build_replay_plan(ticket) for ticket in tickets]
    calls_total = sum(len(plan) for plan in plans)
    scored_total = sum(len(ticket.evaluation_steps) for ticket in tickets)
    strict_live = not _is_in_process_mock_transport(transport)
    if strict_live:
        if expected_runtime_git_sha is None:
            raise ValueError("live GoldTicket run requires exact runtime Git SHA")
        if max_llm_cost_rub is None or max_llm_cost_rub <= 0:
            raise ValueError("live GoldTicket run requires a positive finite cost cap")
        if not high_cost_approval_id:
            raise ValueError("live GoldTicket run requires a one-time owner approval id")
        if trace_lookup is not True:
            raise ValueError("live GoldTicket run requires trace lookup")
        pricing_failure = _local_llm_pricing_preflight_failure()
        if pricing_failure is not None:
            raise ValueError(
                "live GoldTicket pricing preflight failed: " + pricing_failure
            )

    eval_run_id = f"gold-ticket-{uuid4()}"
    headers = _auth_headers("API_AUTH_TOKEN")
    headers["X-Eval-Run-Id"] = eval_run_id
    headers[eval_cache_bypass.HEADER_BYPASS] = "1"
    cache_bypass_secret = headers.get("X-API-Key", "").strip()
    if strict_live and not cache_bypass_secret:
        raise ValueError("live GoldTicket run requires API_AUTH_TOKEN")

    trace_pool = await _open_trace_pool(trace_dsn) if trace_lookup else None
    if strict_live and trace_pool is None:
        raise RuntimeError("live GoldTicket run requires PostgreSQL trace access")

    reservation: LiveEvalCostReservation | None = None
    all_results: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    context_results: list[dict[str, Any]] = []
    budget_stopped = False
    pricing_stopped = False
    pricing_failure: str | None = None
    strict_cost_total = 0.0
    run_namespace = uuid4().hex[:12]
    started_at = datetime.now(UTC)
    signed_bypass_required = _requires_signed_cache_bypass(target)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=request_timeout,
            trust_env=False,
        ) as client:
            if strict_live or signed_bypass_required:
                await _verify_cache_bypass_runtime(
                    client=client,
                    target=target,
                    headers=headers,
                    expected_git_sha=expected_runtime_git_sha,
                    eval_run_id=eval_run_id,
                    cache_bypass_secret=cache_bypass_secret,
                )
            if strict_live:
                assert max_llm_cost_rub is not None
                reservation = reserve_live_eval_cost(
                    scope="gold-ticket-full-run",
                    run_id=eval_run_id,
                    runtime_git_sha=_cost_governance_runtime_git_sha(
                        explicit_sha=expected_runtime_git_sha,
                        evaluation_runtime_git_sha=None,
                    ),
                    manifest_sha256=_file_sha256(tickets_path),
                    case_count=calls_total,
                    approved_cap_rub=max_llm_cost_rub,
                    private_full=True,
                    high_cost_approval_id=high_cost_approval_id,
                )
            call_ordinal = 0
            for ticket_ordinal, (ticket, plan) in enumerate(
                zip(tickets, plans, strict=True),
                start=1,
            ):
                user_id = f"gold-{run_namespace}-{ticket_ordinal:03d}"
                for replay_call in plan:
                    call_ordinal += 1
                    eval_case_id = f"gt-{ticket_ordinal:03d}-{call_ordinal:04d}"
                    result = await _run_replay_call(
                        client=client,
                        target=target,
                        headers=headers,
                        cache_bypass_secret=cache_bypass_secret,
                        trace_pool=trace_pool,
                        eval_case_id=eval_case_id,
                        ticket=ticket,
                        replay_call=replay_call,
                        user_id=user_id,
                    )
                    all_results.append(result)
                    if replay_call.scored:
                        observations.append(result)
                    else:
                        context_results.append(result)

                    if strict_live:
                        pricing_failure = _llm_cost_accounting_failure(result)
                        if pricing_failure is not None:
                            pricing_stopped = True
                            break
                        strict_cost_total += float(
                            result.get("llm_estimated_cost_rub") or 0.0
                        )
                        calls_remain = len(all_results) < calls_total
                        if strict_cost_total > max_llm_cost_rub or (
                            calls_remain and strict_cost_total >= max_llm_cost_rub
                        ):
                            budget_stopped = True
                            break
                if budget_stopped or pricing_stopped:
                    break
    finally:
        if trace_pool is not None:
            await trace_pool.close()

    completed_at = datetime.now(UTC)
    tickets_executed = len(
        {
            str(item.get("ticket_id_hash") or "")
            for item in observations
            if item.get("ticket_id_hash")
        }
    )
    run_complete = (
        len(all_results) == calls_total
        and len(observations) == scored_total
        and not budget_stopped
        and not pricing_stopped
    )
    http_complete = all(item.get("http_success") is True for item in all_results)
    trace_complete = (
        all(item.get("trace_found") is True for item in all_results)
        if trace_lookup
        else True
    )
    execution_ok = run_complete and http_complete and trace_complete
    llm_cost = round(
        sum(float(item.get("llm_estimated_cost_rub") or 0.0) for item in all_results),
        6,
    )
    private_report = {
        "schema_version": PRIVATE_REPORT_SCHEMA,
        "dataset_id": tickets[0].dataset_id,
        "eval_run_id": eval_run_id,
        "target": target,
        "expected_runtime_git_sha": expected_runtime_git_sha,
        "tickets_file_sha256": _file_sha256(tickets_path),
        "knowledge_snapshot": knowledge_snapshot,
        "run_window_utc": {
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
        },
        "tickets_total": len(tickets),
        "tickets_executed": tickets_executed,
        "calls_total": calls_total,
        "calls_executed": len(all_results),
        "evaluation_steps_total": scored_total,
        "observations_total": len(observations),
        "context_calls_total": calls_total - scored_total,
        "context_calls_executed": len(context_results),
        "run_complete": run_complete,
        "budget_stopped": budget_stopped,
        "pricing_stopped": pricing_stopped,
        "pricing_failure": pricing_failure,
        "llm_cost_rub": llm_cost,
        "cost_cap_rub": max_llm_cost_rub,
        "cost_reservation": reservation.path.name if reservation else None,
        "human_product_verdict": False,
        "observations": observations,
        "context_results": context_results,
    }
    safe_report = {
        "schema_version": SAFE_REPORT_SCHEMA,
        "status": "OK" if execution_ok else "STOP",
        "classification": "independent_holdout_execution_only",
        "human_product_verdict": False,
        "tickets_file_sha256": private_report["tickets_file_sha256"],
        "knowledge_snapshot_sha256": knowledge_snapshot["canonical_seed_sha256"],
        "expected_runtime_git_sha": expected_runtime_git_sha,
        "run_window_utc": private_report["run_window_utc"],
        "counts": {
            "tickets_total": len(tickets),
            "tickets_executed": tickets_executed,
            "calls_total": calls_total,
            "calls_executed": len(all_results),
            "evaluation_steps_total": scored_total,
            "observations_total": len(observations),
            "context_calls_total": calls_total - scored_total,
            "http_success": sum(
                item.get("http_success") is True for item in all_results
            ),
            "trace_found": sum(item.get("trace_found") is True for item in all_results),
            "cache_hits": sum(item.get("cache_hit") is True for item in all_results),
        },
        "run_complete": run_complete,
        "budget": {
            "max_rub": max_llm_cost_rub,
            "cost_rub": llm_cost,
            "stopped": budget_stopped,
            "exceeded": bool(
                max_llm_cost_rub is not None and llm_cost > max_llm_cost_rub
            ),
        },
        "pricing_stopped": pricing_stopped,
        "generator_model_counts": dict(
            sorted(
                Counter(
                    str(item.get("generator_model") or "unknown")
                    for item in all_results
                ).items()
            )
        ),
        "observed_action_counts": dict(
            sorted(
                Counter(
                    str(item.get("observed_behavior") or "unknown")
                    for item in observations
                ).items()
            )
        ),
        "next_gate": "human_product_verdict_and_stage_funnel",
    }
    await asyncio.to_thread(
        _write_report_pair,
        private_output_path,
        safe_output_path,
        private_report,
        safe_report,
        overwrite,
    )
    return safe_report


async def _run_replay_call(
    *,
    client: httpx.AsyncClient,
    target: str,
    headers: dict[str, str],
    cache_bypass_secret: str,
    trace_pool: asyncpg.Pool | None,
    eval_case_id: str,
    ticket: GoldTicketV1,
    replay_call: ReplayCall,
    user_id: str,
) -> dict[str, Any]:
    request_payload = {
        "user_id": user_id,
        "channel": "api",
        "text": replay_call.text,
    }
    request_headers = {**headers, "X-Eval-Case-Id": eval_case_id}
    if cache_bypass_secret:
        request_headers.update(
            eval_cache_bypass.build_signed_headers(
                cache_bypass_secret,
                method="POST",
                path=urlsplit(target).path or "/",
                eval_run_id=str(headers["X-Eval-Run-Id"]),
                eval_case_id=eval_case_id,
                payload_sha256=eval_cache_bypass.canonical_payload_sha256(
                    eval_cache_bypass.canonical_ask_payload(request_payload)
                ),
            )
        )
    started = perf_counter()
    request_id = ""
    try:
        response = await client.post(
            target,
            headers=request_headers,
            json=request_payload,
        )
        payload = response.json() if response.content else {}
        request_id = str(payload.get("request_id") or "")
        http_result = {
            "http_status": response.status_code,
            "request_id": request_id,
            "response": str(payload.get("response") or response.text),
            "latency_ms": int((perf_counter() - started) * 1000),
            "error": None if response.is_success else response.text[:500],
        }
    except Exception as exc:
        http_result = {
            "http_status": None,
            "request_id": request_id,
            "response": "",
            "latency_ms": int((perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }
    trace = (
        await _fetch_trace(trace_pool, request_id)
        if trace_pool is not None and request_id
        else None
    )
    case = {
        "id": eval_case_id,
        "ticket_id_hash": ticket.ticket_id_hash,
        "step_id": replay_call.step_id,
        "query": replay_call.text,
        "user_id": user_id,
        "channel": "api",
        "tags": ["gold_ticket:v1", "split:holdout", "measurement:full_ticket"],
    }
    result = score_case(case, http_result, trace)
    result["source_turn_index"] = replay_call.source_turn_index
    result["observation_kind"] = "scored" if replay_call.scored else "context"
    return result


async def _open_trace_pool(trace_dsn: str | None) -> asyncpg.Pool | None:
    for candidate in _trace_dsn_candidates(trace_dsn):
        try:
            return await asyncpg.create_pool(candidate, min_size=1, max_size=1)
        except Exception:
            continue
    return None


def _valid_target(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"} and port is not None
    internal = parsed.hostname == "app-ml" and port == 8000
    return (
        parsed.scheme == "http"
        and (loopback or internal)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/ask"
        and not parsed.query
        and not parsed.fragment
    )


def _write_report_pair(
    private_path: Path,
    safe_path: Path,
    private_report: dict[str, Any],
    safe_report: dict[str, Any],
    overwrite: bool,
) -> None:
    for path in (private_path, safe_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    with private_path.open(mode, encoding="utf-8", newline="\n") as handle:
        json.dump(private_report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    try:
        with safe_path.open(mode, encoding="utf-8", newline="\n") as handle:
            json.dump(safe_report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    except Exception:
        if not overwrite:
            private_path.unlink(missing_ok=True)
        raise


def _validate_output_paths(
    private_path: Path,
    safe_path: Path,
    overwrite: bool,
) -> None:
    if private_path.resolve() == safe_path.resolve():
        raise ValueError("private and safe output paths must be distinct")
    if not overwrite and (private_path.exists() or safe_path.exists()):
        raise ValueError("GoldTicket run output already exists")


def _private_cli_path(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(PRIVATE_DATA_ROOT)
    except ValueError as exc:
        raise ValueError(f"{label} must stay under data/private") from exc
    if resolved.is_symlink():
        raise ValueError(f"{label} cannot be a symlink")
    return resolved


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run sealed GoldTicket holdout dialogues against a local /ask runtime."
    )
    parser.add_argument("--tickets", required=True, type=Path)
    parser.add_argument("--private-output", required=True, type=Path)
    parser.add_argument("--safe-output", required=True, type=Path)
    parser.add_argument("--target", default="http://localhost:8001/ask")
    parser.add_argument("--kb-seed", type=Path, default=DEFAULT_KB_SEED)
    parser.add_argument("--expected-runtime-git-sha", required=True)
    parser.add_argument("--request-timeout", type=float, default=180.0)
    parser.add_argument("--trace-dsn", default=None)
    parser.add_argument("--max-llm-cost-rub", type=float, default=200.0)
    parser.add_argument("--high-cost-approval-id", required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    tickets_path = _private_cli_path(args.tickets, label="GoldTicket input")
    private_output = _private_cli_path(args.private_output, label="private output")
    safe_output = _private_cli_path(args.safe_output, label="safe output")
    safe_report = asyncio.run(
        run_gold_ticket_eval(
            tickets_path=tickets_path,
            private_output_path=private_output,
            safe_output_path=safe_output,
            target=args.target,
            kb_seed_path=args.kb_seed,
            expected_runtime_git_sha=args.expected_runtime_git_sha,
            request_timeout=args.request_timeout,
            trace_dsn=args.trace_dsn,
            max_llm_cost_rub=args.max_llm_cost_rub,
            high_cost_approval_id=args.high_cost_approval_id,
            overwrite=args.overwrite,
        )
    )
    print(json.dumps(safe_report, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
