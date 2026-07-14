from __future__ import annotations

import pytest

from scripts.report_traces import _host_fallback_dsn
from src.ops.reports import (
    EXPECTED_ESCALATION_REASONS,
    _fetch_ticket_outcomes,
    build_trace_report,
    format_trace_report,
)


def test_expected_escalations_cover_policy_driven_runtime_reasons() -> None:
    assert {
        "attachment_only",
        "operator_requested",
        "personal_status",
        "repeated_support_failure",
        "rate_limited",
        "safety_psychological_crisis",
        "unsafe_sensitive_data_request",
    } <= EXPECTED_ESCALATION_REASONS


class FakeConn:
    async def fetchrow(self, query: str, days: int) -> dict[str, object]:
        return {
            "request_count": 10,
            "escalated_count": 2,
            "cache_hit_count": 3,
            "hde_request_count": 4,
            "hde_delivery_recorded_count": 4,
            "hde_delivered_count": 3,
            "avg_latency_ms": 120,
            "p95_latency_ms": 350.0,
            "llm_prompt_tokens": 1000,
            "llm_completion_tokens": 500,
            "llm_total_tokens": 1500,
            "llm_estimated_cost_rub": 0.85401,
        }

    async def fetch(self, query: str, days: int, *_args: object) -> list[dict[str, object]]:
        if "jsonb_array_elements(llm_usage)" in query:
            return [
                {
                    "model": "GigaChat/GigaChat-2-Max",
                    "calls": 2,
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "total_tokens": 1500,
                    "estimated_cost_rub": 0.85401,
                }
            ]
        if "routing_hint" in query:
            return [{"complexity": "complex", "reason": "personal_condition", "requests": 7}]
        if "ticket_outcome" in query:
            return [
                {"outcome": "bot_resolved_first_turn", "tickets": 2},
                {"outcome": "bot_resolved_multi_turn", "tickets": 1},
                {"outcome": "operator_required", "tickets": 1},
            ]
        if "delivery_status" in query:
            return [{"status": "delivered", "requests": 3}]
        if "ANY($2::text[])" in query:
            if "NOT (" in query:
                return [{"reason": "partial_source_coverage", "requests": 1}]
            return [{"reason": "operator_requested", "requests": 1}]
        if "question->>'topic'" in query:
            return [
                {
                    "topic": "oplata_proezda",
                    "forum": "Амур",
                    "reason": "partial_source_coverage",
                    "requests": 2,
                }
            ]
        if "message_preview" in query:
            return [
                {
                    "timestamp": "2026-06-29 12:00:00+00",
                    "channel": "api",
                    "forum": "Амур",
                    "reason": "partial_source_coverage",
                    "message_preview": "Сложный вопрос",
                    "response_preview": "Передаю специалисту",
                    "total_latency_ms": 1200,
                }
            ]
        if "query_analysis->>'forum_normalized'" in query:
            return [{"forum": "Амур", "reason": "partial_source_coverage", "requests": 2}]
        return [{"reason": "low_confidence", "requests": 2}]


@pytest.mark.asyncio
async def test_build_trace_report_computes_rates() -> None:
    report = await build_trace_report(FakeConn(), days=7)

    assert report["summary"]["escalation_rate"] == 0.2
    assert report["summary"]["expected_escalation_rate"] == 0.1
    assert report["summary"]["quality_issue_rate"] == 0.1
    assert report["summary"]["cache_hit_rate"] == 0.3
    assert report["summary"]["hde_delivery_coverage_rate"] == 1.0
    assert report["summary"]["hde_delivery_success_rate"] == 0.75
    assert report["summary"]["hde_ticket_count"] == 4
    assert report["summary"]["hde_ticket_resolution_rate"] == 0.75
    assert report["summary"]["hde_first_turn_resolution_rate"] == 0.5
    assert report["summary"]["hde_multi_turn_resolution_rate"] == 0.25
    assert report["model_usage"][0]["model"] == "GigaChat/GigaChat-2-Max"
    assert report["routing"][0]["reason"] == "personal_condition"
    assert report["ticket_outcomes"][0]["outcome"] == "bot_resolved_first_turn"
    assert report["delivery_statuses"][0]["status"] == "delivered"
    assert report["expected_escalations"][0]["reason"] == "operator_requested"
    assert report["quality_issue_escalations"][0]["reason"] == "partial_source_coverage"
    assert report["failed_topics"][0]["topic"] == "oplata_proezda"
    assert report["failed_forums"][0]["forum"] == "Амур"
    assert report["recent_escalations"][0]["message_preview"] == "Сложный вопрос"


def test_format_trace_report_includes_key_sections() -> None:
    report = {
        "days": 7,
        "summary": {
            "request_count": 0,
            "escalation_rate": 0.0,
            "expected_escalation_rate": 0.0,
            "quality_issue_rate": 0.0,
            "cache_hit_rate": 0.0,
            "avg_latency_ms": 0,
            "p95_latency_ms": 0,
            "llm_total_tokens": 0,
            "llm_estimated_cost_rub": 0.0,
        },
        "model_usage": [],
        "routing": [],
        "escalations": [],
        "expected_escalations": [],
        "quality_issue_escalations": [],
        "failed_topics": [],
        "failed_forums": [],
        "recent_escalations": [],
    }

    text = format_trace_report(report)

    assert "Сводка" in text
    assert "Использование моделей" in text
    assert "Маршрутизация" in text
    assert "закрыто ботом без оператора" in text
    assert "Эскалации" in text
    assert "Проблемные темы" in text
    assert "Проблемные форумы" in text


def test_report_traces_rewrites_docker_postgres_host_for_local_cli() -> None:
    assert (
        _host_fallback_dsn("postgresql://rosmol:rosmol@postgres:5432/rosmol_ai_bot")
        == "postgresql://rosmol:rosmol@localhost:5432/rosmol_ai_bot"
    )
    assert _host_fallback_dsn("postgresql://rosmol:rosmol@localhost:5432/db") is None


@pytest.mark.asyncio
async def test_ticket_outcomes_include_full_history_for_recent_tickets() -> None:
    class CapturingConn:
        query = ""

        async def fetch(self, query: str, days: int) -> list[dict[str, object]]:
            self.query = query
            assert days == 7
            return []

    conn = CapturingConn()

    await _fetch_ticket_outcomes(conn, 7)

    assert "WITH recent_tickets AS" in conn.query
    assert "INNER JOIN recent_tickets USING (ticket_id_hash)" in conn.query
    assert "WHERE trace.channel = 'hde'" in conn.query
