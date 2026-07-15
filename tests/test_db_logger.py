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
