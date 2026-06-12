from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID

import asyncpg
import httpx

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import get_settings


def _normalize_case(raw: dict[str, Any]) -> dict[str, Any]:
    query = raw.get("query") or raw.get("question") or raw.get("text")
    if not query:
        raise ValueError("ask eval case must contain query, question, or text")

    expected_chunk_ids = _string_list(
        raw.get("expected_chunk_ids")
        or raw.get("expected_chunks")
        or raw.get("relevant_chunk_ids")
        or []
    )
    expected_answer_contains = _string_list(
        raw.get("expected_answer_contains") or raw.get("answer_contains") or []
    )

    return {
        "id": str(raw.get("id") or raw.get("case_id") or query),
        "query": str(query),
        "user_id": str(raw.get("user_id") or "ask-eval"),
        "channel": str(raw.get("channel") or "api"),
        "expected_chunk_ids": expected_chunk_ids,
        "expected_answer_contains": expected_answer_contains,
        "expected_escalated": raw.get("expected_escalated"),
        "expected_escalation_reason": raw.get("expected_escalation_reason"),
        "expected_generator_model": raw.get("expected_generator_model"),
        "tags": _string_list(raw.get("tags") or []),
    }


def build_seed_ask_cases(
    records: list[dict[str, Any]],
    max_cases: int = 50,
    user_prefix: str = "ask-eval",
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for record in records:
        if record.get("status") != "published":
            continue
        query = _seed_smoke_query(record)
        if not query:
            continue
        chunk_id = str(record["chunk_id"])
        cases.append(
            {
                "id": f"seed_smoke::{chunk_id}",
                "query": query,
                "user_id": f"{user_prefix}-{len(cases) + 1}",
                "channel": "api",
                "expected_chunk_ids": [chunk_id],
                "expected_answer_contains": [],
                "expected_escalated": None,
                "expected_escalation_reason": None,
                "expected_generator_model": None,
                "tags": ["seed_smoke"],
            }
        )
        if len(cases) >= max_cases:
            break
    return cases


async def run_eval(
    cases_path: Path,
    output_path: Path,
    target: str = "http://localhost:8001/ask",
    *,
    concurrency: int = 1,
    request_timeout: float = 120.0,
    api_key_env: str | None = "API_AUTH_TOKEN",
    trace_lookup: bool = True,
    kb_seed_path: Path = Path("data/knowledge_base_seed.json"),
    auto_smoke_cases: bool = False,
    max_smoke_cases: int = 50,
    markdown_path: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    cases, generated_smoke_cases = await _load_cases(
        cases_path=cases_path,
        kb_seed_path=kb_seed_path,
        auto_smoke_cases=auto_smoke_cases,
        max_smoke_cases=max_smoke_cases,
    )
    if not cases:
        metrics = _empty_metrics(target=target, cases_path=cases_path, auto_smoke_cases=False)
        await asyncio.to_thread(_write_json, output_path, metrics)
        if markdown_path:
            await asyncio.to_thread(_write_markdown, markdown_path, metrics)
        return metrics

    trace_pool: asyncpg.Pool | None = None
    trace_lookup_error: str | None = None
    if trace_lookup:
        try:
            settings = get_settings()
            trace_pool = await asyncpg.create_pool(
                settings.postgres_dsn,
                min_size=1,
                max_size=max(1, min(concurrency, 5)),
            )
        except Exception as exc:
            trace_lookup_error = f"{type(exc).__name__}: {exc}"

    headers = _auth_headers(api_key_env)
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
    answer_scored = [item for item in results if item.get("expected_answer_contains")]
    trace_scored = [item for item in results if item.get("trace_found")]
    usage_events = [event for item in results for event in item.get("llm_usage", [])]

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
        "answer_contains_rate": _bool_rate(answer_scored, "answer_contains_match"),
        "trace_coverage_rate": len(trace_scored) / len(results) if results else None,
        "escalation_rate": _bool_rate(trace_scored, "was_escalated"),
        "cache_hit_rate": _bool_rate(trace_scored, "cache_hit"),
        "source_chunk_rate": _value_rate(trace_scored, "generator_model", "source_chunk"),
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
    expected_answer_contains = case.get("expected_answer_contains") or []
    expected_escalated = case.get("expected_escalated")
    expected_escalation_reason = case.get("expected_escalation_reason")
    expected_generator_model = case.get("expected_generator_model")

    checks: dict[str, bool | None] = {}
    if expected_chunk_ids:
        checks["expected_chunk_hit"] = bool(set(expected_chunk_ids) & observed_chunk_ids)
    if expected_answer_contains:
        normalized_response = response_text.lower()
        checks["answer_contains_match"] = all(
            expected.lower() in normalized_response for expected in expected_answer_contains
        )
    if expected_escalated is not None:
        checks["escalation_match"] = (
            bool(trace.get("was_escalated")) == bool(expected_escalated)
            if trace
            else None
        )
    if expected_escalation_reason:
        checks["escalation_reason_match"] = (
            trace.get("escalation_reason") == expected_escalation_reason if trace else None
        )
    if expected_generator_model:
        checks["generator_model_match"] = (
            trace.get("generator_model") == expected_generator_model if trace else None
        )

    passed = http_success and all(value is True for value in checks.values())
    if not checks:
        passed = http_success

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
        "observed_chunk_ids": sorted(observed_chunk_ids),
        "expected_chunk_hit": checks.get("expected_chunk_hit"),
        "expected_answer_contains": expected_answer_contains,
        "answer_contains_match": checks.get("answer_contains_match"),
        "expected_escalated": expected_escalated,
        "was_escalated": trace.get("was_escalated"),
        "escalation_match": checks.get("escalation_match"),
        "expected_escalation_reason": expected_escalation_reason,
        "escalation_reason": trace.get("escalation_reason"),
        "escalation_reason_match": checks.get("escalation_reason_match"),
        "expected_generator_model": expected_generator_model,
        "generator_model": trace.get("generator_model"),
        "generator_model_match": checks.get("generator_model_match"),
        "cache_hit": trace.get("cache_hit"),
        "max_reranker_score": trace.get("max_reranker_score"),
        "trace_total_latency_ms": trace.get("total_latency_ms"),
        "llm_usage": trace.get("llm_usage") or [],
        "llm_prompt_tokens": trace.get("llm_prompt_tokens") or 0,
        "llm_completion_tokens": trace.get("llm_completion_tokens") or 0,
        "llm_total_tokens": trace.get("llm_total_tokens") or 0,
        "llm_estimated_cost_rub": trace.get("llm_estimated_cost_rub") or 0.0,
        "passed": passed,
    }


async def _load_cases(
    *,
    cases_path: Path,
    kb_seed_path: Path,
    auto_smoke_cases: bool,
    max_smoke_cases: int,
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

    cases = [_normalize_case(item) for item in raw_cases]
    if not cases and auto_smoke_cases:
        records = await asyncio.to_thread(_read_json, kb_seed_path)
        if not isinstance(records, list):
            raise ValueError("KB seed must be a JSON array")
        cases = build_seed_ask_cases(records, max_cases=max_smoke_cases)
        generated_smoke_cases = True
    return cases, generated_smoke_cases


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


def _seed_smoke_query(record: dict[str, Any]) -> str:
    examples = record.get("intent_examples") or []
    if examples:
        prefix = record.get("forum_normalized") or record.get("source_category") or ""
        return " ".join(part for part in [str(prefix), str(examples[0])] if part).strip()
    intent = record.get("intent_name")
    if intent:
        prefix = record.get("forum_normalized") or record.get("source_category") or ""
        return " ".join(part for part in [str(prefix), str(intent)] if part).strip()
    return str(record.get("text_clean") or "")[:160]


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


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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
        f"- Escalation rate: `{_format_rate(metrics.get('escalation_rate'))}`",
        f"- Cache hit rate: `{_format_rate(metrics.get('cache_hit_rate'))}`",
        f"- LLM cost, RUB: `{metrics.get('llm_estimated_cost_rub')}`",
        "",
        "## Latency",
        "",
        "| Metric | HTTP ms | Trace ms |",
        "|---|---:|---:|",
    ]
    latency = metrics.get("latency_ms") or {}
    trace_latency = metrics.get("trace_total_latency_ms") or {}
    for key in ("avg", "p50", "p95", "max"):
        lines.append(f"| {key} | {latency.get(key)} | {trace_latency.get(key)} |")

    failed = [item for item in metrics.get("results", []) if not item.get("passed")]
    if failed:
        lines.extend(["", "## Failed Cases", ""])
        for item in failed[:20]:
            reason = item.get("error") or item.get("escalation_reason") or "quality check failed"
            lines.append(f"- `{item.get('id')}` status={item.get('http_status')} reason={reason}")

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


def _percentile(values: list[int], percentile: int) -> int | None:
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
    parser.add_argument("--auto-smoke-cases", action="store_true")
    parser.add_argument("--max-smoke-cases", type=int, default=50)
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
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
            kb_seed_path=Path(args.kb_seed),
            auto_smoke_cases=args.auto_smoke_cases,
            max_smoke_cases=args.max_smoke_cases,
            markdown_path=markdown_path,
        )
    )
    print(
        json.dumps(
            {key: value for key, value in metrics.items() if key != "results"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
