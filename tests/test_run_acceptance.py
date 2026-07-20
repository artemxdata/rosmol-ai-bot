from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import run_acceptance


def _clean_provenance(**kwargs) -> dict[str, object]:
    case_paths = kwargs.get("case_paths") or {}
    return {
        "release_run_id": kwargs["release_run_id"],
        "target": kwargs["target"],
        "git_sha": "a" * 40,
        "expected_git_sha": kwargs.get("expected_git_sha"),
        "git_worktree_clean": True,
        "kb_seed": {"sha256": "b" * 64},
        "case_files": {
            name: {"sha256": "c" * 64} for name in case_paths
        },
        "complete": True,
        "errors": [],
    }


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
            release_run_id = command[command.index("--release-run-id") + 1]
            target = command[command.index("--target") + 1]
            kb_seed_path = Path(command[command.index("--kb-seed") + 1])
            expected_git_sha = command[command.index("--expected-git-sha") + 1]
            provenance = run_acceptance.build_release_provenance(
                release_run_id=release_run_id,
                target=target,
                kb_seed_path=kb_seed_path,
                case_paths={"forums": Path("eval/cases/pre_pilot_forums.json")},
                expected_git_sha=expected_git_sha,
            )
            quality_dir.mkdir(parents=True, exist_ok=True)
            (quality_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "release_run_id": release_run_id,
                        "expected_git_sha": expected_git_sha,
                        "target": target,
                        "provenance": provenance,
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
        expected_git_sha: str,
    ) -> run_acceptance.StepResult:
        calls.append(("readiness", timeout_sec, expected_git_sha))
        return run_acceptance.StepResult(
            name="readiness",
            ok=True,
            elapsed_sec=0.01,
            stdout_tail=json.dumps({"urls": list(urls)}),
        )

    monkeypatch.setattr(run_acceptance, "_run_command", fake_run_command)
    monkeypatch.setattr(run_acceptance, "_check_ready", fake_check_ready)
    monkeypatch.setattr(run_acceptance, "build_release_provenance", _clean_provenance)
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
            "--expected-git-sha",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
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
    assert compact["expected_git_sha"] == "a" * 40
    assert compact["expected_git_sha_valid"] is True
    assert compact["quality_provenance_match"] is True
    assert compact["skipped_steps"] == []
    assert compact["report"] == str(output_dir / "summary.md").replace("\\", "/")
    assert calls == [
        ("ruff", 11),
        ("pytest", 22),
        ("kb_validation", 33),
        ("readiness", 44, "a" * 40),
        ("pre_pilot_quality_suite", 55),
    ]
    stored = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert stored["passed"] is True
    assert stored["release_run_id"] == compact["release_run_id"]
    assert stored["provenance"]["kb_seed"]["sha256"]
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
    kb_step = next(step for step in report["steps"] if step["name"] == "kb_validation")
    assert kb_step["ok"] is False
    assert report["skipped_steps"] == [
        "ruff",
        "pytest",
        "readiness",
        "pre_pilot_quality_suite",
    ]


def test_run_acceptance_skips_are_recorded_and_cannot_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "final_acceptance"

    monkeypatch.setattr(
        run_acceptance,
        "_run_command",
        lambda name, command, *, timeout_sec: run_acceptance.StepResult(
            name=name,
            ok=True,
            elapsed_sec=0.01,
            command=command,
        ),
    )
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
    assert report["skipped_steps"] == [
        "ruff",
        "pytest",
        "readiness",
        "pre_pilot_quality_suite",
    ]
    assert all(
        step["skipped"] is True
        for step in report["steps"]
        if step["name"] in report["skipped_steps"]
    )


def test_run_acceptance_cannot_pass_without_expected_git_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "final_acceptance"
    quality_dir = tmp_path / "quality"

    def fake_run_command(
        name: str,
        command: list[str],
        *,
        timeout_sec: int,
    ) -> run_acceptance.StepResult:
        if name == "pre_pilot_quality_suite":
            release_run_id = command[command.index("--release-run-id") + 1]
            target = command[command.index("--target") + 1]
            quality_dir.mkdir(parents=True, exist_ok=True)
            (quality_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "release_run_id": release_run_id,
                        "expected_git_sha": None,
                        "target": target,
                        "provenance": _clean_provenance(
                            release_run_id=release_run_id,
                            target=target,
                            kb_seed_path=Path("data/knowledge_base_seed.json"),
                            case_paths={
                                "forums": Path("eval/cases/pre_pilot_forums.json")
                            },
                        ),
                    }
                ),
                encoding="utf-8",
            )
        return run_acceptance.StepResult(name=name, ok=True, elapsed_sec=0.01, command=command)

    monkeypatch.setattr(run_acceptance, "_run_command", fake_run_command)
    monkeypatch.setattr(
        run_acceptance,
        "_check_ready",
        lambda urls, *, timeout_sec, expected_git_sha: run_acceptance.StepResult(
            name="readiness", ok=True, elapsed_sec=0.01
        ),
    )
    monkeypatch.setattr(run_acceptance, "build_release_provenance", _clean_provenance)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_acceptance.py",
            "--output-dir",
            str(output_dir),
            "--quality-output-dir",
            str(quality_dir),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        run_acceptance.main()

    assert exc_info.value.code == 1
    report = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["expected_git_sha"] is None
    assert report["expected_git_sha_valid"] is False
    assert "expected_git_sha_missing" in report["quality_provenance_errors"]


def test_quality_summary_from_another_release_run_is_rejected() -> None:
    current = _clean_provenance(
        release_run_id="current-run",
        target="http://localhost:8001/ask",
        kb_seed_path=Path("data/knowledge_base_seed.json"),
        expected_git_sha="a" * 40,
    )
    stale = _clean_provenance(
        release_run_id="stale-run",
        target="http://localhost:8001/ask",
        kb_seed_path=Path("data/knowledge_base_seed.json"),
        case_paths={"forums": Path("eval/cases/pre_pilot_forums.json")},
        expected_git_sha="a" * 40,
    )

    matched, errors = run_acceptance._quality_provenance_matches(
        {
            "release_run_id": "stale-run",
            "expected_git_sha": "a" * 40,
            "target": "http://localhost:8001/ask",
            "provenance": stale,
        },
        release_run_id="current-run",
        target="http://localhost:8001/ask",
        provenance=current,
        expected_git_sha="a" * 40,
    )

    assert matched is False
    assert errors == ["release_run_id_mismatch"]
