from __future__ import annotations

import json
from pathlib import Path

from scripts import build_presentation_readiness


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_build_readiness_report_marks_demo_ready(tmp_path: Path) -> None:
    acceptance = tmp_path / "final_acceptance" / "summary.json"
    quality = tmp_path / "presentation_quality" / "presentation_quality_report.json"
    smoke = tmp_path / "presentation_quality" / "pre_demo_smoke_latest" / "pre_demo_smoke.json"
    output_dir = tmp_path / "presentation_readiness"

    _write_json(
        acceptance,
        {
            "generated_at": "2026-07-04T00:00:00+00:00",
            "passed": True,
            "target": "http://localhost:8001/ask",
            "steps": [{"name": "pytest", "ok": True}],
            "quality_summary": {"passed": True},
        },
    )
    _write_json(
        quality,
        {
            "generated_at": "2026-07-04T00:00:00+00:00",
            "target": "http://localhost:8001/ask",
            "total_checks_or_turns": 178,
            "total_passed": 178,
            "total_pass_rate": 1.0,
            "total_llm_estimated_cost_rub": 12.5,
            "typical": {"pass_rate": 1.0},
            "atypical": {"pass_rate": 1.0},
            "safety": {"pass_rate": 1.0},
        },
    )
    _write_json(
        smoke,
        {
            "generated_at": "2026-07-04T00:00:00+00:00",
            "target": "http://localhost:8001/ask",
            "require_trace": True,
            "trace_error": None,
            "cases_total": 12,
            "passed": 12,
            "pass_rate": 1.0,
            "llm_estimated_cost_rub": 0.0,
            "failed": [],
        },
    )

    report = build_presentation_readiness.build_readiness_report(
        output_dir=output_dir,
        final_acceptance=acceptance,
        quality_report=quality,
        pre_demo_smoke=smoke,
    )

    assert report["ready_for_leadership_demo"] is True
    assert all(gate["ok"] for gate in report["gates"])
    assert "--expected-git-sha" in report["commands"]["full_acceptance"]
    assert (output_dir / "summary.json").exists()
    markdown = (output_dir / "summary.md").read_text(encoding="utf-8")
    assert "Готово к презентации" in markdown
    assert "178/178" in markdown
    assert "Yonote" in markdown


def test_build_readiness_report_marks_demo_not_ready_when_smoke_failed(tmp_path: Path) -> None:
    acceptance = tmp_path / "final_acceptance" / "summary.json"
    quality = tmp_path / "presentation_quality" / "presentation_quality_report.json"
    smoke = tmp_path / "presentation_quality" / "pre_demo_smoke_latest" / "pre_demo_smoke.json"

    _write_json(acceptance, {"passed": True, "steps": [], "quality_summary": {"passed": True}})
    _write_json(
        quality,
        {
            "total_checks_or_turns": 10,
            "total_passed": 10,
            "total_pass_rate": 1.0,
            "total_llm_estimated_cost_rub": 1.0,
        },
    )
    _write_json(
        smoke,
        {
            "require_trace": True,
            "trace_error": None,
            "cases_total": 12,
            "passed": 11,
            "pass_rate": 11 / 12,
            "failed": ["case"],
            "llm_estimated_cost_rub": 0.0,
        },
    )

    report = build_presentation_readiness.build_readiness_report(
        output_dir=tmp_path / "presentation_readiness",
        final_acceptance=acceptance,
        quality_report=quality,
        pre_demo_smoke=smoke,
    )

    assert report["ready_for_leadership_demo"] is False
    assert report["decision"] == "not_ready"
    assert any(
        gate["name"] == "pre_demo_smoke_100_percent" and not gate["ok"]
        for gate in report["gates"]
    )


def test_compact_report_contains_actionable_paths(tmp_path: Path) -> None:
    report = {
        "ready_for_leadership_demo": True,
        "decision": "ready",
        "gates": [{"name": "gate", "ok": True}],
        "metrics": {
            "presentation_quality": {"total_pass_rate": 1.0},
            "pre_demo_smoke": {"pass_rate": 1.0},
        },
    }

    compact = build_presentation_readiness._compact_report(report)

    assert compact["ready_for_leadership_demo"] is True
    assert compact["report"] == "reports/presentation_readiness/summary.md"
