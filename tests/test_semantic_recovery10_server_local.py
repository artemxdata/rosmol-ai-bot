from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_semantic_recovery10_candidate_server_local.sh"
COMPOSE = ROOT / "docker-compose.pilot50-candidate.yml"


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


def test_recovery10_runner_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        [_bash(), "-n", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_recovery10_runner_is_bounded_and_does_not_use_channels_or_ssh() -> None:
    text = _text()
    assert 'readonly CASES_TOTAL="10"' in text
    assert 'readonly COST_CAP_RUB="200"' in text
    assert 'readonly TARGET="http://pilot50-candidate-ml:8000/ask"' in text
    assert "owner-chat-20260814-semantic10-" in text
    assert "--concurrency 1" in text
    assert '--max-llm-cost-rub "$COST_CAP_RUB"' in text
    assert '--expected-runtime-git-sha "$EXPECTED_SHA"' in text
    assert "--bypass-cache" in text
    assert "--require-complete-traces" in text
    assert "ssh " not in text
    assert "scp " not in text
    assert "rsync " not in text
    assert "HDE_TRANSPORT_ENABLED" in COMPOSE.read_text(encoding="utf-8")
    assert "channels_status=HDE_VK_DISABLED" in text


def test_recovery10_preflight_has_no_paid_ask_and_binds_prior_evidence() -> None:
    text = _text()
    preflight = _function(text, "preflight_mode")
    assert "eval.run_ask" not in preflight
    assert 'semantic_recovery10.py" prepare' not in preflight
    assert "prepare_source_and_cases" in preflight
    assert "start_candidate" in preflight
    assert "candidate_runtime_smoke=OK" in preflight
    assert "c88a52225f6eec3b21a5837a94f12670f5a8ff1006818f559cb81e438d52fab8" in text
    assert "2defcace63de2a2184b162fcae5fa8f4d50ed8317042ae64aabbb49181076a8d" in text


def test_root_owned_preflight_receipt_is_read_through_sudo() -> None:
    text = _text()
    preflight = _function(text, "preflight_mode")
    receipt_value = _function(text, "receipt_value")
    assert 'sudo tee "$STAGING_DIR/preflight.receipt"' in preflight
    assert 'sudo chmod 0600 "$STAGING_DIR/preflight.receipt"' in preflight
    assert "sudo awk" in receipt_value


def test_quality_gate_exit_with_complete_report_is_summarized() -> None:
    run = _function(_text(), "run_mode")
    assert 'local approval_id ask_exit="0"' in run
    assert '>/dev/null 2>&1 || ask_exit="$?"' in run
    assert 'if [[ "$ask_exit" -eq 1 ]]; then' in run
    assert 'sudo test -f "$raw_report" || fail "candidate_ask_eval_failed"' in run
    assert 'elif [[ "$ask_exit" -eq 2 ]]; then' in run
    assert 'fail "candidate_ask_eval_cost_stop"' in run
    assert 'elif [[ "$ask_exit" -ne 0 ]]; then' in run
    assert run.index('scripts.semantic_recovery10 summarize') > run.index(
        'if [[ "$ask_exit" -eq 1 ]]'
    )


def test_candidate_compose_explicitly_disables_channels_and_enables_recovery() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = payload["services"]["pilot50-candidate-ml"]
    environment = service["environment"]
    assert environment["HDE_TRANSPORT_ENABLED"] == "false"
    assert environment["YONOTE_SYNC_ENABLED"] == "false"
    assert environment["VK_API_TOKEN"] == ""
    assert environment["VK_GROUP_TOKEN"] == ""
    assert environment["SEMANTIC_RECOVERY_ENABLED"] == "true"
    assert environment["SEMANTIC_RECOVERY_MAX_QUESTIONS"] == "6"
    assert service["ports"] == []
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
