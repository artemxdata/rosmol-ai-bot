from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github/workflows/ci.yml",
    ROOT / ".github/workflows/eval.yml",
)
CHECKOUT_ACTION = "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5"
SETUP_PYTHON_ACTION = "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"


def test_ci_actions_and_runner_are_immutable_and_least_privilege() -> None:
    for workflow_path in WORKFLOWS:
        workflow = workflow_path.read_text(encoding="utf-8")

        assert "runs-on: ubuntu-24.04" in workflow
        assert "ubuntu-latest" not in workflow
        assert workflow.index("permissions:\n  contents: read") < workflow.index("jobs:")

        uses = re.findall(r"(?m)^\s*- uses: ([^\s#]+)", workflow)
        assert uses == [CHECKOUT_ACTION, SETUP_PYTHON_ACTION]
        assert re.search(
            rf"(?m)^\s*- uses: {re.escape(CHECKOUT_ACTION)}[^\n]*\n"
            r"\s+with:\n"
            r"\s+persist-credentials: false$",
            workflow,
        )
        assert 'python-version: "3.11.15"' in workflow
        assert "python -m venv --clear .ci-venv" in workflow
        assert "version('pip') == '24.0'" in workflow
        assert "version('setuptools') == '79.0.1'" in workflow
        assert "-r requirements/sdist-build-tools.lock" in workflow
        assert "version('pip') == '26.1.2'" in workflow
        assert "version('setuptools') == '83.0.0'" in workflow
        assert (
            "--require-hashes --no-deps --no-build-isolation "
            "--no-binary=docopt -r requirements/sdist-bootstrap.lock"
        ) in workflow
        assert (
            ".ci-venv/bin/python -m pip install "
            "--require-hashes --only-binary=:all: "
            "-r requirements/dev.lock"
        ) in workflow
        assert 'echo "$PWD/.ci-venv/bin" >> "$GITHUB_PATH"' in workflow
        assert 'pip install ".[dev]"' not in workflow
        assert "python -m pip check" in workflow
