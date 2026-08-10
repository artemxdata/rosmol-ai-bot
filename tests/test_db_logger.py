from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from src.logging.db_logger import log_request, update_delivery_outcome
from src.logging.tracer import Tracer


class FakePool:
    def __init__(self, execute_result: str = "UPDATE 1") -> None:
        self.query: str | None = None
        self.args: tuple[Any, ...] = ()
        self.execute_result = execute_result

    async def execute(self, query: str, *args: Any) -> str:
        self.query = query
        self.args = args
        return self.execute_result


async def _persisted_generator_model(
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, Any],
) -> str:
    monkeypatch.setattr(
        "src.logging.db_logger.get_settings",
        lambda: SimpleNamespace(prompt_version="test"),
    )
    pool = FakePool()
    await log_request(
        pool,  # type: ignore[arg-type]
        {"request_id": uuid4(), **state},
    )
    return str(pool.args[11])


@pytest.mark.asyncio
async def test_log_request_persists_trace_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.logging.db_logger.get_settings",
        lambda: SimpleNamespace(prompt_version="test"),
    )
    tracer = Tracer()
    tracer.add("analyze", 12, model="model-a")
    tracer.add_error("retrieve", 7, "boom")
    pool = FakePool()

    await log_request(
        pool,  # type: ignore[arg-type]
        {
            "request_id": uuid4(),
            "channel": "api",
            "user_id_hash": "hash",
            "message_masked": "Регистрация на форум",
            "upstream_event_id": "message-1",
            "upstream_event_id_source": "message.id",
            "eval_run_id": "run-1",
            "eval_case_id": "case-1",
            "routing_hint": {"complexity": "simple", "reason": "registration_faq"},
            "trace": tracer,
            "generated_response": "Ответ по источнику [src:chunk_1] [src:chunk_1] [src:chunk_2]",
            "llm_usage": [{"model": "GigaChat/GigaChat-2-Max", "total_tokens": 42}],
            "llm_prompt_tokens": 30,
            "llm_completion_tokens": 12,
            "llm_total_tokens": 42,
            "llm_estimated_cost_rub": 0.023912,
            "total_latency_ms": 25,
        },
    )

    assert pool.query is not None
    assert "trace_events" in pool.query
    assert "routing_hint" in pool.query
    assert "llm_usage" in pool.query
    routing_hint = json.loads(pool.args[4])
    assert routing_hint["reason"] == "registration_faq"
    assert pool.args[12] == ["chunk_1", "chunk_2"]
    llm_usage = json.loads(pool.args[18])
    assert llm_usage[0]["model"] == "GigaChat/GigaChat-2-Max"
    assert pool.args[19] == 30
    assert pool.args[20] == 12
    assert pool.args[21] == 42
    assert pool.args[22] == 0.023912
    trace_events = json.loads(pool.args[24])
    assert trace_events[0]["node"] == "analyze"
    assert trace_events[0]["metadata"] == {"model": "model-a"}
    assert trace_events[1]["node"] == "retrieve"
    assert trace_events[1]["error"] == "boom"
    assert pool.args[27] == "message-1"
    assert pool.args[28] == "message.id"
    assert pool.args[29] == "hash"
    assert pool.args[30] == "run-1"
    assert pool.args[31] == "case-1"
    assert pool.args[32] == "answered"
    assert pool.args[11] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        {"escalation_reason": "operator_requested", "should_escalate": True},
        {
            "trace_events": [
                {"node": "analyze", "latency_ms": 1, "metadata": {}, "error": None},
                {"node": "clarify", "latency_ms": 1, "metadata": {}, "error": None},
            ]
        },
        {
            "escalation_reason": "low_confidence",
            "should_escalate": True,
            "trace_events": [
                {"node": "analyze", "latency_ms": 1, "metadata": {}, "error": None},
                {"node": "retrieve", "latency_ms": 1, "metadata": {}, "error": None},
                {"node": "rerank", "latency_ms": 1, "metadata": {}, "error": None},
                {"node": "escalate", "latency_ms": 1, "metadata": {}, "error": None},
            ],
        },
        {"interaction_reason": "profanity"},
    ],
)
async def test_log_request_marks_proven_skipped_generation_not_run(
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, Any],
) -> None:
    assert await _persisted_generator_model(monkeypatch, state) == "not_run"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("generator_model", "extra_state"),
    [
        ("source_only", {}),
        ("source_chunk", {"cache_hit": True}),
        (
            "ai-sage/GigaChat3-10B-A1.8B",
            {
                "error": "llm_generation_failed",
                "trace_events": [
                    {
                        "node": "generate",
                        "latency_ms": 1,
                        "metadata": {},
                        "error": "provider_error",
                    }
                ],
            },
        ),
    ],
)
async def test_log_request_preserves_explicit_generator_model(
    monkeypatch: pytest.MonkeyPatch,
    generator_model: str,
    extra_state: dict[str, Any],
) -> None:
    state = {"generator_model": generator_model, **extra_state}

    assert await _persisted_generator_model(monkeypatch, state) == generator_model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        {"cache_hit": True},
        {"error": "request_timeout", "escalation_reason": "request_timeout"},
        {
            "trace_events": [
                {
                    "node": "generate_selection",
                    "latency_ms": 0,
                    "metadata": {"generator_path": "llm"},
                    "error": None,
                }
            ]
        },
        {
            "trace_events": [
                {
                    "node": "retrieve",
                    "latency_ms": 1,
                    "metadata": {},
                    "error": "retrieval_failed",
                }
            ]
        },
    ],
)
async def test_log_request_marks_ambiguous_missing_generator_unknown(
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, Any],
) -> None:
    assert await _persisted_generator_model(monkeypatch, state) == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["low_confidence", "operator_requested"])
async def test_log_request_generation_evidence_overrides_pre_generation_reason(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    state = {
        "escalation_reason": reason,
        "should_escalate": True,
        "trace_events": [
            {"node": "analyze", "latency_ms": 1, "metadata": {}, "error": None},
            {"node": "generate", "latency_ms": 1, "metadata": {}, "error": None},
            {"node": "guard", "latency_ms": 1, "metadata": {}, "error": None},
            {"node": "respond", "latency_ms": 1, "metadata": {}, "error": None},
        ],
    }

    assert await _persisted_generator_model(monkeypatch, state) == "unknown"


@pytest.mark.asyncio
async def test_update_delivery_outcome_persists_typed_result() -> None:
    pool = FakePool()
    request_id = uuid4()

    await update_delivery_outcome(
        pool,  # type: ignore[arg-type]
        request_id,
        status="rate_limited",
        attempted=True,
        http_status=429,
        retry_after_seconds=1200.0,
        error_code="hde_remote_rate_limit",
    )

    assert pool.query is not None
    assert "delivery_status" in pool.query
    assert "delivery_status = $2::varchar(32)" in pool.query
    assert pool.args[:6] == (
        request_id,
        "rate_limited",
        True,
        429,
        1200.0,
        "hde_remote_rate_limit",
    )
    delivered_at = pool.args[6]
    assert delivered_at.tzinfo is not None
    assert delivered_at.utcoffset() is not None


@pytest.mark.asyncio
async def test_update_delivery_outcome_sets_delivered_timestamp() -> None:
    pool = FakePool()

    await update_delivery_outcome(
        pool,  # type: ignore[arg-type]
        uuid4(),
        status="delivered",
        attempted=True,
        http_status=200,
    )

    delivered_at = pool.args[6]
    assert delivered_at.tzinfo is not None
    assert delivered_at.utcoffset() is not None


@pytest.mark.asyncio
async def test_update_delivery_outcome_rejects_missing_trace_row() -> None:
    pool = FakePool(execute_result="UPDATE 0")

    with pytest.raises(RuntimeError, match="unexpected row count: UPDATE 0"):
        await update_delivery_outcome(
            pool,  # type: ignore[arg-type]
            uuid4(),
            status="delivered",
            attempted=True,
            http_status=200,
        )
