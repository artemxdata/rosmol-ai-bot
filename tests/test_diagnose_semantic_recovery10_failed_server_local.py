from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "diagnose_semantic_recovery10_failed_server_local.sh"


def _bash() -> str:
    if os.name != "nt":
        return "bash"
    candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
    if not candidate.is_file():
        pytest.skip("Git Bash is unavailable")
    return str(candidate)


def test_failed_recovery10_diagnostic_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        [_bash(), "-n", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_failed_recovery10_diagnostic_is_read_only_and_bound_to_sealed_run() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "eda8c2aa355c40e0e8c77ea4a0a6291610ea78ec" in text
    assert "f2168c9e8721c82e46165b3803bb7adc7f89249f50210d96dc3dcb03d2710aaf" in text
    assert "419a6a62671d7dbb03c402ae688f400e5fa1dbe46565e2a477b01cfcb4662068" in text
    assert 'readonly COST_LEDGER_DIR="/var/lib/rosmol/eval-cost-ledger-v1"' in text
    assert '--mount "type=bind,src=$SEALED_RUN_DIR/evidence,dst=/sealed-evidence,readonly"' in text
    assert '--mount "type=bind,src=$COST_LEDGER_DIR,dst=/cost-ledger,readonly"' in text
    assert '--network "$DATA_NETWORK"' in text
    assert "ASK_EVAL_POSTGRES_DSN" in text
    assert "mode & 0o022 == 0" in text
    assert "2 <= len(entries) <= 8" in text
    assert "eval.run_ask" not in text
    assert "docker compose up" not in text
    assert "docker restart" not in text
    assert "ssh " not in text
    assert "scp " not in text
    assert "rsync " not in text
    assert "diagnostic_new_ask_calls" in text
