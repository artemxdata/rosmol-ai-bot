from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts import run_pre_demo_smoke


def test_pre_demo_cases_cover_launch_boundary_failures() -> None:
    case_ids = {case["id"] for case in run_pre_demo_smoke.CASES}

    assert {
        "offtopic_politics",
        "abuse_without_request",
        "profane_fgais_support",
        "rostov_registration_closed",
    }.issubset(case_ids)


@pytest.mark.parametrize("compose_host", ("postgres", "db"))
def test_host_trace_dsn_rewrites_compose_postgres_host(
    monkeypatch: pytest.MonkeyPatch,
    compose_host: str,
) -> None:
    monkeypatch.setattr(run_pre_demo_smoke, "_is_container_runtime", lambda: False)
    dsn = run_pre_demo_smoke._host_trace_dsn(
        {"POSTGRES_DSN": f"postgresql://rosmol:pass@{compose_host}:5432/rosmol_ai_bot"}
    )

    assert dsn == "postgresql://rosmol:pass@127.0.0.1:5432/rosmol_ai_bot"


@pytest.mark.parametrize("compose_host", ("postgres", "db"))
def test_container_trace_dsn_keeps_compose_postgres_host(
    monkeypatch: pytest.MonkeyPatch,
    compose_host: str,
) -> None:
    monkeypatch.setattr(run_pre_demo_smoke, "_is_container_runtime", lambda: True)
    expected = f"postgresql://rosmol:pass@{compose_host}:5432/rosmol_ai_bot"

    assert run_pre_demo_smoke._host_trace_dsn({"POSTGRES_DSN": expected}) == expected


@pytest.mark.asyncio
async def test_run_case_can_pass_without_trace_when_allowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["user_id"] == "pre-demo-smoke-answer"
        assert payload["text"] == "Question"
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Known answer.",
            },
        )

    case = {
        "id": "answer",
        "behavior": "answer",
        "query": "Question",
        "must_contain": ("Known",),
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_pre_demo_smoke._run_case(
            client,
            target="http://test/ask",
            headers={},
            case=case,
            pool=None,
            require_trace=False,
        )

    assert result["passed"] is True
    assert result["checks"]["trace_found"] is True
    assert result["response"] == "Known answer."


@pytest.mark.asyncio
async def test_run_case_fails_without_trace_when_required() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Known answer.",
            },
        )

    case = {
        "id": "answer",
        "behavior": "answer",
        "query": "Question",
        "must_contain": ("Known",),
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_pre_demo_smoke._run_case(
            client,
            target="http://test/ask",
            headers={},
            case=case,
            pool=None,
            require_trace=True,
        )

    assert result["passed"] is False
    assert result["checks"]["trace_found"] is False


@pytest.mark.asyncio
async def test_run_smoke_supports_mock_transport_without_db_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_pre_demo_smoke, "_read_dotenv", lambda: {})
    monkeypatch.setattr(
        run_pre_demo_smoke,
        "CASES",
        (
            {
                "id": "answer",
                "behavior": "answer",
                "query": "Question",
                "must_contain": ("Known",),
            },
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Known answer.",
            },
        )

    summary = await run_pre_demo_smoke.run_smoke(
        target="http://test/ask",
        request_timeout=1,
        require_trace=False,
        transport=httpx.MockTransport(handler),
    )

    assert summary["cases_total"] == 1
    assert summary["passed"] == 1
    assert summary["pass_rate"] == 1.0
    assert summary["require_trace"] is False
    assert summary["failed"] == []


def test_write_summary_creates_json_and_markdown(tmp_path: Path) -> None:
    summary = {
        "generated_at": "2026-07-03T00:00:00+00:00",
        "target": "http://test/ask",
        "require_trace": False,
        "trace_error": None,
        "cases_total": 1,
        "passed": 1,
        "pass_rate": 1.0,
        "llm_estimated_cost_rub": 0.0,
        "failed": [],
        "results": [
            {
                "id": "answer",
                "query": "Question",
                "expected_behavior": "answer",
                "passed": True,
                "checks": {"http_ok": True},
                "http_status": 200,
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Known answer.",
                "error": None,
                "latency_client_ms": 10,
                "trace": {},
            }
        ],
    }

    run_pre_demo_smoke.write_summary(tmp_path, summary)

    assert (tmp_path / "pre_demo_smoke.json").exists()
    markdown = (tmp_path / "pre_demo_smoke.md").read_text(encoding="utf-8")
    assert "Pre-demo smoke 2026-07-03" in markdown
    assert "Known answer." in markdown
