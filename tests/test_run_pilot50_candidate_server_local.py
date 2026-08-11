from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_pilot50_candidate_server_local.sh"
COMPOSE = ROOT / "docker-compose.pilot50-candidate.yml"
EXACT_SHA = "a" * 40


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


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), str(SCRIPT), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _safe_validator_python() -> str:
    function = _function(_text(), "validate_safe_stdout")
    marker = "3<<'PY' 2>/dev/null\n"
    start = function.index(marker) + len(marker)
    end = function.index("\nPY", start)
    return function[start:end]


def _runtime_validator_python() -> str:
    function = _function(_text(), "validate_candidate_runtime")
    marker = "3<<'PY' 2>/dev/null\n"
    start = function.index(marker) + len(marker)
    end = function.index("\nPY", start)
    return function[start:end]


def _runtime_inspect_payload() -> list[dict[str, object]]:
    blank_secrets = (
        "WEBHOOK_AUTH_TOKEN",
        "ADMIN_AUTH_TOKEN",
        "HDE_TRIGGER_PREFIX",
        "HDE_BASE_URL",
        "HDE_API_EMAIL",
        "HDE_API_KEY",
        "HDE_BOT_USER_ID",
        "HDE_TRANSPORT_EVENT_KEY_SECRET",
        "HDE_TRANSPORT_ENCRYPTION_KEY",
        "YONOTE_API_TOKEN",
        "VK_API_TOKEN",
        "VK_GROUP_TOKEN",
        "VK_CONFIRMATION_CODE",
        "VK_SECRET",
        "VK_CALLBACK_SECRET",
    )
    environment = {
        "APP_ENV": "staging",
        "RUNTIME_ROLE": "ml",
        "RELEASE_GIT_SHA": EXACT_SHA,
        "ML_PREWARM_ON_STARTUP": "true",
        "ML_UNLOAD_AFTER_USE": "true",
        "ML_UNLOAD_EMBEDDER_AFTER_USE": "true",
        "ML_UNLOAD_RERANKER_AFTER_USE": "true",
        "HDE_TRANSPORT_ENABLED": "false",
        "YONOTE_SYNC_ENABLED": "false",
        "ADMIN_READ_ONLY": "true",
        "ADMIN_MUTATIONS_ENABLED": "false",
        "API_AUTH_TOKEN": "api-auth-token-placeholder",
        "USER_HASH_SECRET": "user-hash-secret-placeholder",
        **dict.fromkeys(blank_secrets, ""),
    }
    mount_targets = (
        "/app/data/knowledge_base_seed.json",
        "/app/data/private/admin-kb",
        "/home/app/.cache/huggingface",
        "/home/app/.cache/torch",
        "/opt/models",
    )
    return [
        {
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Memory": 6 * 1024**3,
                "NanoCpus": 2_000_000_000,
                "PidsLimit": 256,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges=true"],
                "PortBindings": {},
            },
            "Config": {
                "Labels": {
                    "com.rosmol.purpose": "pilot50-candidate",
                    "com.rosmol.dataset": "pilot50_balanced_v2",
                    "com.rosmol.candidate-git-sha": EXACT_SHA,
                },
                "Env": [f"{key}={value}" for key, value in environment.items()],
            },
            "State": {"Running": True, "OOMKilled": False},
            "NetworkSettings": {
                "Ports": {"8000/tcp": None},
                "Networks": {
                    "candidate-data": {"Aliases": ["pilot50-candidate-ml"]},
                    "candidate-egress": {"Aliases": []},
                },
            },
            "Mounts": [
                {"Destination": target, "RW": False} for target in mount_targets
            ],
        }
    ]


def _run_runtime_validator(
    payload: object,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _runtime_validator_python(),
            EXACT_SHA,
            "candidate-data",
            "candidate-egress",
        ],
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _quality_gate(
    *,
    typical: int,
    atypical: int,
    output_contract: int = 0,
    source_binding: int = 0,
    critical_cases: int = 0,
) -> dict[str, object]:
    values = {
        "overall_closed": (typical + atypical, "minimum", 30, {}),
        "typical_closed": (typical, "minimum", 11, {}),
        "atypical_closed": (atypical, "minimum", 7, {}),
        "output_contract_escalations": (output_contract, "maximum", 6, {}),
        "source_binding_failures": (
            source_binding,
            "maximum",
            0,
            {"applicable_qrel_cases": 38, "total_cases": 50},
        ),
        "critical_case_failures": (
            critical_cases,
            "maximum",
            0,
            {"applicable_critical_cases": 15, "total_cases": 50},
        ),
    }
    criteria: dict[str, dict[str, object]] = {}
    failed: list[str] = []
    for name, (actual, bound_key, bound, extra) in values.items():
        passed = actual >= bound if bound_key == "minimum" else actual <= bound
        criteria[name] = {
            "actual": actual,
            bound_key: bound,
            "passed": passed,
            **extra,
        }
        if not passed:
            failed.append(name)
    return {
        "schema_version": "pilot50-v2-quality-gate-v1",
        "status": "STOP" if failed else "GO",
        "criteria": criteria,
        "failed_criteria": failed,
        "output_contract_reasons": sorted(
            {
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
            }
        ),
        "source_binding_definition": (
            "non_escalated_result_with_qrels_failing_any_effective_expected_retrieval_"
            "or_citation_source_check"
        ),
        "critical_case_definition": (
            "result_passed_is_not_true_for_case_tagged_adversarial_or_off_aspect_guard"
        ),
    }


def _safe_payload(
    *,
    typical: int = 15,
    atypical: int = 15,
    output_contract: int = 0,
    source_binding: int = 0,
    critical_cases: int = 0,
) -> dict[str, object]:
    rate_card = {
        "complex_input_price_rub_per_million": "569.34",
        "complex_model": "GigaChat/GigaChat-2-Max",
        "complex_official_price_rub_per_million": "569.3374",
        "complex_output_price_rub_per_million": "569.34",
        "complex_price_policy": "conservative_round_up",
        "simple_input_price_rub_per_million": "12.2",
        "simple_model": "ai-sage/GigaChat3-10B-A1.8B",
        "simple_output_price_rub_per_million": "12.2",
    }
    rate_card_sha = hashlib.sha256(
        json.dumps(
            rate_card,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()

    def table(count_key: str) -> dict[str, dict[str, float | int]]:
        return {
            "typical": {
                count_key: typical,
                "total": 25,
                "rate": round(typical / 25, 6),
            },
            "atypical": {
                count_key: atypical,
                "total": 25,
                "rate": round(atypical / 25, 6),
            },
            "overall": {
                count_key: typical + atypical,
                "total": 50,
                "rate": round((typical + atypical) / 50, 6),
            },
        }

    return {
        "schema_version": "pilot50-safe-result-v1",
        "dataset_id": "pilot50_balanced_v2",
        "eval_run_id": "ask-eval-12345678-1234-1234-1234-123456789abc",
        "runtime_git_sha": EXACT_SHA,
        "approval_id": "approval-1",
        "run_window_utc": {
            "started_at": "2026-08-11T00:00:00+00:00",
            "completed_at": "2026-08-11T00:01:00+00:00",
        },
        "billing_status": "pending_provider_reconciliation",
        "status": "OK",
        "classification": "calibration_only",
        "human_product_verdict": False,
        "denominator": 50,
        "counts": {"typical": 25, "atypical": 25},
        "mechanical_first_turn_closure": table("closed"),
        "policy_pass": table("passed"),
        "trace_coverage": {"found": 50, "total": 50, "rate": 1.0},
        "cache_hits": 0,
        "budget": {"max_rub": 30, "exceeded": False, "stopped": False},
        "pricing": {
            "complete": True,
            "stopped": False,
            "source": "target_reported",
            "contract_id": "pilot50-v2-candidate-v1",
            "rate_card_sha256": rate_card_sha,
            "target_telemetry_preserved": True,
            "target_telemetry_pricing_complete": True,
        },
        "latency_ms": {"p50": 100, "p95": 200},
        "llm_cost_rub": 1.0,
        "cases_sha256": "c" * 64,
        "report_sha256": "d" * 64,
        "disclaimer": (
            "Tracked regression calibration only. This is a mechanical first-turn "
            "closure result for the balanced Pilot50 set, not an independent holdout, "
            "a human product verdict, ticket-level conversion, or production traffic "
            "conversion."
        ),
        "quality_gate": _quality_gate(
            typical=typical,
            atypical=atypical,
            output_contract=output_contract,
            source_binding=source_binding,
            critical_cases=critical_cases,
        ),
    }


def _run_safe_validator(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _safe_validator_python(),
            "c" * 64,
            "d" * 64,
            EXACT_SHA,
            "approval-1",
            "30",
        ],
        input=json.dumps(payload, ensure_ascii=False),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_candidate_compose_is_a_single_isolated_one_off_service() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    assert payload["name"] == "rosmol-pilot50-candidate"
    assert set(payload["services"]) == {"pilot50-candidate-ml"}
    service = payload["services"]["pilot50-candidate-ml"]
    assert service["container_name"] == "rosmol-pilot50-candidate-ml"
    assert service["pull_policy"] == "never"
    assert service["restart"] == "no"
    assert service["user"] == "app"
    assert service["read_only"] is True
    assert service.get("ports") in (None, [])
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges=true"]
    assert service["mem_limit"] == "6g"
    assert service["cpus"] == 2.0
    assert service["pids_limit"] == 256
    assert "depends_on" not in service
    assert set(service["networks"]) == {"data", "runtime_egress"}
    assert set(payload["networks"]) == {"data", "runtime_egress"}
    assert "edge" not in payload["networks"]
    assert "default" not in payload["networks"]
    assert service["command"] == [
        "python",
        "-m",
        "uvicorn",
        "src.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]


def test_candidate_environment_is_staging_ml_with_only_required_secrets() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    environment = payload["services"]["pilot50-candidate-ml"]["environment"]

    assert environment["APP_ENV"] == "staging"
    assert environment["RUNTIME_ROLE"] == "ml"
    assert environment["PROMPT_VERSION"] == "pilot50-quality-v2"
    assert environment["ML_PREWARM_ON_STARTUP"] == "true"
    assert environment["ML_UNLOAD_AFTER_USE"] == "true"
    assert environment["ML_UNLOAD_EMBEDDER_AFTER_USE"] == "true"
    assert environment["ML_UNLOAD_RERANKER_AFTER_USE"] == "true"
    assert environment["ADMIN_READ_ONLY"] == "true"
    assert environment["ADMIN_MUTATIONS_ENABLED"] == "false"
    assert environment["HDE_TRANSPORT_ENABLED"] == "false"
    assert environment["YONOTE_SYNC_ENABLED"] == "false"
    for key in (
        "WEBHOOK_AUTH_TOKEN",
        "ADMIN_AUTH_TOKEN",
        "HDE_TRIGGER_PREFIX",
        "HDE_BASE_URL",
        "HDE_API_EMAIL",
        "HDE_API_KEY",
        "HDE_BOT_USER_ID",
        "HDE_TRANSPORT_EVENT_KEY_SECRET",
        "HDE_TRANSPORT_ENCRYPTION_KEY",
        "VK_API_TOKEN",
        "VK_GROUP_TOKEN",
        "VK_CONFIRMATION_CODE",
        "VK_SECRET",
        "VK_CALLBACK_SECRET",
        "YONOTE_API_TOKEN",
    ):
        assert environment[key] == ""
    assert environment["CLOUD_RU_MODEL_SIMPLE"] == "ai-sage/GigaChat3-10B-A1.8B"
    assert environment["CLOUD_RU_MODEL_COMPLEX"] == "GigaChat/GigaChat-2-Max"
    for key in (
        "CLOUD_RU_MODEL_SIMPLE_INPUT_PRICE_RUB_PER_MILLION",
        "CLOUD_RU_MODEL_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION",
        "CLOUD_RU_MODEL_COMPLEX_INPUT_PRICE_RUB_PER_MILLION",
        "CLOUD_RU_MODEL_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION",
    ):
        assert "PILOT50_" in environment[key]


def test_candidate_mounts_seed_admin_and_every_model_cache_read_only() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    volumes = payload["services"]["pilot50-candidate-ml"]["volumes"]
    by_target = {item["target"]: item for item in volumes}

    assert set(by_target) == {
        "/app/data/knowledge_base_seed.json",
        "/app/data/private/admin-kb",
        "/home/app/.cache/huggingface",
        "/home/app/.cache/torch",
        "/opt/models",
    }
    assert all(item["read_only"] is True for item in by_target.values())
    assert set(payload["volumes"]) == {"hf_cache", "torch_cache", "model_cache"}
    assert all(item["external"] is True for item in payload["volumes"].values())


def test_candidate_compose_effective_config_keeps_isolation() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")
    environment = {
        **os.environ,
        "PILOT50_CANDIDATE_GIT_SHA": EXACT_SHA,
        "PILOT50_CANDIDATE_SOURCE_DIR": str(ROOT),
        "PILOT50_CANDIDATE_API_AUTH_TOKEN": "a" * 64,
        "PILOT50_CANDIDATE_USER_HASH_SECRET": "b" * 64,
        "POSTGRES_DSN": "postgresql://candidate.invalid/db",
        "REDIS_URL": "redis://candidate.invalid/0",
        "QDRANT_API_KEY": "qdrant-test-placeholder",
        "CLOUD_RU_API_KEY": "cloud-test-placeholder",
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
    rendered = json.loads(result.stdout)
    service = rendered["services"]["pilot50-candidate-ml"]
    assert service.get("ports") in (None, [])
    assert set(service["networks"]) == {"data", "runtime_egress"}
    assert service["read_only"] is True
    assert int(service["mem_limit"]) == 6 * 1024**3
    assert float(service["cpus"]) == 2
    assert int(service["pids_limit"]) == 256
    assert service["security_opt"] == ["no-new-privileges=true"]
    assert service["build"]["context"] == str(ROOT)
    assert set(rendered["services"]) == {"pilot50-candidate-ml"}


@pytest.mark.parametrize(
    "security_opt",
    [
        ["no-new-privileges"],
        ["no-new-privileges=true"],
        ["no-new-privileges:true"],
    ],
)
def test_runtime_accepts_only_portable_enabled_no_new_privileges_forms(
    security_opt: list[str],
) -> None:
    payload = _runtime_inspect_payload()
    payload[0]["HostConfig"]["SecurityOpt"] = security_opt

    result = _run_runtime_validator(payload)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "security_opt",
    [
        None,
        [],
        ["no-new-privileges=false"],
        ["no-new-privileges:false"],
        ["no-new-privileges=true", "seccomp=unconfined"],
        ["prefix-no-new-privileges=true"],
        "no-new-privileges=true",
    ],
)
def test_runtime_rejects_missing_false_extra_or_spoofed_no_new_privileges(
    security_opt: object,
) -> None:
    payload = _runtime_inspect_payload()
    payload[0]["HostConfig"]["SecurityOpt"] = security_opt

    result = _run_runtime_validator(payload)

    assert result.returncode == 1
    assert result.stdout == "no_new_privileges\n"
    assert result.stderr == ""


@pytest.mark.parametrize("name", ["", "no"])
def test_runtime_accepts_portable_disabled_restart_policy_names(name: str) -> None:
    payload = _runtime_inspect_payload()
    payload[0]["HostConfig"]["RestartPolicy"] = {
        "Name": name,
        "MaximumRetryCount": 0,
    }

    result = _run_runtime_validator(payload)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "restart",
    [
        {"Name": "always", "MaximumRetryCount": 0},
        {"Name": "on-failure", "MaximumRetryCount": 0},
        {"Name": "no", "MaximumRetryCount": 1},
        {"Name": "", "MaximumRetryCount": 1},
        None,
    ],
)
def test_runtime_rejects_enabled_or_retrying_restart_policy(restart: object) -> None:
    payload = _runtime_inspect_payload()
    payload[0]["HostConfig"]["RestartPolicy"] = restart

    result = _run_runtime_validator(payload)

    assert result.returncode == 1
    assert result.stdout == "restart\n"
    assert result.stderr == ""


def test_runtime_failure_is_one_payload_free_allowlisted_stage_code() -> None:
    payload = _runtime_inspect_payload()
    payload[0]["HostConfig"]["Memory"] = 1
    serialized = json.dumps(payload)

    result = _run_runtime_validator(payload)

    assert result.returncode == 1
    assert result.stdout == "memory\n"
    assert result.stderr == ""
    assert serialized not in result.stdout
    assert "api-auth-token-placeholder" not in result.stdout


def test_runtime_rejects_unexpected_mount_even_when_required_mounts_remain() -> None:
    payload = _runtime_inspect_payload()
    payload[0]["Mounts"].append({"Destination": "/host-extra", "RW": True})

    result = _run_runtime_validator(payload)

    assert result.returncode == 1
    assert result.stdout == "mounts\n"
    assert result.stderr == ""


def test_runtime_requires_candidate_alias_on_data_network() -> None:
    payload = _runtime_inspect_payload()
    payload[0]["NetworkSettings"]["Networks"]["candidate-data"]["Aliases"] = []

    result = _run_runtime_validator(payload)

    assert result.returncode == 1
    assert result.stdout == "networks\n"
    assert result.stderr == ""


def test_modes_are_explicit_and_review_rejects_non_tty_before_private_reads() -> None:
    text = _text()
    invocation = _function(text, "validate_invocation")
    review = _function(text, "review_mode")

    assert "preflight | run | review | cleanup" in invocation
    tty = '[[ -t 1 ]] || fail "review_requires_owner_terminal"'
    assert tty in invocation
    assert invocation.index(tty) < text.index('case "$MODE" in')
    assert "show-safe" in review
    assert "show-review" not in review
    assert "eval.run_ask" not in review
    assert "query" not in review
    assert "response" not in review

    result = _run_script("review", EXACT_SHA)
    assert result.returncode == 1
    assert result.stdout == (
        "pilot50_candidate_server_local=FAIL "
        "reason=review_requires_owner_terminal\n"
    )
    assert result.stderr == ""


def test_invalid_invocation_is_safe_and_does_not_touch_external_state() -> None:
    result = _run_script()

    assert result.returncode == 1
    assert result.stdout == "pilot50_candidate_server_local=FAIL reason=usage\n"
    assert result.stderr == ""


def test_preflight_is_free_but_proves_runtime_before_go() -> None:
    text = _text()
    preflight = _function(text, "preflight_mode")

    assert "eval.run_ask" not in preflight
    assert "run.started" not in preflight
    assert "reserve_live_eval_cost" not in preflight
    assert "cost_governance_preflight" not in preflight
    assert "runner_command" not in preflight
    assert "HIGH_COST_APPROVAL_ID" not in preflight
    assert "qdrant_snapshot" in preflight
    assert "production_snapshot" in preflight
    assert "capacity_snapshot" in preflight
    assert preflight.index("stop_capacity") < preflight.index("sudo install -d")
    assert "-m scripts.pilot50 prepare" not in preflight
    assert "prepare_cases" in preflight
    assert "manifest_sha256=%s" in preflight
    assert "cases_sha256=%s" in preflight
    assert "qdrant_fingerprint_sha256=%s" in preflight
    assert "build --pull=false pilot50-candidate-ml" in preflight
    assert "run -d --no-deps --use-aliases" in preflight
    assert preflight.count("require_candidate_runtime") == 2
    assert "wait_candidate_ready" in preflight
    assert "remove_owned_candidate" in preflight
    assert "production_changed_during_preflight" in preflight
    assert "qdrant_changed_during_preflight" in preflight
    assert "runtime_smoke_status=OK" in preflight
    build = preflight.index("build --pull=false pilot50-candidate-ml")
    start = preflight.index("run -d --no-deps --use-aliases")
    first_runtime = preflight.index("require_candidate_runtime")
    ready = preflight.index("wait_candidate_ready")
    second_runtime = preflight.index("require_candidate_runtime", first_runtime + 1)
    cleanup = preflight.index("remove_owned_candidate")
    post_prod = preflight.index('post_prod="$(production_snapshot)"')
    post_qdrant = preflight.index('post_qdrant="$(qdrant_snapshot)"')
    go = preflight.index("pilot50_candidate_preflight=GO")
    assert build < start < first_runtime < ready < second_runtime < cleanup
    assert cleanup < post_prod < post_qdrant < go
    assert "pilot50_candidate_preflight=GO" in preflight


def test_capacity_contract_is_exact_and_docker_disk_is_conservative() -> None:
    text = _text()
    capacity = _function(text, "capacity_snapshot")

    assert 'MIN_MEM_AVAILABLE_KIB="$((7 * 1024 * 1024))"' in text
    assert 'MIN_SWAP_FREE_KIB="$((6 * 1024 * 1024))"' in text
    assert 'MIN_DOCKER_HEADROOM_BYTES="$((5 * 1024 * 1024 * 1024))"' in text
    assert "MemAvailable" in capacity
    assert "SwapFree" in capacity
    assert "load1 <= 0.75 * nproc" in capacity
    assert "disk_required = image_size + headroom" in capacity
    assert "capacity_status=" in capacity


def test_source_and_image_are_bound_to_clean_exact_detached_commit() -> None:
    text = _text()
    common = _function(text, "load_common_state")
    preflight = _function(text, "preflight_mode")
    run = _function(text, "run_mode")

    assert 'SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"' in text
    assert "rev-parse HEAD" in common
    assert "candidate_sha_mismatch" in common
    assert "symbolic-ref -q HEAD" in common
    assert "candidate_checkout_not_detached" in common
    assert "--porcelain=v1 --untracked-files=all" in common
    assert "candidate_source_not_clean" in common
    assert "candidate_source_has_symlink" in common
    assert 'archive --format=tar "$EXPECTED_SHA"' in preflight
    assert 'install -d -m 0755 -o "$owner_uid" -g "$owner_gid"' in preflight
    assert '-type d -exec chmod 0555 {} +' in preflight
    assert '-type f -exec chmod 0444 {} +' in preflight
    assert preflight.index("chmod 0555") < preflight.index("prepare_cases")
    verify = _function(text, "verify_source_snapshot")
    assert "-type d ! -perm 0555" in verify
    assert "-type f ! -perm 0444" in verify
    assert "-type l" in verify
    assert "source_snapshot_invalid" in preflight
    assert "build --pull=false pilot50-candidate-ml" in preflight
    assert "source_snapshot_changed_during_build" in preflight
    assert preflight.index("verify_source_snapshot") < preflight.index(
        "build --pull=false pilot50-candidate-ml"
    )
    assert preflight.index("build --pull=false pilot50-candidate-ml") < preflight.index(
        "source_snapshot_changed_during_build"
    )
    assert "build --pull=false pilot50-candidate-ml" in run
    assert "source_snapshot_changed_during_build" in run
    assert "org.opencontainers.image.revision" in run
    assert "candidate_image_sha_mismatch" in run
    ephemeral = _function(text, "create_ephemeral_env")
    assert 'build_source="$SOURCE_DIR"' in ephemeral
    assert "PILOT50_CANDIDATE_SOURCE_DIR=%s" in ephemeral
    compose_command = _function(text, "build_compose_command")
    assert 'compose_root="$SOURCE_DIR"' in compose_command
    assert '-f "$compose_root/$COMPOSE_REL"' in compose_command


def test_effective_compose_and_started_container_are_both_fail_closed() -> None:
    text = _text()
    compose_validator = _function(text, "validate_effective_compose")
    runtime_validator = _function(text, "validate_candidate_runtime")
    run = _function(text, "run_mode")

    for validator in (compose_validator, runtime_validator):
        assert "6 * 1024**3" in validator
        assert "2" in validator
        assert "256" in validator
        assert "pilot50-candidate" in validator
        assert "pilot50_balanced_v2" in validator
        assert "APP_ENV" in validator
        assert "RUNTIME_ROLE" in validator
        assert "HDE_TRANSPORT_ENABLED" in validator
        assert "YONOTE_SYNC_ENABLED" in validator
        assert "ADMIN_MUTATIONS_ENABLED" in validator
    assert "config --format json" in compose_validator
    assert "NetworkSettings" in runtime_validator
    assert "ReadonlyRootfs" in runtime_validator
    assert "OOMKilled" in runtime_validator
    assert "no-new-privileges=true" in compose_validator
    assert "no_new_privileges_enabled" in runtime_validator
    assert run.index("require_candidate_runtime") < run.index("eval.run_ask")
    assert run.count("wait_candidate_ready") >= 2


def test_runtime_isolation_errors_are_payload_free_allowlisted_stage_reasons() -> None:
    text = _text()
    required = _function(text, "require_candidate_runtime")

    for stage in (
        "identity",
        "inspect",
        "state",
        "rootfs",
        "memory",
        "cpu",
        "pids",
        "restart",
        "cap_drop",
        "no_new_privileges",
        "ports",
        "networks",
        "labels",
        "runtime_env",
        "ml_lifecycle",
        "transports",
        "secrets",
        "mounts",
    ):
        assert stage in required
    assert 'fail "candidate_isolation_$stage"' in required
    assert 'fail "candidate_isolation_inspect"' in required
    assert "candidate_isolation_invalid" not in text
    assert "candidate_isolation_changed" not in text


def test_run_uses_exact_bounded_candidate_contract_without_repricing() -> None:
    text = _text()
    run = _function(text, "run_mode")

    assert 'TARGET="http://pilot50-candidate-ml:8000/ask"' in text
    assert 'COST_CAP_RUB="30"' in text
    assert "--concurrency 1" in run
    assert '--max-llm-cost-rub "$COST_CAP_RUB"' in run
    assert '--pilot50-candidate-contract "$CANDIDATE_CONTRACT_ID"' in run
    assert '--expected-runtime-git-sha "$EXPECTED_SHA"' in run
    assert '--high-cost-approval-id "$approval_id"' in run
    assert "--bypass-cache" in run
    assert "--require-complete-traces" in run
    assert '--candidate-contract "$CANDIDATE_CONTRACT_ID"' in run
    assert "--expected-target" not in run
    assert "--pricing-mode" not in run
    assert "llm-cost-repricing" not in run
    assert "--user-prefix" not in run
    assert "--max-cases" not in run
    assert run.index("wait_candidate_ready") < run.index("eval.run_ask")
    marker = "schema_version=pilot50-candidate-run-started-v1"
    assert run.index("build --pull=false") < run.index(marker)
    assert run.index("candidate_owned") < run.index(marker)
    assert run.index("wait_candidate_ready") < run.index(marker)
    assert run.index("cost_governance_preflight") < run.index(marker)
    assert run.index(marker) < run.index("eval.run_ask")
    assert run.index("eval.run_ask") < run.index("scripts.pilot50 summarize")


def test_execution_success_and_quality_verdict_are_separate_and_sealed() -> None:
    text = _text()
    run = _function(text, "run_mode")
    review = _function(text, "review_mode")

    assert "pilot50_candidate_server_local=OK" in run
    assert "pilot50_candidate_quality=%s" in run
    assert "quality_status=%s" in run
    completed = 'completed="$RUN_DIR/run.completed"'
    assert run.index(completed) < run.index("quality_status=%s")
    assert run.index("quality_status=%s") < run.index("pilot50_candidate_quality=%s")
    assert '"$quality_status" == "GO" || "$quality_status" == "STOP"' in run
    assert "candidate_quality_status_mismatch" in review
    assert "pilot50_candidate_quality=%s" in review


def test_cost_governance_eligibility_is_free_read_only_and_before_one_shot() -> None:
    text = _text()
    preflight = _function(text, "cost_governance_preflight")
    run = _function(text, "run_mode")

    assert "--network none" in preflight
    assert '-v "$COST_LEDGER_DIR:/cost-ledger:ro"' in preflight
    assert "_validated_approval_id" in preflight
    assert "_scan_records" in preflight
    assert "_enforce_approval_once" in preflight
    assert "_enforce_rolling_limits" in preflight
    assert "requested_cap=30.0" in preflight
    assert "private_full=True" in preflight
    assert "requested_runtime_git_sha=runtime_sha" in preflight
    assert "reserve_live_eval_cost" not in preflight
    assert "eval.run_ask" not in preflight
    assert ">/dev/null 2>&1" in preflight
    marker = "schema_version=pilot50-candidate-run-started-v1"
    assert run.index("cost_governance_preflight") < run.index(marker)
    assert run.index(marker) < run.index("eval.run_ask")
    assert "cost_governance_preflight_failed" in run


@pytest.mark.parametrize(
    (
        "typical",
        "atypical",
        "output_contract",
        "source_binding",
        "critical_cases",
        "expected_status",
        "expected_failed",
    ),
    [
        (12, 18, 6, 0, 0, "GO", []),
        (11, 18, 0, 0, 0, "STOP", ["overall_closed"]),
        (10, 20, 0, 0, 0, "STOP", ["typical_closed"]),
        (24, 6, 0, 0, 0, "STOP", ["atypical_closed"]),
        (15, 15, 7, 0, 0, "STOP", ["output_contract_escalations"]),
        (15, 15, 0, 1, 0, "STOP", ["source_binding_failures"]),
        (15, 15, 0, 0, 1, "STOP", ["critical_case_failures"]),
    ],
)
def test_safe_validator_enforces_quality_gate_boundaries(
    typical: int,
    atypical: int,
    output_contract: int,
    source_binding: int,
    critical_cases: int,
    expected_status: str,
    expected_failed: list[str],
) -> None:
    payload = _safe_payload(
        typical=typical,
        atypical=atypical,
        output_contract=output_contract,
        source_binding=source_binding,
        critical_cases=critical_cases,
    )

    result = _run_safe_validator(payload)

    assert result.returncode == 0, result.stderr
    validated = json.loads(result.stdout)
    assert validated["status"] == "OK"
    assert validated["quality_gate"]["status"] == expected_status
    assert validated["quality_gate"]["failed_criteria"] == expected_failed


@pytest.mark.parametrize(
    "mutation",
    [
        "unexpected_top_level",
        "wrong_failed_order",
        "wrong_passed_flag",
        "wrong_taxonomy",
        "wrong_source_coverage",
        "wrong_critical_definition",
    ],
)
def test_safe_validator_rejects_quality_gate_schema_drift(mutation: str) -> None:
    payload = _safe_payload(
        typical=10,
        atypical=6,
        output_contract=7,
        source_binding=1,
        critical_cases=1,
    )
    gate = payload["quality_gate"]
    assert isinstance(gate, dict)
    if mutation == "unexpected_top_level":
        payload["unexpected"] = "unsafe"
    elif mutation == "wrong_failed_order":
        gate["failed_criteria"] = list(reversed(gate["failed_criteria"]))
    elif mutation == "wrong_passed_flag":
        criteria = gate["criteria"]
        assert isinstance(criteria, dict)
        criteria["output_contract_escalations"]["passed"] = True
    elif mutation == "wrong_taxonomy":
        gate["output_contract_reasons"] = ["llm_generation_failed"]
    elif mutation == "wrong_source_coverage":
        criteria = gate["criteria"]
        assert isinstance(criteria, dict)
        criteria["source_binding_failures"]["applicable_qrel_cases"] = 50
    else:
        gate["critical_case_definition"] = "weaker_definition"

    result = _run_safe_validator(payload)

    assert result.returncode != 0
    assert result.stdout == ""


def test_run_preserves_private_evidence_and_checks_prod_and_kb_pre_post() -> None:
    text = _text()
    run = _function(text, "run_mode")
    qdrant = _function(text, "qdrant_snapshot")

    assert 'BASE_DIR="/var/lib/rosmol/pilot50-candidate"' in text
    assert 'COST_LEDGER_DIR="/var/lib/rosmol/eval-cost-ledger-v1"' in text
    assert "pilot50-ask-report.json" in run
    assert "pilot50-safe-result.json" in run
    assert "sudo chmod 0600" in run
    assert run.count("production_snapshot") >= 3
    assert run.count("qdrant_snapshot") >= 2
    assert "production_changed_during_run" in run
    assert "production_changed_after_cleanup" in run
    assert "qdrant_changed_during_run" in run
    assert "/points/count" in qdrant
    assert "/points/scroll" in qdrant
    assert '"with_payload": True' in qdrant
    assert '"with_vector": True' in qdrant
    assert "KBSeedRecord" in qdrant
    assert "build_filter_key_payload" in qdrant
    assert "actual_digest == expected_digest" in qdrant
    assert "vector_digest" in qdrant
    assert "seed_sha" in qdrant
    assert "qdrant_seed_sha256" in _function(text, "preflight_mode")
    assert "candidate_seed_differs_from_runtime" in text
    for forbidden in ("upsert", "delete", "set_payload", "overwrite"):
        assert forbidden not in qdrant.casefold()


def test_cleanup_can_only_stop_and_remove_the_exact_labeled_candidate() -> None:
    text = _text()
    identity = _function(text, "candidate_owned")
    remove = _function(text, "remove_owned_candidate")
    cleanup = _function(text, "cleanup_mode")

    assert 'CANDIDATE_CONTAINER="rosmol-pilot50-candidate-ml"' in text
    assert "com.rosmol.purpose" in identity
    assert "com.rosmol.dataset" in identity
    assert "com.rosmol.candidate-git-sha" in identity
    assert "org.opencontainers.image.revision" in identity
    assert '[[ -z "$CANDIDATE_ID" || "$CANDIDATE_ID" == "$candidate_id" ]]' in identity
    assert identity.index('[[ -z "$CANDIDATE_ID"') < identity.index(
        'CANDIDATE_ID="$candidate_id"'
    )
    assert "docker stop --time 45 \"$CANDIDATE_ID\"" in remove
    assert "docker rm \"$CANDIDATE_ID\"" in remove
    assert "docker rm -f" not in text
    assert "candidate_owned" in cleanup
    for forbidden in ("EVIDENCE_DIR", "COST_LEDGER_DIR", "network", "volume", "image rm"):
        assert forbidden not in cleanup


def test_exit_cleanup_failure_is_safe_visible_and_forces_nonzero() -> None:
    text = _text()
    cleanup_temp = _function(text, "cleanup_temp")
    cleanup_exit = _function(text, "cleanup_on_exit")
    run = _function(text, "run_mode")

    assert "|| true" not in cleanup_temp
    assert "remove_owned_candidate" in cleanup_exit
    assert "cleanup_temp || temp_cleanup_failed=1" in cleanup_exit
    assert "pilot50_candidate_exit_cleanup=FAIL reason=candidate_cleanup_failed" in (
        cleanup_exit
    )
    assert "pilot50_candidate_exit_cleanup=FAIL reason=temp_cleanup_failed" in cleanup_exit
    assert "exit 1" in cleanup_exit
    assert 'cleanup_temp || fail "run_temp_cleanup_failed"' in run
    assert run.index('cleanup_temp || fail "run_temp_cleanup_failed"') < run.index(
        "pilot50_candidate_server_local=OK"
    )


def test_no_workstation_transport_or_production_lifecycle_mutation() -> None:
    text = _text().casefold()

    for forbidden in (
        "ssh ",
        "scp ",
        "rsync ",
        "git pull",
        "docker compose up",
        "docker compose down",
        "docker compose restart",
        "docker restart",
        "/webhook/",
        "helpdeskeddy",
    ):
        assert forbidden not in text
    assert "docker exec -i \"$prod_container\"" in text
    qdrant = _function(_text(), "qdrant_snapshot").casefold()
    assert "select " not in qdrant
    assert "insert " not in qdrant
    assert "update " not in qdrant
    assert "delete " not in qdrant


def test_stdout_is_allowlisted_and_ephemeral_secrets_are_never_printed() -> None:
    text = _text()
    secret_env = _function(text, "create_ephemeral_env")
    safe_validator = _function(text, "validate_safe_stdout")

    assert "exec 2>/dev/null" in text
    assert "openssl rand -hex 32" in secret_env
    assert 'printf \'PILOT50_CANDIDATE_API_AUTH_TOKEN=%s' in secret_env
    assert '| sudo tee "$EPHEMERAL_ENV_FILE" >/dev/null' in secret_env
    assert 'printf \'%s\\n\' "$api_token"' not in text
    assert 'printf \'%s\\n\' "$user_hash_secret"' not in text
    assert 'for forbidden in ("query", "response", "request_id", "trace_events", "error")' in (
        safe_validator
    )
    assert "assert forbidden not in payload" in safe_validator
    assert 'payload.get("dataset_id") == "pilot50_balanced_v2"' in safe_validator
    assert '"source": "target_reported"' in safe_validator
    assert '"quality_gate"' in safe_validator
    assert '"source_binding_failures"' in safe_validator
    assert '"applicable_qrel_cases": 38' in safe_validator
    assert '"critical_case_failures"' in safe_validator
    assert '"applicable_critical_cases": 15' in safe_validator
    assert (
        'payload.get("trace_coverage") == '
        '{"found": 50, "total": 50, "rate": 1.0}'
    ) in safe_validator
    assert '0 <= cost <= float(cap)' in safe_validator


def test_missing_candidate_cli_contract_is_an_explicit_free_stop() -> None:
    contract = _function(_text(), "verify_runner_contract_support")

    assert "--pilot50-candidate-contract" in contract
    assert "--expected-runtime-git-sha" in contract
    assert "--candidate-contract" in contract
    assert "pilot50-v2-candidate-v1" in _text()
    assert "target_reported" in contract
    assert "candidate_runner_contract_unavailable" in contract
    assert "candidate_summary_contract_unavailable" in contract


def test_bounded_candidate_cli_contract_is_callable() -> None:
    runner = subprocess.run(
        [sys.executable, "-m", "eval.run_ask", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    summary = subprocess.run(
        [sys.executable, "-m", "scripts.pilot50", "summarize", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert runner.returncode == 0, runner.stderr
    assert "--pilot50-candidate-contract" in runner.stdout
    assert "pilot50-v2-candidate-v1" in runner.stdout
    assert summary.returncode == 0, summary.stderr
    assert "--candidate-contract" in summary.stdout
    assert "pilot50-v2-candidate-v1" in summary.stdout
    assert "--expected-target" not in summary.stdout
    assert "--pricing-mode" not in summary.stdout
