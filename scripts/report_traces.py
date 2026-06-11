from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from pathlib import Path

import asyncpg

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import get_settings
from src.ops.reports import build_trace_report, format_trace_report, format_trace_report_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print request trace operational report.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    pool = await _create_pool(settings.postgres_dsn)
    try:
        async with pool.acquire() as conn:
            report = await build_trace_report(conn, args.days)
    finally:
        await pool.close()

    if args.json:
        print(format_trace_report_json(report))
    else:
        print(format_trace_report(report))


async def _create_pool(dsn: str) -> asyncpg.Pool:
    try:
        return await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    except socket.gaierror:
        fallback = _host_fallback_dsn(dsn)
        if not fallback:
            raise
        print(
            "Postgres host from DSN is not resolvable from host process; retrying localhost.",
            file=sys.stderr,
        )
        return await asyncpg.create_pool(fallback, min_size=1, max_size=1)


def _host_fallback_dsn(dsn: str) -> str | None:
    if "@postgres:" in dsn:
        return dsn.replace("@postgres:", "@localhost:", 1)
    if "@postgres/" in dsn:
        return dsn.replace("@postgres/", "@localhost/", 1)
    return None


if __name__ == "__main__":
    asyncio.run(main())
