from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/recover_pilot50_v5_interrupted_server_local.sh"


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
    candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
    if not candidate.is_file():
        pytest.skip("Git Bash is unavailable")
    return str(candidate)


def test_recovery_is_bound_to_the_exact_interrupted_v5_run() -> None:
    text = _text()

    for required in (
        'readonly ALLOWED_CANDIDATE_SHA="e3277e88ee3bf46ab3d375beed740f96248d53bc"',
        'readonly DATASET_ID="pilot50_balanced_v5"',
        'readonly CANDIDATE_CONTRACT_ID="pilot50-v5-recheck-v1"',
        (
            'readonly EXPECTED_MANIFEST_SHA256="'
            '12747d62190cc5e70d70490e9a649d91596ec69a316b5c2de3843ac3df6f85b4"'
        ),
        (
            'readonly EXPECTED_CASES_SHA256="'
            '9d53114722191330214f5917ee3baf4ccfcf4eb644be34a0253c60531b225529"'
        ),
        'source_run_status=started_report_present',
    ):
        assert required in text
    assert "candidate_commit_missing" in text
    assert "source_snapshot_invalid" in text
    assert "sealed_interruption_invalid" in text
    assert "candidate_image_invalid" in text


def test_recovery_never_replays_ask_and_reads_original_evidence_only() -> None:
    text = _text()
    recover = _function(text, "recover_safe_result")

    assert "--network none" in recover
    assert "scripts.pilot50 summarize" not in recover
    assert "RECOVERY_HELPER_REL" in recover
    assert "dst=/tooling/recover.py,readonly" in recover
    assert "-E /tooling/recover.py" in recover
    assert "eval.run_ask" not in text
    assert "reserve_live_eval_cost" not in text
    assert "pilot50-ask-report.json" in recover
    assert 'src=$EVIDENCE_DIR,dst=/evidence,readonly' in recover
    assert 'src=$STAGING_DIR,dst=/recovery' in recover
    assert "--output /recovery/pilot50-safe-result.json" in recover
    assert "/ask" not in text
    for forbidden in ("ssh ", "scp ", "rsync ", "docker rm -f"):
        assert forbidden not in text


def test_recovered_failure_diagnostics_are_offline_and_payload_free() -> None:
    text = _text()
    diagnose = _function(text, "diagnose_failures")
    runner = _function(text, "run_failure_diagnostics")
    validator = _function(text, "validate_failure_diagnostics")

    for required in (
        (
            'readonly SEALED_REPORT_SHA256="'
            '7693739a623bfc604b5b409b13386e53683b97617d1364bc3712205f0a42f381"'
        ),
        (
            'readonly SEALED_SAFE_RESULT_SHA256="'
            '5983f485a424ee50d9e2c58ed78e3ae01d2498ea867643ee0a5d9ad3b069bf38"'
        ),
        'readonly SEALED_RECOVERY_TOOLING_SHA="f1cf442a47d47d3cbe4395e92c3a4b215ed9d2ed"',
        "pilot50_v5_failure_diagnostics=OK",
        "new_ask_calls=0",
        "network_calls=0",
    ):
        assert required in text
    assert "--network none" in runner
    assert "recover.py diagnose" in runner
    assert "eval.run_ask" not in runner
    assert "reserve_live_eval_cost" not in runner
    assert '"query"' not in validator
    assert '"response"' not in validator
    assert '"failed_total": 5' in validator
    assert '"critical_failed": 2' in validator
    assert "validate_sealed_recovery" in diagnose


def test_recovery_preserves_incomplete_run_and_publishes_separate_evidence() -> None:
    text = _text()
    validate = _function(text, "validate_sealed_interruption")
    main = _function(text, "main")

    assert 'run_dir / "run.completed"' in validate
    assert 'evidence / "pilot50-safe-result.json"' in validate
    assert 'report_payload.get("cases_total") == 50' in validate
    assert 'len(report_payload.get("results") or []) == 50' in validate
    assert 'runtime.get("status") == "verified"' in validate
    assert 'runtime.get("matched_expected_runtime") is True' in validate
    assert 'sudo mv -T -- "$STAGING_DIR" "$RECOVERY_DIR"' in main
    assert '"$EVIDENCE_DIR/pilot50-safe-result.json"' not in main
    assert "new_ask_calls=0" in text
    assert "network_calls=0" in text


def test_recovery_rejects_invalid_invocation_before_external_state() -> None:
    result = subprocess.run(
        [_bash(), str(SCRIPT), "recover", "not-a-sha", "also-not-a-sha"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 1
    assert result.stdout == (
        "pilot50_v5_interrupted_recovery=FAIL reason=tooling_sha_invalid\n"
    )
    assert result.stderr == ""
