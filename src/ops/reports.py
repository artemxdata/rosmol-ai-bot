from __future__ import annotations

import json
from typing import Any

EXPECTED_ESCALATION_REASONS = {
    "operator_requested",
    "needs_operator",
    "low_confidence",
    "no_relevant_chunks",
    "safety_abuse",
    "safety_bullying",
    "safety_dangerous_instruction",
    "safety_medical_emergency",
    "safety_self_harm",
    "safety_threat",
    "unsupported_instruction",
}


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
    expected_escalated_count = sum(
        int(row["requests"])
        for row in await _fetch_escalations(conn, days, expected_only=True)
    )
    quality_issue_count = max(0, int(summary["escalated_count"] or 0) - expected_escalated_count)
    summary["escalation_rate"] = _ratio(summary["escalated_count"], request_count)
    summary["expected_escalation_count"] = expected_escalated_count
    summary["expected_escalation_rate"] = _ratio(expected_escalated_count, request_count)
    summary["quality_issue_count"] = quality_issue_count
    summary["quality_issue_rate"] = _ratio(quality_issue_count, request_count)
    summary["cache_hit_rate"] = _ratio(summary["cache_hit_count"], request_count)

    return {
        "days": days,
        "summary": summary,
        "model_usage": [dict(row) for row in await _fetch_model_usage(conn, days)],
        "routing": [dict(row) for row in await _fetch_routing(conn, days)],
        "escalations": [dict(row) for row in await _fetch_escalations(conn, days)],
        "expected_escalations": [
            dict(row) for row in await _fetch_escalations(conn, days, expected_only=True)
        ],
        "quality_issue_escalations": [
            dict(row) for row in await _fetch_escalations(conn, days, expected_only=False)
        ],
        "failed_topics": [dict(row) for row in await _fetch_failed_topics(conn, days)],
        "failed_forums": [dict(row) for row in await _fetch_failed_forums(conn, days)],
        "recent_escalations": [dict(row) for row in await _fetch_recent_escalations(conn, days)],
    }


def format_trace_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"Отчёт по работе бота за последние {report['days']} дн.",
        "",
        "Сводка",
        f"- запросов: {summary['request_count']}",
        f"- все эскалации: {summary['escalation_rate']:.2%}",
        f"- ожидаемые эскалации: {summary['expected_escalation_rate']:.2%}",
        f"- проблемы качества: {summary['quality_issue_rate']:.2%}",
        f"- попадания в кэш: {summary['cache_hit_rate']:.2%}",
        f"- средняя задержка, мс: {summary['avg_latency_ms']}",
        f"- p95 задержка, мс: {summary['p95_latency_ms']:.0f}",
        f"- LLM-токены всего: {summary['llm_total_tokens']}",
        f"- стоимость LLM, ₽: {summary['llm_estimated_cost_rub']:.6f}",
        "",
        "Использование моделей",
    ]
    lines.extend(_format_rows(report["model_usage"], empty="- вызовов LLM не было"))
    lines.append("")
    lines.append("Маршрутизация")
    lines.extend(_format_rows(report["routing"], empty="- данных по маршрутизации нет"))
    lines.append("")
    lines.append("Эскалации")
    lines.extend(_format_rows(report["escalations"], empty="- эскалаций не было"))
    lines.append("")
    lines.append("Проблемные темы")
    lines.extend(_format_rows(report.get("failed_topics", []), empty="- проблемных тем нет"))
    lines.append("")
    lines.append("Проблемные форумы")
    lines.extend(
        _format_rows(report.get("failed_forums", []), empty="- проблемных форумов нет")
    )
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


async def _fetch_escalations(
    conn: Any,
    days: int,
    *,
    expected_only: bool | None = None,
) -> list[Any]:
    return await _fetch_escalations_by_class(conn, days, expected_only=expected_only)


async def _fetch_escalations_by_class(
    conn: Any,
    days: int,
    *,
    expected_only: bool | None,
) -> list[Any]:
    condition = ""
    if expected_only is True:
        condition = "AND COALESCE(escalation_reason, 'unknown') = ANY($2::text[])"
    elif expected_only is False:
        condition = "AND NOT (COALESCE(escalation_reason, 'unknown') = ANY($2::text[]))"

    args: tuple[Any, ...] = (days,)
    if expected_only is not None:
        args = (days, sorted(EXPECTED_ESCALATION_REASONS))

    return await conn.fetch(
        f"""
        SELECT
            COALESCE(escalation_reason, 'unknown') AS reason,
            COUNT(*)::int AS requests
        FROM request_traces
        WHERE timestamp >= NOW() - ($1::int * INTERVAL '1 day')
          AND was_escalated
          {condition}
        GROUP BY reason
        ORDER BY requests DESC, reason
        """,
        *args,
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
