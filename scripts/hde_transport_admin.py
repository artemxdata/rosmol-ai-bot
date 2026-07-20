from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.channels.hde_transport import (
    HDE_RECOVERY_RECONCILE_DELIVERED_REASON,
    HDE_RECOVERY_REQUEUE_INBOX_REASON,
    HDE_RECOVERY_REQUEUE_OUTBOX_REASON,
    HDETransportError,
    HDETransportRepository,
)
from src.config import get_settings

DEAD_LETTER_LIST_SQL = """
SELECT *
FROM (
    SELECT 'inbox'::text AS queue, id, request_id, event_key, ticket_key,
           status, attempt_count, max_attempts, last_error_code,
           created_at, updated_at, dead_lettered_at
    FROM hde_inbox
    WHERE status = 'dead_letter'
    UNION ALL
    SELECT 'outbox'::text AS queue, id, request_id, event_key, ticket_key,
           status, attempt_count, max_attempts, last_error_code,
           created_at, updated_at, dead_lettered_at
    FROM hde_outbox
    WHERE status = 'dead_letter'
) AS jobs
WHERE ($1::text IS NULL OR jobs.queue = $1)
  AND ($2::bigint IS NULL OR jobs.id = $2)
ORDER BY jobs.dead_lettered_at, jobs.queue, jobs.id
LIMIT $3
"""

AUDIT_LIST_SQL = """
SELECT id, queue, job_id, request_id, event_key, ticket_key, action,
       operator_id, reason_code, evidence_sha256, previous_status,
       resulting_status, previous_attempt_count, previous_error_code,
       previous_dead_lettered_at, previous_delivery_http_status,
       delivery_http_status, created_at
FROM hde_transport_audit
WHERE ($1::text IS NULL OR queue = $1)
  AND ($2::bigint IS NULL OR job_id = $2)
ORDER BY created_at DESC, id DESC
LIMIT $3
"""


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _recovery_arguments(
    parser: argparse.ArgumentParser,
    *,
    reason: str,
) -> None:
    parser.add_argument("--job-id", type=_positive_int, required=True)
    parser.add_argument("--confirm-job-id", type=_positive_int, required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", choices=[reason], required=True)
    parser.add_argument(
        "--evidence-sha256",
        required=True,
        help="SHA-256 of the locally reviewed HDE evidence; never pass raw ticket data.",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Server-local, audited HDE dead-letter inspection and recovery. "
            "No command decrypts or prints ticket IDs or response text."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List privacy-safe dead-letter metadata.")
    list_parser.add_argument("--queue", choices=["inbox", "outbox"])
    list_parser.add_argument("--job-id", type=_positive_int)
    list_parser.add_argument("--limit", type=_positive_int, default=100)

    audit_parser = subparsers.add_parser("audit", help="List append-only recovery audit rows.")
    audit_parser.add_argument("--queue", choices=["inbox", "outbox"])
    audit_parser.add_argument("--job-id", type=_positive_int)
    audit_parser.add_argument("--limit", type=_positive_int, default=100)

    inbox_parser = subparsers.add_parser(
        "requeue-inbox",
        help="Requeue only after all processing side effects were reviewed.",
    )
    _recovery_arguments(inbox_parser, reason=HDE_RECOVERY_REQUEUE_INBOX_REASON)

    outbox_parser = subparsers.add_parser(
        "requeue-outbox",
        help="Requeue only after HDE confirms that no post was accepted.",
    )
    _recovery_arguments(outbox_parser, reason=HDE_RECOVERY_REQUEUE_OUTBOX_REASON)

    reconcile_parser = subparsers.add_parser(
        "reconcile-delivered",
        help="Mark delivered only after HDE confirms that the post exists.",
    )
    _recovery_arguments(
        reconcile_parser,
        reason=HDE_RECOVERY_RECONCILE_DELIVERED_REASON,
    )
    reconcile_parser.add_argument("--http-status", type=int, default=None)
    return parser.parse_args(argv)


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _safe_rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [
        {str(key): _json_value(value) for key, value in dict(row).items()}
        for row in rows
    ]


def _confirm(args: argparse.Namespace) -> None:
    if args.job_id != args.confirm_job_id:
        raise ValueError("confirm_job_id_mismatch")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    pool = await asyncpg.create_pool(
        settings.postgres_dsn,
        min_size=1,
        max_size=1,
        timeout=10,
        command_timeout=15,
    )
    try:
        async with pool.acquire() as connection:
            if args.command == "list":
                rows = await connection.fetch(
                    DEAD_LETTER_LIST_SQL,
                    args.queue,
                    args.job_id,
                    min(args.limit, 500),
                )
                return {"mode": "read-only", "jobs": _safe_rows(list(rows))}
            if args.command == "audit":
                rows = await connection.fetch(
                    AUDIT_LIST_SQL,
                    args.queue,
                    args.job_id,
                    min(args.limit, 500),
                )
                return {"mode": "read-only", "audit": _safe_rows(list(rows))}

            _confirm(args)
            repository = HDETransportRepository(
                connection,
                event_key_secret=settings.hde_transport_event_key_secret,
                encryption_key=settings.hde_transport_encryption_key,
            )
            common = {
                "operator_id": args.operator,
                "reason_code": args.reason,
                "evidence_sha256": args.evidence_sha256,
            }
            if args.command == "requeue-inbox":
                await repository.requeue_dead_letter_inbox(args.job_id, **common)
            elif args.command == "requeue-outbox":
                await repository.requeue_dead_letter_outbox(args.job_id, **common)
            elif args.command == "reconcile-delivered":
                await repository.reconcile_dead_letter_outbox_as_delivered(
                    args.job_id,
                    http_status=args.http_status,
                    **common,
                )
            else:  # pragma: no cover - argparse enforces the command set
                raise ValueError("unsupported_command")
            return {
                "mode": "mutation",
                "action": args.command,
                "job_id": args.job_id,
                "audited": True,
            }
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except (OSError, asyncpg.PostgresError):
        print("HDE transport admin command rejected: external_error", file=sys.stderr)
        return 2
    except (ValueError, HDETransportError) as exc:
        print(
            f"HDE transport admin command rejected: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    except Exception:
        print("HDE transport admin command rejected: internal_error", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
