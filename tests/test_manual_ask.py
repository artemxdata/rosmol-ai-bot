from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

from scripts import manual_ask
from scripts.manual_ask import (
    _load_cases_from_args,
    build_manual_report_item,
    format_report,
    format_report_item,
    normalize_manual_case,
    run_manual_ask,
)


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
                "response": "OK",
            },
        )


class _StaticTracePool:
    def __init__(self, trace: dict[str, object]) -> None:
        self.trace = trace

    async def fetchrow(self, *args: object) -> dict[str, object]:
        return self.trace

    async def close(self) -> None:
        return None


def test_normalize_manual_case_accepts_string_and_object() -> None:
    assert normalize_manual_case("Как зарегистрироваться?", 1) == {
        "id": "manual-1",
        "query": "Как зарегистрироваться?",
        "tags": [],
    }

    assert normalize_manual_case(
        {"id": "forum", "text": "Что с проживанием?", "tags": "forum"},
        2,
    ) == {
        "id": "forum",
        "query": "Что с проживанием?",
        "tags": ["forum"],
    }


def test_build_manual_report_item_extracts_trace_details() -> None:
    item = build_manual_report_item(
        {"id": "case", "query": "Кто оплачивает дорогу?", "tags": ["travel"]},
        {
            "http_status": 200,
            "http_success": True,
            "request_id": "11111111-1111-1111-1111-111111111111",
            "response": "Проезд оплачивается самостоятельно.",
            "latency_ms": 100,
            "error": None,
        },
        {
            "message_masked": "Кто оплачивает дорогу?",
            "cited_sources": ["travel_chunk"],
            "retrieved_chunks": [
                {
                    "chunk_id": "travel_chunk",
                    "text": "Проезд оплачивается самостоятельно.",
                    "metadata": {"forum_normalized": "Машук", "category": "проезд"},
                    "score": 0.8,
                }
            ],
            "reranker_scores": [
                {
                    "chunk_id": "travel_chunk",
                    "text": "Проезд оплачивается самостоятельно.",
                    "metadata": {"forum_normalized": "Машук", "category": "проезд"},
                    "score": 0.8,
                    "reranker_score": 0.91,
                }
            ],
            "max_reranker_score": 0.91,
            "cache_hit": False,
            "generator_model": "source_chunk",
            "was_escalated": False,
            "trace_events": [{"node": "retrieve", "latency_ms": 20, "metadata": {}}],
            "llm_usage": [{"node": "analyze", "model": "test", "total_tokens": 10}],
            "llm_total_tokens": 10,
            "llm_estimated_cost_rub": 0.01,
        },
    )

    assert item["trace_found"] is True
    assert item["observed_chunk_ids"] == ["travel_chunk"]
    assert item["reranked_chunks"][0]["reranker_score"] == 0.91
    assert item["generator_model"] == "source_chunk"
    assert item["quality_verdict"] == "deterministic_source_answer"
    assert "top chunk" in item["review_hint"]


def test_format_report_item_shows_answer_and_rag_signals() -> None:
    rendered = format_report_item(
        {
            "id": "case",
            "query": "Вопрос",
            "http_status": 200,
            "http_success": True,
            "latency_ms": 100,
            "request_id": "11111111-1111-1111-1111-111111111111",
            "quality_verdict": "deterministic_source_answer",
            "review_hint": "Проверить top chunk.",
            "trace_found": True,
            "trace_total_latency_ms": 90,
            "cache_hit": False,
            "generator_model": "source_chunk",
            "was_escalated": False,
            "escalation_reason": None,
            "max_reranker_score": 0.91,
            "cited_sources": ["chunk_1"],
            "observed_chunk_ids": ["chunk_1"],
            "message_masked": "Вопрос",
            "response": "Ответ из чанка.",
            "reranked_chunks": [
                {
                    "chunk_id": "chunk_1",
                    "score": 0.8,
                    "reranker_score": 0.91,
                    "forum_normalized": "Машук",
                    "category": "регистрация",
                    "text": "Фрагмент базы знаний.",
                }
            ],
            "retrieved_chunks": [],
            "llm_usage": [{"node": "analyze", "model": "test", "total_tokens": 10}],
            "trace_events": [{"node": "analyze", "latency_ms": 50, "metadata": {}}],
        },
        index=1,
    )

    assert "Response:" in rendered
    assert "Ответ из чанка." in rendered
    assert "Top reranked chunks:" in rendered
    assert "chunk_1" in rendered
    assert "Graph events:" in rendered
    assert "Quality verdict: deterministic_source_answer" in rendered
    assert "Review hint: Проверить top chunk." in rendered


@pytest.mark.asyncio
async def test_run_manual_ask_without_db_trace_uses_http_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["text"] == "Привет"
        assert request.headers["X-Eval-Run-Id"].startswith("manual-ask-")
        assert request.headers["X-Eval-Case-Id"] == "hello"
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Здравствуйте!",
            },
        )

    report = await run_manual_ask(
        [{"id": "hello", "query": "Привет", "tags": []}],
        target="http://test/ask",
        trace_lookup=False,
        api_key_env=None,
        transport=httpx.MockTransport(handler),
    )

    assert report["cases_total"] == 1
    assert report["eval_run_id"].startswith("manual-ask-")
    assert report["http_success_count"] == 1
    assert report["trace_found_count"] == 0
    assert report["verdict_counts"] == {"answer_without_trace": 1}
    assert report["results"][0]["response"] == "Здравствуйте!"
    assert report["results"][0]["quality_verdict"] == "answer_without_trace"
    assert "Manual Ask Inspection" in format_report(report)
    assert "Verdicts:" in format_report(report)


@pytest.mark.asyncio
async def test_run_manual_ask_can_bypass_cache_and_isolate_users() -> None:
    seen_user_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Bypass-Cache"] == "1"
        payload = json.loads(request.content.decode("utf-8"))
        seen_user_ids.append(payload["user_id"])
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "OK",
            },
        )

    report = await run_manual_ask(
        [
            {"id": "first", "query": "РџРµСЂРІС‹Р№", "tags": []},
            {"id": "second", "query": "Р’С‚РѕСЂРѕР№", "tags": []},
        ],
        target="http://test/ask",
        user_id="manual-demo",
        trace_lookup=False,
        api_key_env=None,
        transport=httpx.MockTransport(handler),
        bypass_cache=True,
        isolate_users=True,
    )

    assert seen_user_ids == ["manual-demo-1", "manual-demo-2"]
    assert report["bypass_cache"] is True
    assert report["isolate_users"] is True
    rendered = format_report(report)
    assert "Bypass cache: True" in rendered
    assert "Isolate users: True" in rendered


@pytest.mark.asyncio
async def test_load_cases_from_args_applies_max_cases() -> None:
    cases = await _load_cases_from_args(
        Namespace(
            text=["one", "two", "three"],
            file="",
            max_cases=2,
        )
    )

    assert [case["query"] for case in cases] == ["one", "two"]


@pytest.mark.asyncio
async def test_live_manual_ask_db_unavailable_sends_zero_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _CountingLiveTransport()

    async def fail_create_pool(*args: object, **kwargs: object) -> object:
        raise OSError("db unavailable")

    monkeypatch.setattr(manual_ask, "_local_llm_pricing_preflight_failure", lambda: None)
    monkeypatch.setattr(manual_ask.asyncpg, "create_pool", fail_create_pool)

    with pytest.raises(RuntimeError, match="requires an available PostgreSQL"):
        await run_manual_ask(
            [{"id": "one", "query": "Question", "tags": []}],
            target="http://live/ask",
            trace_dsn="postgresql://placeholder/rosmol",
            transport=transport,
            max_llm_cost_rub=1.0,
        )

    assert transport.calls == 0


@pytest.mark.asyncio
async def test_live_manual_ask_over_ten_cases_requires_approval_before_post() -> None:
    transport = _CountingLiveTransport()
    cases = [
        {"id": f"case-{index}", "query": "Question", "tags": []}
        for index in range(11)
    ]

    with pytest.raises(ValueError, match="one-time owner approval"):
        await run_manual_ask(
            cases,
            target="http://live/ask",
            trace_dsn="postgresql://placeholder/rosmol",
            transport=transport,
            max_llm_cost_rub=50.0,
        )

    assert transport.calls == 0


@pytest.mark.asyncio
async def test_live_manual_ask_stops_before_second_unpriced_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _CountingLiveTransport()
    pool = _StaticTracePool(
        {
            "llm_usage": [
                {
                    "total_tokens": 10,
                    "estimated_cost_rub": 0.0,
                    "priced": False,
                }
            ],
            "llm_total_tokens": 10,
            "llm_estimated_cost_rub": 0.0,
        }
    )

    async def create_pool(*args: object, **kwargs: object) -> _StaticTracePool:
        return pool

    monkeypatch.setattr(manual_ask, "_local_llm_pricing_preflight_failure", lambda: None)
    monkeypatch.setattr(manual_ask.asyncpg, "create_pool", create_pool)

    report = await run_manual_ask(
        [
            {"id": "one", "query": "Question", "tags": []},
            {"id": "two", "query": "Question", "tags": []},
        ],
        target="http://live/ask",
        trace_dsn="postgresql://placeholder/rosmol",
        transport=transport,
        max_llm_cost_rub=5.0,
    )

    assert transport.calls == 1
    assert report["cases_requested_total"] == 2
    assert report["cases_total"] == 1
    assert report["llm_pricing_stopped"] is True
    assert report["llm_pricing_failure"] == "llm_pricing_unavailable"


@pytest.mark.asyncio
async def test_live_manual_ask_exact_budget_on_final_case_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _CountingLiveTransport()
    pool = _StaticTracePool(
        {
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

    monkeypatch.setattr(manual_ask, "_local_llm_pricing_preflight_failure", lambda: None)
    monkeypatch.setattr(manual_ask.asyncpg, "create_pool", create_pool)

    report = await run_manual_ask(
        [{"id": "one", "query": "Question", "tags": []}],
        target="http://live/ask",
        trace_dsn="postgresql://placeholder/rosmol",
        transport=transport,
        max_llm_cost_rub=1.0,
    )

    assert transport.calls == 1
    assert report["cases_total"] == 1
    assert report["llm_budget_stopped"] is False
    assert report["llm_pricing_stopped"] is False
