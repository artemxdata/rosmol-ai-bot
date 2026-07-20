from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from src.channels.hde import HDEDeliveryResult, HDEDeliveryStatus
from src.channels.hde_transport import (
    HDEInboxJob,
    HDEOutboxJob,
    InboxStatus,
    OutboxStatus,
    RecoveryCounts,
)
from src.channels.hde_worker import HDETransportWorker

REQUEST_ID = UUID("9d5375bc-7b05-4e2c-8d5b-dc0f0b78dca1")
NOW = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


def _inbox_job() -> HDEInboxJob:
    return HDEInboxJob(
        id=17,
        event_key="e" * 64,
        ticket_key="t" * 64,
        request_id=REQUEST_ID,
        upstream_event_id_source="message.id",
        safe_payload={
            "schema_version": 1,
            "channel": "hde",
            "message_masked": "Masked question",
            "timestamp": NOW.isoformat(),
            "has_attachments": False,
            "forum_context_masked": None,
        },
        ticket_id="ticket-42",
        status=InboxStatus.PROCESSING,
        attempt_count=1,
        max_attempts=5,
        locked_by="worker-a",
    )


def _outbox_job() -> HDEOutboxJob:
    return HDEOutboxJob(
        id=23,
        inbox_id=17,
        event_key="e" * 64,
        ticket_key="t" * 64,
        request_id=REQUEST_ID,
        ticket_id="ticket-42",
        response_text="Exact saved response",
        status=OutboxStatus.SENDING,
        attempt_count=1,
        max_attempts=8,
        locked_by="worker-a",
    )


class FakePool:
    def __init__(self, rows: list[dict[str, Any] | None]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((query, args))
        return self.rows.pop(0)


class FakeRepository:
    def __init__(
        self,
        *,
        inbox_jobs: list[HDEInboxJob | None] | None = None,
        outbox_jobs: list[HDEOutboxJob | None] | None = None,
        complete_error: Exception | None = None,
    ) -> None:
        self.inbox_jobs = list(inbox_jobs or [])
        self.outbox_jobs = list(outbox_jobs or [])
        self.complete_error = complete_error
        self.calls: list[tuple[str, Any]] = []

    async def claim_inbox(self, **_kwargs: Any) -> HDEInboxJob | None:
        return self.inbox_jobs.pop(0) if self.inbox_jobs else None

    async def complete_inbox_with_outbox(self, job: HDEInboxJob, **kwargs: Any) -> Any:
        self.calls.append(("complete", kwargs["response_text"]))
        if self.complete_error is not None:
            error, self.complete_error = self.complete_error, None
            raise error
        return job

    async def fail_inbox(self, job: HDEInboxJob, **_kwargs: Any) -> None:
        self.calls.append(("fail_inbox", job.id))

    async def quarantine_inbox(self, job: HDEInboxJob, **_kwargs: Any) -> None:
        self.calls.append(("quarantine_inbox", job.id))

    async def claim_outbox(self, **_kwargs: Any) -> HDEOutboxJob | None:
        return self.outbox_jobs.pop(0) if self.outbox_jobs else None

    async def mark_outbox_delivered(self, job: HDEOutboxJob, **_kwargs: Any) -> None:
        self.calls.append(("delivered", job.id))

    async def fail_outbox(self, job: HDEOutboxJob, **_kwargs: Any) -> None:
        self.calls.append(("retry_outbox", kwargs_snapshot(_kwargs)))

    async def quarantine_outbox(self, job: HDEOutboxJob, **_kwargs: Any) -> None:
        self.calls.append(("quarantine_outbox", kwargs_snapshot(_kwargs)))

    async def recover_stale_leases(self, **_kwargs: Any) -> RecoveryCounts:
        return RecoveryCounts(0, 0, 0, 0)


def kwargs_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    return dict(value)


def _worker(
    repository: FakeRepository,
    pool: FakePool,
    *,
    process: Any,
    send: Any,
) -> HDETransportWorker:
    return HDETransportWorker(
        repository=repository,  # type: ignore[arg-type]
        pg_pool=pool,
        app=object(),
        process_message=process,
        send_message=send,
        worker_id="worker-a",
        lease_timeout_seconds=240,
        poll_interval_seconds=0.01,
        recovery_interval_seconds=0.01,
        shutdown_timeout_seconds=0.1,
    )


async def _unused_process(*_args: Any) -> str:
    raise AssertionError("process_message must not be called")


async def _unused_send(*_args: Any) -> HDEDeliveryResult:
    raise AssertionError("send must not be called")


@pytest.mark.asyncio
async def test_inbox_resumes_exact_trace_response_without_regeneration() -> None:
    repository = FakeRepository(inbox_jobs=[_inbox_job()])
    worker = _worker(
        repository,
        FakePool([{"response_text": "Exact saved response"}]),
        process=_unused_process,
        send=_unused_send,
    )

    assert await worker.process_inbox_once() is True
    assert repository.calls == [("complete", "Exact saved response")]


@pytest.mark.asyncio
async def test_restart_after_trace_before_outbox_reuses_response_once() -> None:
    repository = FakeRepository(
        inbox_jobs=[_inbox_job(), _inbox_job()],
        complete_error=ConnectionError("crash window"),
    )
    pool = FakePool(
        [
            None,
            {"response_text": "Exact saved response"},
            {"response_text": "Exact saved response"},
        ]
    )
    process_calls = 0

    async def process(*_args: Any) -> str:
        nonlocal process_calls
        process_calls += 1
        return "Exact saved response"

    worker = _worker(repository, pool, process=process, send=_unused_send)

    await worker.process_inbox_once()
    # Operator/stale-recovery requeues only after the persisted response is visible.
    await worker.process_inbox_once()

    assert process_calls == 1
    assert repository.calls == [
        ("complete", "Exact saved response"),
        ("quarantine_inbox", 17),
        ("complete", "Exact saved response"),
    ]


@pytest.mark.asyncio
async def test_new_response_without_matching_persisted_trace_is_quarantined() -> None:
    repository = FakeRepository(inbox_jobs=[_inbox_job()])

    async def process(*_args: Any) -> str:
        return "Generated response"

    worker = _worker(
        repository,
        FakePool([None, None]),
        process=process,
        send=_unused_send,
    )

    await worker.process_inbox_once()

    assert repository.calls == [("quarantine_inbox", 17)]


@pytest.mark.asyncio
async def test_trace_without_response_is_quarantined_not_regenerated() -> None:
    repository = FakeRepository(inbox_jobs=[_inbox_job()])
    worker = _worker(
        repository,
        FakePool([{"response_text": None}]),
        process=_unused_process,
        send=_unused_send,
    )

    await worker.process_inbox_once()

    assert repository.calls == [("quarantine_inbox", 17)]


@pytest.mark.asyncio
async def test_ambiguous_attempted_delivery_is_quarantined_without_retry() -> None:
    repository = FakeRepository(outbox_jobs=[_outbox_job()])

    async def send(*_args: Any) -> HDEDeliveryResult:
        return HDEDeliveryResult(
            HDEDeliveryStatus.TIMEOUT,
            attempted=True,
            error_code="hde_timeout",
        )

    worker = _worker(repository, FakePool([]), process=_unused_process, send=send)

    await worker.process_outbox_once()

    assert repository.calls[0][0] == "quarantine_outbox"
    assert not any(name == "retry_outbox" for name, _ in repository.calls)


@pytest.mark.asyncio
async def test_provider_429_is_the_only_attempted_delivery_auto_retry() -> None:
    repository = FakeRepository(outbox_jobs=[_outbox_job()])

    async def send(*_args: Any) -> HDEDeliveryResult:
        return HDEDeliveryResult(
            HDEDeliveryStatus.RATE_LIMITED,
            attempted=True,
            status_code=429,
            retry_after_seconds=120,
            error_code="hde_remote_rate_limit",
        )

    worker = _worker(repository, FakePool([]), process=_unused_process, send=send)

    await worker.process_outbox_once()

    name, kwargs = repository.calls[0]
    assert name == "retry_outbox"
    assert kwargs["http_status"] == 429
    assert kwargs["retry_after_seconds"] == 120


@pytest.mark.asyncio
async def test_delivered_queue_and_trace_are_committed_atomically_by_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(outbox_jobs=[_outbox_job()])
    call_order: list[str] = []

    async def mark_delivered(job: HDEOutboxJob, **_kwargs: Any) -> None:
        call_order.append("delivered")
        repository.calls.append(("delivered", job.id))

    async def record(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("delivered telemetry is atomic in repository SQL")

    async def send(*_args: Any) -> HDEDeliveryResult:
        return HDEDeliveryResult(
            HDEDeliveryStatus.DELIVERED,
            attempted=True,
            status_code=200,
        )

    repository.mark_outbox_delivered = mark_delivered  # type: ignore[method-assign]
    monkeypatch.setattr("src.channels.hde_worker.update_delivery_outcome", record)
    worker = _worker(repository, FakePool([]), process=_unused_process, send=send)

    await worker.process_outbox_once()

    assert call_order == ["delivered"]
