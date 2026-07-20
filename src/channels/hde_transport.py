from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from src.channels.hde import HDE_MESSAGE_TEXT_MAX_LENGTH, HDE_TICKET_ID_MAX_LENGTH
from src.models import Channel, IncomingMessage

HDE_TRANSPORT_SAFE_PAYLOAD_MAX_BYTES = 16 * 1024
HDE_TRANSPORT_ENVELOPE_MAX_BYTES = 64 * 1024
HDE_TRANSPORT_WORKER_ID_MAX_LENGTH = 100
HDE_TRANSPORT_ERROR_CODE_MAX_LENGTH = 100
HDE_TRANSPORT_SECRET_MIN_LENGTH = 32
HDE_TRANSPORT_FORUM_CONTEXT_MAX_LENGTH = 1000
DEFAULT_INBOX_MAX_ATTEMPTS = 5
DEFAULT_OUTBOX_MAX_ATTEMPTS = 8
DEFAULT_BACKOFF_BASE_SECONDS = 5.0
DEFAULT_BACKOFF_CAP_SECONDS = 3600.0
HDE_EVENT_KEY_SOURCE = "hde_event_key_hmac_v1"
HDE_RECOVERY_REQUEUE_INBOX_REASON = "side_effects_reviewed_safe_to_resume"
HDE_RECOVERY_REQUEUE_OUTBOX_REASON = "provider_confirmed_not_delivered"
HDE_RECOVERY_RECONCILE_DELIVERED_REASON = "provider_confirmed_delivered"
HDE_RECOVERY_OPERATOR_RE = re.compile(r"^[A-Za-z0-9_.@-]{2,100}$")
HDE_RECOVERY_EVIDENCE_RE = re.compile(r"^[0-9a-f]{64}$")
HDE_STABLE_EVENT_ID_SOURCES = frozenset(
    {
        "message.id",
        "message.message_id",
        "message.post_id",
        "data.message.id",
        "data.message.message_id",
        "event.id",
        "event.event_id",
        "event_id",
        "message_id",
        "post_id",
    }
)


class HDETransportError(RuntimeError):
    """Base error for the durable HDE transport."""


class HDETransportValidationError(HDETransportError):
    """The caller supplied an unsafe or incomplete transport envelope."""


class HDEStableEventRequired(HDETransportValidationError):
    """A provider-stable event identity is mandatory for durable enqueue."""


class HDETransportLeaseLost(HDETransportError):
    """A worker tried to mutate a job after losing ownership of its lease."""


class HDETransportRecoveryRejected(HDETransportError):
    """A manual recovery request did not match one auditable dead-letter job."""


class InboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY = "retry"
    PROCESSED = "processed"
    DEAD_LETTER = "dead_letter"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    RETRY = "retry"
    DELIVERED = "delivered"
    DEAD_LETTER = "dead_letter"


class _QueryExecutor(Protocol):
    async def fetchrow(self, query: str, *args: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class HDEInboxReceipt:
    id: int
    event_key: str
    request_id: UUID
    status: InboxStatus
    created: bool


@dataclass(frozen=True, slots=True)
class HDEInboxJob:
    id: int
    event_key: str
    ticket_key: str
    request_id: UUID
    upstream_event_id_source: str
    safe_payload: dict[str, Any]
    ticket_id: str
    status: InboxStatus
    attempt_count: int
    max_attempts: int
    locked_by: str

    def incoming_message(self) -> IncomingMessage:
        payload = _validated_safe_payload(self.safe_payload)
        return IncomingMessage(
            user_id=self.ticket_id,
            channel=Channel.HDE,
            text=payload["message_masked"],
            timestamp=payload["timestamp"],
            request_id=self.request_id,
            attachments=[{"redacted": True}] if payload["has_attachments"] else [],
            forum_context=payload.get("forum_context_masked"),
            # Never copy the raw provider event id into request_traces.  The HMAC
            # key remains stable for correlation without exposing that identifier.
            upstream_event_id=self.event_key,
            upstream_event_id_source=HDE_EVENT_KEY_SOURCE,
        )


@dataclass(frozen=True, slots=True)
class HDEOutboxJob:
    id: int
    inbox_id: int
    event_key: str
    ticket_key: str
    request_id: UUID
    ticket_id: str
    response_text: str
    status: OutboxStatus
    attempt_count: int
    max_attempts: int
    locked_by: str | None = None


@dataclass(frozen=True, slots=True)
class FailureDecision:
    status: InboxStatus | OutboxStatus
    next_attempt_at: datetime


@dataclass(frozen=True, slots=True)
class RecoveryCounts:
    inbox_retried: int
    inbox_dead_lettered: int
    outbox_retried: int
    outbox_dead_lettered: int


@dataclass(frozen=True, slots=True)
class HDEQueueCounts:
    inbox_backlog: int
    inbox_processing: int
    inbox_dead_letter: int
    outbox_backlog: int
    outbox_sending: int
    outbox_dead_letter: int
    inbox_oldest_ready_age_seconds: float | None
    inbox_oldest_processing_age_seconds: float | None
    outbox_oldest_ready_age_seconds: float | None
    outbox_oldest_sending_age_seconds: float | None

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "inbox_backlog": self.inbox_backlog,
            "inbox_processing": self.inbox_processing,
            "inbox_dead_letter": self.inbox_dead_letter,
            "outbox_backlog": self.outbox_backlog,
            "outbox_sending": self.outbox_sending,
            "outbox_dead_letter": self.outbox_dead_letter,
            "inbox_oldest_ready_age_seconds": self.inbox_oldest_ready_age_seconds,
            "inbox_oldest_processing_age_seconds": (
                self.inbox_oldest_processing_age_seconds
            ),
            "outbox_oldest_ready_age_seconds": self.outbox_oldest_ready_age_seconds,
            "outbox_oldest_sending_age_seconds": self.outbox_oldest_sending_age_seconds,
        }


def build_hde_event_key(message: IncomingMessage, *, secret: str) -> str:
    """Build a pseudonymous idempotency key from the stable provider identity."""
    ticket_id, upstream_event_id = _require_stable_hde_message(message)
    return _keyed_digest(
        secret,
        domain="hde-event:v1",
        parts=(ticket_id, upstream_event_id),
    )


def build_hde_ticket_key(ticket_id: str, *, secret: str) -> str:
    normalized = _bounded_required_string(
        ticket_id,
        field="ticket_id",
        limit=HDE_TICKET_ID_MAX_LENGTH,
    )
    return _keyed_digest(secret, domain="hde-ticket:v1", parts=(normalized,))


def calculate_backoff_seconds(
    attempt_count: int,
    *,
    retry_after_seconds: float | None = None,
    base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
) -> float:
    """Return capped exponential backoff; a provider Retry-After takes precedence."""
    if attempt_count < 1:
        raise HDETransportValidationError("attempt_count_must_be_positive")
    if base_seconds <= 0 or cap_seconds <= 0:
        raise HDETransportValidationError("backoff_bounds_must_be_positive")
    if retry_after_seconds is not None:
        requested = max(0.0, float(retry_after_seconds))
        return min(float(cap_seconds), requested)
    exponent = min(attempt_count - 1, 30)
    return min(float(cap_seconds), float(base_seconds) * (2**exponent))


def plan_failure(
    *,
    attempt_count: int,
    max_attempts: int,
    retry_status: InboxStatus | OutboxStatus,
    dead_letter_status: InboxStatus | OutboxStatus,
    now: datetime | None = None,
    retry_after_seconds: float | None = None,
) -> FailureDecision:
    if max_attempts < 1:
        raise HDETransportValidationError("max_attempts_must_be_positive")
    current = _as_utc(now or datetime.now(UTC))
    if attempt_count >= max_attempts:
        return FailureDecision(status=dead_letter_status, next_attempt_at=current)
    delay = calculate_backoff_seconds(
        attempt_count,
        retry_after_seconds=retry_after_seconds,
    )
    return FailureDecision(
        status=retry_status,
        next_attempt_at=current + timedelta(seconds=delay),
    )


class HDETransportRepository:
    """Privacy-safe PostgreSQL HDE inbox/outbox with explicit worker leases.

    ``safe_payload`` contains masked text only. Reversible ticket references and
    outbound text are encrypted in PostgreSQL with pgcrypto and a dedicated key.
    """

    def __init__(
        self,
        executor: _QueryExecutor,
        *,
        event_key_secret: str,
        encryption_key: str,
    ) -> None:
        self._executor = executor
        self._event_key_secret = _transport_secret(
            event_key_secret,
            field="event_key_secret",
        )
        self._encryption_key = _transport_secret(
            encryption_key,
            field="encryption_key",
        )
        if hmac.compare_digest(self._event_key_secret, self._encryption_key):
            raise HDETransportValidationError("transport_secrets_must_be_distinct")

    async def enqueue_inbox(
        self,
        message: IncomingMessage,
        *,
        masked_text: str,
        masked_forum_context: str | None = None,
        max_attempts: int = DEFAULT_INBOX_MAX_ATTEMPTS,
        now: datetime | None = None,
    ) -> HDEInboxReceipt:
        ticket_id, _ = _require_stable_hde_message(message)
        attempts = _validated_max_attempts(max_attempts)
        event_key = build_hde_event_key(message, secret=self._event_key_secret)
        ticket_key = build_hde_ticket_key(ticket_id, secret=self._event_key_secret)
        safe_payload_json = _serialize_safe_payload(
            message,
            masked_text=masked_text,
            masked_forum_context=masked_forum_context,
        )
        ticket_ref_json = _serialize_envelope(
            {"schema_version": 1, "ticket_id": ticket_id},
            error_code="ticket_ref_too_large",
        )
        current = _as_utc(now or datetime.now(UTC))
        row = await self._executor.fetchrow(
            ENQUEUE_INBOX_SQL,
            event_key,
            ticket_key,
            message.request_id,
            message.upstream_event_id_source,
            safe_payload_json,
            ticket_ref_json,
            attempts,
            current,
            self._encryption_key,
        )
        if row is None:
            raise HDETransportError("inbox_enqueue_returned_no_row")
        return HDEInboxReceipt(
            id=int(row["id"]),
            event_key=str(row["event_key"]),
            request_id=_uuid(row["request_id"]),
            status=InboxStatus(str(row["status"])),
            created=bool(row["created"]),
        )

    async def claim_inbox(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> HDEInboxJob | None:
        worker = _worker_id(worker_id)
        row = await self._executor.fetchrow(
            CLAIM_INBOX_SQL,
            worker,
            _as_utc(now or datetime.now(UTC)),
            self._encryption_key,
        )
        return _inbox_job(row) if row is not None else None

    async def renew_inbox_lease(
        self,
        job: HDEInboxJob,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> None:
        row = await self._executor.fetchrow(
            RENEW_INBOX_LEASE_SQL,
            job.id,
            _worker_id(worker_id),
            _as_utc(now or datetime.now(UTC)),
        )
        _require_row(row, queue="inbox", job_id=job.id)

    async def complete_inbox_with_outbox(
        self,
        job: HDEInboxJob,
        *,
        worker_id: str,
        response_text: str,
        outbox_max_attempts: int = DEFAULT_OUTBOX_MAX_ATTEMPTS,
        now: datetime | None = None,
    ) -> HDEOutboxJob:
        response = str(response_text or "")
        if not response.strip():
            raise HDETransportValidationError("response_text_required")
        delivery_envelope = _serialize_envelope(
            {
                "schema_version": 1,
                "ticket_id": job.ticket_id,
                "response_text": response,
            },
            error_code="delivery_envelope_too_large",
        )
        current = _as_utc(now or datetime.now(UTC))
        row = await self._executor.fetchrow(
            COMPLETE_INBOX_WITH_OUTBOX_SQL,
            job.id,
            _worker_id(worker_id),
            current,
            delivery_envelope,
            self._encryption_key,
            _validated_max_attempts(outbox_max_attempts),
        )
        if row is None:
            raise HDETransportLeaseLost(f"inbox lease lost for job {job.id}")
        return _outbox_job(row)

    async def fail_inbox(
        self,
        job: HDEInboxJob,
        *,
        worker_id: str,
        error_code: str,
        retry_after_seconds: float | None = None,
        now: datetime | None = None,
    ) -> InboxStatus:
        current = _as_utc(now or datetime.now(UTC))
        decision = plan_failure(
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            retry_status=InboxStatus.RETRY,
            dead_letter_status=InboxStatus.DEAD_LETTER,
            now=current,
            retry_after_seconds=retry_after_seconds,
        )
        row = await self._executor.fetchrow(
            FAIL_INBOX_SQL,
            job.id,
            _worker_id(worker_id),
            decision.status.value,
            decision.next_attempt_at,
            _error_code(error_code),
            current,
        )
        _require_row(row, queue="inbox", job_id=job.id)
        return InboxStatus(str(row["status"]))

    async def quarantine_inbox(
        self,
        job: HDEInboxJob,
        *,
        worker_id: str,
        error_code: str,
        now: datetime | None = None,
    ) -> None:
        row = await self._executor.fetchrow(
            QUARANTINE_INBOX_SQL,
            job.id,
            _worker_id(worker_id),
            _error_code(error_code),
            _as_utc(now or datetime.now(UTC)),
        )
        _require_row(row, queue="inbox quarantine", job_id=job.id)

    async def claim_outbox(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> HDEOutboxJob | None:
        row = await self._executor.fetchrow(
            CLAIM_OUTBOX_SQL,
            _worker_id(worker_id),
            _as_utc(now or datetime.now(UTC)),
            self._encryption_key,
        )
        return _outbox_job(row) if row is not None else None

    async def renew_outbox_lease(
        self,
        job: HDEOutboxJob,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> None:
        row = await self._executor.fetchrow(
            RENEW_OUTBOX_LEASE_SQL,
            job.id,
            _worker_id(worker_id),
            _as_utc(now or datetime.now(UTC)),
        )
        _require_row(row, queue="outbox", job_id=job.id)

    async def mark_outbox_delivered(
        self,
        job: HDEOutboxJob,
        *,
        worker_id: str,
        http_status: int | None,
        now: datetime | None = None,
    ) -> None:
        row = await self._executor.fetchrow(
            MARK_OUTBOX_DELIVERED_SQL,
            job.id,
            _worker_id(worker_id),
            http_status,
            _as_utc(now or datetime.now(UTC)),
        )
        _require_row(row, queue="outbox", job_id=job.id)

    async def fail_outbox(
        self,
        job: HDEOutboxJob,
        *,
        worker_id: str,
        error_code: str,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        now: datetime | None = None,
    ) -> OutboxStatus:
        current = _as_utc(now or datetime.now(UTC))
        decision = plan_failure(
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            retry_status=OutboxStatus.RETRY,
            dead_letter_status=OutboxStatus.DEAD_LETTER,
            now=current,
            retry_after_seconds=retry_after_seconds,
        )
        row = await self._executor.fetchrow(
            FAIL_OUTBOX_SQL,
            job.id,
            _worker_id(worker_id),
            decision.status.value,
            decision.next_attempt_at,
            _error_code(error_code),
            http_status,
            retry_after_seconds,
            current,
        )
        _require_row(row, queue="outbox", job_id=job.id)
        return OutboxStatus(str(row["status"]))

    async def quarantine_outbox(
        self,
        job: HDEOutboxJob,
        *,
        worker_id: str,
        error_code: str,
        http_status: int | None = None,
        now: datetime | None = None,
    ) -> None:
        row = await self._executor.fetchrow(
            QUARANTINE_OUTBOX_SQL,
            job.id,
            _worker_id(worker_id),
            _error_code(error_code),
            http_status,
            _as_utc(now or datetime.now(UTC)),
        )
        _require_row(row, queue="outbox quarantine", job_id=job.id)

    async def recover_stale_leases(
        self,
        *,
        stale_before: datetime,
        now: datetime | None = None,
    ) -> RecoveryCounts:
        cutoff = _as_utc(stale_before)
        current = _as_utc(now or datetime.now(UTC))
        inbox = await self._executor.fetchrow(RECOVER_STALE_INBOX_SQL, cutoff, current)
        outbox = await self._executor.fetchrow(RECOVER_STALE_OUTBOX_SQL, cutoff, current)
        return RecoveryCounts(
            inbox_retried=_count(inbox, "retried"),
            inbox_dead_lettered=_count(inbox, "dead_lettered"),
            outbox_retried=_count(outbox, "retried"),
            outbox_dead_lettered=_count(outbox, "dead_lettered"),
        )

    async def get_queue_counts(self) -> HDEQueueCounts:
        row = await self._executor.fetchrow(GET_QUEUE_COUNTS_SQL)
        if row is None:
            raise HDETransportError("queue_counts_returned_no_row")
        return HDEQueueCounts(
            inbox_backlog=_count(row, "inbox_backlog"),
            inbox_processing=_count(row, "inbox_processing"),
            inbox_dead_letter=_count(row, "inbox_dead_letter"),
            outbox_backlog=_count(row, "outbox_backlog"),
            outbox_sending=_count(row, "outbox_sending"),
            outbox_dead_letter=_count(row, "outbox_dead_letter"),
            inbox_oldest_ready_age_seconds=_optional_float(
                row,
                "inbox_oldest_ready_age_seconds",
            ),
            inbox_oldest_processing_age_seconds=_optional_float(
                row,
                "inbox_oldest_processing_age_seconds",
            ),
            outbox_oldest_ready_age_seconds=_optional_float(
                row,
                "outbox_oldest_ready_age_seconds",
            ),
            outbox_oldest_sending_age_seconds=_optional_float(
                row,
                "outbox_oldest_sending_age_seconds",
            ),
        )

    async def requeue_dead_letter_inbox(
        self,
        job_id: int,
        *,
        operator_id: str,
        reason_code: str,
        evidence_sha256: str,
        now: datetime | None = None,
    ) -> None:
        job = _positive_job_id(job_id)
        row = await self._executor.fetchrow(
            REQUEUE_DEAD_INBOX_SQL,
            job,
            _recovery_operator(operator_id),
            _recovery_reason(
                reason_code,
                expected=HDE_RECOVERY_REQUEUE_INBOX_REASON,
            ),
            _recovery_evidence(evidence_sha256),
            _as_utc(now or datetime.now(UTC)),
        )
        _require_recovery_row(row, action="requeue inbox", job_id=job)

    async def requeue_dead_letter_outbox(
        self,
        job_id: int,
        *,
        operator_id: str,
        reason_code: str,
        evidence_sha256: str,
        now: datetime | None = None,
    ) -> None:
        job = _positive_job_id(job_id)
        row = await self._executor.fetchrow(
            REQUEUE_DEAD_OUTBOX_SQL,
            job,
            _recovery_operator(operator_id),
            _recovery_reason(
                reason_code,
                expected=HDE_RECOVERY_REQUEUE_OUTBOX_REASON,
            ),
            _recovery_evidence(evidence_sha256),
            _as_utc(now or datetime.now(UTC)),
        )
        _require_recovery_row(row, action="requeue outbox", job_id=job)

    async def reconcile_dead_letter_outbox_as_delivered(
        self,
        job_id: int,
        *,
        operator_id: str,
        reason_code: str,
        evidence_sha256: str,
        http_status: int | None = None,
        now: datetime | None = None,
    ) -> None:
        job = _positive_job_id(job_id)
        status = _optional_http_status(http_status)
        row = await self._executor.fetchrow(
            RECONCILE_DEAD_OUTBOX_DELIVERED_SQL,
            job,
            _recovery_operator(operator_id),
            _recovery_reason(
                reason_code,
                expected=HDE_RECOVERY_RECONCILE_DELIVERED_REASON,
            ),
            _recovery_evidence(evidence_sha256),
            status,
            _as_utc(now or datetime.now(UTC)),
        )
        _require_recovery_row(row, action="reconcile outbox delivered", job_id=job)


ENQUEUE_INBOX_SQL = """
INSERT INTO hde_inbox (
    event_key, ticket_key, request_id, upstream_event_id_source,
    safe_payload, ticket_ref_ciphertext, status, attempt_count,
    max_attempts, next_attempt_at, created_at, updated_at
)
VALUES (
    $1, $2, $3, $4, $5::jsonb,
    pgp_sym_encrypt(
        $6::text,
        $9::text,
        'cipher-algo=aes256,compress-algo=0,s2k-mode=3,s2k-digest-algo=sha256'
    ),
    'pending', 0, $7, $8, $8, $8
)
ON CONFLICT (event_key) DO UPDATE
SET event_key = hde_inbox.event_key
RETURNING id, event_key, request_id, status, (xmax = 0) AS created
"""


CLAIM_INBOX_SQL = """
WITH candidate AS (
    SELECT queued.id
    FROM hde_inbox AS queued
    WHERE queued.status IN ('pending', 'retry')
      AND queued.next_attempt_at <= $2
      AND queued.attempt_count < queued.max_attempts
      AND NOT EXISTS (
          SELECT 1
          FROM hde_inbox AS older
          WHERE older.ticket_key = queued.ticket_key
            AND older.id < queued.id
            AND older.status <> 'processed'
      )
    ORDER BY queued.id
    FOR UPDATE OF queued SKIP LOCKED
    LIMIT 1
)
UPDATE hde_inbox AS queued
SET status = 'processing',
    attempt_count = queued.attempt_count + 1,
    locked_at = $2,
    locked_by = $1,
    updated_at = $2
FROM candidate
WHERE queued.id = candidate.id
RETURNING queued.id, queued.event_key, queued.ticket_key, queued.request_id,
          queued.upstream_event_id_source, queued.safe_payload,
          pgp_sym_decrypt(queued.ticket_ref_ciphertext, $3::text)::jsonb AS ticket_ref,
          queued.status, queued.attempt_count, queued.max_attempts, queued.locked_by
"""


RENEW_INBOX_LEASE_SQL = """
UPDATE hde_inbox
SET locked_at = $3, updated_at = $3
WHERE id = $1 AND status = 'processing' AND locked_by = $2
RETURNING id
"""


COMPLETE_INBOX_WITH_OUTBOX_SQL = """
WITH completed AS (
    UPDATE hde_inbox
    SET status = 'processed',
        processed_at = $3,
        ticket_ref_ciphertext = NULL,
        locked_at = NULL,
        locked_by = NULL,
        last_error_code = NULL,
        updated_at = $3
    WHERE id = $1 AND status = 'processing' AND locked_by = $2
    RETURNING id, event_key, ticket_key, request_id
), queued AS (
    INSERT INTO hde_outbox (
        inbox_id, event_key, ticket_key, request_id, delivery_envelope_ciphertext,
        status, attempt_count, max_attempts, next_attempt_at, created_at, updated_at
    )
    SELECT id, event_key, ticket_key, request_id,
           pgp_sym_encrypt(
               $4::text,
               $5::text,
               'cipher-algo=aes256,compress-algo=0,s2k-mode=3,s2k-digest-algo=sha256'
           ),
           'pending', 0, $6, $3, $3, $3
    FROM completed
    ON CONFLICT (inbox_id) DO NOTHING
    RETURNING id, inbox_id, event_key, ticket_key, request_id,
              pgp_sym_decrypt(delivery_envelope_ciphertext, $5::text)::jsonb
                  AS delivery_envelope,
              status, attempt_count, max_attempts, locked_by
)
SELECT * FROM queued
LIMIT 1
"""


FAIL_INBOX_SQL = """
UPDATE hde_inbox
SET status = $3,
    next_attempt_at = $4,
    last_error_code = $5,
    dead_lettered_at = CASE WHEN $3 = 'dead_letter' THEN $6 ELSE NULL END,
    locked_at = NULL,
    locked_by = NULL,
    updated_at = $6
WHERE id = $1 AND status = 'processing' AND locked_by = $2
RETURNING status
"""


QUARANTINE_INBOX_SQL = """
UPDATE hde_inbox
SET status = 'dead_letter',
    next_attempt_at = $4,
    last_error_code = $3,
    dead_lettered_at = $4,
    locked_at = NULL,
    locked_by = NULL,
    updated_at = $4
WHERE id = $1 AND status = 'processing' AND locked_by = $2
RETURNING id
"""


CLAIM_OUTBOX_SQL = """
WITH candidate AS (
    SELECT queued.id
    FROM hde_outbox AS queued
    WHERE queued.status IN ('pending', 'retry')
      AND queued.next_attempt_at <= $2
      AND queued.attempt_count < queued.max_attempts
      AND NOT EXISTS (
          SELECT 1
          FROM hde_outbox AS older
          WHERE older.ticket_key = queued.ticket_key
            AND older.id < queued.id
            AND older.status <> 'delivered'
      )
    ORDER BY queued.id
    FOR UPDATE OF queued SKIP LOCKED
    LIMIT 1
)
UPDATE hde_outbox AS queued
SET status = 'sending',
    attempt_count = queued.attempt_count + 1,
    locked_at = $2,
    locked_by = $1,
    updated_at = $2
FROM candidate
WHERE queued.id = candidate.id
RETURNING queued.id, queued.inbox_id, queued.event_key, queued.ticket_key,
          queued.request_id,
          pgp_sym_decrypt(queued.delivery_envelope_ciphertext, $3::text)::jsonb
              AS delivery_envelope,
          queued.status,
          queued.attempt_count, queued.max_attempts, queued.locked_by
"""


RENEW_OUTBOX_LEASE_SQL = """
UPDATE hde_outbox
SET locked_at = $3, updated_at = $3
WHERE id = $1 AND status = 'sending' AND locked_by = $2
RETURNING id
"""


MARK_OUTBOX_DELIVERED_SQL = """
WITH candidate AS (
    SELECT id, inbox_id, request_id
    FROM hde_outbox
    WHERE id = $1 AND status = 'sending' AND locked_by = $2
    FOR UPDATE
), updated_trace AS (
    UPDATE request_traces AS trace
    SET delivery_status = 'delivered',
        delivery_attempted = TRUE,
        delivery_http_status = $3,
        delivery_retry_after_seconds = NULL,
        delivery_error_code = NULL,
        delivered_at = $4
    FROM candidate
    WHERE trace.request_id = candidate.request_id
    RETURNING trace.request_id
), delivered AS (
    UPDATE hde_outbox AS outbox
    SET status = 'delivered',
        delivery_envelope_ciphertext = NULL,
        delivery_http_status = $3,
        retry_after_seconds = NULL,
        last_error_code = NULL,
        delivered_at = $4,
        dead_lettered_at = NULL,
        locked_at = NULL,
        locked_by = NULL,
        updated_at = $4
    FROM candidate, updated_trace
    WHERE outbox.id = candidate.id
      AND updated_trace.request_id = candidate.request_id
    RETURNING outbox.id, outbox.inbox_id
), purged_inbox AS (
    UPDATE hde_inbox AS inbox
    SET safe_payload = '{"schema_version":1,"purged":true}'::jsonb,
        updated_at = $4
    FROM delivered
    WHERE inbox.id = delivered.inbox_id
    RETURNING inbox.id
)
SELECT delivered.id
FROM delivered
JOIN purged_inbox ON purged_inbox.id = delivered.inbox_id
"""


FAIL_OUTBOX_SQL = """
UPDATE hde_outbox
SET status = $3,
    next_attempt_at = $4,
    last_error_code = $5,
    delivery_http_status = $6,
    retry_after_seconds = $7,
    dead_lettered_at = CASE WHEN $3 = 'dead_letter' THEN $8 ELSE NULL END,
    locked_at = NULL,
    locked_by = NULL,
    updated_at = $8
WHERE id = $1 AND status = 'sending' AND locked_by = $2
RETURNING status
"""


QUARANTINE_OUTBOX_SQL = """
UPDATE hde_outbox
SET status = 'dead_letter',
    next_attempt_at = $5,
    last_error_code = $3,
    delivery_http_status = $4,
    retry_after_seconds = NULL,
    dead_lettered_at = $5,
    locked_at = NULL,
    locked_by = NULL,
    updated_at = $5
WHERE id = $1 AND status = 'sending' AND locked_by = $2
RETURNING id
"""


RECOVER_STALE_INBOX_SQL = """
WITH recovered AS (
    UPDATE hde_inbox AS inbox
    SET status = CASE
            WHEN inbox.attempt_count < inbox.max_attempts
                 AND EXISTS (
                     SELECT 1
                     FROM request_traces AS trace
                     WHERE trace.request_id = inbox.request_id
                       AND NULLIF(trace.response_text, '') IS NOT NULL
                 )
            THEN 'retry'
            ELSE 'dead_letter'
        END,
        next_attempt_at = $2,
        last_error_code = CASE
            WHEN inbox.attempt_count < inbox.max_attempts
                 AND EXISTS (
                     SELECT 1
                     FROM request_traces AS trace
                     WHERE trace.request_id = inbox.request_id
                       AND NULLIF(trace.response_text, '') IS NOT NULL
                 )
            THEN 'worker_lease_expired_resume_trace'
            ELSE 'worker_lease_expired_unproven_side_effects'
        END,
        dead_lettered_at = CASE
            WHEN inbox.attempt_count < inbox.max_attempts
                 AND EXISTS (
                     SELECT 1
                     FROM request_traces AS trace
                     WHERE trace.request_id = inbox.request_id
                       AND NULLIF(trace.response_text, '') IS NOT NULL
                 )
            THEN NULL
            ELSE $2
        END,
        locked_at = NULL,
        locked_by = NULL,
        updated_at = $2
    WHERE inbox.status = 'processing' AND inbox.locked_at < $1
    RETURNING inbox.status
)
SELECT COUNT(*) FILTER (WHERE status = 'retry')::int AS retried,
       COUNT(*) FILTER (WHERE status = 'dead_letter')::int AS dead_lettered
FROM recovered
"""


RECOVER_STALE_OUTBOX_SQL = """
WITH recovered AS (
    UPDATE hde_outbox
    SET status = 'dead_letter',
        next_attempt_at = $2,
        last_error_code = 'worker_lease_expired_ambiguous_delivery',
        dead_lettered_at = $2,
        locked_at = NULL,
        locked_by = NULL,
        updated_at = $2
    WHERE status = 'sending' AND locked_at < $1
    RETURNING status
)
SELECT COUNT(*) FILTER (WHERE status = 'retry')::int AS retried,
       COUNT(*) FILTER (WHERE status = 'dead_letter')::int AS dead_lettered
FROM recovered
"""


GET_QUEUE_COUNTS_SQL = """
SELECT
    (SELECT COUNT(*) FROM hde_inbox WHERE status IN ('pending', 'retry'))::int
        AS inbox_backlog,
    (SELECT COUNT(*) FROM hde_inbox WHERE status = 'processing')::int
        AS inbox_processing,
    (SELECT COUNT(*) FROM hde_inbox WHERE status = 'dead_letter')::int
        AS inbox_dead_letter,
    (SELECT COUNT(*) FROM hde_outbox WHERE status IN ('pending', 'retry'))::int
        AS outbox_backlog,
    (SELECT COUNT(*) FROM hde_outbox WHERE status = 'sending')::int
        AS outbox_sending,
    (SELECT COUNT(*) FROM hde_outbox WHERE status = 'dead_letter')::int
        AS outbox_dead_letter,
    (
        SELECT EXTRACT(EPOCH FROM (NOW() - MIN(next_attempt_at)))::double precision
        FROM hde_inbox
        WHERE status IN ('pending', 'retry') AND next_attempt_at <= NOW()
    ) AS inbox_oldest_ready_age_seconds,
    (
        SELECT EXTRACT(EPOCH FROM (NOW() - MIN(locked_at)))::double precision
        FROM hde_inbox
        WHERE status = 'processing'
    ) AS inbox_oldest_processing_age_seconds,
    (
        SELECT EXTRACT(EPOCH FROM (NOW() - MIN(next_attempt_at)))::double precision
        FROM hde_outbox
        WHERE status IN ('pending', 'retry') AND next_attempt_at <= NOW()
    ) AS outbox_oldest_ready_age_seconds,
    (
        SELECT EXTRACT(EPOCH FROM (NOW() - MIN(locked_at)))::double precision
        FROM hde_outbox
        WHERE status = 'sending'
    ) AS outbox_oldest_sending_age_seconds
"""


REQUEUE_DEAD_INBOX_SQL = """
WITH candidate AS (
    SELECT id, request_id, event_key, ticket_key, status,
           attempt_count, last_error_code, dead_lettered_at
    FROM hde_inbox
    WHERE id = $1 AND status = 'dead_letter'
    FOR UPDATE
), requeued AS (
    UPDATE hde_inbox AS inbox
    SET status = 'retry',
        attempt_count = 0,
        next_attempt_at = $5,
        last_error_code = NULL,
        dead_lettered_at = NULL,
        locked_at = NULL,
        locked_by = NULL,
        updated_at = $5
    FROM candidate
    WHERE inbox.id = candidate.id
    RETURNING inbox.id, inbox.request_id, inbox.event_key, inbox.ticket_key,
              candidate.status AS previous_status, inbox.status AS resulting_status,
              candidate.attempt_count AS previous_attempt_count,
              candidate.last_error_code AS previous_error_code,
              candidate.dead_lettered_at AS previous_dead_lettered_at
), audited AS (
    INSERT INTO hde_transport_audit (
        queue, job_id, request_id, event_key, ticket_key, action,
        operator_id, reason_code, evidence_sha256,
        previous_status, resulting_status, previous_attempt_count,
        previous_error_code, previous_dead_lettered_at, created_at
    )
    SELECT 'inbox', id, request_id, event_key, ticket_key, 'requeue_inbox',
           $2, $3, $4, previous_status, resulting_status,
           previous_attempt_count, previous_error_code, previous_dead_lettered_at, $5
    FROM requeued
    RETURNING id
)
SELECT requeued.id
FROM requeued
JOIN audited ON TRUE
"""


REQUEUE_DEAD_OUTBOX_SQL = """
WITH candidate AS (
    SELECT id, request_id, event_key, ticket_key, status,
           attempt_count, last_error_code, dead_lettered_at, delivery_http_status
    FROM hde_outbox
    WHERE id = $1 AND status = 'dead_letter'
    FOR UPDATE
), requeued AS (
    UPDATE hde_outbox AS outbox
    SET status = 'retry',
        attempt_count = 0,
        next_attempt_at = $5,
        last_error_code = NULL,
        delivery_http_status = NULL,
        retry_after_seconds = NULL,
        dead_lettered_at = NULL,
        locked_at = NULL,
        locked_by = NULL,
        updated_at = $5
    FROM candidate
    WHERE outbox.id = candidate.id
    RETURNING outbox.id, outbox.request_id, outbox.event_key, outbox.ticket_key,
              candidate.status AS previous_status, outbox.status AS resulting_status,
              candidate.attempt_count AS previous_attempt_count,
              candidate.last_error_code AS previous_error_code,
              candidate.dead_lettered_at AS previous_dead_lettered_at,
              candidate.delivery_http_status AS previous_delivery_http_status
), audited AS (
    INSERT INTO hde_transport_audit (
        queue, job_id, request_id, event_key, ticket_key, action,
        operator_id, reason_code, evidence_sha256,
        previous_status, resulting_status, previous_attempt_count,
        previous_error_code, previous_dead_lettered_at,
        previous_delivery_http_status, created_at
    )
    SELECT 'outbox', id, request_id, event_key, ticket_key, 'requeue_outbox',
           $2, $3, $4, previous_status, resulting_status,
           previous_attempt_count, previous_error_code, previous_dead_lettered_at,
           previous_delivery_http_status, $5
    FROM requeued
    RETURNING id
)
SELECT requeued.id
FROM requeued
JOIN audited ON TRUE
"""


RECONCILE_DEAD_OUTBOX_DELIVERED_SQL = """
WITH candidate AS (
    SELECT id, inbox_id, request_id, event_key, ticket_key, status,
           attempt_count, last_error_code, dead_lettered_at, delivery_http_status
    FROM hde_outbox
    WHERE id = $1 AND status = 'dead_letter'
    FOR UPDATE
), updated_trace AS (
    UPDATE request_traces AS trace
    SET delivery_status = 'delivered',
        delivery_attempted = TRUE,
        delivery_http_status = $5,
        delivery_retry_after_seconds = NULL,
        delivery_error_code = NULL,
        delivered_at = $6
    FROM candidate
    WHERE trace.request_id = candidate.request_id
    RETURNING trace.request_id
), delivered AS (
    UPDATE hde_outbox AS outbox
    SET status = 'delivered',
        delivery_envelope_ciphertext = NULL,
        delivery_http_status = $5,
        retry_after_seconds = NULL,
        last_error_code = NULL,
        delivered_at = $6,
        dead_lettered_at = NULL,
        locked_at = NULL,
        locked_by = NULL,
        updated_at = $6
    FROM candidate, updated_trace
    WHERE outbox.id = candidate.id
      AND updated_trace.request_id = candidate.request_id
    RETURNING outbox.id, outbox.inbox_id, outbox.request_id,
              outbox.event_key, outbox.ticket_key,
              candidate.status AS previous_status, outbox.status AS resulting_status,
              candidate.attempt_count AS previous_attempt_count,
              candidate.last_error_code AS previous_error_code,
              candidate.dead_lettered_at AS previous_dead_lettered_at,
              candidate.delivery_http_status AS previous_delivery_http_status
), purged_inbox AS (
    UPDATE hde_inbox AS inbox
    SET safe_payload = '{"schema_version":1,"purged":true}'::jsonb,
        updated_at = $6
    FROM delivered
    WHERE inbox.id = delivered.inbox_id
    RETURNING inbox.id
), audited AS (
    INSERT INTO hde_transport_audit (
        queue, job_id, request_id, event_key, ticket_key, action,
        operator_id, reason_code, evidence_sha256,
        previous_status, resulting_status, previous_attempt_count,
        previous_error_code, previous_dead_lettered_at,
        previous_delivery_http_status, delivery_http_status, created_at
    )
    SELECT 'outbox', delivered.id, delivered.request_id,
           delivered.event_key, delivered.ticket_key, 'reconcile_outbox_delivered',
           $2, $3, $4, delivered.previous_status, delivered.resulting_status,
           delivered.previous_attempt_count, delivered.previous_error_code,
           delivered.previous_dead_lettered_at,
           delivered.previous_delivery_http_status, $5, $6
    FROM delivered
    JOIN purged_inbox ON purged_inbox.id = delivered.inbox_id
    RETURNING id
)
SELECT delivered.id
FROM delivered
JOIN updated_trace ON updated_trace.request_id = delivered.request_id
JOIN purged_inbox ON purged_inbox.id = delivered.inbox_id
JOIN audited ON TRUE
"""


def _require_stable_hde_message(message: IncomingMessage) -> tuple[str, str]:
    if message.channel != Channel.HDE:
        raise HDETransportValidationError("hde_channel_required")
    ticket_id = _bounded_required_string(
        message.user_id,
        field="ticket_id",
        limit=HDE_TICKET_ID_MAX_LENGTH,
    )
    upstream_event_id = _bounded_required_string(
        message.upstream_event_id,
        field="upstream_event_id",
        limit=HDE_TICKET_ID_MAX_LENGTH,
    )
    source = str(message.upstream_event_id_source or "").strip()
    if not source or source == "request_id_fallback":
        raise HDEStableEventRequired("stable_upstream_event_id_required")
    if source not in HDE_STABLE_EVENT_ID_SOURCES:
        raise HDEStableEventRequired("unsupported_upstream_event_id_source")
    if len(message.text) > HDE_MESSAGE_TEXT_MAX_LENGTH:
        raise HDETransportValidationError("message_text_too_long")
    return ticket_id, upstream_event_id


def _serialize_safe_payload(
    message: IncomingMessage,
    *,
    masked_text: str,
    masked_forum_context: str | None,
) -> str:
    text = _safe_text(masked_text, field="masked_text", allow_empty=True)
    forum_context = _optional_safe_text(
        masked_forum_context,
        field="masked_forum_context",
        limit=HDE_TRANSPORT_FORUM_CONTEXT_MAX_LENGTH,
    )
    payload = {
        "schema_version": 1,
        "channel": Channel.HDE.value,
        "message_masked": text,
        "timestamp": _as_utc(message.timestamp).isoformat(),
        # Attachment content/IDs are never persisted.  This boolean preserves
        # attachment-only routing without retaining the raw provider object.
        "has_attachments": bool(message.attachments),
        "forum_context_masked": forum_context,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > HDE_TRANSPORT_SAFE_PAYLOAD_MAX_BYTES:
        raise HDETransportValidationError("safe_inbox_payload_too_large")
    return encoded


def _serialize_envelope(payload: dict[str, Any], *, error_code: str) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > HDE_TRANSPORT_ENVELOPE_MAX_BYTES:
        raise HDETransportValidationError(error_code)
    return encoded


def _keyed_digest(secret: str, *, domain: str, parts: tuple[str, ...]) -> str:
    normalized_secret = _transport_secret(secret, field="event_key_secret")
    payload = bytearray(domain.encode("utf-8"))
    for part in parts:
        encoded = part.encode("utf-8")
        payload.extend(len(encoded).to_bytes(4, byteorder="big", signed=False))
        payload.extend(encoded)
    return hmac.new(
        normalized_secret.encode("utf-8"),
        bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _transport_secret(value: str, *, field: str) -> str:
    secret = str(value or "")
    if not secret:
        raise HDETransportValidationError(f"{field}_required")
    if "\x00" in secret:
        raise HDETransportValidationError(f"{field}_contains_nul")
    if len(secret.encode("utf-8")) < HDE_TRANSPORT_SECRET_MIN_LENGTH:
        raise HDETransportValidationError(f"{field}_too_short")
    return secret


def _safe_text(
    value: Any,
    *,
    field: str,
    allow_empty: bool,
    limit: int = HDE_TRANSPORT_SAFE_PAYLOAD_MAX_BYTES,
) -> str:
    if not isinstance(value, str):
        raise HDETransportValidationError(f"{field}_must_be_string")
    if not allow_empty and not value.strip():
        raise HDETransportValidationError(f"{field}_required")
    if "\x00" in value:
        raise HDETransportValidationError(f"{field}_contains_nul")
    if len(value) > limit:
        raise HDETransportValidationError(f"{field}_too_long")
    return value


def _optional_safe_text(value: Any, *, field: str, limit: int) -> str | None:
    if value is None:
        return None
    return _safe_text(value, field=field, allow_empty=True, limit=limit) or None


def _bounded_required_string(value: Any, *, field: str, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HDEStableEventRequired(f"{field}_required")
    if len(normalized) > limit:
        raise HDETransportValidationError(f"{field}_too_long")
    return normalized


def _validated_max_attempts(value: int) -> int:
    attempts = int(value)
    if not 1 <= attempts <= 100:
        raise HDETransportValidationError("max_attempts_out_of_range")
    return attempts


def _worker_id(value: str) -> str:
    worker = str(value or "").strip()
    if not worker:
        raise HDETransportValidationError("worker_id_required")
    if len(worker) > HDE_TRANSPORT_WORKER_ID_MAX_LENGTH:
        raise HDETransportValidationError("worker_id_too_long")
    return worker


def _error_code(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return "unspecified_transport_error"
    return normalized[:HDE_TRANSPORT_ERROR_CODE_MAX_LENGTH]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HDETransportValidationError("timezone_aware_datetime_required")
    return value.astimezone(UTC)


def _uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _decode_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise HDETransportError("inbox_payload_is_not_object")
    return dict(value)


def _validated_safe_payload(value: Any) -> dict[str, Any]:
    payload = _decode_payload(value)
    expected_keys = {
        "schema_version",
        "channel",
        "message_masked",
        "timestamp",
        "has_attachments",
        "forum_context_masked",
    }
    if set(payload) != expected_keys:
        raise HDETransportError("inbox_safe_payload_schema_invalid")
    if payload.get("schema_version") != 1 or payload.get("channel") != Channel.HDE.value:
        raise HDETransportError("inbox_safe_payload_version_invalid")
    text = _safe_text(
        payload.get("message_masked"),
        field="message_masked",
        allow_empty=True,
    )
    forum_context = _optional_safe_text(
        payload.get("forum_context_masked"),
        field="forum_context_masked",
        limit=HDE_TRANSPORT_FORUM_CONTEXT_MAX_LENGTH,
    )
    if not isinstance(payload.get("has_attachments"), bool):
        raise HDETransportError("inbox_has_attachments_invalid")
    try:
        timestamp = _as_utc(datetime.fromisoformat(str(payload.get("timestamp"))))
    except (TypeError, ValueError) as exc:
        raise HDETransportError("inbox_timestamp_invalid") from exc
    return {
        **payload,
        "message_masked": text,
        "forum_context_masked": forum_context,
        "timestamp": timestamp,
    }


def _decode_ticket_ref(value: Any) -> str:
    envelope = _decode_payload(value)
    if set(envelope) != {"schema_version", "ticket_id"} or envelope.get("schema_version") != 1:
        raise HDETransportError("ticket_ref_schema_invalid")
    return _bounded_required_string(
        envelope.get("ticket_id"),
        field="ticket_id",
        limit=HDE_TICKET_ID_MAX_LENGTH,
    )


def _decode_delivery_envelope(value: Any) -> tuple[str, str]:
    envelope = _decode_payload(value)
    expected_keys = {"schema_version", "ticket_id", "response_text"}
    if set(envelope) != expected_keys or envelope.get("schema_version") != 1:
        raise HDETransportError("delivery_envelope_schema_invalid")
    ticket_id = _bounded_required_string(
        envelope.get("ticket_id"),
        field="ticket_id",
        limit=HDE_TICKET_ID_MAX_LENGTH,
    )
    response_text = _safe_text(
        envelope.get("response_text"),
        field="response_text",
        allow_empty=False,
        limit=HDE_TRANSPORT_ENVELOPE_MAX_BYTES,
    )
    return ticket_id, response_text


def _inbox_job(row: Any) -> HDEInboxJob:
    return HDEInboxJob(
        id=int(row["id"]),
        event_key=str(row["event_key"]),
        ticket_key=str(row["ticket_key"]),
        request_id=_uuid(row["request_id"]),
        upstream_event_id_source=str(row["upstream_event_id_source"]),
        safe_payload=_decode_payload(row["safe_payload"]),
        ticket_id=_decode_ticket_ref(row["ticket_ref"]),
        status=InboxStatus(str(row["status"])),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        locked_by=str(row["locked_by"]),
    )


def _outbox_job(row: Any) -> HDEOutboxJob:
    ticket_id, response_text = _decode_delivery_envelope(row["delivery_envelope"])
    return HDEOutboxJob(
        id=int(row["id"]),
        inbox_id=int(row["inbox_id"]),
        event_key=str(row["event_key"]),
        ticket_key=str(row["ticket_key"]),
        request_id=_uuid(row["request_id"]),
        ticket_id=ticket_id,
        response_text=response_text,
        status=OutboxStatus(str(row["status"])),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        locked_by=str(row["locked_by"]) if row["locked_by"] is not None else None,
    )


def _require_row(row: Any, *, queue: str, job_id: int) -> None:
    if row is None:
        raise HDETransportLeaseLost(f"{queue} lease lost for job {job_id}")


def _require_recovery_row(row: Any, *, action: str, job_id: int) -> None:
    if row is None:
        raise HDETransportRecoveryRejected(
            f"manual {action} rejected for dead-letter job {job_id}"
        )


def _positive_job_id(value: int) -> int:
    try:
        job_id = int(value)
    except (TypeError, ValueError) as exc:
        raise HDETransportValidationError("recovery_job_id_invalid") from exc
    if job_id < 1:
        raise HDETransportValidationError("recovery_job_id_invalid")
    return job_id


def _recovery_operator(value: str) -> str:
    operator = str(value or "").strip()
    if not HDE_RECOVERY_OPERATOR_RE.fullmatch(operator):
        raise HDETransportValidationError("recovery_operator_invalid")
    return operator


def _recovery_reason(value: str, *, expected: str) -> str:
    reason = str(value or "").strip()
    if reason != expected:
        raise HDETransportValidationError("recovery_reason_invalid")
    return reason


def _recovery_evidence(value: str) -> str:
    evidence = str(value or "").strip()
    if not HDE_RECOVERY_EVIDENCE_RE.fullmatch(evidence):
        raise HDETransportValidationError("recovery_evidence_sha256_invalid")
    return evidence


def _optional_http_status(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        status = int(value)
    except (TypeError, ValueError) as exc:
        raise HDETransportValidationError("recovery_http_status_invalid") from exc
    if status < 100 or status > 599:
        raise HDETransportValidationError("recovery_http_status_invalid")
    return status


def _count(row: Any, key: str) -> int:
    if row is None or row[key] is None:
        return 0
    return int(row[key])


def _optional_float(row: Any, key: str) -> float | None:
    if row is None or row[key] is None:
        return None
    return max(0.0, float(row[key]))
