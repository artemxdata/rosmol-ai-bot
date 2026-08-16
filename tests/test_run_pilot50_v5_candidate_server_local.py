from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts/run_pilot50_v5_candidate_server_local.sh"
SHARED = ROOT / "scripts/run_pilot50_candidate_server_local.sh"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


def test_v5_wrapper_selects_only_the_shared_v5_contract() -> None:
    wrapper = _text(WRAPPER)

    assert 'readonly PILOT50_RUNNER_GENERATION="v5"' in wrapper
    assert 'readonly PILOT50_CANDIDATE_DATASET_ID="pilot50_balanced_v5"' in wrapper
    assert 'readonly PILOT50_CANDIDATE_PROMPT_VERSION="pilot50-quality-v5"' in wrapper
    assert 'source "$shared_runner" "$@"' in wrapper
    assert "eval.run_ask" not in wrapper
    assert "docker compose" not in wrapper
    assert "ssh " not in wrapper and "scp " not in wrapper and "rsync " not in wrapper


def test_v5_contract_and_cost_bindings_are_exact() -> None:
    text = _text(SHARED)
    for required in (
        'readonly DATASET_ID="pilot50_balanced_v5"',
        'readonly CANDIDATE_PROMPT_VERSION="pilot50-quality-v5"',
        'readonly CANDIDATE_CONTRACT_ID="pilot50-v5-recheck-v1"',
        'readonly CANDIDATE_COST_SCOPE="pilot50-v5-recheck"',
        'readonly MANIFEST_REL="eval/cases/pilot50_balanced_v5.json"',
        (
            'readonly EXPECTED_MANIFEST_SHA256="'
            '12747d62190cc5e70d70490e9a649d91596ec69a316b5c2de3843ac3df6f85b4"'
        ),
        (
            'readonly EXPECTED_CASES_SHA256="'
            '9d53114722191330214f5917ee3baf4ccfcf4eb644be34a0253c60531b225529"'
        ),
        'readonly QUALITY_GATE_SCHEMA_VERSION="pilot50-v5-quality-gate-v1"',
        '"owner-chat-20260816-pilot50-v5-${EXPECTED_SHA}-cap30"',
    ):
        assert required in text


def test_v5_preflight_is_read_only_and_run_forbids_a_waiver() -> None:
    text = _text(SHARED)
    preflight = _function(text, "preflight_mode")
    governance = _function(text, "routine_cost_capacity_check")
    run = _function(text, "run_mode")

    assert preflight.index("candidate_image_sha_mismatch") < preflight.index(
        "routine_cost_capacity_check"
    )
    assert preflight.index("routine_cost_capacity_check") < preflight.index(
        'CANDIDATE_ID="$("${compose[@]}" run'
    )
    assert "eval.run_ask" not in preflight
    assert "reserve_live_eval_cost" not in preflight
    assert "--network none" in governance
    assert '-v "$COST_LEDGER_DIR:/cost-ledger:ro"' in governance
    assert "_enforce_approval_once" in governance
    assert "_enforce_rolling_limits" in governance
    assert "private_full=True" in governance
    assert "comparison_waiver=None" in governance
    assert "rolling_24h_comparison_waiver_forbidden" in run
    assert "cost_precheck_status=GO" in run
    assert run.index("routine_cost_capacity_check") < run.index("eval.run_ask")


def test_v5_wrapper_rejects_invalid_invocation_before_external_state() -> None:
    result = subprocess.run(
        [
            _bash(),
            "scripts/run_pilot50_v5_candidate_server_local.sh",
            "preflight",
            "not-a-sha",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 1
    assert result.stdout == (
        "pilot50_candidate_server_local=FAIL reason=candidate_sha_invalid\n"
    )
    assert result.stderr == ""
