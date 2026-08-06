from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_phase0_safe_metrics import build_phase0_safe_metrics


def test_phase0_safe_metrics_rejects_unbound_report_without_output(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    report_path = tmp_path / "report.json"
    output_path = tmp_path / "safe.json"
    manifest_path.write_text("{}", encoding="utf-8")
    report_path.write_text(
        json.dumps({"phase0_run": {"completed": True}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not a completed server-local run"):
        build_phase0_safe_metrics(
            manifest_path=manifest_path,
            ask_report_path=report_path,
            output_path=output_path,
        )

    assert not output_path.exists()
