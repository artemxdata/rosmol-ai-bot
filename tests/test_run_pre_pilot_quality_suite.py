from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from eval import run_pre_pilot_quality_suite as suite


@pytest.mark.asyncio
async def test_run_pre_pilot_quality_suite_writes_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    custom_followup_cases = tmp_path / "dialog_memory_regression.json"
    custom_followup_cases.write_text("[]", encoding="utf-8")
    captured_followup_path: Path | None = None

    async def fake_run_ask_eval(**kwargs):
        kwargs["output_path"].write_text("{}", encoding="utf-8")
        kwargs["markdown_path"].write_text("# ask\n", encoding="utf-8")
        return {
            "cases_total": 2,
            "pass_rate": 1.0,
            "http_success_rate": 1.0,
            "behavior_match_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "expected_or_equivalent_chunk_hit_rate": 1.0,
            "llm_estimated_cost_rub": 0.5,
            "generator_model_counts": {"source_chunk": 1, "GigaChat/GigaChat-2-Max": 1},
            "failure_reason_counts": {},
            "eval_run_id": "ask-eval-test",
            "results": [],
        }

    async def fake_followup_eval(**kwargs):
        nonlocal captured_followup_path
        captured_followup_path = kwargs["cases_path"]
        kwargs["output_path"].write_text("{}", encoding="utf-8")
        kwargs["markdown_path"].write_text("# followup\n", encoding="utf-8")
        return {
            "turns_total": 2,
            "conversations_total": 1,
            "conversations_executed": 1,
            "turn_pass_rate": 1.0,
            "conversation_pass_rate": 1.0,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "expected_or_equivalent_chunk_hit_rate": 1.0,
            "llm_estimated_cost_rub": 0.25,
            "generator_model_counts": {"source_chunk": 2},
            "failure_reason_counts": {},
            "eval_run_id": "followup-eval-test",
            "results": [],
        }

    def fake_release_provenance(**kwargs):
        return {
            "release_run_id": kwargs["release_run_id"],
            "target": kwargs["target"],
            "git_sha": "a" * 40,
            "expected_git_sha": kwargs.get("expected_git_sha"),
            "git_worktree_clean": True,
            "kb_seed": {"sha256": "b" * 64},
            "case_files": {
                name: {"sha256": "c" * 64} for name in kwargs["case_paths"]
            },
            "complete": True,
            "errors": [],
        }

    monkeypatch.setattr(suite, "run_ask_eval", fake_run_ask_eval)
    monkeypatch.setattr(suite, "run_followup_eval", fake_followup_eval)
    monkeypatch.setattr(suite, "build_release_provenance", fake_release_provenance)

    summary = await suite.run_pre_pilot_quality_suite(
        output_dir=tmp_path / "out",
        cases_dir=tmp_path / "cases",
        kb_seed_path=Path("data/knowledge_base_seed.json"),
        sections=("forums", "pii", "followup"),
        followup_cases_path=custom_followup_cases,
        max_llm_cost_rub=10.0,
        expected_git_sha="a" * 40,
    )

    assert summary["passed"] is True
    assert summary["completed_sections"] == ["forums", "pii", "followup"]
    assert summary["llm_estimated_cost_rub"] == 1.25
    assert summary["sections_complete"] is True
    assert summary["trace_required"] is True
    assert summary["release_run_id"].startswith("pre-pilot-")
    assert summary["expected_git_sha"] == "a" * 40
    assert summary["provenance"]["complete"] is True
    assert len(summary["provenance"]["git_sha"]) == 40
    assert set(summary["provenance"]["case_files"]) == {"forums", "pii", "followup"}
    assert captured_followup_path == custom_followup_cases
    assert (tmp_path / "out" / "summary.md").exists()
    stored = json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    assert stored["sections"]["followup"]["turn_pass_rate"] == 1.0


@pytest.mark.asyncio
async def test_run_pre_pilot_quality_suite_stops_on_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_run_ask_eval(**kwargs):
        return {
            "cases_total": 2,
            "pass_rate": 1.0,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "llm_estimated_cost_rub": 2.0,
            "results": [],
        }

    monkeypatch.setattr(suite, "run_ask_eval", fake_run_ask_eval)

    summary = await suite.run_pre_pilot_quality_suite(
        output_dir=tmp_path / "out",
        cases_dir=tmp_path / "cases",
        kb_seed_path=Path("data/knowledge_base_seed.json"),
        sections=("forums", "pii"),
        max_llm_cost_rub=1.0,
    )

    assert summary["passed"] is False
    assert summary["completed_sections"] == ["forums"]
    assert summary["llm_budget_stopped"] is True


def test_followup_section_requires_conversation_pass_rate() -> None:
    assert suite._section_passed(
        "followup",
        {
            "turns_total": 2,
            "conversations_total": 1,
            "conversations_executed": 1,
            "turn_pass_rate": 0.9375,
            "conversation_pass_rate": 0.75,
            "trace_coverage_rate": 1.0,
        },
    ) is False
    assert suite._section_passed(
        "followup",
        {
            "turns_total": 2,
            "conversations_total": 1,
            "conversations_executed": 1,
            "turn_pass_rate": 0.9,
            "conversation_pass_rate": 0.9,
            "trace_coverage_rate": 1.0,
        },
    ) is True


def test_release_sections_fail_closed_for_empty_safety_and_missing_trace() -> None:
    with pytest.raises(ValueError, match="At least one"):
        suite._validate_sections(())

    assert suite._section_passed(
        "forums",
        {"cases_total": 0, "pass_rate": 1.0, "trace_coverage_rate": 1.0},
    ) is False
    assert suite._section_passed(
        "safety",
        {"cases_total": 16, "pass_rate": 0.99, "trace_coverage_rate": 1.0},
    ) is False
    assert suite._section_passed(
        "forums",
        {"cases_total": 16, "pass_rate": 1.0, "trace_coverage_rate": 0.99},
    ) is False


def test_release_summary_requires_every_requested_section() -> None:
    summary = suite._build_summary(
        output_dir=Path("reports/test"),
        cases_dir=Path("eval/cases"),
        target="http://localhost:8001/ask",
        sections=("forums", "safety"),
        section_reports={
            "forums": {
                "cases_total": 1,
                "pass_rate": 1.0,
                "trace_coverage_rate": 1.0,
            }
        },
        max_llm_cost_rub=1.0,
        stopped_by_budget=False,
        release_run_id="release-test",
        trace_required=True,
        provenance={"complete": True},
    )

    assert summary["passed"] is False
    assert summary["sections_complete"] is False
    assert summary["completed_sections"] == ["forums"]


@pytest.mark.asyncio
async def test_followup_turn_sends_eval_trace_headers() -> None:
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        return httpx.Response(
            200,
            json={"request_id": "request-1", "response": "Тестовый ответ"},
        )

    case = suite._normalize_case(
        {
            "id": "followup-turn-1",
            "query": "Когда начинается мероприятие?",
            "user_id": "eval-user",
            "channel": "api",
            "expected_behavior": "answer",
            "expected_escalated": False,
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await suite._run_followup_turn(
            client=client,
            target="http://test/ask",
            headers={"X-Eval-Run-Id": "followup-eval-test"},
            case=case,
            trace_pool=None,
            conversation_id="conversation-1",
        )

    assert captured_headers["x-eval-run-id"] == "followup-eval-test"
    assert captured_headers["x-eval-case-id"] == "followup-turn-1"
    assert result["conversation_id"] == "conversation-1"
