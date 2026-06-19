from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from eval.run_ask import (
    _json_safe,
    _normalize_case,
    _trace_dsn_candidates,
    build_seed_ask_cases,
    run_eval,
    score_case,
    summarize_results,
)


def test_normalize_case_accepts_common_fields() -> None:
    case = _normalize_case(
        {
            "case_id": "mashuk-travel",
            "question": "Кто оплачивает проезд на Машук?",
            "expected_chunks": "chunk_1",
            "answer_contains": "оплачивает самостоятельно",
            "expected_escalated": False,
            "expected_generator_model": "source_chunk",
            "tags": "travel",
        }
    )

    assert case == {
        "id": "mashuk-travel",
        "query": "Кто оплачивает проезд на Машук?",
        "user_id": "ask-eval",
        "channel": "api",
        "expected_chunk_ids": ["chunk_1"],
        "expected_answer_contains": ["оплачивает самостоятельно"],
        "expected_escalated": False,
        "expected_escalation_reason": None,
        "expected_generator_model": "source_chunk",
        "tags": ["travel"],
    }


def test_build_seed_ask_cases_uses_intent_examples() -> None:
    cases = build_seed_ask_cases(
        [
            {
                "chunk_id": "travel",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Машук",
                "intent_examples": ["кто платит за дорогу"],
            },
            {
                "chunk_id": "old",
                "status": "archived",
                "intent_examples": ["старый вопрос"],
            },
        ],
        user_prefix="local",
    )

    assert cases == [
        {
            "id": "seed_balanced::travel",
            "query": "Машук кто платит за дорогу",
            "user_id": "local-1",
            "channel": "api",
            "expected_chunk_ids": ["travel"],
            "expected_answer_contains": [],
            "expected_escalated": None,
            "expected_escalation_reason": None,
            "expected_generator_model": None,
            "tags": ["seed_balanced", "category:форумы", "forum:Машук"],
        }
    ]


def test_trace_dsn_candidates_add_localhost_fallback() -> None:
    candidates = _trace_dsn_candidates(
        "postgresql://rosmol:rosmol@postgres:5432/rosmol_ai_bot"
    )

    assert candidates == [
        "postgresql://rosmol:rosmol@postgres:5432/rosmol_ai_bot",
        "postgresql://rosmol:rosmol@localhost:5432/rosmol_ai_bot",
    ]


def test_json_safe_decodes_asyncpg_json_strings() -> None:
    value = _json_safe('[{"chunk_id": "travel", "score": 0.9}]')

    assert value == [{"chunk_id": "travel", "score": 0.9}]


def test_score_case_uses_trace_for_chunk_model_and_escalation_checks() -> None:
    case = _normalize_case(
        {
            "id": "travel",
            "query": "Кто платит за дорогу?",
            "expected_chunk_ids": ["travel"],
            "expected_escalated": False,
            "expected_generator_model": "source_chunk",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Проезд оплачивается самостоятельно.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["travel"],
        "retrieved_chunks": [],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
        "cache_hit": False,
        "total_latency_ms": 110,
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is True
    assert result["escalation_match"] is True
    assert result["generator_model_match"] is True
    assert result["passed"] is True


def test_summarize_results_counts_core_metrics() -> None:
    metrics = summarize_results(
        [
            {
                "passed": True,
                "http_success": True,
                "expected_chunk_ids": ["a"],
                "expected_chunk_hit": True,
                "expected_answer_contains": [],
                "trace_found": True,
                "was_escalated": False,
                "cache_hit": False,
                "generator_model": "source_chunk",
                "max_reranker_score": 0.9,
                "latency_ms": 100,
                "trace_total_latency_ms": 90,
                "llm_prompt_tokens": 0,
                "llm_completion_tokens": 0,
                "llm_total_tokens": 0,
                "llm_estimated_cost_rub": 0.0,
                "llm_usage": [],
            },
            {
                "passed": False,
                "http_success": True,
                "expected_chunk_ids": ["b"],
                "expected_chunk_hit": True,
                "expected_answer_contains": [],
                "trace_found": True,
                "was_escalated": True,
                "escalation_reason": "low_confidence",
                "cache_hit": False,
                "generator_model": None,
                "max_reranker_score": 0.05,
                "latency_ms": 300,
                "trace_total_latency_ms": 250,
                "llm_prompt_tokens": 10,
                "llm_completion_tokens": 5,
                "llm_total_tokens": 15,
                "llm_estimated_cost_rub": 0.01,
                "llm_usage": [{"model": "m", "total_tokens": 15}],
            },
        ],
        target="http://test/ask",
        cases_path=Path("cases.json"),
    )

    assert metrics["cases_total"] == 2
    assert metrics["pass_rate"] == 0.5
    assert metrics["expected_chunk_hit_rate"] == 1.0
    assert metrics["escalation_rate"] == 0.5
    assert metrics["source_chunk_rate"] == 0.5
    assert metrics["low_confidence_expected_chunk_hits"] == 1
    assert metrics["low_confidence_expected_chunk_hit_rate"] == 0.5
    assert metrics["reranker_score"] == {"avg": 0.475, "p50": 0.05, "p95": 0.9, "max": 0.9}
    assert metrics["latency_ms"]["p95"] == 300
    assert metrics["llm_total_tokens"] == 15
    assert metrics["llm_estimated_cost_rub"] == 0.01


@pytest.mark.asyncio
async def test_run_eval_writes_json_and_markdown_without_db(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    markdown = tmp_path / "ask_metrics.md"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "hello",
                    "query": "Привет",
                    "expected_answer_contains": ["Здравствуйте"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

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

    metrics = await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://test/ask",
        trace_lookup=False,
        api_key_env=None,
        markdown_path=markdown,
        transport=httpx.MockTransport(handler),
    )

    assert metrics["cases_total"] == 1
    assert metrics["pass_rate"] == 1.0
    assert json.loads(output.read_text(encoding="utf-8"))["results"][0]["passed"] is True
    assert "Ask Eval Report" in markdown.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_eval_can_send_bypass_cache_header(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps([{"id": "hello", "query": "Привет"}], ensure_ascii=False),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Bypass-Cache"] == "1"
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Здравствуйте!",
            },
        )

    metrics = await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://test/ask",
        trace_lookup=False,
        api_key_env=None,
        transport=httpx.MockTransport(handler),
        bypass_cache=True,
    )

    assert metrics["cases_total"] == 1
    assert metrics["http_success_rate"] == 1.0
