from __future__ import annotations

import json

import httpx
import pytest

from scripts.manual_ask import (
    build_manual_report_item,
    format_report,
    format_report_item,
    normalize_manual_case,
    run_manual_ask,
)


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


def test_format_report_item_shows_answer_and_rag_signals() -> None:
    rendered = format_report_item(
        {
            "id": "case",
            "query": "Вопрос",
            "http_status": 200,
            "http_success": True,
            "latency_ms": 100,
            "request_id": "11111111-1111-1111-1111-111111111111",
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


@pytest.mark.asyncio
async def test_run_manual_ask_without_db_trace_uses_http_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["text"] == "Привет"
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
    assert report["http_success_count"] == 1
    assert report["trace_found_count"] == 0
    assert report["results"][0]["response"] == "Здравствуйте!"
    assert "Manual Ask Inspection" in format_report(report)
