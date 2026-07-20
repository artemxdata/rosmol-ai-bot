from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from scripts import hde_transport_admin
from src.channels.hde_transport import HDE_RECOVERY_REQUEUE_OUTBOX_REASON


class _Acquire:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def __aenter__(self) -> Any:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeConnection:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return self.rows


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.closed = False

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)

    async def close(self) -> None:
        self.closed = True


def test_mutations_require_exact_reason_evidence_and_double_confirmation() -> None:
    parser_args = hde_transport_admin.parse_args(
        [
            "requeue-outbox",
            "--job-id",
            "23",
            "--confirm-job-id",
            "24",
            "--operator",
            "operator.test",
            "--reason",
            HDE_RECOVERY_REQUEUE_OUTBOX_REASON,
            "--evidence-sha256",
            "a" * 64,
        ]
    )

    with pytest.raises(ValueError, match="confirm_job_id_mismatch"):
        hde_transport_admin._confirm(parser_args)

    with pytest.raises(SystemExit):
        hde_transport_admin.parse_args(
            [
                "requeue-outbox",
                "--job-id",
                "23",
                "--confirm-job-id",
                "23",
                "--operator",
                "operator.test",
                "--reason",
                "free-form-reason",
                "--evidence-sha256",
                "a" * 64,
            ]
        )


@pytest.mark.asyncio
async def test_list_outputs_only_privacy_safe_dead_letter_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            {
                "queue": "outbox",
                "id": 23,
                "request_id": UUID("9d5375bc-7b05-4e2c-8d5b-dc0f0b78dca1"),
                "event_key": "e" * 64,
                "ticket_key": "t" * 64,
                "status": "dead_letter",
                "attempt_count": 1,
                "max_attempts": 8,
                "last_error_code": "ambiguous_delivery",
                "created_at": datetime(2026, 7, 20, tzinfo=UTC),
                "updated_at": datetime(2026, 7, 20, tzinfo=UTC),
                "dead_lettered_at": datetime(2026, 7, 20, tzinfo=UTC),
            }
        ]
    )
    pool = FakePool(connection)

    async def create_pool(*_args: Any, **_kwargs: Any) -> FakePool:
        return pool

    monkeypatch.setattr(
        hde_transport_admin,
        "get_settings",
        lambda: SimpleNamespace(postgres_dsn="unused"),
    )
    monkeypatch.setattr(hde_transport_admin.asyncpg, "create_pool", create_pool)

    result = await hde_transport_admin.run(
        hde_transport_admin.parse_args(["list", "--queue", "outbox"])
    )

    serialized = repr(result)
    assert result["jobs"][0]["request_id"] == "9d5375bc-7b05-4e2c-8d5b-dc0f0b78dca1"
    assert "ticket_id" not in serialized
    assert "response_text" not in serialized
    assert "ciphertext" not in serialized
    assert pool.closed is True


@pytest.mark.asyncio
async def test_requeue_outbox_forwards_mandatory_audit_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    pool = FakePool(connection)
    calls: list[tuple[int, dict[str, Any]]] = []

    async def create_pool(*_args: Any, **_kwargs: Any) -> FakePool:
        return pool

    class FakeRepository:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def requeue_dead_letter_outbox(
            self,
            job_id: int,
            **kwargs: Any,
        ) -> None:
            calls.append((job_id, kwargs))

    monkeypatch.setattr(
        hde_transport_admin,
        "get_settings",
        lambda: SimpleNamespace(
            postgres_dsn="unused",
            hde_transport_event_key_secret="e" * 48,
            hde_transport_encryption_key="k" * 48,
        ),
    )
    monkeypatch.setattr(hde_transport_admin.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(hde_transport_admin, "HDETransportRepository", FakeRepository)
    args = hde_transport_admin.parse_args(
        [
            "requeue-outbox",
            "--job-id",
            "23",
            "--confirm-job-id",
            "23",
            "--operator",
            "operator.test",
            "--reason",
            HDE_RECOVERY_REQUEUE_OUTBOX_REASON,
            "--evidence-sha256",
            "a" * 64,
        ]
    )

    result = await hde_transport_admin.run(args)

    assert result == {
        "mode": "mutation",
        "action": "requeue-outbox",
        "job_id": 23,
        "audited": True,
    }
    assert calls == [
        (
            23,
            {
                "operator_id": "operator.test",
                "reason_code": HDE_RECOVERY_REQUEUE_OUTBOX_REASON,
                "evidence_sha256": "a" * 64,
            },
        )
    ]


def test_main_redacts_database_and_unexpected_error_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leaked = "postgresql://user:secret-password@database.internal/private"

    async def database_failure(_args: Any) -> dict[str, Any]:
        raise asyncpg.InvalidPasswordError(leaked)

    monkeypatch.setattr(hde_transport_admin, "run", database_failure)
    assert hde_transport_admin.main(["list"]) == 2
    captured = capsys.readouterr()
    assert "external_error" in captured.err
    assert leaked not in captured.err

    async def unexpected_failure(_args: Any) -> dict[str, Any]:
        raise RuntimeError(leaked)

    monkeypatch.setattr(hde_transport_admin, "run", unexpected_failure)
    assert hde_transport_admin.main(["list"]) == 2
    captured = capsys.readouterr()
    assert "internal_error" in captured.err
    assert leaked not in captured.err
