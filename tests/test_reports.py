from __future__ import annotations

import pytest

from scripts.report_traces import _host_fallback_dsn
from src.ops.reports import build_trace_report, format_trace_report


class FakeConn:
    async def fetchrow(self, query: str, days: int) -> dict[str, object]:
        return {
            "request_count": 10,
            "escalated_count": 2,
            "cache_hit_count": 3,
            "avg_latency_ms": 120,
            "p95_latency_ms": 350.0,
            "llm_prompt_tokens": 1000,
            "llm_completion_tokens": 500,
            "llm_total_tokens": 1500,
            "llm_estimated_cost_rub": 0.85401,
        }

    async def fetch(self, query: str, days: int) -> list[dict[str, object]]:
        if "jsonb_array_elements" in query:
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
        return [{"reason": "low_confidence", "requests": 2}]


@pytest.mark.asyncio
async def test_build_trace_report_computes_rates() -> None:
    report = await build_trace_report(FakeConn(), days=7)

    assert report["summary"]["escalation_rate"] == 0.2
    assert report["summary"]["cache_hit_rate"] == 0.3
    assert report["model_usage"][0]["model"] == "GigaChat/GigaChat-2-Max"
    assert report["routing"][0]["reason"] == "personal_condition"


def test_format_trace_report_includes_key_sections() -> None:
    report = {
        "days": 7,
        "summary": {
            "request_count": 0,
            "escalation_rate": 0.0,
            "cache_hit_rate": 0.0,
            "avg_latency_ms": 0,
            "p95_latency_ms": 0,
            "llm_total_tokens": 0,
            "llm_estimated_cost_rub": 0.0,
        },
        "model_usage": [],
        "routing": [],
        "escalations": [],
    }

    text = format_trace_report(report)

    assert "Summary" in text
    assert "Model Usage" in text
    assert "Routing" in text
    assert "Escalations" in text


def test_report_traces_rewrites_docker_postgres_host_for_local_cli() -> None:
    assert (
        _host_fallback_dsn("postgresql://rosmol:rosmol@postgres:5432/rosmol_ai_bot")
        == "postgresql://rosmol:rosmol@localhost:5432/rosmol_ai_bot"
    )
    assert _host_fallback_dsn("postgresql://rosmol:rosmol@localhost:5432/db") is None
