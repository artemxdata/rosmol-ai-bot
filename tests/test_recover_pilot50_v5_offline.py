from __future__ import annotations

from uuid import uuid4

import pytest

from scripts import recover_pilot50_v5_offline as recovery


def _report(total: int = 50) -> dict[str, object]:
    eval_run_id = f"ask-eval-{uuid4()}"
    return {
        "eval_run_id": eval_run_id,
        "results": [
            {
                "id": f"case-{index:02d}",
                "request_id": str(uuid4()),
                "trace_found": True,
                "trace_binding_match": True,
                "trace_eval_run_id": eval_run_id,
                "trace_eval_case_id": f"case-{index:02d}",
                "cache_hit": False,
                "trace_lookup_error": None,
                "trace_error": None,
                "error": None,
            }
            for index in range(total)
        ],
    }


def test_trace_rows_are_recovered_only_from_complete_bound_evidence() -> None:
    report = _report()

    rows = recovery.trace_rows_from_sealed_report(
        report,
        expected_cases_total=50,
    )

    assert len(rows) == 50
    assert rows[0] == {
        "eval_run_id": report["eval_run_id"],
        "request_id": report["results"][0]["request_id"],
        "eval_case_id": "case-00",
        "cache_hit": False,
        "error_present": False,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trace_found", False),
        ("trace_binding_match", False),
        ("cache_hit", True),
        ("trace_lookup_error", "lookup_failed"),
        ("trace_error", "trace_failed"),
        ("error", "request_failed"),
    ],
)
def test_trace_recovery_fails_closed_on_invalid_evidence(
    field: str,
    value: object,
) -> None:
    report = _report()
    report["results"][0][field] = value

    with pytest.raises(recovery.RecoveryError, match="sealed trace evidence is invalid"):
        recovery.trace_rows_from_sealed_report(report, expected_cases_total=50)


def test_trace_recovery_rejects_wrong_cardinality_and_identity() -> None:
    short_report = _report(total=49)
    with pytest.raises(recovery.RecoveryError, match="cardinality"):
        recovery.trace_rows_from_sealed_report(
            short_report,
            expected_cases_total=50,
        )

    report = _report()
    report["results"][0]["trace_eval_case_id"] = "another-case"
    with pytest.raises(recovery.RecoveryError, match="sealed trace evidence is invalid"):
        recovery.trace_rows_from_sealed_report(report, expected_cases_total=50)
