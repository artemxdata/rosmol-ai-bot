from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.cost_governance import reserve_live_eval_cost  # noqa: E402
from eval.run_ask import (  # noqa: E402
    ROUTINE_LIVE_EVAL_MAX_CASES,
    _auth_headers,
    _collect_trace_chunk_ids,
    _cost_governance_runtime_git_sha,
    _guard_large_live_run_budget,
    _is_in_process_mock_transport,
    _json_safe,
    _llm_cost_accounting_failure,
    _local_llm_pricing_preflight_failure,
    _safe_response_json,
    _trace_dsn_candidates,
)


def normalize_manual_case(raw: str | dict[str, Any], index: int) -> dict[str, Any]:
    if isinstance(raw, str):
        query = raw.strip()
        case_id = f"manual-{index}"
        tags: list[str] = []
    elif isinstance(raw, dict):
        query = str(raw.get("query") or raw.get("question") or raw.get("text") or "").strip()
        case_id = str(raw.get("id") or raw.get("case_id") or f"manual-{index}")
        tags = _string_list(raw.get("tags") or [])
    else:
        raise ValueError("manual case must be a string or object")

    if not query:
        raise ValueError(f"manual case {index} has empty query")
    return {"id": case_id, "query": query, "tags": tags}


async def run_manual_ask(
    cases: list[dict[str, Any]],
    *,
    target: str = "http://localhost:8001/ask",
    user_id: str = "manual-local",
    channel: str = "api",
    request_timeout: float = 180.0,
    concurrency: int = 1,
    api_key_env: str | None = "API_AUTH_TOKEN",
    trace_lookup: bool = True,
    trace_dsn: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    bypass_cache: bool = False,
    isolate_users: bool = False,
    max_llm_cost_rub: float | None = None,
    high_cost_approval_id: str | None = None,
) -> dict[str, Any]:
    is_live_transport = not _is_in_process_mock_transport(transport)
    approval_id = _guard_large_live_run_budget(
        cases=cases,
        target=target,
        transport=transport,
        max_llm_cost_rub=max_llm_cost_rub,
        require_budget=True,
        large_run_threshold=ROUTINE_LIVE_EVAL_MAX_CASES,
        trace_lookup=trace_lookup,
        private_contract_run=False,
        high_cost_approval_id=high_cost_approval_id,
    )
    if is_live_transport:
        pricing_failure = _local_llm_pricing_preflight_failure()
        if pricing_failure is not None:
            raise ValueError(
                "Live manual ask pricing preflight failed: " + pricing_failure
            )

    eval_run_id = f"manual-ask-{uuid4()}"
    trace_pool: asyncpg.Pool | None = None
    trace_lookup_error: str | None = None
    if trace_lookup:
        errors: list[str] = []
        for candidate in _trace_dsn_candidates(trace_dsn):
            try:
                trace_pool = await asyncpg.create_pool(
                    candidate,
                    min_size=1,
                    max_size=1 if is_live_transport else max(1, min(concurrency, 5)),
                )
                break
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        if trace_pool is None and errors:
            trace_lookup_error = "; ".join(errors)
    if is_live_transport and trace_pool is None:
        raise RuntimeError(
            "Live manual ask cost enforcement requires an available PostgreSQL trace DB"
        )

    cost_reservation = None
    if is_live_transport:
        assert max_llm_cost_rub is not None
        try:
            cost_reservation = reserve_live_eval_cost(
                scope="manual-ask",
                run_id=eval_run_id,
                runtime_git_sha=_cost_governance_runtime_git_sha(
                    explicit_sha=None,
                    evaluation_runtime_git_sha=None,
                ),
                manifest_sha256=_manual_cases_sha256(cases),
                case_count=len(cases),
                approved_cap_rub=max_llm_cost_rub,
                private_full=False,
                high_cost_approval_id=approval_id,
            )
        except ValueError:
            if trace_pool is not None:
                await trace_pool.close()
            raise

    headers = _auth_headers(api_key_env)
    if bypass_cache:
        headers["X-Bypass-Cache"] = "1"
    results: list[dict[str, Any]] = []
    budget_stopped = False
    pricing_stopped = False
    pricing_failure: str | None = None
    strict_cost_total = 0.0
    try:
        async with httpx.AsyncClient(transport=transport, timeout=request_timeout) as client:
            if is_live_transport:
                assert max_llm_cost_rub is not None
                sequential_semaphore = asyncio.Semaphore(1)
                for index, case in enumerate(cases, start=1):
                    result = await _run_manual_case(
                        client=client,
                        target=target,
                        headers=headers,
                        eval_run_id=eval_run_id,
                        case=case,
                        user_id=f"{user_id}-{index}" if isolate_users else user_id,
                        channel=channel,
                        semaphore=sequential_semaphore,
                        trace_pool=trace_pool,
                    )
                    results.append(result)
                    pricing_failure = _llm_cost_accounting_failure(result)
                    if pricing_failure is not None:
                        pricing_stopped = True
                        break
                    strict_cost_total += float(
                        result.get("llm_estimated_cost_rub") or 0.0
                    )
                    cases_remain = index < len(cases)
                    if strict_cost_total > max_llm_cost_rub or (
                        cases_remain and strict_cost_total >= max_llm_cost_rub
                    ):
                        budget_stopped = True
                        break
            else:
                semaphore = asyncio.Semaphore(max(1, concurrency))
                results = await asyncio.gather(
                    *[
                        _run_manual_case(
                            client=client,
                            target=target,
                            headers=headers,
                            eval_run_id=eval_run_id,
                            case=case,
                            user_id=f"{user_id}-{index}" if isolate_users else user_id,
                            channel=channel,
                            semaphore=semaphore,
                            trace_pool=trace_pool,
                        )
                        for index, case in enumerate(cases, start=1)
                    ]
                )
    finally:
        if trace_pool:
            await trace_pool.close()

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "eval_run_id": eval_run_id,
        "target": target,
        "high_cost_approval_id": approval_id,
        "cost_reservation": (
            cost_reservation.path.name if cost_reservation is not None else None
        ),
        "bypass_cache": bypass_cache,
        "isolate_users": isolate_users,
        "cases_requested_total": len(cases),
        "cases_total": len(results),
        "http_success_count": sum(1 for item in results if item.get("http_success")),
        "trace_found_count": sum(1 for item in results if item.get("trace_found")),
        "escalated_count": sum(1 for item in results if item.get("was_escalated") is True),
        "cache_hit_count": sum(1 for item in results if item.get("cache_hit") is True),
        "verdict_counts": _count_values(results, "quality_verdict"),
        "llm_total_tokens": sum(int(item.get("llm_total_tokens") or 0) for item in results),
        "llm_estimated_cost_rub": round(
            sum(float(item.get("llm_estimated_cost_rub") or 0.0) for item in results),
            6,
        ),
        "llm_budget_rub": max_llm_cost_rub,
        "llm_budget_stopped": budget_stopped,
        "llm_pricing_stopped": pricing_stopped,
        "llm_pricing_failure": pricing_failure,
        "results": results,
    }
    if trace_lookup_error:
        report["trace_lookup_error"] = trace_lookup_error
    return report


def build_manual_report_item(
    case: dict[str, Any],
    http_result: dict[str, Any],
    trace: dict[str, Any] | None,
) -> dict[str, Any]:
    trace = trace or {}
    observed_chunk_ids = sorted(_collect_trace_chunk_ids(trace)) if trace else []
    verdict, review_hint = _quality_verdict(http_result, trace)
    return {
        "id": case["id"],
        "query": case["query"],
        "tags": case.get("tags", []),
        "request_id": http_result.get("request_id"),
        "http_status": http_result.get("http_status"),
        "http_success": http_result.get("http_success"),
        "latency_ms": http_result.get("latency_ms"),
        "quality_verdict": verdict,
        "review_hint": review_hint,
        "response": http_result.get("response") or "",
        "error": http_result.get("error") or trace.get("error"),
        "trace_found": bool(trace),
        "message_masked": trace.get("message_masked"),
        "routing_hint": trace.get("routing_hint"),
        "query_analysis": trace.get("query_analysis"),
        "metadata_filter": trace.get("metadata_filter"),
        "observed_chunk_ids": observed_chunk_ids,
        "retrieved_chunks": _top_chunks(trace.get("retrieved_chunks") or []),
        "reranked_chunks": _top_chunks(trace.get("reranker_scores") or []),
        "max_reranker_score": trace.get("max_reranker_score"),
        "cache_hit": trace.get("cache_hit"),
        "generator_model": trace.get("generator_model"),
        "cited_sources": trace.get("cited_sources") or [],
        "was_escalated": trace.get("was_escalated"),
        "escalation_reason": trace.get("escalation_reason"),
        "verifier_result": trace.get("verifier_result"),
        "trace_total_latency_ms": trace.get("total_latency_ms"),
        "trace_events": trace.get("trace_events") or [],
        "llm_usage": trace.get("llm_usage") or [],
        "llm_prompt_tokens": trace.get("llm_prompt_tokens") or 0,
        "llm_completion_tokens": trace.get("llm_completion_tokens") or 0,
        "llm_total_tokens": trace.get("llm_total_tokens") or 0,
        "llm_estimated_cost_rub": trace.get("llm_estimated_cost_rub") or 0.0,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        "Manual Ask Inspection",
        f"Target: {report.get('target')}",
        f"Bypass cache: {report.get('bypass_cache')}",
        f"Isolate users: {report.get('isolate_users')}",
        f"Cases: {report.get('cases_total')}",
        f"HTTP OK: {report.get('http_success_count')}",
        f"Trace found: {report.get('trace_found_count')}",
        f"Escalated: {report.get('escalated_count')}",
        f"Cache hits: {report.get('cache_hit_count')}",
        f"Verdicts: {json.dumps(report.get('verdict_counts') or {}, ensure_ascii=False)}",
        f"LLM tokens: {report.get('llm_total_tokens')}",
        f"Estimated cost, RUB: {report.get('llm_estimated_cost_rub')}",
        f"LLM budget, RUB: {report.get('llm_budget_rub')}",
        f"Budget stopped: {report.get('llm_budget_stopped')}",
        f"Pricing stopped: {report.get('llm_pricing_stopped')}",
    ]
    if report.get("trace_lookup_error"):
        lines.append(f"Trace lookup error: {report['trace_lookup_error']}")
    for index, item in enumerate(report.get("results") or [], start=1):
        lines.extend(["", format_report_item(item, index=index)])
    return "\n".join(lines)


def format_report_item(item: dict[str, Any], *, index: int = 1) -> str:
    lines = [
        "=" * 80,
        f"[{index}] {item.get('id')}",
        f"Question: {item.get('query')}",
        "HTTP: "
        f"{item.get('http_status')} "
        f"success={item.get('http_success')} "
        f"latency_ms={item.get('latency_ms')} "
        f"request_id={item.get('request_id')}",
        "Trace: "
        f"found={item.get('trace_found')} "
        f"graph_latency_ms={item.get('trace_total_latency_ms')} "
        f"cache_hit={item.get('cache_hit')}",
        "Route: "
        f"generator_model={item.get('generator_model')} "
        f"escalated={item.get('was_escalated')} "
        f"reason={item.get('escalation_reason') or '-'}",
        f"Max reranker score: {item.get('max_reranker_score')}",
        f"Cited sources: {_join_short(item.get('cited_sources') or [])}",
        f"Observed chunks: {_join_short(item.get('observed_chunk_ids') or [])}",
        f"Quality verdict: {item.get('quality_verdict') or '-'}",
    ]
    if item.get("review_hint"):
        lines.append(f"Review hint: {item['review_hint']}")
    if item.get("message_masked"):
        lines.append(f"Masked text: {item['message_masked']}")
    if item.get("error"):
        lines.append(f"Error: {item['error']}")

    lines.extend(["", "Response:", str(item.get("response") or "").strip() or "-"])
    lines.extend(_format_chunks("Top reranked chunks", item.get("reranked_chunks") or []))
    lines.extend(_format_chunks("Top retrieved chunks", item.get("retrieved_chunks") or []))
    lines.extend(_format_llm_usage(item.get("llm_usage") or []))
    lines.extend(_format_trace_events(item.get("trace_events") or []))
    return "\n".join(lines)


async def _run_manual_case(
    *,
    client: httpx.AsyncClient,
    target: str,
    headers: dict[str, str],
    eval_run_id: str,
    case: dict[str, Any],
    user_id: str,
    channel: str,
    semaphore: asyncio.Semaphore,
    trace_pool: asyncpg.Pool | None,
) -> dict[str, Any]:
    async with semaphore:
        started_at = perf_counter()
        request_id: str | None = None
        try:
            request_headers = {
                **headers,
                "X-Eval-Run-Id": eval_run_id,
                "X-Eval-Case-Id": str(case["id"]),
            }
            response = await client.post(
                target,
                headers=request_headers,
                json={"user_id": user_id, "channel": channel, "text": case["query"]},
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
                "http_success": response.is_success,
                "request_id": request_id,
                "response": response_text,
                "latency_ms": latency_ms,
                "error": None if response.is_success else response.text[:500],
            }
        except Exception as exc:
            http_result = {
                "http_status": None,
                "http_success": False,
                "request_id": request_id,
                "response": "",
                "latency_ms": int((perf_counter() - started_at) * 1000),
                "error": f"{type(exc).__name__}: {exc}",
            }

        trace: dict[str, Any] | None = None
        if trace_pool and request_id:
            trace = await fetch_manual_trace(trace_pool, request_id)
        return build_manual_report_item(case, http_result, trace)


async def fetch_manual_trace(pool: asyncpg.Pool, request_id: str) -> dict[str, Any] | None:
    try:
        request_uuid = UUID(request_id)
    except ValueError:
        return None

    row = await pool.fetchrow(
        """
        SELECT
            message_masked, routing_hint, query_analysis, metadata_filter,
            retrieved_chunks, reranker_scores, max_reranker_score,
            cache_hit, generator_model, cited_sources, verifier_result,
            was_escalated, escalation_reason, llm_usage, llm_prompt_tokens,
            llm_completion_tokens, llm_total_tokens, llm_estimated_cost_rub,
            total_latency_ms, trace_events, error
        FROM request_traces
        WHERE request_id = $1
        """,
        request_uuid,
    )
    if not row:
        return None
    return {key: _json_safe(row[key]) for key in row.keys()}


async def _load_cases_from_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    raw_cases: list[str | dict[str, Any]] = []
    raw_cases.extend(args.text or [])
    if args.file:
        raw = await asyncio.to_thread(_read_json, Path(args.file))
        if not isinstance(raw, list):
            raise ValueError("manual ask file must be a JSON array")
        raw_cases.extend(raw)
    if not raw_cases:
        raise ValueError("pass --text or --file")
    cases = [normalize_manual_case(raw, index) for index, raw in enumerate(raw_cases, start=1)]
    if args.max_cases is None:
        return cases
    if args.max_cases < 1:
        raise ValueError("--max-cases must be greater than zero")
    return cases[: args.max_cases]


def _top_chunks(chunks: list[Any], limit: int = 5) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in chunks[:limit]:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        normalized.append(
            {
                "chunk_id": item.get("chunk_id") or metadata.get("chunk_id"),
                "score": item.get("score"),
                "reranker_score": item.get("reranker_score"),
                "forum_normalized": metadata.get("forum_normalized"),
                "category": metadata.get("category"),
                "status": metadata.get("status"),
                "text": _preview(str(item.get("text") or ""), limit=260),
            }
        )
    return normalized


def _format_chunks(title: str, chunks: list[dict[str, Any]]) -> list[str]:
    if not chunks:
        return ["", f"{title}: -"]
    lines = ["", f"{title}:"]
    for index, chunk in enumerate(chunks, start=1):
        lines.append(
            "  "
            f"{index}. {chunk.get('chunk_id')} "
            f"score={chunk.get('score')} "
            f"reranker={chunk.get('reranker_score')} "
            f"forum={chunk.get('forum_normalized') or '-'} "
            f"category={chunk.get('category') or '-'}"
        )
        if chunk.get("text"):
            lines.append(f"     {chunk['text']}")
    return lines


def _format_llm_usage(events: list[dict[str, Any]]) -> list[str]:
    if not events:
        return ["", "LLM usage: -"]
    lines = ["", "LLM usage:"]
    for event in events:
        lines.append(
            "  "
            f"node={event.get('node') or '-'} "
            f"model={event.get('model') or '-'} "
            f"prompt={event.get('prompt_tokens') or 0} "
            f"completion={event.get('completion_tokens') or 0} "
            f"total={event.get('total_tokens') or 0} "
            f"cost_rub={event.get('estimated_cost_rub') or 0}"
        )
    return lines


def _format_trace_events(events: list[dict[str, Any]]) -> list[str]:
    if not events:
        return ["", "Graph events: -"]
    lines = ["", "Graph events:"]
    for event in events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        details = _compact_metadata(metadata)
        line = (
            "  "
            f"{event.get('node') or '-'} "
            f"latency_ms={event.get('latency_ms')} "
            f"error={event.get('error') or '-'}"
        )
        if details:
            line += f" metadata={details}"
        lines.append(line)
    return lines


def _quality_verdict(
    http_result: dict[str, Any],
    trace: dict[str, Any],
) -> tuple[str, str]:
    if not http_result.get("http_success"):
        return "http_error", "Проверить доступность /ask, Docker/app и текст HTTP-ошибки."
    if not trace:
        return (
            "answer_without_trace",
            "Ответ получен, но trace не найден; включите trace lookup для проверки RAG-пути.",
        )
    if trace.get("error"):
        return "trace_error", "В trace есть ошибка узла графа; смотреть поле Error и Graph events."
    if trace.get("was_escalated") is True:
        reason = str(trace.get("escalation_reason") or "needs_operator")
        return f"controlled_escalation:{reason}", _escalation_review_hint(reason)
    if trace.get("cache_hit") is True:
        return (
            "cache_hit_answer",
            "Ответ пришёл из semantic cache; для просмотра полного RAG-пути измените формулировку.",
        )

    model = str(trace.get("generator_model") or "")
    observed_chunks = _collect_trace_chunk_ids(trace)
    if model == "source_chunk":
        return (
            "deterministic_source_answer",
            "Проверить, что выбранный top chunk действительно полностью отвечает на вопрос.",
        )
    if model and observed_chunks:
        return (
            "llm_grounded_answer",
            "Проверить полноту ответа и соответствие cited/retrieved chunks всем аспектам вопроса.",
        )
    if observed_chunks:
        return (
            "rag_signals_without_route",
            "Есть найденные chunks, но маршрут ответа неочевиден; "
            "смотреть generator_model и events.",
        )
    return "no_rag_signals", "Нет видимых RAG-сигналов в trace; проверить retrieve/rerank/cache."


def _escalation_review_hint(reason: str) -> str:
    hints = {
        "partial_source_coverage": (
            "Хорошая эскалация, если источники покрыли только часть составного вопроса."
        ),
        "ambiguous_forum_context": (
            "Хорошая эскалация, если вопрос смешивает условия разных форумов без уточнения."
        ),
        "insufficient_sources": (
            "Проверить, действительно ли в KB нет подтверждённого ответа на этот аспект."
        ),
        "low_confidence": "Проверить expected chunks и пороги reranker на golden set.",
        "no_relevant_chunks": "Это KB/retrieval gap: нужен chunk, metadata или новая формулировка.",
        "ml_dependency_missing": "ML runtime не поднят; для live RAG нужен app-ml/INSTALL_ML=true.",
    }
    return hints.get(reason, "Проверить, что эскалация безопаснее ответа без источников.")


def _compact_metadata(metadata: dict[str, Any]) -> str:
    allowed = [
        "model",
        "complexity",
        "chunk_count",
        "top_score",
        "max_confidence",
        "escalation_reason",
        "needs_clarification",
        "cache_hit",
    ]
    compact = {key: metadata[key] for key in allowed if key in metadata}
    if not compact:
        return ""
    return json.dumps(compact, ensure_ascii=False, sort_keys=True)


def _count_values(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _join_short(values: list[Any], *, limit: int = 8) -> str:
    if not values:
        return "-"
    rendered = [str(item) for item in values[:limit]]
    if len(values) > limit:
        rendered.append(f"...+{len(values) - limit}")
    return ", ".join(rendered)


def _preview(text: str, *, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _manual_cases_sha256(cases: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        cases,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    _configure_stdout()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--text",
        action="append",
        help="Question text. Can be passed multiple times.",
    )
    parser.add_argument(
        "--file",
        default="",
        help="JSON array with strings or objects containing query/text.",
    )
    parser.add_argument("--target", default="http://localhost:8001/ask")
    parser.add_argument("--user-id", default="manual-local")
    parser.add_argument("--channel", default="api")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--api-key-env", default="API_AUTH_TOKEN")
    parser.add_argument("--no-db-traces", action="store_true")
    parser.add_argument("--trace-dsn", default="")
    parser.add_argument("--output", default="reports/manual_ask.json")
    parser.add_argument("--no-output", action="store_true")
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--max-llm-cost-rub", type=float, default=None)
    parser.add_argument("--high-cost-approval-id", default=None)
    parser.add_argument(
        "--bypass-cache",
        action="store_true",
        help="Send X-Bypass-Cache=1 so local /ask executes the full RAG path.",
    )
    parser.add_argument(
        "--isolate-users",
        action="store_true",
        help="Use a unique user_id per case to avoid Redis session context leakage.",
    )
    args = parser.parse_args()

    async def _run() -> dict[str, Any]:
        cases = await _load_cases_from_args(args)
        report = await run_manual_ask(
            cases,
            target=args.target,
            user_id=args.user_id,
            channel=args.channel,
            request_timeout=args.timeout,
            concurrency=args.concurrency,
            api_key_env=args.api_key_env,
            trace_lookup=not args.no_db_traces,
            trace_dsn=args.trace_dsn or None,
            bypass_cache=args.bypass_cache,
            isolate_users=args.isolate_users,
            max_llm_cost_rub=args.max_llm_cost_rub,
            high_cost_approval_id=args.high_cost_approval_id,
        )
        if not args.no_output:
            await asyncio.to_thread(_write_json, Path(args.output), report)
        return report

    report = asyncio.run(_run())
    print(format_report(report))
    if report["llm_budget_stopped"] or report["llm_pricing_stopped"]:
        raise SystemExit(2)


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    main()
