from __future__ import annotations

import json
from typing import Any

EXPECTED_ESCALATION_REASONS = {
    "attachment_only",
    "operator_requested",
    "personal_status",
    "repeated_support_failure",
    "rate_limited",
    "needs_operator",
    "low_confidence",
    "no_relevant_chunks",
    "safety_abuse",
    "safety_bullying",
    "safety_dangerous_instruction",
    "safety_medical_emergency",
    "safety_psychological_crisis",
    "safety_self_harm",
    "safety_threat",
    "unsafe_sensitive_data_request",
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
                COUNT(*) FILTER (WHERE channel = 'hde')::int AS hde_request_count,
                COUNT(*) FILTER (
                    WHERE channel = 'hde' AND delivery_status IS NOT NULL
                )::int AS hde_delivery_recorded_count,
                COUNT(*) FILTER (
                    WHERE channel = 'hde' AND delivery_status = 'delivered'
                )::int AS hde_delivered_count,
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
    summary["hde_delivery_coverage_rate"] = _ratio(
        summary.get("hde_delivery_recorded_count", 0),
        summary.get("hde_request_count", 0),
    )
    summary["hde_delivery_success_rate"] = _ratio(
        summary.get("hde_delivered_count", 0),
        summary.get("hde_delivery_recorded_count", 0),
    )
    ticket_outcomes = [dict(row) for row in await _fetch_ticket_outcomes(conn, days)]
    ticket_count = sum(int(row.get("tickets") or 0) for row in ticket_outcomes)
    first_turn_resolved = _ticket_outcome_count(
        ticket_outcomes,
        "bot_resolved_first_turn",
    )
    multi_turn_resolved = _ticket_outcome_count(
        ticket_outcomes,
        "bot_resolved_multi_turn",
    )
    bot_resolved = first_turn_resolved + multi_turn_resolved
    summary["hde_ticket_count"] = ticket_count
    summary["hde_bot_resolved_ticket_count"] = bot_resolved
    summary["hde_ticket_resolution_rate"] = _ratio(bot_resolved, ticket_count)
    summary["hde_first_turn_resolution_rate"] = _ratio(first_turn_resolved, ticket_count)
    summary["hde_multi_turn_resolution_rate"] = _ratio(multi_turn_resolved, ticket_count)

    return {
        "days": days,
        "summary": summary,
        "model_usage": [dict(row) for row in await _fetch_model_usage(conn, days)],
        "routing": [dict(row) for row in await _fetch_routing(conn, days)],
        "ticket_outcomes": ticket_outcomes,
        "delivery_statuses": [dict(row) for row in await _fetch_delivery_statuses(conn, days)],
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
        f"- HDE delivery telemetry coverage: {summary.get('hde_delivery_coverage_rate', 0):.2%}",
        f"- HDE успешно доставлено: {summary.get('hde_delivery_success_rate', 0):.2%}",
        f"- HDE тикетов: {summary.get('hde_ticket_count', 0)}",
        (
            "- HDE закрыто ботом без оператора: "
            f"{summary.get('hde_ticket_resolution_rate', 0):.2%}"
        ),
        (
            "- HDE закрыто с первого ответа: "
            f"{summary.get('hde_first_turn_resolution_rate', 0):.2%}"
        ),
        (
            "- HDE закрыто после диалога: "
            f"{summary.get('hde_multi_turn_resolution_rate', 0):.2%}"
        ),
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
    lines.append("Исходы обращений")
    lines.extend(_format_rows(report.get("ticket_outcomes", []), empty="- исходов нет"))
    lines.append("")
    lines.append("Доставка HDE")
    lines.extend(_format_rows(report.get("delivery_statuses", []), empty="- данных нет"))
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


async def _fetch_ticket_outcomes(conn: Any, days: int) -> list[Any]:
    return await conn.fetch(
        """
        WITH recent_tickets AS (
            SELECT DISTINCT ticket_id_hash
            FROM request_traces
            WHERE timestamp >= NOW() - ($1::int * INTERVAL '1 day')
              AND channel = 'hde'
              AND ticket_id_hash IS NOT NULL
        ),
        ordered_turns AS (
            SELECT
                trace.ticket_id_hash,
                trace.ticket_outcome,
                trace.delivery_status,
                trace.was_escalated,
                COUNT(*) OVER (PARTITION BY trace.ticket_id_hash)::int AS turns,
                BOOL_OR(trace.was_escalated) OVER (
                    PARTITION BY trace.ticket_id_hash
                ) AS had_escalation,
                ROW_NUMBER() OVER (
                    PARTITION BY trace.ticket_id_hash
                    ORDER BY trace.timestamp DESC, trace.request_id DESC
                ) AS recency_rank
            FROM request_traces AS trace
            INNER JOIN recent_tickets USING (ticket_id_hash)
            WHERE trace.channel = 'hde'
        ),
        tickets AS (
            SELECT
                CASE
                    WHEN had_escalation THEN 'operator_required'
                    WHEN delivery_status IS NULL THEN 'delivery_unknown'
                    WHEN delivery_status <> 'delivered' THEN 'not_delivered'
                    WHEN ticket_outcome = 'answered' AND turns = 1
                        THEN 'bot_resolved_first_turn'
                    WHEN ticket_outcome = 'answered' THEN 'bot_resolved_multi_turn'
                    WHEN ticket_outcome = 'clarification' THEN 'unresolved_clarification'
                    WHEN ticket_outcome = 'error' THEN 'error'
                    ELSE 'unresolved'
                END AS outcome
            FROM ordered_turns
            WHERE recency_rank = 1
        )
        SELECT outcome, COUNT(*)::int AS tickets
        FROM tickets
        GROUP BY outcome
        ORDER BY tickets DESC, outcome
        """,
        days,
    )


async def _fetch_delivery_statuses(conn: Any, days: int) -> list[Any]:
    return await conn.fetch(
        """
        SELECT COALESCE(delivery_status, 'unknown') AS status, COUNT(*)::int AS requests
        FROM request_traces
        WHERE timestamp >= NOW() - ($1::int * INTERVAL '1 day')
          AND channel = 'hde'
        GROUP BY status
        ORDER BY requests DESC, status
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


def _ticket_outcome_count(rows: list[dict[str, Any]], outcome: str) -> int:
    return sum(
        int(row.get("tickets") or 0)
        for row in rows
        if row.get("outcome") == outcome
    )
