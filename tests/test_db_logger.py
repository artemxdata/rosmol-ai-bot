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
            "trace": tracer,
            "total_latency_ms": 25,
        },
    )

    assert pool.query is not None
    assert "trace_events" in pool.query
    trace_events = json.loads(pool.args[18])
    assert trace_events[0]["node"] == "analyze"
    assert trace_events[0]["metadata"] == {"model": "model-a"}
    assert trace_events[1]["node"] == "retrieve"
    assert trace_events[1]["error"] == "boom"
