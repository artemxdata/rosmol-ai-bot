from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_balanced50_runtime_server_local.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


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
    assert "exec 2>/dev/null" in text
    assert 'SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"' in text
    assert 'RUNTIME_CONTAINER="rosmol-app-ml"' in text
    assert 'CHANNEL_ATTESTATION" == "HDE_VK_DISABLED"' in text
    assert "tooling_not_detached" in text
    assert "tooling_worktree_not_clean" in text
    assert "tooling_contains_runtime_change" in text
    assert "runtime_image_identity_mismatch" not in text
    assert "runtime_changed_during_run" in text
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


def test_runner_has_200_ruble_approval_and_persistent_cost_ledger() -> None:
    text = _text()
    run = _function(text, "run_eval_once")
    ledger = _function(text, "normalize_cost_ledger_access")

    assert 'COST_CAP_RUB="200"' in text
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
    assert "response" not in analyze
    assert "query" not in analyze
    assert "request_id" not in analyze
    assert ">/dev/null" in _function(text, "run_eval_once")
