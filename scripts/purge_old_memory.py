from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import asyncpg

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import get_settings

MEMORY_COUNT_QUERIES = {
    "conversation_turns": """
        SELECT COUNT(*)
        FROM conversation_turns
        WHERE created_at < NOW() - ($1::int * INTERVAL '1 day')
    """,
    "user_memory": """
        SELECT COUNT(*)
        FROM user_memory
        WHERE last_interaction < NOW() - ($1::int * INTERVAL '1 day')
    """,
}

MEMORY_DELETE_QUERIES = {
    "conversation_turns": """
        DELETE FROM conversation_turns
        WHERE created_at < NOW() - ($1::int * INTERVAL '1 day')
    """,
    "user_memory": """
        DELETE FROM user_memory
        WHERE last_interaction < NOW() - ($1::int * INTERVAL '1 day')
    """,
}

TRACE_COUNT_QUERY = """
    SELECT COUNT(*)
    FROM request_traces
    WHERE timestamp < NOW() - ($1::int * INTERVAL '1 day')
"""

TRACE_DELETE_QUERY = """
    DELETE FROM request_traces
    WHERE timestamp < NOW() - ($1::int * INTERVAL '1 day')
"""


def positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("retention days must be an integer") from exc
    if days < 1:
        raise argparse.ArgumentTypeError("retention days must be positive")
    return days


def validate_retention_days(value: Any, *, field_name: str) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if days < 1:
        raise ValueError(f"{field_name} must be at least 1 day")
    return days


def command_count(status: str) -> int:
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"unexpected PostgreSQL command status: {status!r}") from exc


async def preview_retention(
    connection: Any,
    *,
    memory_ttl_days: int,
    request_trace_ttl_days: int | None,
) -> dict[str, int]:
    counts = {
        table: int(await connection.fetchval(query, memory_ttl_days) or 0)
        for table, query in MEMORY_COUNT_QUERIES.items()
    }
    if request_trace_ttl_days is not None:
        counts["request_traces"] = int(
            await connection.fetchval(TRACE_COUNT_QUERY, request_trace_ttl_days) or 0
        )
    return counts


async def apply_retention(
    connection: Any,
    *,
    memory_ttl_days: int,
    request_trace_ttl_days: int | None,
) -> dict[str, int]:
    deleted: dict[str, int] = {}
    async with connection.transaction():
        for table, query in MEMORY_DELETE_QUERIES.items():
            deleted[table] = command_count(await connection.execute(query, memory_ttl_days))
        if request_trace_ttl_days is not None:
            deleted["request_traces"] = command_count(
                await connection.execute(TRACE_DELETE_QUERY, request_trace_ttl_days)
            )
    return deleted


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    configured_memory_ttl = (
        args.memory_ttl_days
        if args.memory_ttl_days is not None
        else settings.memory_ttl_days
    )
    memory_ttl_days = validate_retention_days(
        configured_memory_ttl,
        field_name="memory_ttl_days",
    )
    request_trace_ttl_days = (
        validate_retention_days(
            args.request_trace_ttl_days,
            field_name="request_trace_ttl_days",
        )
        if args.request_trace_ttl_days is not None
        else None
    )
    pool = await asyncpg.create_pool(settings.postgres_dsn, min_size=1, max_size=1)
    try:
        async with pool.acquire() as connection:
            eligible = await preview_retention(
                connection,
                memory_ttl_days=memory_ttl_days,
                request_trace_ttl_days=request_trace_ttl_days,
            )
            deleted = (
                await apply_retention(
                    connection,
                    memory_ttl_days=memory_ttl_days,
                    request_trace_ttl_days=request_trace_ttl_days,
                )
                if args.apply
                else {}
            )
    finally:
        await pool.close()

    return {
        "mode": "apply" if args.apply else "dry-run",
        "memory_ttl_days": memory_ttl_days,
        "request_trace_ttl_days": request_trace_ttl_days,
        "eligible_rows": eligible,
        "deleted_rows": deleted,
        "scheduler_configured": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or apply retention for conversation memory. "
            "The default is a read-only dry run; deletion requires --apply."
        )
    )
    parser.add_argument(
        "--memory-ttl-days",
        type=positive_days,
        default=None,
        help="Override MEMORY_TTL_DAYS for user_memory and conversation_turns.",
    )
    parser.add_argument(
        "--request-trace-ttl-days",
        type=positive_days,
        default=None,
        help=(
            "Include request_traces with this separately approved retention. "
            "Without this option request_traces are never touched."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete eligible rows. Without this flag the command only reports counts.",
    )
    return parser.parse_args()


def main() -> None:
    print(json.dumps(asyncio.run(run(parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
