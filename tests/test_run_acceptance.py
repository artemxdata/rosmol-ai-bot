from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import run_acceptance


def test_run_acceptance_main_writes_custom_report_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = tmp_path / "final_acceptance"
    quality_dir = tmp_path / "quality"
    calls: list[tuple[str, int]] = []

    def fake_run_command(
        name: str,
        command: list[str],
        *,
        timeout_sec: int,
    ) -> run_acceptance.StepResult:
        calls.append((name, timeout_sec))
        if name == "pre_pilot_quality_suite":
            quality_dir.mkdir(parents=True, exist_ok=True)
            (quality_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "llm_estimated_cost_rub": 0.0,
                        "completed_sections": ["forums"],
                        "sections": {
                            "forums": {
                                "cases_total": 1,
                                "pass_rate": 1.0,
                                "trace_coverage_rate": 1.0,
                                "expected_or_equivalent_chunk_hit_rate": 1.0,
                                "llm_estimated_cost_rub": 0.0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
        return run_acceptance.StepResult(
            name=name,
            ok=True,
            elapsed_sec=0.01,
            command=command,
            stdout_tail="ok",
        )

    def fake_check_ready(
        urls: tuple[str, ...],
        *,
        timeout_sec: int = run_acceptance.DEFAULT_READY_TIMEOUT_SEC,
    ) -> run_acceptance.StepResult:
        calls.append(("readiness", timeout_sec))
        return run_acceptance.StepResult(
            name="readiness",
            ok=True,
            elapsed_sec=0.01,
            stdout_tail=json.dumps({"urls": list(urls)}),
        )

    monkeypatch.setattr(run_acceptance, "_run_command", fake_run_command)
    monkeypatch.setattr(run_acceptance, "_check_ready", fake_check_ready)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_acceptance.py",
            "--output-dir",
            str(output_dir),
            "--quality-output-dir",
            str(quality_dir),
            "--target",
            "http://localhost:8001/ask",
            "--ruff-timeout-sec",
            "11",
            "--pytest-timeout-sec",
            "22",
            "--kb-timeout-sec",
            "33",
            "--ready-timeout-sec",
            "44",
            "--quality-timeout-sec",
            "55",
        ],
    )

    run_acceptance.main()

    compact = json.loads(capsys.readouterr().out)
    assert compact["passed"] is True
    assert compact["report"] == str(output_dir / "summary.md").replace("\\", "/")
    assert calls == [
        ("ruff", 11),
        ("pytest", 22),
        ("kb_validation", 33),
        ("readiness", 44),
        ("pre_pilot_quality_suite", 55),
    ]
    assert json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))["passed"] is True
    assert "Final Acceptance Report" in (output_dir / "summary.md").read_text(encoding="utf-8")


def test_run_acceptance_main_exits_when_required_step_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "final_acceptance"

    def fake_run_command(
        name: str,
        command: list[str],
        *,
        timeout_sec: int,
    ) -> run_acceptance.StepResult:
        return run_acceptance.StepResult(
            name=name,
            ok=False,
            elapsed_sec=0.01,
            command=command,
            error="exit_code=1",
        )

    monkeypatch.setattr(run_acceptance, "_run_command", fake_run_command)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_acceptance.py",
            "--output-dir",
            str(output_dir),
            "--skip-ruff",
            "--skip-pytest",
            "--skip-ready",
            "--skip-quality",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        run_acceptance.main()

    assert exc_info.value.code == 1
    report = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["steps"][0]["name"] == "kb_validation"
    assert report["steps"][0]["ok"] is False
