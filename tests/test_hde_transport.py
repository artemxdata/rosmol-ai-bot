from __future__ import annotations

import importlib.util
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from src.channels.hde_transport import (
    CLAIM_INBOX_SQL,
    CLAIM_OUTBOX_SQL,
    COMPLETE_INBOX_WITH_OUTBOX_SQL,
    ENQUEUE_INBOX_SQL,
    FAIL_INBOX_SQL,
    FAIL_OUTBOX_SQL,
    HDE_RECOVERY_RECONCILE_DELIVERED_REASON,
    HDE_RECOVERY_REQUEUE_INBOX_REASON,
    HDE_RECOVERY_REQUEUE_OUTBOX_REASON,
    MARK_OUTBOX_DELIVERED_SQL,
    RECOVER_STALE_INBOX_SQL,
    RECOVER_STALE_OUTBOX_SQL,
    HDEInboxJob,
    HDEOutboxJob,
    HDEStableEventRequired,
    HDETransportError,
    HDETransportLeaseLost,
    HDETransportRepository,
    HDETransportValidationError,
    InboxStatus,
    OutboxStatus,
    build_hde_event_key,
    calculate_backoff_seconds,
    plan_failure,
)
from src.models import Channel, IncomingMessage

NOW = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
RECOVERY_OPERATOR = "operator.test"
RECOVERY_EVIDENCE = "f" * 64
REQUEST_ID = UUID("9d5375bc-7b05-4e2c-8d5b-dc0f0b78dca1")
EVENT_KEY_SECRET = "event-key-secret-" + "a" * 48
ENCRYPTION_KEY = "transport-encryption-key-" + "b" * 48
MASKED_TEXT = "Question from [EMAIL]"
MASKED_FORUM_CONTEXT = "Forum [NAME]"
RAW_TEXT = "Question from ivan@example.test"
RESPONSE_TEXT = "Confirmed answer"


class FakeExecutor:
    def __init__(self, *rows: dict[str, Any] | None) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((query, args))
        if not self.rows:
            raise AssertionError("unexpected fetchrow call")
        return self.rows.pop(0)


def _repository(executor: FakeExecutor) -> HDETransportRepository:
    return HDETransportRepository(
        executor,
        event_key_secret=EVENT_KEY_SECRET,
        encryption_key=ENCRYPTION_KEY,
    )


def _message(
    *,
    ticket_id: str = "ticket-42",
    event_id: str | None = "message-9001",
    source: str | None = "message.id",
) -> IncomingMessage:
    return IncomingMessage(
        user_id=ticket_id,
        channel=Channel.HDE,
        text=RAW_TEXT,
        timestamp=NOW,
        request_id=REQUEST_ID,
        upstream_event_id=event_id,
        upstream_event_id_source=source,
        attachments=[
            {
                "type": "image",
                "id": "raw-attachment-1",
                "url": "https://private.invalid/raw-attachment-1",
            }
        ],
        forum_context="Raw forum for Ivan Petrov",
        eval_run_id="raw-eval-run-must-not-persist",
        eval_case_id="raw-eval-case-must-not-persist",
    )


def _safe_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "channel": "hde",
        "message_masked": MASKED_TEXT,
        "timestamp": NOW.isoformat(),
        "has_attachments": True,
        "forum_context_masked": MASKED_FORUM_CONTEXT,
    }


def _inbox_row(*, attempt_count: int = 1, max_attempts: int = 5) -> dict[str, Any]:
    return {
        "id": 17,
        "event_key": "e" * 64,
        "ticket_key": "t" * 64,
        "request_id": REQUEST_ID,
        "upstream_event_id_source": "message.id",
        "safe_payload": _safe_payload(),
        # In production this alias is produced only by pgp_sym_decrypt in the
        # claim query; the table itself contains ciphertext.
        "ticket_ref": {"schema_version": 1, "ticket_id": "ticket-42"},
        "status": "processing",
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "locked_by": "worker-a",
    }


def _outbox_row(*, attempt_count: int = 0, max_attempts: int = 8) -> dict[str, Any]:
    return {
        "id": 23,
        "inbox_id": 17,
        "event_key": "e" * 64,
        "ticket_key": "t" * 64,
        "request_id": REQUEST_ID,
        # This alias is returned by pgp_sym_decrypt, never stored as JSON/plaintext.
        "delivery_envelope": {
            "schema_version": 1,
            "ticket_id": "ticket-42",
            "response_text": RESPONSE_TEXT,
        },
        "status": "pending" if attempt_count == 0 else "sending",
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "locked_by": None if attempt_count == 0 else "worker-a",
    }


def _inbox_job(*, attempt_count: int = 1, max_attempts: int = 5) -> HDEInboxJob:
    row = _inbox_row(attempt_count=attempt_count, max_attempts=max_attempts)
    return HDEInboxJob(
        id=row["id"],
        event_key=row["event_key"],
        ticket_key=row["ticket_key"],
        request_id=row["request_id"],
        upstream_event_id_source=row["upstream_event_id_source"],
        safe_payload=row["safe_payload"],
        ticket_id="ticket-42",
        status=InboxStatus.PROCESSING,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        locked_by="worker-a",
    )


def _outbox_job(*, attempt_count: int = 1, max_attempts: int = 8) -> HDEOutboxJob:
    return HDEOutboxJob(
        id=23,
        inbox_id=17,
        event_key="e" * 64,
        ticket_key="t" * 64,
        request_id=REQUEST_ID,
        ticket_id="ticket-42",
        response_text=RESPONSE_TEXT,
        status=OutboxStatus.SENDING,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        locked_by="worker-a",
    )


def test_event_key_is_deterministic_pseudonymous_and_scoped() -> None:
    message = _message()

    first = build_hde_event_key(message, secret=EVENT_KEY_SECRET)
    second = build_hde_event_key(message, secret=EVENT_KEY_SECRET)
    other_event = build_hde_event_key(
        _message(event_id="message-9002"),
        secret=EVENT_KEY_SECRET,
    )
    other_ticket = build_hde_event_key(
        _message(ticket_id="ticket-43"),
        secret=EVENT_KEY_SECRET,
    )

    assert first == second
    assert len(first) == 64
    assert "ticket" not in first
    assert first != other_event
    assert first != other_ticket


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_message(event_id=None, source=None), "upstream_event_id_required"),
        (
            _message(event_id=str(REQUEST_ID), source="request_id_fallback"),
            "stable_upstream_event_id_required",
        ),
        (
            _message(source="attacker.controlled.source"),
            "unsupported_upstream_event_id_source",
        ),
    ],
)
def test_event_key_rejects_missing_fallback_or_unknown_identity_source(
    message: IncomingMessage,
    expected: str,
) -> None:
    with pytest.raises(HDEStableEventRequired, match=expected):
        build_hde_event_key(message, secret=EVENT_KEY_SECRET)


def test_transport_requires_long_distinct_dedicated_secrets() -> None:
    executor = FakeExecutor()
    with pytest.raises(HDETransportValidationError, match="event_key_secret_too_short"):
        HDETransportRepository(
            executor,
            event_key_secret="short",
            encryption_key=ENCRYPTION_KEY,
        )
    with pytest.raises(HDETransportValidationError, match="encryption_key_too_short"):
        HDETransportRepository(
            executor,
            event_key_secret=EVENT_KEY_SECRET,
            encryption_key="short",
        )
    with pytest.raises(HDETransportValidationError, match="transport_secrets_must_be_distinct"):
        HDETransportRepository(
            executor,
            event_key_secret=EVENT_KEY_SECRET,
            encryption_key=EVENT_KEY_SECRET,
        )


def test_exponential_backoff_and_dead_letter_plan() -> None:
    assert calculate_backoff_seconds(1) == 5.0
    assert calculate_backoff_seconds(4) == 40.0
    assert calculate_backoff_seconds(20) == 3600.0
    assert calculate_backoff_seconds(2, retry_after_seconds=1200) == 1200.0

    retry = plan_failure(
        attempt_count=2,
        max_attempts=5,
        retry_status=InboxStatus.RETRY,
        dead_letter_status=InboxStatus.DEAD_LETTER,
        now=NOW,
    )
    dead = plan_failure(
        attempt_count=5,
        max_attempts=5,
        retry_status=InboxStatus.RETRY,
        dead_letter_status=InboxStatus.DEAD_LETTER,
        now=NOW,
    )

    assert retry.status == InboxStatus.RETRY
    assert retry.next_attempt_at == NOW + timedelta(seconds=10)
    assert dead.status == InboxStatus.DEAD_LETTER
    assert dead.next_attempt_at == NOW


@pytest.mark.asyncio
async def test_enqueue_persists_only_masked_payload_and_pgcrypto_ticket_ref() -> None:
    executor = FakeExecutor(
        {
            "id": 17,
            "event_key": "e" * 64,
            "request_id": REQUEST_ID,
            "status": "pending",
            "created": True,
        }
    )
    repository = _repository(executor)

    receipt = await repository.enqueue_inbox(
        _message(),
        masked_text=MASKED_TEXT,
        masked_forum_context=MASKED_FORUM_CONTEXT,
        now=NOW,
    )

    query, args = executor.calls[0]
    safe_payload = json.loads(args[4])
    ticket_ref = json.loads(args[5])
    assert query == ENQUEUE_INBOX_SQL
    assert "ON CONFLICT (event_key)" in query
    assert "pgp_sym_encrypt" in query
    assert "ticket_ref_ciphertext" in query
    assert "upstream_event_id," not in query
    assert receipt.created is True
    assert receipt.request_id == REQUEST_ID
    assert safe_payload == _safe_payload()
    assert set(safe_payload).isdisjoint(
        {"user_id", "text", "attachments", "upstream_event_id", "forum_context"}
    )
    assert ticket_ref == {"schema_version": 1, "ticket_id": "ticket-42"}
    serialized_safe_payload = json.dumps(safe_payload)
    assert RAW_TEXT not in serialized_safe_payload
    assert "raw-attachment-1" not in serialized_safe_payload
    assert "message-9001" not in "|".join(str(arg) for arg in args)
    assert args[7] == NOW
    assert args[8] == ENCRYPTION_KEY


@pytest.mark.asyncio
async def test_enqueue_rejects_fallback_before_touching_database() -> None:
    executor = FakeExecutor()
    repository = _repository(executor)

    with pytest.raises(HDEStableEventRequired):
        await repository.enqueue_inbox(
            _message(event_id=str(REQUEST_ID), source="request_id_fallback"),
            masked_text=MASKED_TEXT,
            now=NOW,
        )

    assert executor.calls == []


@pytest.mark.asyncio
async def test_claim_inbox_decrypts_ticket_and_reconstructs_redacted_message() -> None:
    executor = FakeExecutor(_inbox_row())
    repository = _repository(executor)

    job = await repository.claim_inbox(worker_id="worker-a", now=NOW)

    assert job is not None
    message = job.incoming_message()
    assert message.request_id == REQUEST_ID
    assert message.user_id == "ticket-42"
    assert message.text == MASKED_TEXT
    assert message.forum_context == MASKED_FORUM_CONTEXT
    assert message.attachments == [{"redacted": True}]
    assert message.upstream_event_id == "e" * 64
    assert message.upstream_event_id_source == "hde_event_key_hmac_v1"
    query, args = executor.calls[0]
    assert query == CLAIM_INBOX_SQL
    assert "FOR UPDATE OF queued SKIP LOCKED" in query
    assert "older.status <> 'processed'" in query
    assert "pgp_sym_decrypt" in query
    assert "attempt_count = queued.attempt_count + 1" in query
    assert args == ("worker-a", NOW, ENCRYPTION_KEY)


@pytest.mark.asyncio
async def test_inbox_job_rejects_unexpected_safe_payload_keys() -> None:
    row = _inbox_row()
    row["safe_payload"] = {**_safe_payload(), "user_id": "raw-ticket"}
    repository = _repository(FakeExecutor(row))
    job = await repository.claim_inbox(worker_id="worker-a", now=NOW)

    assert job is not None
    with pytest.raises(HDETransportError, match="safe_payload_schema_invalid"):
        job.incoming_message()


@pytest.mark.asyncio
async def test_complete_inbox_and_encrypted_outbox_are_one_database_statement() -> None:
    executor = FakeExecutor(_outbox_row())
    repository = _repository(executor)

    outbox = await repository.complete_inbox_with_outbox(
        _inbox_job(),
        worker_id="worker-a",
        response_text=RESPONSE_TEXT,
        now=NOW,
    )

    query, args = executor.calls[0]
    envelope = json.loads(args[3])
    assert query == COMPLETE_INBOX_WITH_OUTBOX_SQL
    assert "WITH completed AS" in query
    assert "ticket_ref_ciphertext = NULL" in query
    assert "INSERT INTO hde_outbox" in query
    assert "delivery_envelope_ciphertext" in query
    assert "pgp_sym_encrypt" in query
    assert "ON CONFLICT (inbox_id) DO NOTHING" in query
    assert envelope == {
        "schema_version": 1,
        "ticket_id": "ticket-42",
        "response_text": RESPONSE_TEXT,
    }
    assert args[0:3] == (17, "worker-a", NOW)
    assert args[4:] == (ENCRYPTION_KEY, 8)
    assert outbox.inbox_id == 17
    assert outbox.status == OutboxStatus.PENDING


@pytest.mark.asyncio
async def test_fail_inbox_retries_then_dead_letters_at_attempt_limit() -> None:
    retry_executor = FakeExecutor({"status": "retry"})
    retry_repository = _repository(retry_executor)
    retry_status = await retry_repository.fail_inbox(
        _inbox_job(attempt_count=2, max_attempts=5),
        worker_id="worker-a",
        error_code="processing_failed",
        now=NOW,
    )
    assert retry_status == InboxStatus.RETRY
    assert retry_executor.calls[0][1][2] == "retry"
    assert retry_executor.calls[0][1][3] == NOW + timedelta(seconds=10)

    dead_executor = FakeExecutor({"status": "dead_letter"})
    dead_repository = _repository(dead_executor)
    dead_status = await dead_repository.fail_inbox(
        _inbox_job(attempt_count=5, max_attempts=5),
        worker_id="worker-a",
        error_code="processing_failed",
        now=NOW,
    )
    assert dead_status == InboxStatus.DEAD_LETTER
    assert dead_executor.calls[0][1][2] == "dead_letter"


@pytest.mark.asyncio
async def test_claim_and_finish_outbox_decrypts_then_purges_sensitive_envelopes() -> None:
    sending = _outbox_row(attempt_count=1)
    delivered = {"id": 23}
    executor = FakeExecutor(sending, delivered)
    repository = _repository(executor)

    job = await repository.claim_outbox(worker_id="worker-a", now=NOW)
    assert job is not None
    assert job.ticket_id == "ticket-42"
    assert job.response_text == RESPONSE_TEXT
    await repository.mark_outbox_delivered(
        job,
        worker_id="worker-a",
        http_status=200,
        now=NOW,
    )

    claim_query, claim_args = executor.calls[0]
    delivery_query, delivery_args = executor.calls[1]
    assert claim_query == CLAIM_OUTBOX_SQL
    assert "older.status <> 'delivered'" in claim_query
    assert "pgp_sym_decrypt" in claim_query
    assert claim_args == ("worker-a", NOW, ENCRYPTION_KEY)
    assert delivery_query == MARK_OUTBOX_DELIVERED_SQL
    assert "delivery_envelope_ciphertext = NULL" in delivery_query
    assert "UPDATE request_traces AS trace" in delivery_query
    assert "delivery_status = 'delivered'" in delivery_query
    assert "delivery_attempted = TRUE" in delivery_query
    assert "safe_payload =" in delivery_query
    assert '"purged":true' in delivery_query
    assert delivery_args == (23, "worker-a", 200, NOW)


@pytest.mark.asyncio
async def test_fail_outbox_honors_provider_retry_after() -> None:
    executor = FakeExecutor({"status": "retry"})
    repository = _repository(executor)

    status = await repository.fail_outbox(
        _outbox_job(attempt_count=1),
        worker_id="worker-a",
        error_code="hde_remote_rate_limit",
        http_status=429,
        retry_after_seconds=1200,
        now=NOW,
    )

    args = executor.calls[0][1]
    assert status == OutboxStatus.RETRY
    assert args[2] == "retry"
    assert args[3] == NOW + timedelta(seconds=1200)
    assert args[5:7] == (429, 1200)


@pytest.mark.asyncio
async def test_lost_worker_lease_fails_closed() -> None:
    executor = FakeExecutor(None)
    repository = _repository(executor)

    with pytest.raises(HDETransportLeaseLost, match="inbox lease lost"):
        await repository.complete_inbox_with_outbox(
            _inbox_job(),
            worker_id="worker-a",
            response_text="Answer",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_recovery_requeues_or_dead_letters_stale_worker_leases() -> None:
    executor = FakeExecutor(
        {"retried": 3, "dead_lettered": 1},
        {"retried": 2, "dead_lettered": 4},
    )
    repository = _repository(executor)
    stale_before = NOW - timedelta(minutes=5)

    result = await repository.recover_stale_leases(
        stale_before=stale_before,
        now=NOW,
    )

    assert result.inbox_retried == 3
    assert result.inbox_dead_lettered == 1
    assert result.outbox_retried == 2
    assert result.outbox_dead_lettered == 4
    assert "worker_lease_expired" in executor.calls[0][0]
    assert "NULLIF(trace.response_text, '') IS NOT NULL" in executor.calls[0][0]
    assert "worker_lease_expired_ambiguous_delivery" in executor.calls[1][0]
    assert executor.calls[0][1] == (stale_before, NOW)


@pytest.mark.parametrize(
    ("query", "timestamp_parameters"),
    [
        (RECOVER_STALE_INBOX_SQL, (1, 2)),
        (RECOVER_STALE_OUTBOX_SQL, (1, 2)),
        (FAIL_INBOX_SQL, (4, 6)),
        (FAIL_OUTBOX_SQL, (4, 8)),
    ],
)
def test_transport_timestamp_parameters_are_explicitly_typed(
    query: str,
    timestamp_parameters: tuple[int, ...],
) -> None:
    # asyncpg asks PostgreSQL to infer positional parameter types while preparing
    # the statement. A parameter used in a CASE with NULL can otherwise resolve
    # to text even when another occurrence targets a timestamptz column.
    for parameter in timestamp_parameters:
        assert re.search(rf"\${parameter}(?!::timestamptz)", query) is None


@pytest.mark.parametrize("query", [FAIL_INBOX_SQL, FAIL_OUTBOX_SQL])
def test_transport_failure_status_parameter_is_explicitly_typed(query: str) -> None:
    assert re.search(r"\$3(?!::varchar\(24\))", query) is None


@pytest.mark.asyncio
async def test_manual_dead_letter_recovery_is_explicit() -> None:
    executor = FakeExecutor({"id": 17}, {"id": 23})
    repository = _repository(executor)

    await repository.requeue_dead_letter_inbox(
        17,
        operator_id=RECOVERY_OPERATOR,
        reason_code=HDE_RECOVERY_REQUEUE_INBOX_REASON,
        evidence_sha256=RECOVERY_EVIDENCE,
        now=NOW,
    )
    await repository.requeue_dead_letter_outbox(
        23,
        operator_id=RECOVERY_OPERATOR,
        reason_code=HDE_RECOVERY_REQUEUE_OUTBOX_REASON,
        evidence_sha256=RECOVERY_EVIDENCE,
        now=NOW,
    )

    assert "status = 'dead_letter'" in executor.calls[0][0]
    assert "attempt_count = 0" in executor.calls[0][0]
    assert "INSERT INTO hde_transport_audit" in executor.calls[0][0]
    assert "previous_attempt_count" in executor.calls[0][0]
    assert "previous_error_code" in executor.calls[0][0]
    assert "previous_dead_lettered_at" in executor.calls[0][0]
    assert executor.calls[0][1] == (
        17,
        RECOVERY_OPERATOR,
        HDE_RECOVERY_REQUEUE_INBOX_REASON,
        RECOVERY_EVIDENCE,
        NOW,
    )
    assert "status = 'dead_letter'" in executor.calls[1][0]
    assert "INSERT INTO hde_transport_audit" in executor.calls[1][0]
    assert "previous_delivery_http_status" in executor.calls[1][0]
    assert executor.calls[1][1] == (
        23,
        RECOVERY_OPERATOR,
        HDE_RECOVERY_REQUEUE_OUTBOX_REASON,
        RECOVERY_EVIDENCE,
        NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operator_id", "reason_code", "evidence_sha256", "expected"),
    [
        ("bad operator", HDE_RECOVERY_REQUEUE_INBOX_REASON, RECOVERY_EVIDENCE, "operator"),
        (RECOVERY_OPERATOR, "free-form-unsafe-reason", RECOVERY_EVIDENCE, "reason"),
        (RECOVERY_OPERATOR, HDE_RECOVERY_REQUEUE_INBOX_REASON, "not-a-digest", "evidence"),
    ],
)
async def test_manual_recovery_rejects_unauditable_metadata(
    operator_id: str,
    reason_code: str,
    evidence_sha256: str,
    expected: str,
) -> None:
    executor = FakeExecutor()
    repository = _repository(executor)

    with pytest.raises(HDETransportValidationError, match=expected):
        await repository.requeue_dead_letter_inbox(
            17,
            operator_id=operator_id,
            reason_code=reason_code,
            evidence_sha256=evidence_sha256,
            now=NOW,
        )

    assert executor.calls == []


@pytest.mark.asyncio
async def test_quarantine_reconciliation_and_aggregate_counts_are_explicit() -> None:
    executor = FakeExecutor(
        {"id": 17},
        {"id": 23},
        {"id": 23},
        {
            "inbox_backlog": 2,
            "inbox_processing": 1,
            "inbox_dead_letter": 3,
            "outbox_backlog": 4,
            "outbox_sending": 1,
            "outbox_dead_letter": 5,
            "inbox_oldest_ready_age_seconds": 12.5,
            "inbox_oldest_processing_age_seconds": 4.0,
            "outbox_oldest_ready_age_seconds": None,
            "outbox_oldest_sending_age_seconds": 3.0,
        },
    )
    repository = _repository(executor)

    await repository.quarantine_inbox(
        _inbox_job(),
        worker_id="worker-a",
        error_code="unproven_side_effects",
        now=NOW,
    )
    await repository.quarantine_outbox(
        _outbox_job(),
        worker_id="worker-a",
        error_code="ambiguous_delivery",
        now=NOW,
    )
    await repository.reconcile_dead_letter_outbox_as_delivered(
        23,
        operator_id=RECOVERY_OPERATOR,
        reason_code=HDE_RECOVERY_RECONCILE_DELIVERED_REASON,
        evidence_sha256=RECOVERY_EVIDENCE,
        now=NOW,
    )
    counts = await repository.get_queue_counts()

    assert "status = 'dead_letter'" in executor.calls[0][0]
    assert "status = 'dead_letter'" in executor.calls[1][0]
    assert "delivery_envelope_ciphertext = NULL" in executor.calls[2][0]
    assert "UPDATE request_traces AS trace" in executor.calls[2][0]
    assert "delivery_status = 'delivered'" in executor.calls[2][0]
    assert "delivery_attempted = TRUE" in executor.calls[2][0]
    assert "WITH candidate AS" in executor.calls[2][0]
    assert "FROM candidate" in executor.calls[2][0]
    assert "INSERT INTO hde_transport_audit" in executor.calls[2][0]
    assert '"purged":true' in executor.calls[2][0]
    assert counts.inbox_dead_letter == 3
    assert counts.outbox_dead_letter == 5
    assert counts.as_dict()["outbox_backlog"] == 4
    assert counts.inbox_oldest_ready_age_seconds == 12.5
    assert counts.outbox_oldest_ready_age_seconds is None
    assert "MIN(next_attempt_at)" in executor.calls[3][0]
    assert "MIN(locked_at)" in executor.calls[3][0]


def test_migration_008_declares_privacy_safe_constrained_queue_tables() -> None:
    path = Path("migrations/versions/008_hde_durable_transport.py")
    spec = importlib.util.spec_from_file_location("migration_008", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "008_hde_durable_transport"
    assert module.down_revision == "007_hde_delivery_telemetry"
    source = path.read_text(encoding="utf-8")
    assert '"hde_inbox"' in source
    assert '"hde_outbox"' in source
    assert '"hde_transport_audit"' in source
    assert "safe_payload" in source
    assert "ticket_ref_ciphertext" in source
    assert "delivery_envelope_ciphertext" in source
    assert "ck_hde_inbox_safe_payload_keys" in source
    assert "ck_hde_inbox_ticket_ref_lifecycle" in source
    assert "ck_hde_outbox_envelope_lifecycle" in source
    assert "ck_hde_inbox_lease_lifecycle" in source
    assert "ck_hde_outbox_lease_lifecycle" in source
    assert "ck_hde_transport_audit_action_reason" in source
    assert "safe_payload - ARRAY[" in source
    assert "jsonb_typeof(safe_payload->'has_attachments') = 'boolean'" in source
    assert "previous_attempt_count" in source
    assert "previous_error_code" in source
    assert "previous_dead_lettered_at" in source
    assert "trg_hde_transport_audit_append_only" in source
    assert "BEFORE UPDATE OR DELETE ON hde_transport_audit" in source
    assert 'sa.Column("upstream_event_id",' not in source
    assert 'sa.Column("ticket_id",' not in source
    assert 'sa.Column("response_text",' not in source
