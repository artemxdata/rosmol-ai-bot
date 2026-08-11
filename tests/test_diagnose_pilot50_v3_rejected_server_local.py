from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/diagnose_pilot50_v3_rejected_server_local.sh"
TOOLING_SHA = "b" * 40
RUNTIME_SHA = "a5c5539ce2e8487418ed78ba64ae8ed9eab54863"
MANIFEST_SHA = "fef1caa227777e2c198bd6acdc77471fbf2551732c85e2334f8cad781025e875"
CASES_SHA = "3c76d0de9a31cf3a36a38346d38fa75d5173ac2b452ddcbf60c551678580d112"
REPORT_SHA = "151d282ea78c532742343b2f901766ed4e42fbe761c551657ba03748d5cb95da"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function(text: str, name: str) -> str:
    marker = f"{name}() {{"
    start = text.index(marker)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated function: {name}")


def _bash() -> str:
    if os.name != "nt":
        return "bash"
    candidate = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
    )
    if not candidate.is_file():
        pytest.skip("Git Bash is unavailable")
    return str(candidate)


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), str(SCRIPT), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _diagnostic_validator_python() -> str:
    function = _function(_text(), "validate_diagnostic_stdout")
    marker = "3<<'PY' 2>/dev/null\n"
    start = function.index(marker) + len(marker)
    end = function.index("\nPY", start)
    return function[start:end]


def _row(ordinal: int) -> dict[str, object]:
    group = "typical" if ordinal <= 25 else "atypical"
    if ordinal == 20:
        return {
            "ordinal": ordinal,
            "group": group,
            "passed": False,
            "was_escalated": True,
            "escalation_reason": "request_timeout",
            "observed_behavior": "escalate",
            "failed_boolean_checks": ["behavior_match", "escalation_match"],
            "generator_path": "unknown",
            "generate_retry_reasons": [],
            "latency_bucket": ">=30s",
            "failure_stage": "execution",
            "execution_issue": "request_timeout",
        }
    return {
        "ordinal": ordinal,
        "group": group,
        "passed": True,
        "was_escalated": False,
        "escalation_reason": None,
        "observed_behavior": "answer",
        "failed_boolean_checks": [],
        "generator_path": "simple",
        "generate_retry_reasons": [],
        "latency_bucket": "<5s",
        "failure_stage": "pass",
        "execution_issue": "none",
    }


OUTPUT_CONTRACT_REASONS = [
    "empty_generated_response",
    "final_response_empty",
    "final_response_too_long",
    "final_response_too_many_links",
    "final_response_unapproved_emoji",
    "llm_response_contract_failed",
    "llm_response_profile_failed",
    "llm_response_too_long",
    "llm_source_citation_failed",
    "llm_source_coverage_failed",
    "llm_source_fact_binding_failed",
    "source_response_contract_failed",
]


def _criterion(actual: int, *, minimum: int | None = None) -> dict[str, object]:
    if minimum is not None:
        return {"actual": actual, "minimum": minimum, "passed": actual >= minimum}
    return {"actual": actual, "maximum": 0, "passed": actual == 0}


def _payload() -> dict[str, object]:
    criteria = {
        "overall_closed": _criterion(49, minimum=30),
        "typical_closed": _criterion(24, minimum=11),
        "atypical_closed": _criterion(25, minimum=7),
        "output_contract_escalations": {
            "actual": 0,
            "maximum": 6,
            "passed": True,
        },
        "source_binding_failures": {
            **_criterion(0),
            "applicable_qrel_cases": 50,
            "total_cases": 50,
        },
        "critical_case_failures": {
            **_criterion(0),
            "applicable_critical_cases": 15,
            "total_cases": 50,
        },
    }
    return {
        "schema_version": "pilot50-v3-integrity-rejected-diagnostics-v1",
        "bindings": {
            "manifest_sha256": MANIFEST_SHA,
            "cases_sha256": CASES_SHA,
            "report_sha256": REPORT_SHA,
            "runtime_git_sha": RUNTIME_SHA,
        },
        "integrity": {
            "status": "integrity_rejected",
            "failures": ["trace_error_present"],
            "executed_cases_total": 50,
            "canonical_quality_gate_eligible": False,
            "selective_reruns_forbidden": True,
        },
        "directional_quality": {
            "classification": "directional_calibration_only_integrity_rejected",
            "human_product_verdict": False,
            "denominator": 50,
            "counts": {"typical": 25, "atypical": 25},
            "mechanical_first_turn_closure": {
                "typical": {"closed": 24, "total": 25, "rate": 0.96},
                "atypical": {"closed": 25, "total": 25, "rate": 1.0},
                "overall": {"closed": 49, "total": 50, "rate": 0.98},
            },
            "policy_pass": {
                "typical": {"passed": 24, "total": 25, "rate": 0.96},
                "atypical": {"passed": 25, "total": 25, "rate": 1.0},
                "overall": {"passed": 49, "total": 50, "rate": 0.98},
            },
            "trace_coverage": {"found": 50, "total": 50, "rate": 1.0},
            "cache_hits": 0,
            "llm_cost_rub": 13.31825,
            "latency_ms": {"p50": 1000, "p95": 2000},
            "projected_quality_gate": {
                "schema_version": "pilot50-v3-quality-gate-v1",
                "status": "GO",
                "criteria": criteria,
                "failed_criteria": [],
                "output_contract_reasons": OUTPUT_CONTRACT_REASONS,
                "source_binding_definition": (
                    "non_escalated_result_with_qrels_failing_any_effective_expected_"
                    "retrieval_or_citation_source_check"
                ),
                "critical_case_definition": (
                    "result_passed_is_not_true_for_case_tagged_adversarial_or_"
                    "off_aspect_guard"
                ),
            },
        },
        "failure_matrix": [_row(ordinal) for ordinal in range(1, 51)],
    }


def _canonical_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _run_validator(
    tmp_path: Path,
    *,
    payload: object | None = None,
    raw: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if raw is None:
        raw = _canonical_bytes(_payload() if payload is None else payload)
    stdout = tmp_path / "diagnostic.stdout"
    stdout.write_bytes(raw)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _diagnostic_validator_python(),
            str(stdout),
            str(64 * 1024 + 1),
            MANIFEST_SHA,
            CASES_SHA,
            REPORT_SHA,
            RUNTIME_SHA,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_script_has_valid_bash_syntax() -> None:
    completed = subprocess.run(
        [_bash(), "-n", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ((), "usage"),
        (("bad",), "tooling_sha_invalid"),
        (("0" * 40,), "tooling_sha_invalid"),
        ((RUNTIME_SHA,), "tooling_sha_invalid"),
        ((TOOLING_SHA, TOOLING_SHA), "usage"),
    ],
)
def test_invalid_invocation_fails_before_server_or_docker_access(
    args: tuple[str, ...],
    reason: str,
) -> None:
    completed = _run_script(*args)
    assert completed.returncode == 1
    assert completed.stdout == (
        f"pilot50_v3_rejected_diagnostics=FAIL reason={reason}\n"
    )
    assert completed.stderr == ""


def test_launcher_pins_exact_rejected_artifact_taxonomy_and_hashes() -> None:
    text = _text()
    for value in (RUNTIME_SHA, MANIFEST_SHA, CASES_SHA, REPORT_SHA):
        assert value in text
    sealed = _function(text, "validate_sealed_run")
    assert 'glob("pilot50-ask-report.ask-eval-*.rejected.json")' in sealed
    assert "assert len(rejected) == 1" in sealed
    assert "not canonical_report.exists()" in sealed
    assert "not safe_result.exists()" in sealed
    assert "not completed.exists()" in sealed
    assert 'candidate.get("integrity_failures") == ["trace_error_present"]' in sealed
    assert "metadata.st_nlink == 1" in sealed
    assert "metadata.st_uid == 10001 and metadata.st_gid == 10001" in sealed
    assert "stat.S_IMODE(metadata.st_mode) == 0o600" in sealed


def test_tooling_snapshot_is_exact_detached_clean_and_excludes_private_state() -> None:
    text = _text()
    load = _function(text, "load_tooling")
    snapshot = _function(text, "create_tooling_snapshot")
    assert '"$TOOLING_ROOT" == "$SERVER_PROJECT_DIR"' in load
    assert "symbolic-ref -q HEAD" in load
    assert "--porcelain=v1 --untracked-files=no" in load
    assert "tooling_tracks_private_state" in load
    assert "tooling_source_has_symlink" in load
    assert 'archive --format=tar "$EXPECTED_TOOLING_SHA"' in snapshot
    assert '--work-tree="$TOOLING_SOURCE"' in snapshot
    assert 'diff --quiet "$EXPECTED_TOOLING_SHA"' in snapshot
    assert '"$TOOLING_SOURCE/.env.production"' in snapshot


def test_diagnostic_container_is_read_only_offline_and_cannot_call_runtime() -> None:
    run = _function(_text(), "run_diagnostics")
    for required in (
        "docker run --rm --pull never --network none",
        "--user app --read-only",
        "--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m",
        "--cap-drop ALL --security-opt no-new-privileges=true",
        'src=$REJECTED_REPORT,dst=/sealed-rejected-report.json,readonly',
        "-E -m scripts.pilot50 diagnose-rejected-v3",
        '--expected-report-sha256 "$SEALED_REPORT_SHA256"',
        '--expected-runtime-git-sha "$SEALED_RUNTIME_SHA"',
    ):
        assert required in run
    for forbidden in (
        "docker build",
        "docker pull",
        "docker exec",
        "docker compose",
        "--env-file",
        "eval-cost-ledger",
        "/ask",
        "POSTGRES",
        "QDRANT_URL",
    ):
        assert forbidden not in run


def test_stdout_validator_accepts_only_canonical_payload_free_projection(
    tmp_path: Path,
) -> None:
    payload = _payload()
    completed = _run_validator(tmp_path, payload=payload)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.encode("ascii") == _canonical_bytes(payload)[:-1]
    assert len(json.loads(completed.stdout)["failure_matrix"]) == 50
    for forbidden_key in (
        '"query":',
        '"response":',
        '"id":',
        '"request_id":',
        '"eval_run_id":',
        '"trace_metadata":',
        '"failure_reasons":',
        '"error":',
    ):
        assert forbidden_key not in completed.stdout


def test_stdout_validator_fails_closed_under_python_optimize(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONOPTIMIZE"] = "1"
    completed = _run_validator(tmp_path, env=environment)
    assert completed.returncode != 0
    assert completed.stdout == ""


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_binding",
        "raw_error_field",
        "unknown_reason",
        "extra_trace_error",
        "timeout_row_moved",
        "directional_extra_field",
        "integrity_changed",
    ],
)
def test_stdout_validator_rejects_binding_schema_privacy_and_timeout_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    payload = _payload()
    rows = payload["failure_matrix"]
    assert isinstance(rows, list)
    if mutation == "wrong_binding":
        payload["bindings"]["report_sha256"] = "0" * 64
    elif mutation == "raw_error_field":
        rows[19]["error"] = "request_timeout"
    elif mutation == "unknown_reason":
        rows[19]["escalation_reason"] = "private_exception_canary"
    elif mutation == "extra_trace_error":
        rows[20]["passed"] = False
        rows[20]["failure_stage"] = "execution"
        rows[20]["execution_issue"] = "trace_error_present"
    elif mutation == "timeout_row_moved":
        rows[19] = _row(19)
        moved = dict(_row(20))
        moved["ordinal"] = 21
        moved["group"] = "typical"
        rows[20] = moved
    elif mutation == "directional_extra_field":
        payload["directional_quality"]["raw_latency"] = 45_022
    else:
        payload["integrity"]["canonical_quality_gate_eligible"] = True
    assert _run_validator(tmp_path, payload=payload).returncode != 0


@pytest.mark.parametrize(
    "raw_builder",
    [
        lambda payload: json.dumps(payload).encode("ascii") + b"\n",
        lambda payload: _canonical_bytes(payload) + b"{}\n",
        lambda payload: _canonical_bytes(payload) + b"\x00",
        lambda payload: b"x" * (64 * 1024 + 2),
    ],
)
def test_stdout_validator_rejects_noncanonical_or_oversized_stdout(
    tmp_path: Path,
    raw_builder: Any,
) -> None:
    assert _run_validator(tmp_path, raw=raw_builder(_payload())).returncode != 0
