from __future__ import annotations

import json
from typing import Any


async def build_trace_report(conn: Any, days: int) -> dict[str, Any]:
    summary = dict(
        await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int AS request_count,
                COUNT(*) FILTER (WHERE was_escalated)::int AS escalated_count,
                COUNT(*) FILTER (WHERE cache_hit)::int AS cache_hit_count,
                COALESCE(ROUND(AVG(total_latency_ms))::int, 0) AS avg_latency_ms,
                COALESCE(
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY total_latency_ms),
                    0
                )::float AS p95_latency_ms,
                COALESCE(SUM(llm_prompt_tokens), 0)::int AS llm_prompt_tokens,
                COALESCE(SUM(llm_completion_tokens), 0)::int AS llm_completion_tokens,
                COALESCE(SUM(llm_total_tokens), 0)::int AS llm_total_tokens,
                COALESCE(SUM(llm_estimated_cost_rub), 0)::float AS llm_estimated_cost_rub
            FROM request_traces
            WHERE timestamp >= NOW() - ($1::int * INTERVAL '1 day')
            """,
            days,
        )
    )
    request_count = int(summary["request_count"] or 0)
    summary["escalation_rate"] = _ratio(summary["escalated_count"], request_count)
    summary["cache_hit_rate"] = _ratio(summary["cache_hit_count"], request_count)

    return {
        "days": days,
        "summary": summary,
        "model_usage": [dict(row) for row in await _fetch_model_usage(conn, days)],
        "routing": [dict(row) for row in await _fetch_routing(conn, days)],
        "escalations": [dict(row) for row in await _fetch_escalations(conn, days)],
        "failed_topics": [dict(row) for row in await _fetch_failed_topics(conn, days)],
        "failed_forums": [dict(row) for row in await _fetch_failed_forums(conn, days)],
        "recent_escalations": [dict(row) for row in await _fetch_recent_escalations(conn, days)],
    }


def format_trace_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"Trace report for last {report['days']} day(s)",
        "",
        "Summary",
        f"- requests: {summary['request_count']}",
        f"- escalation_rate: {summary['escalation_rate']:.2%}",
        f"- cache_hit_rate: {summary['cache_hit_rate']:.2%}",
        f"- avg_latency_ms: {summary['avg_latency_ms']}",
        f"- p95_latency_ms: {summary['p95_latency_ms']:.0f}",
        f"- llm_total_tokens: {summary['llm_total_tokens']}",
        f"- llm_estimated_cost_rub: {summary['llm_estimated_cost_rub']:.6f}",
        "",
        "Model Usage",
    ]
    lines.extend(_format_rows(report["model_usage"], empty="- no llm usage"))
    lines.append("")
    lines.append("Routing")
    lines.extend(_format_rows(report["routing"], empty="- no routing data"))
    lines.append("")
    lines.append("Escalations")
    lines.extend(_format_rows(report["escalations"], empty="- no escalations"))
    lines.append("")
    lines.append("Failed Topics")
    lines.extend(_format_rows(report.get("failed_topics", []), empty="- no failed topics"))
    lines.append("")
    lines.append("Failed Forums")
    lines.extend(_format_rows(report.get("failed_forums", []), empty="- no failed forums"))
    return "\n".join(lines)


def format_trace_report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


async def _fetch_model_usage(conn: Any, days: int) -> list[Any]:
    return await conn.fetch(
        """
        SELECT
            usage->>'model' AS model,
            COUNT(*)::int AS calls,
            COALESCE(SUM((usage->>'prompt_tokens')::int), 0)::int AS prompt_tokens,
            COALESCE(SUM((usage->>'completion_tokens')::int), 0)::int AS completion_tokens,
            COALESCE(SUM((usage->>'total_tokens')::int), 0)::int AS total_tokens,
            COALESCE(SUM((usage->>'estimated_cost_rub')::numeric), 0)::float
                AS estimated_cost_rub
        FROM request_traces
        CROSS JOIN LATERAL jsonb_array_elements(llm_usage) AS usage
        WHERE timestamp >= NOW() - ($1::int * INTERVAL '1 day')
        GROUP BY usage->>'model'
        ORDER BY estimated_cost_rub DESC, calls DESC
        """,
        days,
    )


async def _fetch_routing(conn: Any, days: int) -> list[Any]:
    return await conn.fetch(
        """
        SELECT
            COALESCE(routing_hint->>'complexity', 'unknown') AS complexity,
            COALESCE(routing_hint->>'reason', 'unknown') AS reason,
            COUNT(*)::int AS requests
        FROM request_traces
        WHERE timestamp >= NOW() - ($1::int * INTERVAL '1 day')
        GROUP BY complexity, reason
        ORDER BY requests DESC, complexity, reason
        """,
        days,
    )


async def _fetch_escalations(conn: Any, days: int) -> list[Any]:
    return await conn.fetch(
        """
        SELECT
            COALESCE(escalation_reason, 'unknown') AS reason,
            COUNT(*)::int AS requests
        FROM request_traces
        WHERE timestamp >= NOW() - ($1::int * INTERVAL '1 day')
          AND was_escalated
        GROUP BY reason
        ORDER BY requests DESC, reason
        """,
        days,
    )


async def _fetch_failed_topics(conn: Any, days: int) -> list[Any]:
    return await conn.fetch(
        """
        SELECT
            COALESCE(question->>'topic', 'unknown') AS topic,
            COALESCE(query_analysis->>'forum_normalized', query_analysis->>'forum', 'unknown')
                AS forum,
            COALESCE(escalation_reason, 'unknown') AS reason,
            COUNT(*)::int AS requests
        FROM request_traces
        LEFT JOIN LATERAL jsonb_array_elements(
            CASE
                WHEN jsonb_typeof(query_analysis->'questions') = 'array'
                    THEN query_analysis->'questions'
                ELSE '[]'::jsonb
            END
        ) AS question ON TRUE
        WHERE timestamp >= NOW() - ($1::int * INTERVAL '1 day')
          AND was_escalated
        GROUP BY topic, forum, reason
        ORDER BY requests DESC, topic, forum
        LIMIT 20
        """,
        days,
    )


async def _fetch_failed_forums(conn: Any, days: int) -> list[Any]:
    return await conn.fetch(
        """
        SELECT
            COALESCE(query_analysis->>'forum_normalized', query_analysis->>'forum', 'unknown')
                AS forum,
            COALESCE(escalation_reason, 'unknown') AS reason,
            COUNT(*)::int AS requests
        FROM request_traces
        WHERE timestamp >= NOW() - ($1::int * INTERVAL '1 day')
          AND was_escalated
        GROUP BY forum, reason
        ORDER BY requests DESC, forum
        LIMIT 20
        """,
        days,
    )


async def _fetch_recent_escalations(conn: Any, days: int) -> list[Any]:
    return await conn.fetch(
        """
        SELECT
            timestamp,
            channel,
            COALESCE(query_analysis->>'forum_normalized', query_analysis->>'forum', 'unknown')
                AS forum,
            COALESCE(escalation_reason, 'unknown') AS reason,
            LEFT(COALESCE(message_masked, ''), 240) AS message_preview,
            LEFT(COALESCE(response_text, ''), 240) AS response_preview,
            total_latency_ms
        FROM request_traces
        WHERE timestamp >= NOW() - ($1::int * INTERVAL '1 day')
          AND was_escalated
        ORDER BY timestamp DESC
        LIMIT 20
        """,
        days,
    )


def _format_rows(rows: list[dict[str, Any]], empty: str) -> list[str]:
    if not rows:
        return [empty]
    return ["- " + ", ".join(f"{key}={value}" for key, value in row.items()) for row in rows]


def _ratio(value: int, total: int) -> float:
    return float(value or 0) / total if total else 0.0
