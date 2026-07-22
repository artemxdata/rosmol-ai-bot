from __future__ import annotations

import os
from urllib.parse import urlsplit

import asyncpg
import pytest

from src.channels.hde_transport import (
    FAIL_INBOX_SQL,
    FAIL_OUTBOX_SQL,
    RECOVER_STALE_INBOX_SQL,
    RECOVER_STALE_OUTBOX_SQL,
)


@pytest.mark.asyncio
async def test_transport_timestamp_queries_compile_against_postgres() -> None:
    dsn = os.getenv("HDE_TRANSPORT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("HDE_TRANSPORT_TEST_POSTGRES_DSN is not configured")

    database_name = urlsplit(dsn).path.lstrip("/")
    if not database_name.endswith("_test"):
        pytest.fail("HDE transport SQL regression requires an isolated *_test database")

    connection = await asyncpg.connect(dsn)
    try:
        for query in (
            RECOVER_STALE_INBOX_SQL,
            RECOVER_STALE_OUTBOX_SQL,
            FAIL_INBOX_SQL,
            FAIL_OUTBOX_SQL,
        ):
            await connection.prepare(query)
    finally:
        await connection.close()
