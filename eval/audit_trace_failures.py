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

PIPELINE_LINEAGE_SCHEMA_VERSION = "question-pipeline-provenance-v1"
LINEAGE_STAGES = frozenset(
    {"retrieve", "rerank", "source_selection", "citation", "verify"}
)


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
    attribution_confidence_counts = Counter(
        row["attribution_confidence"] for row in rows
    )
    analysis_category_counts = Counter(row["analysis"]["category"] for row in rows)
    analysis_forum_counts = Counter(row["analysis"]["forum_normalized"] for row in rows)
    return {
        "target": metrics.get("target"),
        "cases_path": metrics.get("cases_path"),
        "cases_total": metrics.get("cases_total"),
        "failed_cases": len(rows),
        "loss_stage_counts": dict(loss_stage_counts),
        "attribution_confidence_counts": dict(attribution_confidence_counts),
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
                f"attribution=`{row['attribution_confidence']}` "
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
                cited_sources, trace_events,
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
    stage_available = _validated_lineage_stages(result)
    retrieved_available = stage_available["retrieve"]
    reranked_available = stage_available["rerank"]
    selected_available = stage_available["source_selection"]
    citation_available = stage_available["citation"]
    verify_available = stage_available["verify"]
    cited_ids = (
        _string_list(result.get("cited_source_ids") or trace.get("cited_sources"))
        if citation_available
        else []
    )
    retrieved_ids = _string_list(result.get("retrieved_chunk_ids"))
    if retrieved_available and not retrieved_ids and "retrieved_chunk_ids" not in result:
        retrieved_ids = _chunk_ids(trace.get("retrieved_chunks"))
    reranked_ids = _string_list(result.get("reranked_chunk_ids"))
    if reranked_available and not reranked_ids and "reranked_chunk_ids" not in result:
        reranked_ids = _chunk_ids(trace.get("reranker_scores"))
    generate_event = _trace_event_metadata(trace, "generate_selection")
    verify_event = _trace_event_metadata(trace, "verify_decision")
    selected_ids = (
        _string_list(
            result.get("selected_source_ids")
            or generate_event.get("selected_source_ids")
        )
        if selected_available
        else []
    )
    verification_ids = (
        _string_list(
            result.get("verification_source_ids")
            or verify_event.get("referenced_source_ids")
        )
        if verify_available
        else []
    )
    verification_decision = (
        str(result.get("verification_decision") or verify_event.get("decision") or "")
        if verify_available
        else ""
    )
    failure_reasons = _string_list(result.get("failure_reasons"))
    attribution_confidence = _attribution_confidence(
        retrieved_available=retrieved_available,
        reranked_available=reranked_available,
        selected_available=selected_available,
        citation_available=citation_available,
        verify_available=verify_available,
    )
    return {
        "case_id": str(result.get("id") or ""),
        "request_id": str(result.get("request_id") or ""),
        "failure_reasons": failure_reasons,
        "attribution_confidence": attribution_confidence,
        "loss_stage": _loss_stage(
            expected_ids=expected_ids,
            retrieved_ids=retrieved_ids,
            reranked_ids=reranked_ids,
            selected_ids=selected_ids,
            cited_ids=cited_ids,
            verification_decision=verification_decision,
            retrieved_available=retrieved_available,
            reranked_available=reranked_available,
            selected_available=selected_available,
            citation_available=citation_available,
            verify_available=verify_available,
        ),
        "expected_chunk_ids": expected_ids,
        "retrieved_chunk_ids": retrieved_ids,
        "selected_source_ids": selected_ids,
        "cited_source_ids": cited_ids,
        "expected_observed_ranks": _ranks(expected_ids, observed_ids),
        "expected_retrieved_ranks": _ranks(expected_ids, retrieved_ids),
        "expected_reranked_ranks": _ranks(expected_ids, reranked_ids),
        "expected_selected_ranks": _ranks(expected_ids, selected_ids),
        "verification_source_ids": verification_ids,
        "verification_decision": verification_decision,
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
    retrieved_ids: list[str],
    reranked_ids: list[str],
    selected_ids: list[str],
    cited_ids: list[str],
    verification_decision: str,
    retrieved_available: bool,
    reranked_available: bool,
    selected_available: bool,
    citation_available: bool,
    verify_available: bool,
) -> str:
    if not expected_ids:
        return "no_expected_label"
    if not any(
        (
            retrieved_available,
            reranked_available,
            selected_available,
            citation_available,
            verify_available,
        )
    ):
        return "legacy_coarse"
    if not retrieved_available:
        return "partial_lineage"
    if any(chunk_id not in retrieved_ids for chunk_id in expected_ids):
        return "retrieval"
    if not reranked_available:
        return "partial_lineage"
    if any(chunk_id not in reranked_ids for chunk_id in expected_ids):
        return "rerank"
    if not selected_available:
        return "partial_lineage"
    if any(chunk_id not in selected_ids for chunk_id in expected_ids):
        return "source_selection"
    if not citation_available:
        return "partial_lineage"
    if any(chunk_id not in cited_ids for chunk_id in expected_ids):
        return "citation_binding"
    if not verify_available:
        return "partial_lineage"
    if verify_available and verification_decision not in {"", "pass", "partial"}:
        return "verify"
    return "other_failure"


def _attribution_confidence(
    *,
    retrieved_available: bool,
    reranked_available: bool,
    selected_available: bool,
    citation_available: bool,
    verify_available: bool,
) -> str:
    available = (
        retrieved_available,
        reranked_available,
        selected_available,
        citation_available,
        verify_available,
    )
    if all(available):
        return "exact"
    if any(available):
        return "partial"
    return "legacy_coarse"


def _validated_lineage_stages(result: dict[str, Any]) -> dict[str, bool]:
    unavailable = {stage: False for stage in LINEAGE_STAGES}
    if result.get("lineage_schema_version") != PIPELINE_LINEAGE_SCHEMA_VERSION:
        return unavailable
    attribution = result.get("lineage_attribution")
    if attribution not in {"exact", "partial"}:
        return unavailable
    value = result.get("lineage_stage_available")
    if not isinstance(value, dict) or set(value) != LINEAGE_STAGES:
        return unavailable
    if any(type(available) is not bool for available in value.values()):
        return unavailable
    expected_attribution = "exact" if all(value.values()) else "partial"
    if attribution != expected_attribution:
        return unavailable
    evidence_fields = {
        "retrieve": ("retrieved_chunk_ids",),
        "rerank": ("reranked_chunk_ids",),
        "source_selection": ("selected_source_ids",),
        "citation": ("ordered_cited_source_ids", "cited_source_ids"),
        "verify": ("verification_source_ids",),
    }
    if any(
        available and not _has_ordered_evidence(result, evidence_fields[stage])
        for stage, available in value.items()
    ):
        return unavailable
    if value["verify"] and result.get("verification_decision") not in {
        "pass",
        "partial",
        "escalate",
        "reject",
    }:
        return unavailable
    return dict(value)


def _has_ordered_evidence(result: dict[str, Any], fields: tuple[str, ...]) -> bool:
    for field in fields:
        if field not in result:
            continue
        evidence = result[field]
        return isinstance(evidence, (list, tuple)) and all(
            isinstance(item, str) and bool(item.strip()) for item in evidence
        )
    return False


def _trace_event_metadata(trace: dict[str, Any], node: str) -> dict[str, Any]:
    events = _jsonish(trace.get("trace_events"))
    if not isinstance(events, list):
        return {}
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("node") != node:
            continue
        metadata = event.get("metadata")
        if (
            isinstance(metadata, dict)
            and metadata.get("schema_version") == PIPELINE_LINEAGE_SCHEMA_VERSION
        ):
            return metadata
        return {}
    return {}


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
