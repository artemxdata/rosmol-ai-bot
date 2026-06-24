from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import asyncpg

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import get_settings


def build_trace_failure_report(
    *,
    metrics: dict[str, Any],
    traces: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows = []
    for result in metrics.get("results") or []:
        failure_reasons = _string_list(result.get("failure_reasons"))
        if not failure_reasons:
            continue
        request_id = str(result.get("request_id") or "")
        trace = traces.get(request_id) or {}
        rows.append(_build_row(result, trace))

    loss_stage_counts = Counter(row["loss_stage"] for row in rows)
    analysis_category_counts = Counter(row["analysis"]["category"] for row in rows)
    analysis_forum_counts = Counter(row["analysis"]["forum_normalized"] for row in rows)
    return {
        "target": metrics.get("target"),
        "cases_path": metrics.get("cases_path"),
        "cases_total": metrics.get("cases_total"),
        "failed_cases": len(rows),
        "loss_stage_counts": dict(loss_stage_counts),
        "analysis_category_counts": dict(analysis_category_counts),
        "analysis_forum_counts": dict(analysis_forum_counts),
        "rows": rows,
    }


def write_markdown(report: dict[str, Any], output_path: Path) -> None:
    lines = [
        "# Trace Failure Audit",
        "",
        f"- Target: `{report['target']}`",
        f"- Cases: `{report['cases_path']}`",
        f"- Failed cases: `{report['failed_cases']}`",
        "",
        "## Loss Stages",
        "",
    ]
    lines.extend(_counter_lines(report["loss_stage_counts"]))
    lines.extend(["", "## Analysis Categories", ""])
    lines.extend(_counter_lines(report["analysis_category_counts"]))

    rows = report.get("rows") or []
    if rows:
        lines.extend(["", "## Rows", ""])
        for row in rows[:200]:
            lines.append(
                f"- `{row['loss_stage']}` case=`{row['case_id']}` "
                f"expected=`{', '.join(row['expected_chunk_ids'])}` "
                f"cited=`{', '.join(row['cited_source_ids'])}` "
                f"observed_rank=`{row['expected_observed_ranks']}` "
                f"reranked_rank=`{row['expected_reranked_ranks']}` "
                f"analysis_category=`{row['analysis']['category']}` "
                f"analysis_forum=`{row['analysis']['forum_normalized']}` "
                f"escalation=`{row['escalation_reason']}`"
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def fetch_traces(request_ids: list[str], dsn: str) -> dict[str, dict[str, Any]]:
    if not request_ids:
        return {}
    connection = await asyncpg.connect(dsn)
    try:
        records = await connection.fetch(
            """
            SELECT
                request_id::text,
                query_analysis,
                metadata_filter,
                retrieved_chunks,
                reranker_scores,
                cited_sources,
                was_escalated,
                escalation_reason,
                generator_model
            FROM request_traces
            WHERE request_id = ANY($1::uuid[])
            """,
            request_ids,
        )
    finally:
        await connection.close()
    return {record["request_id"]: dict(record) for record in records}


def _build_row(result: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    expected_ids = _string_list(
        result.get("expected_cited_chunk_ids") or result.get("expected_chunk_ids")
    )
    observed_ids = _string_list(result.get("observed_chunk_ids"))
    cited_ids = _string_list(result.get("cited_source_ids") or trace.get("cited_sources"))
    reranked_ids = _chunk_ids(trace.get("reranker_scores"))
    failure_reasons = _string_list(result.get("failure_reasons"))
    return {
        "case_id": str(result.get("id") or ""),
        "request_id": str(result.get("request_id") or ""),
        "failure_reasons": failure_reasons,
        "loss_stage": _loss_stage(
            expected_ids=expected_ids,
            observed_ids=observed_ids,
            reranked_ids=reranked_ids,
            cited_ids=cited_ids,
        ),
        "expected_chunk_ids": expected_ids,
        "cited_source_ids": cited_ids,
        "expected_observed_ranks": _ranks(expected_ids, observed_ids),
        "expected_reranked_ranks": _ranks(expected_ids, reranked_ids),
        "reranked_top_ids": reranked_ids[:8],
        "observed_count": len(observed_ids),
        "reranked_count": len(reranked_ids),
        "analysis": _safe_analysis(_jsonish(trace.get("query_analysis"))),
        "metadata_filter": _safe_filter(_jsonish(trace.get("metadata_filter"))),
        "generator_model": str(trace.get("generator_model") or result.get("generator_model") or ""),
        "was_escalated": bool(trace.get("was_escalated") or result.get("was_escalated")),
        "escalation_reason": str(
            trace.get("escalation_reason") or result.get("escalation_reason") or ""
        ),
    }


def _loss_stage(
    *,
    expected_ids: list[str],
    observed_ids: list[str],
    reranked_ids: list[str],
    cited_ids: list[str],
) -> str:
    if not expected_ids:
        return "no_expected_label"
    if any(chunk_id not in observed_ids for chunk_id in expected_ids):
        return "retrieval"
    if reranked_ids and any(chunk_id not in reranked_ids for chunk_id in expected_ids):
        return "rerank"
    if any(chunk_id not in cited_ids for chunk_id in expected_ids):
        return "generate_or_verify"
    return "other_failure"


def _safe_analysis(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    questions = value.get("questions") or []
    return {
        "category": str(value.get("category") or ""),
        "forum_normalized": str(value.get("forum_normalized") or ""),
        "complexity": str(value.get("complexity") or ""),
        "questions_count": len(questions) if isinstance(questions, list) else 0,
        "needs_clarification": bool(value.get("needs_clarification")),
        "should_escalate": bool(value.get("should_escalate")),
    }


def _safe_filter(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        value = {}
    return {
        key: str(value.get(key) or "")
        for key in ("category", "forum_normalized", "topic")
        if value.get(key)
    }


def _chunk_ids(value: Any) -> list[str]:
    items = _jsonish(value)
    if not isinstance(items, list):
        return []
    ids = []
    for item in items:
        if isinstance(item, dict) and item.get("chunk_id"):
            ids.append(str(item["chunk_id"]))
    return ids


def _ranks(expected_ids: list[str], candidate_ids: list[str]) -> dict[str, int | None]:
    rank_by_id = {chunk_id: index for index, chunk_id in enumerate(candidate_ids, start=1)}
    return {chunk_id: rank_by_id.get(chunk_id) for chunk_id in expected_ids}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item) for item in value if str(item).strip()]


def _jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if value is not None else {}


def _counter_lines(counter: dict[str, int]) -> list[str]:
    if not counter:
        return ["- none"]
    return [
        f"- `{key}`: `{value}`"
        for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"file must contain a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a safe trace audit for failed ask eval cases."
    )
    parser.add_argument("--metrics", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument("--postgres-dsn", default=None)
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    metrics = _read_json_object(args.metrics)
    request_ids = [
        str(result.get("request_id"))
        for result in metrics.get("results") or []
        if result.get("request_id") and result.get("failure_reasons")
    ]
    traces = await fetch_traces(
        request_ids,
        args.postgres_dsn or get_settings().postgres_dsn,
    )
    report = build_trace_failure_report(metrics=metrics, traces=traces)
    if args.output:
        _write_json(args.output, report)
    if args.markdown:
        write_markdown(report, args.markdown)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "rows"},
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
