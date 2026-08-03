from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from eval.cost_governance import (
    CostGovernanceError,
    CostLedgerLockedError,
    approval_required,
    reserve_live_eval_cost,
)

RUNTIME_SHA = "a" * 40
MANIFEST_SHA = "b" * 64
NOW = datetime(2026, 8, 3, 9, 30, tzinfo=UTC)


def _reserve(
    ledger: Path,
    *,
    run_id: str,
    runtime_git_sha: str = RUNTIME_SHA,
    cap: float = 100.0,
    cases: int = 10,
    private_full: bool = False,
    approval_id: str | None = None,
    now: datetime = NOW,
):
    return reserve_live_eval_cost(
        scope="product80-calibration",
        run_id=run_id,
        runtime_git_sha=runtime_git_sha,
        manifest_sha256=MANIFEST_SHA,
        case_count=cases,
        approved_cap_rub=cap,
        private_full=private_full,
        high_cost_approval_id=approval_id,
        ledger_dir=ledger,
        now=now,
    )


@pytest.mark.parametrize(
    ("cases", "budget", "private_full", "expected"),
    [
        (10, 100.0, False, False),
        (11, 100.0, False, True),
        (10, 100.01, False, True),
        (1, 1.0, True, True),
    ],
)
def test_approval_required(
    cases: int,
    budget: float,
    private_full: bool,
    expected: bool,
) -> None:
    assert (
        approval_required(
            case_count=cases,
            budget_rub=budget,
            private_full=private_full,
        )
        is expected
    )


def test_reservation_uses_env_ledger_and_persists_complete_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = tmp_path / "global-ledger"
    monkeypatch.setenv("EVAL_COST_LEDGER_DIR", str(ledger))

    reservation = reserve_live_eval_cost(
        scope="routine-smoke",
        run_id="smoke-20260803-01",
        runtime_git_sha=RUNTIME_SHA.upper(),
        manifest_sha256=MANIFEST_SHA.upper(),
        case_count=4,
        approved_cap_rub=25,
        private_full=False,
        now=NOW,
    )

    assert reservation.path.parent == ledger
    assert reservation.path.exists()
    assert reservation.reservation_class == "routine"
    persisted = json.loads(reservation.path.read_text(encoding="utf-8"))
    assert persisted == reservation.record
    assert persisted == {
        "schema_version": "1.0.0",
        "record_type": "live_eval_cost_reservation",
        "reserved_at": "2026-08-03T09:30:00Z",
        "reservation_class": "routine",
        "scope": "routine-smoke",
        "run_id": "smoke-20260803-01",
        "runtime_git_sha": RUNTIME_SHA,
        "manifest_sha256": MANIFEST_SHA,
        "case_count": 4,
        "approved_cap_rub": 25.0,
        "private_full": False,
        "approval_required": False,
        "high_cost_approval_id": None,
    }


def test_sequential_high_cost_approval_replay_is_rejected_forever(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    approval_id = "OWNER-20260803-PRODUCT80-001"
    _reserve(
        ledger,
        run_id="full-first",
        cap=750,
        cases=80,
        private_full=True,
        approval_id=approval_id,
        now=NOW,
    )

    with pytest.raises(CostGovernanceError, match="already been consumed"):
        _reserve(
            ledger,
            run_id="full-replay",
            cap=750,
            cases=80,
            private_full=True,
            approval_id=approval_id,
            now=NOW + timedelta(days=2),
        )


def test_concurrent_same_approval_writes_exactly_one_record(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    barrier = Barrier(2)

    def attempt(run_id: str) -> tuple[str, str]:
        barrier.wait(timeout=5)
        try:
            result = _reserve(
                ledger,
                run_id=run_id,
                cap=150,
                cases=11,
                approval_id="OWNER-20260803-HIGH-001",
            )
        except CostGovernanceError as exc:
            return "error", str(exc)
        return "ok", str(result.path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ("concurrent-a", "concurrent-b")))

    assert [status for status, _ in outcomes].count("ok") == 1
    assert [status for status, _ in outcomes].count("error") == 1
    assert len(list(ledger.glob("*.reservation.json"))) == 1


def test_concurrent_private_full_runs_write_exactly_one_record(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    barrier = Barrier(2)

    def attempt(index: int) -> tuple[str, str]:
        barrier.wait(timeout=5)
        try:
            result = _reserve(
                ledger,
                run_id=f"full-concurrent-{index}",
                cap=750,
                cases=80,
                private_full=True,
                approval_id=f"OWNER-20260803-FULL-00{index}",
            )
        except CostGovernanceError as exc:
            return "error", str(exc)
        return "ok", str(result.path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (1, 2)))

    assert [status for status, _ in outcomes].count("ok") == 1
    assert [status for status, _ in outcomes].count("error") == 1
    assert len(list(ledger.glob("*.reservation.json"))) == 1


def test_routine_rolling_24h_cap_uses_reserved_caps(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    for index in range(3):
        _reserve(
            ledger,
            run_id=f"routine-{index}",
            now=NOW + timedelta(hours=index),
        )

    with pytest.raises(CostGovernanceError, match="rolling 24h cap"):
        _reserve(
            ledger,
            run_id="routine-over-cap",
            cap=0.01,
            now=NOW + timedelta(hours=3),
        )

    reservation = _reserve(
        ledger,
        run_id="routine-after-window",
        cap=100,
        now=NOW + timedelta(hours=26, seconds=1),
    )
    assert reservation.path.exists()


def test_private_full_can_exceed_routine_cap_but_only_once_per_24h(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    _reserve(
        ledger,
        run_id="full-allowed",
        cap=750,
        cases=80,
        private_full=True,
        approval_id="OWNER-20260803-FULL-101",
    )

    with pytest.raises(CostGovernanceError, match="another private full"):
        _reserve(
            ledger,
            run_id="full-blocked",
            runtime_git_sha="c" * 40,
            cap=750,
            cases=80,
            private_full=True,
            approval_id="OWNER-20260803-FULL-102",
            now=NOW + timedelta(hours=23),
        )


def test_private_full_is_once_per_release_candidate_even_after_24h(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    _reserve(
        ledger,
        run_id="full-first",
        cap=750,
        cases=80,
        private_full=True,
        approval_id="OWNER-20260803-FULL-201",
    )

    with pytest.raises(CostGovernanceError, match="release candidate"):
        _reserve(
            ledger,
            run_id="full-same-rc",
            cap=750,
            cases=80,
            private_full=True,
            approval_id="OWNER-20260805-FULL-202",
            now=NOW + timedelta(hours=49),
        )


@pytest.mark.parametrize("contents", ["{bad-json", "[]", "{}"])
def test_malformed_ledger_fails_closed(tmp_path: Path, contents: str) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    malformed = ledger / (
        "20260803T090000000000Z-" + "1" * 32 + ".reservation.json"
    )
    malformed.write_text(contents, encoding="utf-8")

    with pytest.raises(CostGovernanceError):
        _reserve(ledger, run_id="blocked-by-malformed")

    assert len(list(ledger.glob("*.reservation.json"))) == 1


def test_future_ledger_timestamp_fails_closed(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    first = _reserve(
        ledger,
        run_id="future-record",
        now=NOW + timedelta(minutes=1),
    )
    assert first.path.exists()

    with pytest.raises(CostGovernanceError, match="future timestamp"):
        _reserve(ledger, run_id="present-attempt", now=NOW)


@pytest.mark.parametrize(
    "approval_id",
    [
        "OWNER-YYYYMMDD-PRODUCT80",
        "OWNER-20260803-RC",
        "OWNER-20260803-RC_SHA-01",
        "OWNER-RC-COMMIT-20260803",
    ],
)
def test_placeholder_approval_ids_are_rejected(
    tmp_path: Path,
    approval_id: str,
) -> None:
    with pytest.raises(CostGovernanceError, match="placeholder"):
        _reserve(
            tmp_path / "ledger",
            run_id="placeholder-attempt",
            cap=750,
            cases=80,
            private_full=True,
            approval_id=approval_id,
        )


def test_existing_lock_is_fail_closed_and_not_removed(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    lock = ledger / ".cost-governance.lock"
    lock.write_text("stale\n", encoding="ascii")

    with pytest.raises(CostLedgerLockedError, match="stale lock"):
        _reserve(ledger, run_id="blocked-by-lock")

    assert lock.read_text(encoding="ascii") == "stale\n"
    assert list(ledger.glob("*.reservation.json")) == []


def test_ledger_rejects_unexpected_entries(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "README.txt").write_text("not a record", encoding="utf-8")

    with pytest.raises(CostGovernanceError, match="unexpected file"):
        _reserve(ledger, run_id="blocked-by-unexpected")


def test_symlink_ledger_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "ledger-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    with pytest.raises(CostGovernanceError, match="real directory"):
        _reserve(link, run_id="blocked-by-symlink")


def test_record_with_duplicate_json_field_fails_closed(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    path = ledger / (
        "20260803T090000000000Z-" + "2" * 32 + ".reservation.json"
    )
    path.write_text('{"schema_version":"1.0.0","schema_version":"1.0.0"}', encoding="utf-8")

    with pytest.raises(CostGovernanceError, match="duplicate fields"):
        _reserve(ledger, run_id="blocked-by-duplicate")


def test_missing_required_approval_stops_before_ledger_creation(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"

    with pytest.raises(CostGovernanceError, match="requires a one-time"):
        _reserve(
            ledger,
            run_id="approval-missing",
            cases=80,
            cap=750,
            private_full=True,
        )

    assert not ledger.exists()


def _valid_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": "1.0.0",
        "record_type": "live_eval_cost_reservation",
        "reserved_at": "2026-08-03T09:30:00Z",
        "reservation_class": "routine",
        "scope": "routine-smoke",
        "run_id": "manual-record",
        "runtime_git_sha": RUNTIME_SHA,
        "manifest_sha256": MANIFEST_SHA,
        "case_count": 1,
        "approved_cap_rub": 1.0,
        "private_full": False,
        "approval_required": False,
        "high_cost_approval_id": None,
    }
    record.update(overrides)
    return record


def test_future_timestamp_in_manually_created_valid_record_is_rejected(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    path = ledger / (
        "20260803T093100000000Z-" + "3" * 32 + ".reservation.json"
    )
    path.write_text(
        json.dumps(_valid_record(reserved_at="2026-08-03T09:31:00Z")),
        encoding="utf-8",
    )

    with pytest.raises(CostGovernanceError, match="future timestamp"):
        _reserve(ledger, run_id="present-attempt", now=NOW)
