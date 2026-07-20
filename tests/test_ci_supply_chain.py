from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
WORKFLOWS = tuple(sorted(WORKFLOW_DIR.glob("*.yml")))
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"
CHECKOUT_ACTION = "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10"
SETUP_PYTHON_ACTION = "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405"


def _normalized_shell(workflow: str) -> str:
    return re.sub(r"\s+", " ", workflow.replace("\\", ""))


def test_ci_actions_and_runner_are_immutable_and_least_privilege() -> None:
    for workflow_path in WORKFLOWS:
        workflow = workflow_path.read_text(encoding="utf-8")

        assert "runs-on: ubuntu-24.04" in workflow
        assert "ubuntu-latest" not in workflow
        assert workflow.index("permissions:\n  contents: read") < workflow.index("jobs:")

        uses = re.findall(r"(?m)^\s*(?:-\s*)?uses: ([^\s#]+)", workflow)
        assert uses == [CHECKOUT_ACTION, SETUP_PYTHON_ACTION]

        checkout_block = workflow[
            workflow.index(f"uses: {CHECKOUT_ACTION}") : workflow.index(
                f"uses: {SETUP_PYTHON_ACTION}"
            )
        ]
        assert "persist-credentials: false" in checkout_block
        assert 'python-version: "3.11.15"' in workflow
        assert "python -m venv --clear" in workflow
        assert "version('pip') == '24.0'" in workflow
        assert "version('setuptools') == '79.0.1'" in workflow
        assert "version('pip') == '26.1.2'" in workflow
        assert "version('setuptools') == '83.0.0'" in workflow

        normalized = _normalized_shell(workflow)
        assert "-r requirements/sdist-build-tools.lock" in normalized
        assert (
            "--require-hashes --no-deps --no-build-isolation "
            "--no-binary=docopt -r requirements/sdist-bootstrap.lock"
        ) in normalized
        assert "--require-hashes --only-binary=:all: -r requirements/dev.lock" in normalized
        assert '>> "$GITHUB_PATH"' in workflow
        assert 'pip install ".[dev]"' not in workflow
        assert "python -m pip check" in workflow

        if workflow_path == CI_WORKFLOW:
            assert 'python -m venv --clear "$RUNNER_TEMP/rosmol-ci-venv"' in workflow
            assert 'echo "$RUNNER_TEMP/rosmol-ci-venv/bin" >> "$GITHUB_PATH"' in workflow
        else:
            assert "python -m venv --clear .ci-venv" in workflow
            assert 'echo "$PWD/.ci-venv/bin" >> "$GITHUB_PATH"' in workflow


def test_ci_is_secretless_and_runs_the_release_gate() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    all_workflows = "\n".join(path.read_text(encoding="utf-8") for path in WORKFLOWS)

    assert "permissions:\n  contents: read" in workflow
    assert "pull_request_target" not in all_workflows
    assert "secrets." not in all_workflows
    assert "environment:" not in all_workflows
    assert "persist-credentials: false" in workflow
    assert "RELEASE_GIT_SHA: ${{ github.sha }}" in workflow
    assert "workflow_dispatch:" in workflow
    assert "fetch-depth: 0" in workflow
    assert "gitleaks:v8.28.0@sha256:" in workflow
    assert "--redact" in workflow
    assert "python -m ruff check ." in workflow
    assert "python -m pytest -p no:cacheprovider -q" in workflow
    assert "python scripts/index_kb.py --validate-only" in workflow
    assert "requirements/dev.lock" in workflow
    assert "docker-compose.prod.yml" in workflow


def test_actions_cannot_deploy_or_open_a_remote_shell() -> None:
    workflow = "\n".join(path.read_text(encoding="utf-8") for path in WORKFLOWS).lower()

    forbidden = ("appleboy/ssh-action", "scp ", "rsync ", "ssh ", "docker/login-action")
    assert all(value not in workflow for value in forbidden)
