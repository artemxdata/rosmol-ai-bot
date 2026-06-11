from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from src.logging.db_logger import log_request
from src.logging.tracer import Tracer


class FakePool:
    def __init__(self) -> None:
        self.query: str | None = None
        self.args: tuple[Any, ...] = ()

    async def execute(self, query: str, *args: Any) -> None:
        self.query = query
        self.args = args


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
            "routing_hint": {"complexity": "simple", "reason": "registration_faq"},
            "trace": tracer,
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
