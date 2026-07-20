from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from loguru import logger

from src.channels.hde import HDEDeliveryResult, HDEDeliveryStatus
from src.channels.hde_transport import HDETransportRepository
from src.logging.db_logger import update_delivery_outcome
from src.models import IncomingMessage

TRACE_RESPONSE_SQL = """
SELECT response_text
FROM request_traces
WHERE request_id = $1
"""


class _TracePool(Protocol):
    async def fetchrow(self, query: str, *args: Any) -> Any: ...


ProcessMessage = Callable[[IncomingMessage, Any], Awaitable[str]]
SendMessage = Callable[[str, str], Awaitable[HDEDeliveryResult]]


class HDETraceResumeConflict(RuntimeError):
    """A trace exists but has no reusable response; regeneration is unsafe."""


@dataclass(frozen=True, slots=True)
class TraceResponse:
    exists: bool
    response_text: str | None


async def load_trace_response(pool: _TracePool, request_id: Any) -> TraceResponse:
    row = await pool.fetchrow(TRACE_RESPONSE_SQL, request_id)
    if row is None:
        return TraceResponse(exists=False, response_text=None)
    raw_response = row["response_text"]
    response = str(raw_response) if raw_response is not None else None
    return TraceResponse(exists=True, response_text=response if response else None)


class HDETransportWorker:
    """Runs durable inbox/outbox loops for the ML runtime only.

    HDE exposes no confirmed delivery idempotency key. Ambiguous attempted sends
    are therefore quarantined instead of auto-retried. Delivery remains an
    at-least-once operational boundary: a manual requeue after an incomplete HDE
    reconciliation can duplicate a response accepted just before a crash.
    """

    def __init__(
        self,
        *,
        repository: HDETransportRepository,
        pg_pool: _TracePool,
        app: Any,
        process_message: ProcessMessage,
        send_message: SendMessage,
        worker_id: str,
        lease_timeout_seconds: float,
        poll_interval_seconds: float = 0.25,
        recovery_interval_seconds: float = 30.0,
        shutdown_timeout_seconds: float = 60.0,
    ) -> None:
        self.repository = repository
        self._pg_pool = pg_pool
        self._app = app
        self._process_message = process_message
        self._send_message = send_message
        self._worker_id = str(worker_id or "").strip()
        self._lease_timeout_seconds = _positive(
            lease_timeout_seconds,
            field="lease_timeout_seconds",
        )
        self._poll_interval_seconds = _positive(
            poll_interval_seconds,
            field="poll_interval_seconds",
        )
        self._recovery_interval_seconds = _positive(
            recovery_interval_seconds,
            field="recovery_interval_seconds",
        )
        self._shutdown_timeout_seconds = _positive(
            shutdown_timeout_seconds,
            field="shutdown_timeout_seconds",
        )
        if not self._worker_id:
            raise ValueError("worker_id_required")
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False
        self._last_error_type: str | None = None

    @property
    def is_running(self) -> bool:
        return self._started and len(self._tasks) == 3 and all(
            not task.done() for task in self._tasks
        )

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "running": self.is_running,
            "task_count": len(self._tasks),
            "last_error_type": self._last_error_type,
        }

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("hde_transport_worker_already_started")
        self._stop_event.clear()
        await self.recover_stale_once()
        self._started = True
        self._tasks = [
            asyncio.create_task(self._inbox_loop(), name="hde-inbox-worker"),
            asyncio.create_task(self._outbox_loop(), name="hde-outbox-worker"),
            asyncio.create_task(self._recovery_loop(), name="hde-lease-recovery"),
        ]

    async def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        try:
            async with asyncio.timeout(self._shutdown_timeout_seconds):
                await asyncio.gather(*self._tasks)
        except TimeoutError:
            logger.warning("hde_transport_shutdown_timeout")
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            self._started = False
            self._tasks = []

    async def process_inbox_once(self) -> bool:
        job = await self.repository.claim_inbox(worker_id=self._worker_id)
        if job is None:
            return False
        process_invoked = False
        safe_to_retry = False
        try:
            trace_response = await load_trace_response(self._pg_pool, job.request_id)
            if trace_response.exists:
                if trace_response.response_text is None:
                    raise HDETraceResumeConflict("trace_response_missing")
                response = trace_response.response_text
                safe_to_retry = True
            else:
                process_invoked = True
                response = await self._process_message(job.incoming_message(), self._app)
                persisted = await load_trace_response(self._pg_pool, job.request_id)
                if persisted.response_text is None:
                    raise HDETraceResumeConflict("trace_response_missing_after_process")
                if persisted.response_text != response:
                    raise HDETraceResumeConflict("trace_response_mismatch_after_process")
                response = persisted.response_text
            await self.repository.complete_inbox_with_outbox(
                job,
                worker_id=self._worker_id,
                response_text=response,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error_type = type(exc).__name__
            logger.exception(
                "hde_inbox_job_failed",
                job_id=job.id,
                request_id=str(job.request_id),
                error_type=type(exc).__name__,
            )
            if safe_to_retry and not isinstance(exc, HDETraceResumeConflict):
                await self.repository.fail_inbox(
                    job,
                    worker_id=self._worker_id,
                    error_code=_error_code("inbox", exc),
                )
            elif process_invoked or isinstance(exc, HDETraceResumeConflict):
                await self.repository.quarantine_inbox(
                    job,
                    worker_id=self._worker_id,
                    error_code=_error_code("inbox", exc),
                )
            else:
                # Trace lookup/message validation failed before process_message;
                # no session/trace side effect could have happened.
                await self.repository.fail_inbox(
                    job,
                    worker_id=self._worker_id,
                    error_code=_error_code("inbox", exc),
                )
        return True

    async def process_outbox_once(self) -> bool:
        job = await self.repository.claim_outbox(worker_id=self._worker_id)
        if job is None:
            return False
        try:
            delivery = await self._send_message(job.ticket_id, job.response_text)
            if delivery.delivered:
                # The repository records queue state, trace telemetry and PII
                # purge in one SQL statement. Missing trace/lease therefore
                # fails closed into the ambiguous-delivery quarantine below.
                await self.repository.mark_outbox_delivered(
                    job,
                    worker_id=self._worker_id,
                    http_status=delivery.status_code,
                )
            elif not delivery.attempted or delivery.status_code == 429:
                await self.repository.fail_outbox(
                    job,
                    worker_id=self._worker_id,
                    error_code=delivery.error_code or f"hde_{delivery.status.value}",
                    http_status=delivery.status_code,
                    retry_after_seconds=delivery.retry_after_seconds,
                )
                await self._record_delivery(job.request_id, delivery)
            else:
                await self.repository.quarantine_outbox(
                    job,
                    worker_id=self._worker_id,
                    error_code=delivery.error_code
                    or f"ambiguous_hde_{delivery.status.value}",
                    http_status=delivery.status_code,
                )
                await self._record_delivery(job.request_id, delivery)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error_type = type(exc).__name__
            logger.exception(
                "hde_outbox_job_failed",
                job_id=job.id,
                request_id=str(job.request_id),
                error_type=type(exc).__name__,
            )
            # Once the provider call was entered, an unexpected exception is
            # ambiguous: never auto-resend without reconciling HDE ticket posts.
            await self.repository.quarantine_outbox(
                job,
                worker_id=self._worker_id,
                error_code=_error_code("ambiguous_outbox", exc),
            )
            await self._record_delivery(
                job.request_id,
                HDEDeliveryResult(
                    status=HDEDeliveryStatus.NETWORK_ERROR,
                    attempted=True,
                    error_code=_error_code("ambiguous_outbox", exc),
                ),
            )
        return True

    async def recover_stale_once(self) -> None:
        now = datetime.now(UTC)
        counts = await self.repository.recover_stale_leases(
            stale_before=now - timedelta(seconds=self._lease_timeout_seconds),
            now=now,
        )
        recovered = (
            counts.inbox_retried
            + counts.inbox_dead_lettered
            + counts.outbox_retried
            + counts.outbox_dead_lettered
        )
        if recovered:
            logger.warning(
                "hde_stale_leases_recovered",
                inbox_retried=counts.inbox_retried,
                inbox_dead_lettered=counts.inbox_dead_lettered,
                outbox_retried=counts.outbox_retried,
                outbox_dead_lettered=counts.outbox_dead_lettered,
            )

    async def _record_delivery(self, request_id: Any, delivery: HDEDeliveryResult) -> None:
        try:
            await update_delivery_outcome(
                self._pg_pool,  # type: ignore[arg-type]
                request_id,
                status=delivery.status.value,
                attempted=delivery.attempted,
                http_status=delivery.status_code,
                retry_after_seconds=delivery.retry_after_seconds,
                error_code=delivery.error_code,
            )
        except Exception as exc:
            self._last_error_type = type(exc).__name__
            logger.exception(
                "hde_delivery_trace_update_failed",
                request_id=str(request_id),
                delivery_status=delivery.status.value,
                error_type=type(exc).__name__,
            )

    async def _inbox_loop(self) -> None:
        await self._work_loop(self.process_inbox_once, loop_name="inbox")

    async def _outbox_loop(self) -> None:
        await self._work_loop(self.process_outbox_once, loop_name="outbox")

    async def _work_loop(
        self,
        process_once: Callable[[], Awaitable[bool]],
        *,
        loop_name: str,
    ) -> None:
        while not self._stop_event.is_set():
            try:
                progressed = await process_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error_type = type(exc).__name__
                logger.exception(
                    "hde_transport_loop_failed",
                    loop=loop_name,
                    error_type=type(exc).__name__,
                )
                progressed = False
            if not progressed:
                await self._wait_or_stop(self._poll_interval_seconds)

    async def _recovery_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._wait_or_stop(self._recovery_interval_seconds)
            if self._stop_event.is_set():
                break
            try:
                await self.recover_stale_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error_type = type(exc).__name__
                logger.exception(
                    "hde_transport_recovery_failed",
                    error_type=type(exc).__name__,
                )

    async def _wait_or_stop(self, seconds: float) -> None:
        try:
            async with asyncio.timeout(seconds):
                await self._stop_event.wait()
        except TimeoutError:
            pass


def _positive(value: float, *, field: str) -> float:
    number = float(value)
    if number <= 0:
        raise ValueError(f"{field}_must_be_positive")
    return number


def _error_code(queue: str, exc: Exception) -> str:
    if isinstance(exc, HDETraceResumeConflict):
        return "trace_response_missing"
    return f"{queue}_{type(exc).__name__}"[:100]


__all__ = [
    "HDEDeliveryStatus",
    "HDETraceResumeConflict",
    "HDETransportWorker",
    "TraceResponse",
    "load_trace_response",
]
