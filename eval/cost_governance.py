"""Fail-closed persistent reservations for paid live evaluation runs.

The ledger stores approved *caps*, not provider billing facts.  Reserving the
cap before the first request prevents concurrent runners from each assuming
that the same daily allowance or owner approval is still available.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

LEDGER_ENV_VAR = "EVAL_COST_LEDGER_DIR"
DEFAULT_LEDGER_DIR = Path("data/private/eval-cost-ledger-v1")
LEDGER_SCHEMA_VERSION = "1.0.0"
LEDGER_RECORD_TYPE = "live_eval_cost_reservation"
ROUTINE_LIVE_EVAL_MAX_CASES = 10
ROUTINE_LIVE_EVAL_MAX_COST_RUB = 100.0
ROUTINE_ROLLING_24H_CAP_RUB = 300.0

_LOCK_FILENAME = ".cost-governance.lock"
_RECORD_FILENAME_RE = re.compile(
    r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}\.reservation\.json$"
)
_SAFE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_APPROVAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_GIT_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PLACEHOLDER_RC_TOKEN_RE = re.compile(
    r"(?:^|[._:-])RC(?:[._:-](?:SHA|COMMIT|TOKEN|ID))?(?:$|[._:-])",
    re.IGNORECASE,
)
_MAX_RECORD_BYTES = 64 * 1024
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "record_type",
        "reserved_at",
        "reservation_class",
        "scope",
        "run_id",
        "runtime_git_sha",
        "manifest_sha256",
        "case_count",
        "approved_cap_rub",
        "private_full",
        "approval_required",
        "high_cost_approval_id",
    }
)


class CostGovernanceError(ValueError):
    """A live cost reservation could not be proven safe."""


class CostLedgerLockedError(CostGovernanceError):
    """The fixed ledger lock exists and must not be recovered automatically."""


@dataclass(frozen=True, slots=True)
class LiveEvalCostReservation:
    """A successfully persisted reservation."""

    path: Path
    record: Mapping[str, Any]

    @property
    def reservation_class(self) -> str:
        return str(self.record["reservation_class"])


def approval_required(
    *,
    case_count: int,
    budget_rub: float,
    private_full: bool,
) -> bool:
    """Return whether a live run needs a one-time owner approval reference."""

    validated_case_count = _validated_case_count(case_count)
    validated_budget = _validated_cap(budget_rub)
    if not isinstance(private_full, bool):
        raise CostGovernanceError("private_full must be a boolean")
    return (
        private_full
        or validated_case_count > ROUTINE_LIVE_EVAL_MAX_CASES
        or validated_budget > ROUTINE_LIVE_EVAL_MAX_COST_RUB
    )


def reserve_live_eval_cost(
    *,
    scope: str,
    run_id: str,
    runtime_git_sha: str,
    manifest_sha256: str,
    case_count: int,
    approved_cap_rub: float,
    private_full: bool,
    high_cost_approval_id: str | None = None,
    ledger_dir: str | Path | None = None,
    now: datetime | None = None,
) -> LiveEvalCostReservation:
    """Atomically reserve a cap before a paid live evaluation starts.

    ``high_cost_approval_id`` is an opaque, non-secret owner reference.  It is
    consumed globally and permanently once its reservation is written.
    Existing malformed records, future timestamps, or any existing fixed lock
    stop the operation without attempting automatic recovery.
    """

    validated_scope = _validated_reference(scope, label="scope")
    validated_run_id = _validated_reference(run_id, label="run_id")
    validated_runtime_sha = _validated_runtime_sha(runtime_git_sha)
    validated_manifest_sha = _validated_manifest_sha(manifest_sha256)
    validated_case_count = _validated_case_count(case_count)
    validated_cap = _validated_cap(approved_cap_rub)
    if not isinstance(private_full, bool):
        raise CostGovernanceError("private_full must be a boolean")

    needs_approval = approval_required(
        case_count=validated_case_count,
        budget_rub=validated_cap,
        private_full=private_full,
    )
    approval_id = _validated_approval_id(high_cost_approval_id)
    if needs_approval and approval_id is None:
        raise CostGovernanceError(
            "this live evaluation requires a one-time high-cost approval id"
        )
    if not needs_approval:
        approval_id = None

    reservation_time = _validated_now(now)
    target_ledger = _validated_ledger_dir(ledger_dir)
    with _fixed_ledger_lock(target_ledger):
        existing_records = _scan_records(target_ledger, now=reservation_time)
        _enforce_approval_once(existing_records, approval_id=approval_id)
        _enforce_rolling_limits(
            existing_records,
            now=reservation_time,
            requested_cap=validated_cap,
            private_full=private_full,
            requested_runtime_git_sha=validated_runtime_sha,
        )

        record: dict[str, Any] = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "record_type": LEDGER_RECORD_TYPE,
            "reserved_at": _format_utc(reservation_time),
            "reservation_class": "private_full" if private_full else "routine",
            "scope": validated_scope,
            "run_id": validated_run_id,
            "runtime_git_sha": validated_runtime_sha,
            "manifest_sha256": validated_manifest_sha,
            "case_count": validated_case_count,
            "approved_cap_rub": validated_cap,
            "private_full": private_full,
            "approval_required": needs_approval,
            "high_cost_approval_id": approval_id,
        }
        filename = (
            reservation_time.strftime("%Y%m%dT%H%M%S%fZ")
            + f"-{uuid.uuid4().hex}.reservation.json"
        )
        path = target_ledger / filename
        _write_record_exclusive(path, record)
        return LiveEvalCostReservation(path=path, record=dict(record))


def _validated_ledger_dir(value: str | Path | None) -> Path:
    configured = value
    if configured is None:
        configured = os.getenv(LEDGER_ENV_VAR, "").strip() or DEFAULT_LEDGER_DIR
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.abspath(candidate))

    if not os.path.lexists(candidate):
        parent = candidate.parent
        _require_real_directory(parent, label="cost ledger parent")
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise CostGovernanceError("cost ledger directory cannot be created") from exc
    _require_real_directory(candidate, label="cost ledger")
    return candidate


def _require_real_directory(path: Path, *, label: str) -> None:
    if not os.path.lexists(path) or path.is_symlink() or not path.is_dir():
        raise CostGovernanceError(f"{label} must be an existing real directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CostGovernanceError(f"{label} cannot be resolved") from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(path)):
        raise CostGovernanceError(f"{label} must not traverse a symlink")


@contextmanager
def _fixed_ledger_lock(ledger_dir: Path) -> Iterator[None]:
    lock_path = ledger_dir / _LOCK_FILENAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise CostLedgerLockedError(
            "cost ledger is locked or contains a stale lock; refusing automatic recovery"
        ) from exc
    except OSError as exc:
        raise CostGovernanceError("cost ledger lock cannot be created") from exc

    cleanup_error: OSError | None = None
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            handle.write("live-eval-cost-reservation\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            raise CostGovernanceError(
                "cost ledger lock could not be released; ledger remains fail-closed"
            ) from cleanup_error


def _scan_records(ledger_dir: Path, *, now: datetime) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        entries = sorted(ledger_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise CostGovernanceError("cost ledger cannot be scanned") from exc
    for path in entries:
        if path.name == _LOCK_FILENAME:
            continue
        if path.is_symlink() or not path.is_file():
            raise CostGovernanceError("cost ledger contains a non-regular entry")
        if _RECORD_FILENAME_RE.fullmatch(path.name) is None:
            raise CostGovernanceError("cost ledger contains an unexpected file")
        record = _load_and_validate_record(path)
        reserved_at = _parse_utc(record["reserved_at"], label="reserved_at")
        if reserved_at > now:
            raise CostGovernanceError("cost ledger contains a future timestamp")
        record["_reserved_at_datetime"] = reserved_at
        records.append(record)
    return records


def _load_and_validate_record(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > _MAX_RECORD_BYTES:
            raise CostGovernanceError("cost ledger record size is invalid")
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except CostGovernanceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CostGovernanceError("cost ledger contains an unreadable record") from exc
    if not isinstance(payload, dict) or set(payload) != _RECORD_FIELDS:
        raise CostGovernanceError("cost ledger record has an invalid schema")
    if payload["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise CostGovernanceError("cost ledger record has an unsupported schema version")
    if payload["record_type"] != LEDGER_RECORD_TYPE:
        raise CostGovernanceError("cost ledger record type is invalid")

    _parse_utc(payload["reserved_at"], label="reserved_at")
    private_full = payload["private_full"]
    if not isinstance(private_full, bool):
        raise CostGovernanceError("cost ledger private_full value is invalid")
    expected_class = "private_full" if private_full else "routine"
    if payload["reservation_class"] != expected_class:
        raise CostGovernanceError("cost ledger reservation class is invalid")
    _validated_reference(payload["scope"], label="scope")
    _validated_reference(payload["run_id"], label="run_id")
    _validated_runtime_sha(payload["runtime_git_sha"])
    _validated_manifest_sha(payload["manifest_sha256"])
    case_count = _validated_case_count(payload["case_count"])
    if isinstance(payload["approved_cap_rub"], bool) or not isinstance(
        payload["approved_cap_rub"], (int, float)
    ):
        raise CostGovernanceError("cost ledger approved cost cap type is invalid")
    cap = _validated_cap(payload["approved_cap_rub"])
    expected_approval = approval_required(
        case_count=case_count,
        budget_rub=cap,
        private_full=private_full,
    )
    if payload["approval_required"] is not expected_approval:
        raise CostGovernanceError("cost ledger approval_required value is invalid")
    approval_id = _validated_approval_id(payload["high_cost_approval_id"])
    if expected_approval and approval_id is None:
        raise CostGovernanceError("cost ledger high-cost approval is missing")
    return payload


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CostGovernanceError("cost ledger record contains duplicate fields")
        result[key] = value
    return result


def _enforce_approval_once(
    records: list[dict[str, Any]],
    *,
    approval_id: str | None,
) -> None:
    if approval_id is None:
        return
    if any(record["high_cost_approval_id"] == approval_id for record in records):
        raise CostGovernanceError("high-cost approval id has already been consumed")


def _enforce_rolling_limits(
    records: list[dict[str, Any]],
    *,
    now: datetime,
    requested_cap: float,
    private_full: bool,
    requested_runtime_git_sha: str,
) -> None:
    cutoff = now - timedelta(hours=24)
    recent = [
        record
        for record in records
        if record["_reserved_at_datetime"] >= cutoff
    ]
    if private_full:
        if any(
            record["private_full"]
            and record["runtime_git_sha"] == requested_runtime_git_sha
            for record in records
        ):
            raise CostGovernanceError(
                "a private full evaluation is already reserved for this release candidate"
            )
        if any(record["private_full"] for record in recent):
            raise CostGovernanceError(
                "another private full evaluation was reserved in the rolling 24h window"
            )
        return

    routine_total = sum(
        (
            Decimal(str(record["approved_cap_rub"]))
            for record in recent
            if record["reservation_class"] == "routine"
        ),
        start=Decimal("0"),
    )
    requested = Decimal(str(requested_cap))
    if routine_total + requested > Decimal(str(ROUTINE_ROLLING_24H_CAP_RUB)):
        raise CostGovernanceError(
            "routine live evaluation reservations would exceed the rolling 24h cap"
        )


def _write_record_exclusive(path: Path, record: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            dict(record),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise CostGovernanceError("cost reservation path already exists") from exc
    except OSError as exc:
        raise CostGovernanceError("cost reservation cannot be created") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise CostGovernanceError(
            "cost reservation could not be persisted; ledger is fail-closed"
        ) from exc


def _validated_now(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise CostGovernanceError("reservation timestamp must be timezone-aware")
    return result.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CostGovernanceError(f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CostGovernanceError(f"{label} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CostGovernanceError(f"{label} must use UTC")
    return parsed.astimezone(UTC)


def _validated_reference(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise CostGovernanceError(f"{label} must be a safe non-secret reference")
    text = value.strip()
    if _SAFE_REFERENCE_RE.fullmatch(text) is None:
        raise CostGovernanceError(f"{label} must be a safe non-secret reference")
    return text


def _validated_runtime_sha(value: Any) -> str:
    if not isinstance(value, str):
        raise CostGovernanceError("runtime_git_sha must be a full Git object id")
    text = value.strip()
    if _GIT_SHA_RE.fullmatch(text) is None:
        raise CostGovernanceError("runtime_git_sha must be a full Git object id")
    return text.lower()


def _validated_manifest_sha(value: Any) -> str:
    if not isinstance(value, str):
        raise CostGovernanceError("manifest_sha256 must be a SHA-256 hex digest")
    text = value.strip()
    if _SHA256_RE.fullmatch(text) is None:
        raise CostGovernanceError("manifest_sha256 must be a SHA-256 hex digest")
    return text.lower()


def _validated_case_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CostGovernanceError("case_count must be a positive integer")
    return value


def _validated_cap(value: Any) -> float:
    if isinstance(value, bool):
        raise CostGovernanceError("approved cost cap must be a finite positive number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CostGovernanceError(
            "approved cost cap must be a finite positive number"
        ) from exc
    if not math.isfinite(result) or result <= 0:
        raise CostGovernanceError(
            "approved cost cap must be a finite positive number"
        )
    return result


def _validated_approval_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CostGovernanceError(
            "high-cost approval id must be a safe non-secret reference"
        )
    text = value.strip()
    if _SAFE_APPROVAL_RE.fullmatch(text) is None:
        raise CostGovernanceError(
            "high-cost approval id must be a safe non-secret reference"
        )
    if "YYYYMMDD" in text.upper() or _PLACEHOLDER_RC_TOKEN_RE.search(text):
        raise CostGovernanceError("high-cost approval id contains a placeholder token")
    return text
