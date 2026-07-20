from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from scripts.purge_old_memory import (
    HDE_TERMINAL_COUNT_QUERIES,
    HDE_TERMINAL_DELETE_QUERIES,
    TRACE_COUNT_QUERY,
    TRACE_DELETE_QUERY,
    apply_retention,
    command_count,
    positive_days,
    preview_retention,
    run,
    validate_retention_days,
)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def fetchval(self, query: str, days: int) -> int:
        table = _table_name(query)
        self.calls.append(("count", table, days))
        return {
            "conversation_turns": 7,
            "user_memory": 3,
            "request_traces": 11,
            "hde_outbox_delivered": 5,
            "hde_inbox_processed": 4,
        }[table]

    async def execute(self, query: str, days: int) -> str:
        table = _table_name(query)
        self.calls.append(("delete", table, days))
        count = {
            "conversation_turns": 7,
            "user_memory": 3,
            "request_traces": 11,
            "hde_outbox_delivered": 5,
            "hde_inbox_processed": 4,
        }[table]
        return f"DELETE {count}"

    def transaction(self) -> _Transaction:
        return _Transaction()


def _table_name(query: str) -> str:
    if "FROM request_traces" in query or "DELETE FROM request_traces" in query:
        return "request_traces"
    if "FROM hde_inbox AS inbox" in query or "DELETE FROM hde_inbox AS inbox" in query:
        return "hde_inbox_processed"
    if "FROM hde_outbox" in query or "DELETE FROM hde_outbox" in query:
        return "hde_outbox_delivered"
    return next(
        table
        for table in (
            "conversation_turns",
            "user_memory",
            "request_traces",
        )
        if table in query
    )


@pytest.mark.asyncio
async def test_preview_retention_is_read_only_and_excludes_traces_by_default() -> None:
    connection = _Connection()

    result = await preview_retention(
        connection,
        memory_ttl_days=30,
        request_trace_ttl_days=None,
    )

    assert result == {"conversation_turns": 7, "user_memory": 3}
    assert connection.calls == [
        ("count", "conversation_turns", 30),
        ("count", "user_memory", 30),
    ]


@pytest.mark.asyncio
async def test_apply_retention_includes_traces_only_with_explicit_ttl() -> None:
    connection = _Connection()

    result = await apply_retention(
        connection,
        memory_ttl_days=30,
        request_trace_ttl_days=90,
    )

    assert result == {"conversation_turns": 7, "user_memory": 3, "request_traces": 11}
    assert connection.calls[-1] == ("delete", "request_traces", 90)


@pytest.mark.asyncio
async def test_hde_terminal_retention_is_explicit_and_fk_ordered() -> None:
    connection = _Connection()

    preview = await preview_retention(
        connection,
        memory_ttl_days=30,
        request_trace_ttl_days=None,
        hde_terminal_ttl_days=30,
    )
    deleted = await apply_retention(
        connection,
        memory_ttl_days=30,
        request_trace_ttl_days=None,
        hde_terminal_ttl_days=30,
    )

    assert preview["hde_outbox_delivered"] == 5
    assert preview["hde_inbox_processed"] == 4
    assert deleted["hde_outbox_delivered"] == 5
    assert deleted["hde_inbox_processed"] == 4
    hde_deletes = [call for call in connection.calls if call[0] == "delete" and "hde_" in call[1]]
    assert hde_deletes == [
        ("delete", "hde_outbox_delivered", 30),
        ("delete", "hde_inbox_processed", 30),
    ]


def test_hde_terminal_retention_never_matches_unresolved_or_audit_rows() -> None:
    combined = "\n".join(
        [*HDE_TERMINAL_COUNT_QUERIES.values(), *HDE_TERMINAL_DELETE_QUERIES.values()]
    )

    assert "status = 'delivered'" in combined
    assert "status = 'processed'" in combined
    assert "hde_transport_audit" not in combined
    assert "dead_letter" not in combined


def test_retention_argument_and_command_status_validation() -> None:
    assert positive_days("30") == 30
    assert validate_retention_days("30", field_name="memory_ttl_days") == 30
    assert command_count("DELETE 12") == 12
    with pytest.raises(argparse.ArgumentTypeError, match="positive"):
        positive_days("0")
    with pytest.raises(RuntimeError, match="unexpected PostgreSQL"):
        command_count("unexpected")


def test_trace_retention_preserves_unresolved_hde_jobs() -> None:
    for query in (TRACE_COUNT_QUERY, TRACE_DELETE_QUERY):
        assert "FROM hde_inbox AS inbox" in query
        assert "inbox.status <> 'processed'" in query
        assert "FROM hde_outbox AS outbox" in query
        assert "outbox.status <> 'delivered'" in query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_ttl", "argument_ttl"),
    (
        (0, None),
        (-1, None),
        (30, 0),
        (30, -1),
    ),
)
async def test_run_rejects_non_positive_final_memory_ttl_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
    configured_ttl: int,
    argument_ttl: int | None,
) -> None:
    pool_created = False

    async def forbidden_create_pool(*_args: object, **_kwargs: object) -> object:
        nonlocal pool_created
        pool_created = True
        raise AssertionError("database must not be accessed")

    monkeypatch.setattr(
        "scripts.purge_old_memory.get_settings",
        lambda: SimpleNamespace(memory_ttl_days=configured_ttl, postgres_dsn="unused"),
    )
    monkeypatch.setattr(
        "scripts.purge_old_memory.asyncpg.create_pool",
        forbidden_create_pool,
    )
    args = argparse.Namespace(
        memory_ttl_days=argument_ttl,
        request_trace_ttl_days=None,
        apply=True,
    )

    with pytest.raises(ValueError, match="memory_ttl_days must be at least 1 day"):
        await run(args)

    assert pool_created is False
