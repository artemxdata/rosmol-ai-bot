from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts import run_pre_demo_smoke


@pytest.fixture(autouse=True)
def _isolate_live_cost_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVAL_COST_LEDGER_DIR", str(tmp_path / "eval-cost-ledger"))
    monkeypatch.setenv("RELEASE_GIT_SHA", "a" * 40)


class _CountingLiveTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Known answer.",
            },
        )


class _StaticTracePool:
    def __init__(self, trace: dict[str, object]) -> None:
        self.trace = trace

    async def fetchrow(self, *args: object) -> dict[str, object]:
        return self.trace

    async def close(self) -> None:
        return None


def test_pre_demo_cases_cover_launch_boundary_failures() -> None:
    case_ids = {case["id"] for case in run_pre_demo_smoke.CASES}

    assert {
        "offtopic_politics",
        "abuse_without_request",
        "profane_fgais_support",
        "rostov_registration_closed",
    }.issubset(case_ids)


def test_profane_fgais_smoke_requires_grounded_first_line_support() -> None:
    case = next(case for case in run_pre_demo_smoke.CASES if case["id"] == "profane_fgais_support")

    assert case["behavior"] == "answer"
    assert case["must_contain"] == ("очисти кеш", "браузер")
    assert case["expected_sources_any"] == (
        "xlsx_fallback_r0014_tehnicheskaya_oshibka",
    )


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
        assert request.headers["X-Eval-Run-Id"].startswith("pre-demo-smoke-")
        assert request.headers["X-Eval-Case-Id"] == "answer"
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
    assert summary["eval_run_id"].startswith("pre-demo-smoke-")
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


@pytest.mark.asyncio
async def test_live_pre_demo_db_unavailable_sends_zero_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _CountingLiveTransport()

    async def fail_create_pool(*args: object, **kwargs: object) -> object:
        raise OSError("db unavailable")

    monkeypatch.setattr(
        run_pre_demo_smoke,
        "_read_dotenv",
        lambda: {"POSTGRES_DSN": "postgresql://placeholder/rosmol"},
    )
    monkeypatch.setattr(
        run_pre_demo_smoke,
        "_local_llm_pricing_preflight_failure",
        lambda: None,
    )
    monkeypatch.setattr(run_pre_demo_smoke.asyncpg, "create_pool", fail_create_pool)

    with pytest.raises(RuntimeError, match="requires an available PostgreSQL"):
        await run_pre_demo_smoke.run_smoke(
            target="http://live/ask",
            request_timeout=1,
            transport=transport,
            max_llm_cost_rub=10.0,
        )

    assert transport.calls == 0


@pytest.mark.asyncio
async def test_live_pre_demo_over_ten_cases_requires_approval_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _CountingLiveTransport()
    monkeypatch.setattr(
        run_pre_demo_smoke,
        "CASES",
        tuple(
            {
                "id": f"case-{index}",
                "behavior": "answer",
                "query": "Question",
                "must_contain": ("Known",),
            }
            for index in range(11)
        ),
    )

    with pytest.raises(ValueError, match="one-time owner approval"):
        await run_pre_demo_smoke.run_smoke(
            target="http://live/ask",
            request_timeout=1,
            transport=transport,
            max_cases=11,
            max_llm_cost_rub=50.0,
        )

    assert transport.calls == 0


@pytest.mark.asyncio
async def test_pre_demo_mock_default_runs_at_most_ten_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    monkeypatch.setattr(run_pre_demo_smoke, "_read_dotenv", lambda: {})
    monkeypatch.setattr(
        run_pre_demo_smoke,
        "CASES",
        tuple(
            {
                "id": f"case-{index}",
                "behavior": "answer",
                "query": "Question",
                "must_contain": ("Known",),
            }
            for index in range(11)
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
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

    assert calls == 10
    assert summary["cases_available_total"] == 11
    assert summary["cases_total"] == 10


@pytest.mark.asyncio
async def test_live_pre_demo_stops_before_next_case_at_exact_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _CountingLiveTransport()
    pool = _StaticTracePool(
        {
            "was_escalated": False,
            "llm_usage": [
                {
                    "total_tokens": 10,
                    "estimated_cost_rub": 1.0,
                    "priced": True,
                }
            ],
            "llm_total_tokens": 10,
            "llm_estimated_cost_rub": 1.0,
        }
    )

    async def create_pool(*args: object, **kwargs: object) -> _StaticTracePool:
        return pool

    monkeypatch.setattr(
        run_pre_demo_smoke,
        "CASES",
        (
            {
                "id": "one",
                "behavior": "answer",
                "query": "Question",
                "must_contain": ("Known",),
            },
            {
                "id": "two",
                "behavior": "answer",
                "query": "Question",
                "must_contain": ("Known",),
            },
        ),
    )
    monkeypatch.setattr(
        run_pre_demo_smoke,
        "_read_dotenv",
        lambda: {"POSTGRES_DSN": "postgresql://placeholder/rosmol"},
    )
    monkeypatch.setattr(
        run_pre_demo_smoke,
        "_local_llm_pricing_preflight_failure",
        lambda: None,
    )
    monkeypatch.setattr(run_pre_demo_smoke.asyncpg, "create_pool", create_pool)

    summary = await run_pre_demo_smoke.run_smoke(
        target="http://live/ask",
        request_timeout=1,
        transport=transport,
        max_cases=2,
        max_llm_cost_rub=1.0,
    )

    assert transport.calls == 1
    assert summary["cases_total"] == 2
    assert summary["executed_cases_total"] == 1
    assert summary["llm_budget_stopped"] is True
    assert summary["failed"] == ["two"]


@pytest.mark.asyncio
async def test_live_pre_demo_exact_budget_on_final_case_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _CountingLiveTransport()
    pool = _StaticTracePool(
        {
            "was_escalated": False,
            "llm_usage": [
                {
                    "total_tokens": 10,
                    "estimated_cost_rub": 1.0,
                    "priced": True,
                }
            ],
            "llm_total_tokens": 10,
            "llm_estimated_cost_rub": 1.0,
        }
    )

    async def create_pool(*args: object, **kwargs: object) -> _StaticTracePool:
        return pool

    monkeypatch.setattr(
        run_pre_demo_smoke,
        "CASES",
        (
            {
                "id": "one",
                "behavior": "answer",
                "query": "Question",
                "must_contain": ("Known",),
            },
        ),
    )
    monkeypatch.setattr(
        run_pre_demo_smoke,
        "_read_dotenv",
        lambda: {"POSTGRES_DSN": "postgresql://placeholder/rosmol"},
    )
    monkeypatch.setattr(
        run_pre_demo_smoke,
        "_local_llm_pricing_preflight_failure",
        lambda: None,
    )
    monkeypatch.setattr(run_pre_demo_smoke.asyncpg, "create_pool", create_pool)

    summary = await run_pre_demo_smoke.run_smoke(
        target="http://live/ask",
        request_timeout=1,
        transport=transport,
        max_cases=1,
        max_llm_cost_rub=1.0,
    )

    assert transport.calls == 1
    assert summary["executed_cases_total"] == 1
    assert summary["llm_budget_stopped"] is False
    assert summary["llm_pricing_stopped"] is False
