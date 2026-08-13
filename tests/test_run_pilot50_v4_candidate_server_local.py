from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts/run_pilot50_v4_candidate_server_local.sh"
SHARED = ROOT / "scripts/run_pilot50_candidate_server_local.sh"
COMPOSE = ROOT / "docker-compose.pilot50-candidate.yml"
EXACT_SHA = "a" * 40


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


def test_v4_wrapper_selects_only_the_shared_v4_contract() -> None:
    wrapper = _text(WRAPPER)

    assert 'readonly PILOT50_RUNNER_GENERATION="v4"' in wrapper
    assert 'readonly PILOT50_CANDIDATE_DATASET_ID="pilot50_balanced_v4"' in wrapper
    assert 'readonly PILOT50_CANDIDATE_PROMPT_VERSION="pilot50-quality-v4"' in wrapper
    assert 'source "$shared_runner" "$@"' in wrapper
    assert "eval.run_ask" not in wrapper
    assert "docker compose" not in wrapper
    assert "ssh " not in wrapper and "scp " not in wrapper and "rsync " not in wrapper


def test_v4_contract_constants_are_exact_and_v3_defaults_remain_present() -> None:
    text = _text(SHARED)

    for required in (
        'readonly DATASET_ID="pilot50_balanced_v4"',
        'readonly CANDIDATE_PROMPT_VERSION="pilot50-quality-v4"',
        'readonly CANDIDATE_CONTRACT_ID="pilot50-v4-candidate-v1"',
        'readonly CANDIDATE_COST_SCOPE="pilot50-v4-candidate"',
        'readonly MANIFEST_REL="eval/cases/pilot50_balanced_v4.json"',
        (
            'readonly EXPECTED_MANIFEST_SHA256="'
            'bfd14ae638da0d65b2c07ff299f8f366a2d8fb8be772223a931e601691125ede"'
        ),
        (
            'readonly EXPECTED_CASES_SHA256="'
            'c88a52225f6eec3b21a5837a94f12670f5a8ff1006818f559cb81e438d52fab8"'
        ),
        'readonly QUALITY_GATE_SCHEMA_VERSION="pilot50-v4-quality-gate-v1"',
        'readonly COMPARISON_WAIVER_DECISION_ID="D-042"',
        'readonly PRIOR_WAIVER_DECISION_ID="D-041"',
        'readonly DATASET_ID="pilot50_balanced_v3"',
        'readonly CANDIDATE_PROMPT_VERSION="pilot50-quality-v3"',
    ):
        assert required in text


def test_v4_preflight_is_offline_free_and_before_runtime_smoke() -> None:
    text = _text(SHARED)
    preflight = _function(text, "preflight_mode")
    v4 = _function(text, "v4_read_only_preflight")
    rescore = _function(text, "run_v4_offline_rescore")
    governance = _function(text, "cost_governance_preflight")

    assert preflight.index("qdrant_snapshot") < preflight.index("v4_read_only_preflight")
    assert preflight.index("candidate_image_sha_mismatch") < preflight.index(
        "v4_read_only_preflight"
    )
    assert preflight.index("v4_read_only_preflight") < preflight.index(
        'CANDIDATE_ID="$("${compose[@]}" run'
    )
    assert "eval.run_ask" not in preflight
    assert "run.started" not in preflight
    assert "reserve_live_eval_cost" not in preflight
    assert "--network none" in rescore
    assert "offline-rescore-v4" in rescore
    assert "--expected-v3-report-sha256" in rescore
    assert "--expected-v4-runtime-git-sha" in rescore
    assert "/ask" not in rescore
    assert "network_calls" in _function(text, "validate_v4_offline_rescore")
    assert "ask_requests" in _function(text, "validate_v4_offline_rescore")
    assert "incremental_llm_cost_rub" in _function(
        text, "validate_v4_offline_rescore"
    )
    assert "--network none" in governance
    assert '-v "$COST_LEDGER_DIR:/cost-ledger:ro"' in governance
    assert "reserve_live_eval_cost" not in governance
    assert "_enforce_approval_once" in governance
    assert "_enforce_waiver_once" in governance
    assert "_enforce_rolling_limits" in governance
    assert "cost_governance_preflight" in v4
    assert "run_v4_offline_rescore" in v4
    assert v4.index("run_v4_offline_rescore") < v4.index("cost_governance_preflight")


def test_v4_preflight_receipt_seals_rescore_and_chained_governance() -> None:
    text = _text(SHARED)
    receipt = _function(text, "validate_receipt")
    run = _function(text, "run_mode")
    review = _function(text, "review_mode")

    for binding in (
        "offline_rescore_sha256",
        "governance_precheck_sha256",
        "governance_decision_id",
        "prior_waiver_decision_id",
    ):
        assert binding in receipt
    assert "offline_rescore_artifact_changed" in run
    assert "governance_changed_since_preflight" in run
    assert run.index("governance_changed_since_preflight") < run.index("eval.run_ask")
    assert "owner-chat-20260814-pilot50-v4-${EXPECTED_SHA}-cap30" in run
    assert "owner-chat-20260814-waive-v3-to-v4-${EXPECTED_SHA}-cap30" in run
    assert "prior_waiver_decision_id=%s" in run
    assert "offline_rescore_sha256=%s" in run
    assert "governance_precheck_sha256=%s" in run
    assert "prior_waiver_decision_id" in review
    assert "offline_rescore_sha256" in review
    assert "governance_precheck_sha256" in review


def test_v4_compose_values_are_explicitly_selectable_without_weakening_defaults() -> None:
    compose = _text(COMPOSE)

    assert "${PILOT50_CANDIDATE_DATASET_ID:-pilot50_balanced_v3}" in compose
    assert "${PILOT50_CANDIDATE_PROMPT_VERSION:-pilot50-quality-v3}" in compose
    create_env = _function(_text(SHARED), "create_ephemeral_env")
    assert "PILOT50_CANDIDATE_DATASET_ID=%s" in create_env
    assert "PILOT50_CANDIDATE_PROMPT_VERSION=%s" in create_env


def test_v4_effective_compose_selects_exact_dataset_and_prompt() -> None:
    if not shutil.which("docker"):
        pytest.skip("Docker CLI is unavailable")
    environment = {
        **os.environ,
        "PILOT50_CANDIDATE_GIT_SHA": EXACT_SHA,
        "PILOT50_CANDIDATE_SOURCE_DIR": str(ROOT),
        "PILOT50_CANDIDATE_API_AUTH_TOKEN": "a" * 64,
        "PILOT50_CANDIDATE_USER_HASH_SECRET": "b" * 64,
        "PILOT50_CANDIDATE_DATASET_ID": "pilot50_balanced_v4",
        "PILOT50_CANDIDATE_PROMPT_VERSION": "pilot50-quality-v4",
        "POSTGRES_DSN": "postgresql://candidate.invalid/db",
        "REDIS_URL": "redis://candidate.invalid/0",
        "QDRANT_API_KEY": "qdrant-placeholder",
        "CLOUD_RU_API_KEY": "cloud-placeholder",
        "CLOUD_RU_CHAT_COMPLETIONS_URL": "https://example.invalid/v1/chat/completions",
        "PILOT50_SIMPLE_INPUT_PRICE_RUB_PER_MILLION": "12.2",
        "PILOT50_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION": "12.2",
        "PILOT50_COMPLEX_INPUT_PRICE_RUB_PER_MILLION": "569.34",
        "PILOT50_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION": "569.34",
        "PILOT50_KB_SEED_PATH": str(ROOT / "data/knowledge_base_seed.json"),
        "PILOT50_ADMIN_KB_DIR": str(ROOT),
        "PILOT50_DATA_NETWORK": "pilot50-test-data",
        "PILOT50_RUNTIME_EGRESS_NETWORK": "pilot50-test-egress",
        "PILOT50_HF_CACHE_VOLUME": "pilot50-test-hf",
        "PILOT50_TORCH_CACHE_VOLUME": "pilot50-test-torch",
        "PILOT50_MODEL_CACHE_VOLUME": "pilot50-test-model",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0 and "Access is denied" in result.stderr:
        pytest.skip("Docker CLI config is unavailable in the sandbox")
    assert result.returncode == 0, result.stderr
    rendered = yaml.safe_load(result.stdout)
    service = rendered["services"]["pilot50-candidate-ml"]
    assert service["labels"]["com.rosmol.dataset"] == "pilot50_balanced_v4"
    assert service["environment"]["PROMPT_VERSION"] == "pilot50-quality-v4"
    assert service["environment"]["YONOTE_SYNC_ENABLED"] == "false"
    assert service["environment"]["HDE_TRANSPORT_ENABLED"] == "false"
    assert service["read_only"] is True
    assert service.get("ports") in (None, [])


def test_v4_wrapper_rejects_invalid_invocation_before_external_state() -> None:
    result = subprocess.run(
        [
            _bash(),
            "scripts/run_pilot50_v4_candidate_server_local.sh",
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
