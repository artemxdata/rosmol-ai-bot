from __future__ import annotations

import hashlib
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
    PrivateFullComparisonWaiver,
    approval_required,
    reserve_live_eval_cost,
)

RUNTIME_SHA = "a" * 40
MANIFEST_SHA = "b" * 64
SECOND_RUNTIME_SHA = "c" * 40
SECOND_MANIFEST_SHA = "d" * 64
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
    scope: str = "product80-calibration",
    manifest_sha256: str = MANIFEST_SHA,
    comparison_waiver: PrivateFullComparisonWaiver | None = None,
    now: datetime = NOW,
):
    return reserve_live_eval_cost(
        scope=scope,
        run_id=run_id,
        runtime_git_sha=runtime_git_sha,
        manifest_sha256=manifest_sha256,
        case_count=cases,
        approved_cap_rub=cap,
        private_full=private_full,
        high_cost_approval_id=approval_id,
        private_full_comparison_waiver=comparison_waiver,
        ledger_dir=ledger,
        now=now,
    )


def _comparison_waiver(
    **overrides: Any,
) -> PrivateFullComparisonWaiver:
    values: dict[str, Any] = {
        "waiver_id": "owner-waive-v2-to-v3-20260803",
        "decision_id": "D-041",
        "provider_risk_ceiling_rub": 500.0,
        "prior_scope": "pilot50-v2-candidate",
        "prior_runtime_git_sha": RUNTIME_SHA,
        "prior_manifest_sha256": MANIFEST_SHA,
        "prior_case_count": 50,
        "prior_approved_cap_rub": 30.0,
        "requested_scope": "pilot50-v3-candidate",
        "requested_runtime_git_sha": SECOND_RUNTIME_SHA,
        "requested_manifest_sha256": SECOND_MANIFEST_SHA,
        "requested_case_count": 50,
        "requested_approved_cap_rub": 30.0,
    }
    values.update(overrides)
    return PrivateFullComparisonWaiver(**values)


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


def test_exact_comparison_waiver_allows_one_cross_candidate_run_and_is_audited(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    prior = _reserve(
        ledger,
        run_id="pilot50-v2",
        runtime_git_sha=RUNTIME_SHA,
        manifest_sha256=MANIFEST_SHA,
        scope="pilot50-v2-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v2-20260803",
    )
    waiver = _comparison_waiver()

    current = _reserve(
        ledger,
        run_id="pilot50-v3",
        runtime_git_sha=SECOND_RUNTIME_SHA,
        manifest_sha256=SECOND_MANIFEST_SHA,
        scope="pilot50-v3-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v3-20260803",
        comparison_waiver=waiver,
        now=NOW + timedelta(hours=1),
    )

    prior_binding = hashlib.sha256(
        json.dumps(
            dict(prior.record),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert current.record == {
        "schema_version": "1.1.0",
        "record_type": "live_eval_cost_reservation",
        "reserved_at": "2026-08-03T10:30:00Z",
        "reservation_class": "private_full",
        "scope": "pilot50-v3-candidate",
        "run_id": "pilot50-v3",
        "runtime_git_sha": SECOND_RUNTIME_SHA,
        "manifest_sha256": SECOND_MANIFEST_SHA,
        "case_count": 50,
        "approved_cap_rub": 30.0,
        "private_full": True,
        "approval_required": True,
        "high_cost_approval_id": "owner-pilot50-v3-20260803",
        "rolling_24h_waiver_id": waiver.waiver_id,
        "rolling_24h_waiver_decision_id": "D-041",
        "waived_reservation_sha256": prior_binding,
        "provider_risk_ceiling_rub": 500.0,
    }
    assert json.loads(current.path.read_text(encoding="utf-8")) == current.record


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"prior_scope": "wrong-scope"}, "baseline does not match"),
        ({"prior_runtime_git_sha": "e" * 40}, "baseline does not match"),
        ({"prior_manifest_sha256": "f" * 64}, "baseline does not match"),
        ({"prior_case_count": 49}, "baseline does not match"),
        ({"prior_approved_cap_rub": 29.0}, "baseline does not match"),
        ({"requested_scope": "wrong-scope"}, "candidate does not match"),
        ({"requested_runtime_git_sha": "e" * 40}, "candidate does not match"),
        ({"requested_manifest_sha256": "f" * 64}, "candidate does not match"),
        ({"requested_case_count": 49}, "candidate does not match"),
        ({"requested_approved_cap_rub": 29.0}, "candidate does not match"),
        ({"provider_risk_ceiling_rub": 500.01}, "risk ceiling is invalid"),
    ],
)
def test_comparison_waiver_rejects_any_baseline_or_candidate_mismatch(
    tmp_path: Path,
    override: dict[str, Any],
    message: str,
) -> None:
    ledger = tmp_path / "ledger"
    _reserve(
        ledger,
        run_id="pilot50-v2",
        scope="pilot50-v2-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v2-20260803",
    )

    with pytest.raises(CostGovernanceError, match=message):
        _reserve(
            ledger,
            run_id="pilot50-v3",
            runtime_git_sha=SECOND_RUNTIME_SHA,
            manifest_sha256=SECOND_MANIFEST_SHA,
            scope="pilot50-v3-candidate",
            cap=30,
            cases=50,
            private_full=True,
            approval_id="owner-pilot50-v3-20260803",
            comparison_waiver=_comparison_waiver(**override),
            now=NOW + timedelta(hours=1),
        )

    assert len(list(ledger.glob("*.reservation.json"))) == 1


def test_comparison_waiver_requires_exactly_one_recent_private_full(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"

    with pytest.raises(CostGovernanceError, match="exactly one conflicting"):
        _reserve(
            ledger,
            run_id="pilot50-v3",
            runtime_git_sha=SECOND_RUNTIME_SHA,
            manifest_sha256=SECOND_MANIFEST_SHA,
            scope="pilot50-v3-candidate",
            cap=30,
            cases=50,
            private_full=True,
            approval_id="owner-pilot50-v3-20260803",
            comparison_waiver=_comparison_waiver(),
        )

    assert list(ledger.glob("*.reservation.json")) == []


def test_comparison_waiver_requires_strictly_earlier_baseline_timestamp(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    _reserve(
        ledger,
        run_id="pilot50-v2",
        scope="pilot50-v2-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v2-20260803",
        now=NOW,
    )

    with pytest.raises(CostGovernanceError, match="baseline is not earlier"):
        _reserve(
            ledger,
            run_id="pilot50-v3",
            runtime_git_sha=SECOND_RUNTIME_SHA,
            manifest_sha256=SECOND_MANIFEST_SHA,
            scope="pilot50-v3-candidate",
            cap=30,
            cases=50,
            private_full=True,
            approval_id="owner-pilot50-v3-20260803",
            comparison_waiver=_comparison_waiver(),
            now=NOW,
        )

    assert len(list(ledger.glob("*.reservation.json"))) == 1


def test_comparison_waiver_cannot_repeat_same_candidate_or_share_approval(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    _reserve(
        ledger,
        run_id="pilot50-v2",
        scope="pilot50-v2-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v2-20260803",
    )

    with pytest.raises(CostGovernanceError, match="distinct candidates"):
        _reserve(
            ledger,
            run_id="pilot50-v2-repeat",
            scope="pilot50-v2-candidate",
            cap=30,
            cases=50,
            private_full=True,
            approval_id="owner-pilot50-repeat-20260803",
            comparison_waiver=_comparison_waiver(
                requested_scope="pilot50-v2-candidate",
                requested_runtime_git_sha=RUNTIME_SHA,
                requested_manifest_sha256=MANIFEST_SHA,
            ),
            now=NOW + timedelta(hours=1),
        )

    with pytest.raises(CostGovernanceError, match="must be distinct"):
        _reserve(
            ledger,
            run_id="pilot50-v3",
            runtime_git_sha=SECOND_RUNTIME_SHA,
            manifest_sha256=SECOND_MANIFEST_SHA,
            scope="pilot50-v3-candidate",
            cap=30,
            cases=50,
            private_full=True,
            approval_id="shared-owner-reference-20260803",
            comparison_waiver=_comparison_waiver(
                waiver_id="shared-owner-reference-20260803"
            ),
            now=NOW + timedelta(hours=1),
        )


def test_comparison_waiver_is_globally_one_use(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    _reserve(
        ledger,
        run_id="pilot50-v2",
        scope="pilot50-v2-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v2-20260803",
    )
    waiver = _comparison_waiver()
    _reserve(
        ledger,
        run_id="pilot50-v3",
        runtime_git_sha=SECOND_RUNTIME_SHA,
        manifest_sha256=SECOND_MANIFEST_SHA,
        scope="pilot50-v3-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v3-20260803",
        comparison_waiver=waiver,
        now=NOW + timedelta(hours=1),
    )

    with pytest.raises(CostGovernanceError, match="waiver has already"):
        _reserve(
            ledger,
            run_id="pilot50-v4",
            runtime_git_sha="e" * 40,
            manifest_sha256="f" * 64,
            scope="pilot50-v4-candidate",
            cap=30,
            cases=50,
            private_full=True,
            approval_id="owner-pilot50-v4-20260803",
            comparison_waiver=_comparison_waiver(
                requested_scope="pilot50-v4-candidate",
                requested_runtime_git_sha="e" * 40,
                requested_manifest_sha256="f" * 64,
            ),
            now=NOW + timedelta(hours=25),
        )


def test_concurrent_comparison_waivers_write_exactly_one_candidate_record(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    _reserve(
        ledger,
        run_id="pilot50-v2",
        scope="pilot50-v2-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v2-20260803",
    )
    barrier = Barrier(2)

    def attempt(index: int) -> tuple[str, str]:
        barrier.wait(timeout=5)
        try:
            result = _reserve(
                ledger,
                run_id=f"pilot50-v3-{index}",
                runtime_git_sha=SECOND_RUNTIME_SHA,
                manifest_sha256=SECOND_MANIFEST_SHA,
                scope="pilot50-v3-candidate",
                cap=30,
                cases=50,
                private_full=True,
                approval_id=f"owner-pilot50-v3-20260803-{index}",
                comparison_waiver=_comparison_waiver(
                    waiver_id=f"owner-waive-v2-to-v3-20260803-{index}"
                ),
                now=NOW + timedelta(hours=1),
            )
        except CostGovernanceError as exc:
            return "error", str(exc)
        return "ok", str(result.path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (1, 2)))

    assert [status for status, _ in outcomes].count("ok") == 1
    assert [status for status, _ in outcomes].count("error") == 1
    assert len(list(ledger.glob("*.reservation.json"))) == 2


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("waived_reservation_sha256", "0" * 64, "waiver binding is invalid"),
        ("provider_risk_ceiling_rub", "500", "risk ceiling type is invalid"),
        ("provider_risk_ceiling_rub", 500.01, "risk ceiling is invalid"),
        (
            "rolling_24h_waiver_id",
            "owner-pilot50-v3-20260803",
            "waiver and approval ids must be distinct",
        ),
        ("runtime_git_sha", RUNTIME_SHA, "candidates are not distinct"),
        ("manifest_sha256", MANIFEST_SHA, "candidates are not distinct"),
    ],
)
def test_corrupt_waiver_ledger_record_fails_closed(
    tmp_path: Path,
    field: str,
    value: Any,
    message: str,
) -> None:
    ledger = tmp_path / "ledger"
    _reserve(
        ledger,
        run_id="pilot50-v2",
        scope="pilot50-v2-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v2-20260803",
    )
    current = _reserve(
        ledger,
        run_id="pilot50-v3",
        runtime_git_sha=SECOND_RUNTIME_SHA,
        manifest_sha256=SECOND_MANIFEST_SHA,
        scope="pilot50-v3-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v3-20260803",
        comparison_waiver=_comparison_waiver(),
        now=NOW + timedelta(hours=1),
    )
    payload = json.loads(current.path.read_text(encoding="utf-8"))
    payload[field] = value
    current.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CostGovernanceError, match=message):
        _reserve(
            ledger,
            run_id="later-routine",
            now=NOW + timedelta(hours=2),
        )

    assert len(list(ledger.glob("*.reservation.json"))) == 2


def test_valid_v1_and_v1_1_mixed_ledger_remains_readable(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    _reserve(
        ledger,
        run_id="pilot50-v2",
        scope="pilot50-v2-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v2-20260803",
    )
    _reserve(
        ledger,
        run_id="pilot50-v3",
        runtime_git_sha=SECOND_RUNTIME_SHA,
        manifest_sha256=SECOND_MANIFEST_SHA,
        scope="pilot50-v3-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v3-20260803",
        comparison_waiver=_comparison_waiver(),
        now=NOW + timedelta(hours=1),
    )

    routine = _reserve(
        ledger,
        run_id="later-routine",
        now=NOW + timedelta(hours=25, seconds=1),
    )

    assert routine.path.exists()
    assert len(list(ledger.glob("*.reservation.json"))) == 3


def test_waiver_ledger_rejects_a_second_prior_private_full_conflict(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    _reserve(
        ledger,
        run_id="pilot50-v2",
        scope="pilot50-v2-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v2-20260803",
    )
    _reserve(
        ledger,
        run_id="pilot50-v3",
        runtime_git_sha=SECOND_RUNTIME_SHA,
        manifest_sha256=SECOND_MANIFEST_SHA,
        scope="pilot50-v3-candidate",
        cap=30,
        cases=50,
        private_full=True,
        approval_id="owner-pilot50-v3-20260803",
        comparison_waiver=_comparison_waiver(),
        now=NOW + timedelta(hours=1),
    )
    injected = _valid_record(
        reserved_at="2026-08-03T10:00:00Z",
        reservation_class="private_full",
        scope="other-private-full",
        run_id="injected-conflict",
        runtime_git_sha="e" * 40,
        manifest_sha256="f" * 64,
        case_count=50,
        approved_cap_rub=30.0,
        private_full=True,
        approval_required=True,
        high_cost_approval_id="owner-other-private-full-20260803",
    )
    injected_path = ledger / (
        "20260803T100000000000Z-" + "9" * 32 + ".reservation.json"
    )
    injected_path.write_text(json.dumps(injected), encoding="utf-8")

    with pytest.raises(CostGovernanceError, match="waiver binding is invalid"):
        _reserve(
            ledger,
            run_id="later-routine",
            now=NOW + timedelta(hours=2),
        )

    assert len(list(ledger.glob("*.reservation.json"))) == 3


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
