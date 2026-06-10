from __future__ import annotations

from time import perf_counter
from typing import Any

from src.models import TraceEvent


class Tracer:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def add(self, node: str, latency_ms: int, **metadata: Any) -> None:
        self.events.append(TraceEvent(node=node, latency_ms=latency_ms, metadata=metadata))

    def add_error(self, node: str, latency_ms: int, error: Exception | str) -> None:
        self.events.append(TraceEvent(node=node, latency_ms=latency_ms, error=str(error)))

    def as_list(self) -> list[dict[str, Any]]:
        return [event.model_dump() for event in self.events]


class trace_span:
    def __init__(self, tracer: Tracer, node: str) -> None:
        self.tracer = tracer
        self.node = node
        self.started_at = 0.0

    def __enter__(self) -> trace_span:
        self.started_at = perf_counter()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        latency_ms = int((perf_counter() - self.started_at) * 1000)
        if exc is None:
            self.tracer.add(self.node, latency_ms)
        else:
            self.tracer.add_error(self.node, latency_ms, str(exc))
