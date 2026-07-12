from __future__ import annotations

# ruff: noqa: E402,I001

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import get_settings
from src.session.memory import hash_user_id

DEFAULT_CASES = Path("data/private/operator_qa/analysis/operator_golden_calibration.json")
DEFAULT_OUTPUT = Path("data/private/operator_qa/analysis/calibration_bot_eval.json")
SCOPE_NOTE_MARKERS = (
    "я отвечаю на вопросы по мероприятиям",
    "задай, пожалуйста, вопрос по этим темам",
)
CLARIFY_MARKERS = (
    "уточни, пожалуйста",
    "уточните, пожалуйста",
    "о каком форуме",
    "каком мероприятии",
)


async def export_trace_eval(
    cases_path: Path = DEFAULT_CASES,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    since_minutes: int = 60,
    postgres_dsn: str | None = None,
    user_prefix: str | None = None,
) -> dict[str, Any]:
    cases = _read_json_array(cases_path)
    query_to_cases: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        query_to_cases.setdefault(_normalize(str(case.get("query") or "")), []).append(case)

    pool = await _create_trace_pool(postgres_dsn or get_settings().postgres_dsn)
    try:
        traces = await pool.fetch(
            """
            select timestamp, user_id_hash, message_masked, response_text, was_escalated,
                   escalation_reason, generator_model, cited_sources,
                   total_latency_ms, llm_total_tokens, llm_estimated_cost_rub
            from request_traces
            where channel = 'api'
              and timestamp >= now() - ($1::text || ' minutes')::interval
            order by timestamp desc
            """,
            str(max(1, since_minutes)),
        )
    finally:
        await pool.close()

    latest_by_query: dict[str, dict[str, Any]] = {}
    latest_by_user_hash: dict[str, dict[str, Any]] = {}
    for trace in traces:
        user_hash = str(trace["user_id_hash"] or "")
        if user_hash and user_hash not in latest_by_user_hash:
            latest_by_user_hash[user_hash] = dict(trace)
        key = _normalize(str(trace["message_masked"] or ""))
        if key in query_to_cases and key not in latest_by_query:
            latest_by_query[key] = dict(trace)

    results: list[dict[str, Any]] = []
    recovered_by_user_hash = 0
    recovered_by_query = 0
    for index, case in enumerate(cases, start=1):
        query = str(case.get("query") or "")
        trace = None
        if user_prefix:
            trace = latest_by_user_hash.get(
                expected_trace_user_hash(user_prefix, index)
            )
            if trace:
                recovered_by_user_hash += 1
        if not trace and not user_prefix:
            trace = latest_by_query.get(_normalize(query))
            if trace:
                recovered_by_query += 1
        if not trace:
            continue
        response = str(trace.get("response_text") or "")
        results.append(
            {
                "id": str(case.get("id") or query),
                "query": query,
                "response": response,
                "observed_behavior": observed_behavior(
                    response,
                    was_escalated=bool(trace.get("was_escalated")),
                ),
                "was_escalated": bool(trace.get("was_escalated")),
                "escalation_reason": trace.get("escalation_reason"),
                "generator_model": trace.get("generator_model"),
                "cited_sources": list(trace.get("cited_sources") or []),
                "trace_total_latency_ms": trace.get("total_latency_ms"),
                "llm_total_tokens": int(trace.get("llm_total_tokens") or 0),
                "llm_estimated_cost_rub": float(
                    trace.get("llm_estimated_cost_rub") or 0.0
                ),
                "trace_timestamp": trace.get("timestamp").isoformat()
                if trace.get("timestamp")
                else None,
            }
        )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cases_path": str(cases_path),
        "source": "request_traces_recovery",
        "user_prefix": user_prefix,
        "since_minutes": since_minutes,
        "cases_total": len(cases),
        "cases_recovered": len(results),
        "cases_missing": len(cases) - len(results),
        "recovered_by_user_hash": recovered_by_user_hash,
        "recovered_by_query": recovered_by_query,
        "observed_behavior_counts": dict(
            Counter(result["observed_behavior"] for result in results)
        ),
        "escalation_reason_counts": dict(
            Counter(str(result.get("escalation_reason") or "none") for result in results)
        ),
        "llm_estimated_cost_rub": round(
            sum(result["llm_estimated_cost_rub"] for result in results),
            6,
        ),
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        output_path.write_text,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in payload.items() if key != "results"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return payload


def expected_trace_user_hash(user_prefix: str, index: int) -> str:
    return hash_user_id("api", f"{user_prefix}-{index}")


def observed_behavior(response: str, *, was_escalated: bool) -> str:
    if was_escalated:
        return "escalate"
    normalized = _normalize(response)
    if any(marker in normalized for marker in SCOPE_NOTE_MARKERS):
        return "scope_note"
    if any(marker in normalized for marker in CLARIFY_MARKERS):
        return "clarify"
    return "answer"


async def _create_trace_pool(dsn: str) -> asyncpg.Pool:
    candidates = [dsn]
    local_dsn = dsn.replace("@postgres:", "@127.0.0.1:").replace(
        "//postgres:", "//127.0.0.1:"
    )
    if local_dsn != dsn:
        candidates.append(local_dsn)
    errors: list[Exception] = []
    for candidate in candidates:
        try:
            return await asyncpg.create_pool(candidate)
        except Exception as exc:
            errors.append(exc)
    raise RuntimeError("Could not connect to request trace PostgreSQL") from errors[-1]


def _normalize(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{path} must contain a JSON array of objects")
    return [dict(item) for item in value]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover operator golden eval results from PostgreSQL request traces."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--since-minutes", type=int, default=60)
    parser.add_argument("--postgres-dsn", default="")
    parser.add_argument("--user-prefix", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        export_trace_eval(
            args.cases,
            args.output,
            since_minutes=args.since_minutes,
            postgres_dsn=args.postgres_dsn or None,
            user_prefix=args.user_prefix or None,
        )
    )


if __name__ == "__main__":
    main()
