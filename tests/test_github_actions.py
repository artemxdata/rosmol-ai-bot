from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "secretless-ci.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_secretless_ci_has_read_only_permissions_and_no_secret_context() -> None:
    workflow = _workflow_text()

    assert "permissions:\n  contents: read" in workflow
    assert "pull_request_target" not in workflow
    assert "secrets." not in workflow
    assert "environment:" not in workflow
    assert "persist-credentials: false" in workflow
    assert "RELEASE_GIT_SHA: ${{ github.sha }}" in workflow


def test_secretless_ci_pins_every_external_action_to_a_full_commit() -> None:
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", _workflow_text(), flags=re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", value) for value in uses)


def test_secretless_ci_runs_the_required_local_gate_and_history_scan() -> None:
    workflow = _workflow_text()

    assert "fetch-depth: 0" in workflow
    assert "gitleaks:v8.28.0@sha256:" in workflow
    assert "--redact" in workflow
    assert "python -m ruff check ." in workflow
    assert "python -m pytest -p no:cacheprovider -q" in workflow
    assert "python scripts/index_kb.py --validate-only" in workflow
    assert "requirements/dev.lock" in workflow
    assert "docker-compose.prod.yml" in workflow


def test_secretless_ci_cannot_deploy_or_open_a_remote_shell() -> None:
    workflow = _workflow_text().lower()

    forbidden = ("appleboy/ssh-action", "scp ", "rsync ", "ssh ", "docker/login-action")
    assert all(value not in workflow for value in forbidden)
