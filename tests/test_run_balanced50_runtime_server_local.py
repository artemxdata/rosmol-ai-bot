from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_balanced50_runtime_server_local.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _bash_execution() -> tuple[str, dict[str, str]]:
    environment = os.environ.copy()
    if os.name == "nt":
        git_root = (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Git"
        )
        git_bash = git_root / "usr" / "bin" / "bash.exe"
        if git_bash.is_file():
            environment["PATH"] = os.pathsep.join(
                [
                    str(git_root / "usr" / "bin"),
                    str(git_root / "bin"),
                    environment.get("PATH", ""),
                ]
            )
            return str(git_bash), environment
    return "bash", environment


def _function(text: str, name: str) -> str:
    declarations = list(re.finditer(r"(?m)^([a-z_]+)\(\) \{$", text))
    for index, declaration in enumerate(declarations):
        if declaration.group(1) != name:
            continue
        end = declarations[index + 1].start() if index + 1 < len(declarations) else len(text)
        return text[declaration.end() : end]
    raise AssertionError(name)


def test_runner_is_server_local_exact_runtime_and_channel_bound() -> None:
    text = _text()

    assert text.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
    assert "umask 077" in text
    assert "exec 2>/dev/null" not in text
    assert "DIAGNOSTIC_STDERR_FILE" in text
    assert "stderr_tail_begin" in _function(text, "print_stderr_tail")
    assert "step=%s exit_code=%s" in _function(text, "fail")
    assert 'SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"' in text
    assert 'RUNTIME_CONTAINER="rosmol-app-ml"' in text
    assert 'CHANNEL_ATTESTATION" == "HDE_VK_DISABLED"' in text
    assert "tooling_not_detached" in text
    assert "tooling_worktree_not_clean" in text
    assert "tooling_contains_runtime_change" in text
    assert "runtime_image_identity_mismatch" not in text
    assert "postflight_runtime_ready" in text
    assert "release_git_sha" in _function(text, "verify_runtime_ready")

    lowered = text.casefold()
    for forbidden in (
        "ssh ",
        "scp ",
        "rsync ",
        "git pull",
        "docker compose up",
        "docker compose build",
        "docker compose restart",
        "/webhook/",
    ):
        assert forbidden not in lowered


def test_runner_materializes_exact_v5_25_plus_25_once() -> None:
    text = _text()
    prepare = _function(text, "prepare_cases_if_needed")
    run = _function(text, "run_eval_once")

    assert 'DATASET_ID="pilot50_balanced_v5"' in text
    assert 'MANIFEST_REL="eval/cases/pilot50_balanced_v5.json"' in text
    assert (
        'EXPECTED_CASES_SHA256="9d53114722191330214f5917ee3baf4ccfcf4eb644be34a0253c60531b225529"'
        in text
    )
    assert "-m scripts.pilot50 prepare" in prepare
    assert 'groups == {"typical": 25, "atypical": 25}' in _function(
        text, "validate_cases"
    )
    assert run.count("-m eval.run_ask") == 1
    assert "--max-cases" not in run
    assert "--fail-on-any-case" not in run
    assert "--require-complete-traces" in run
    assert "--bypass-cache" in run
    assert "--concurrency 2" in run
    assert "--timeout 180" in run
    assert "run.started" in run
    assert "interrupted_after_start_no_retry" in run


def test_runner_has_100_ruble_approval_and_persistent_cost_ledger() -> None:
    text = _text()
    run = _function(text, "run_eval_once")
    ledger = _function(text, "normalize_cost_ledger_access")

    assert 'COST_CAP_RUB="100"' in text
    assert '"cost_cap_rub": float(sys.argv[5])' in _function(
        text, "write_or_validate_binding"
    )
    assert 'COST_LEDGER_DIR="/var/lib/rosmol/eval-cost-ledger-v1"' in text
    assert '--max-llm-cost-rub "$COST_CAP_RUB"' in run
    assert '--high-cost-approval-id "$APPROVAL_ID"' in run
    assert "--large-run-threshold 10" in run
    assert '"ACCEPTANCE_COST_LEDGER_DIR=$COST_LEDGER_DIR"' in _function(
        text, "build_compose_command"
    )
    assert "run.binding.json" in text
    assert "cost_ledger_not_writable" not in text
    assert "stat.S_ISDIR" in ledger and "stat.S_ISREG" in ledger
    assert "value.st_dev == root_device" in ledger
    assert 'chown -R --no-dereference "$APP_UID:$APP_GID"' in ledger
    assert '-type d -exec chmod 0700' in ledger
    assert '-type f -exec chmod 0600' in ledger
    assert 'value.st_uid == app_uid and value.st_gid == app_gid' in ledger
    assert 'stat.S_IMODE(value.st_mode) & 0o700 == 0o700' in ledger
    assert 'stat.S_IMODE(value.st_mode) & 0o400 == 0o400' in ledger
    assert 'sudo -u "#$APP_UID"' not in ledger
    container_probe = _function(text, "verify_cost_ledger_container_access")
    assert "quality-acceptance" in container_probe
    assert 'dir="/cost-ledger"' in container_probe
    assert "os.unlink(path)" in container_probe
    assert "eval.run_ask" not in container_probe
    assert _function(text, "main").index(
        "verify_cost_ledger_container_access"
    ) < _function(text, "main").index("run_eval_once")


def test_runner_verifies_pricing_inside_target_runtime_before_start() -> None:
    text = _text()
    pricing = _function(text, "verify_runtime_pricing")
    load_state = _function(text, "load_state")
    main = _function(text, "main")
    run = _function(text, "run_eval_once")

    assert 'docker exec -i "$RUNTIME_CONTAINER" python -' in pricing
    assert "from src.config import get_settings" in pricing
    assert "cloud_ru_model_simple_input_price_rub_per_million" in pricing
    assert "cloud_ru_model_simple_output_price_rub_per_million" in pricing
    assert "cloud_ru_model_complex_input_price_rub_per_million" in pricing
    assert "cloud_ru_model_complex_output_price_rub_per_million" in pricing
    assert "cloud_ru_model_analyzer" in pricing
    assert "cloud_ru_model_judge" in pricing
    assert "runtime_pricing_invalid" in load_state
    assert "runtime_pricing_changed_before_run" in main
    assert main.index("verify_runtime_pricing") < main.index("run_eval_once")
    assert "verify_runtime_pricing" not in run


def test_compose_sets_required_inactive_phase0_bindings() -> None:
    text = _text()
    compose = _function(text, "build_compose_command")
    acceptance_compose = (ROOT / "docker-compose.acceptance.yml").read_text(
        encoding="utf-8"
    )
    required = set(re.findall(r"\$\{(PHASE0_[A-Z0-9_]+):\?", acceptance_compose))

    assert required == {
        "PHASE0_RUNTIME_GIT_SHA",
        "PHASE0_RUNNER_SOURCE_DIR",
        "PHASE0_BUILDER_SOURCE_DIR",
        "PHASE0_PRIVATE_DIR",
        "PHASE0_COST_LEDGER_DIR",
    }
    assert all(f'"{name}=' in compose for name in required)
    assert "--profile phase0" not in compose
    assert "docker-compose.acceptance.yml" in compose
    assert "config --quiet" in _function(text, "main")


def test_runner_uses_archived_source_and_prints_only_global_analysis() -> None:
    text = _text()
    source = _function(text, "prepare_source_snapshot")
    analyze = _function(text, "analyze_and_print")

    assert 'git -C "$TOOLING_ROOT" archive --format=tar "$TOOLING_SHA"' in source
    assert "source_snapshot_contains_env" in _function(text, "verify_source_snapshot")
    assert "source_snapshot_writable" in _function(text, "verify_source_snapshot")
    assert "/workspace/scripts/analyze_balanced50_runtime.py" in analyze
    assert "global-summary.json" in analyze
    assert '--expected-cost-cap-rub "$COST_CAP_RUB"' in analyze
    assert "response" not in analyze
    assert "query" not in analyze
    assert "request_id" not in analyze
    assert ">/dev/null" in _function(text, "run_eval_once")


def test_runner_removes_orphans_for_every_compose_run() -> None:
    text = _text()

    assert text.count('"${compose[@]}" run') == 4
    assert text.count("run --rm --remove-orphans --no-deps --pull never") == 4


def test_runner_preserves_primary_results_before_postflight_and_analysis() -> None:
    text = _text()
    run = _function(text, "run_eval_once")
    preserve = _function(text, "preserve_primary_results")
    finalize = _function(text, "finalize_preserved_results")
    fail = _function(text, "fail")

    assert 'PRIMARY_RESULTS_PRESERVED="true"' in preserve
    assert "balanced50_primary_results=PRESERVED" in preserve
    assert "postflight_runtime_ready" in finalize
    assert finalize.index("verify_runtime_ready") < finalize.index("analyze_and_print")
    assert run.index("preserve_primary_results") < run.index(
        "finalize_preserved_results"
    )
    assert run.index("finalize_preserved_results") < run.index(
        "ask_eval_auxiliary_failure_after_report"
    )
    assert "primary_results_preserved=%s" in fail


def test_diagnostic_self_test_prints_step_exit_code_and_last_20_stderr_lines() -> None:
    bash, environment = _bash_execution()
    completed = subprocess.run(
        [bash, SCRIPT.as_posix(), "diagnostic-self-test"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 23
    assert completed.stderr == ""
    lines = completed.stdout.splitlines()
    assert lines[0] == (
        "balanced50_runtime_server_local=FAIL step=intentional_diagnostic_step "
        "exit_code=23 primary_results_preserved=false"
    )
    assert lines[1] == "stderr_tail_begin"
    assert lines[2] == "intentional diagnostic stderr line 06"
    assert lines[21] == "intentional diagnostic stderr line 25"
    assert lines[22] == "stderr_tail_end"
    assert "intentional diagnostic stderr line 05" not in completed.stdout
