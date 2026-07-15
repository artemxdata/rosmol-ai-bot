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
            "results": [],
        }

    async def fake_followup_eval(**kwargs):
        nonlocal captured_followup_path
        captured_followup_path = kwargs["cases_path"]
        kwargs["output_path"].write_text("{}", encoding="utf-8")
        kwargs["markdown_path"].write_text("# followup\n", encoding="utf-8")
        return {
            "turns_total": 2,
            "turn_pass_rate": 1.0,
            "conversation_pass_rate": 1.0,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "expected_or_equivalent_chunk_hit_rate": 1.0,
            "llm_estimated_cost_rub": 0.25,
            "generator_model_counts": {"source_chunk": 2},
            "failure_reason_counts": {},
            "results": [],
        }

    monkeypatch.setattr(suite, "run_ask_eval", fake_run_ask_eval)
    monkeypatch.setattr(suite, "run_followup_eval", fake_followup_eval)

    summary = await suite.run_pre_pilot_quality_suite(
        output_dir=tmp_path / "out",
        cases_dir=tmp_path / "cases",
        kb_seed_path=Path("data/knowledge_base_seed.json"),
        sections=("forums", "pii", "followup"),
        followup_cases_path=custom_followup_cases,
        max_llm_cost_rub=10.0,
    )

    assert summary["passed"] is True
    assert summary["completed_sections"] == ["forums", "pii", "followup"]
    assert summary["llm_estimated_cost_rub"] == 1.25
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
        {"turn_pass_rate": 0.9375, "conversation_pass_rate": 0.75}
    ) is False
    assert suite._section_passed(
        {"turn_pass_rate": 0.9, "conversation_pass_rate": 0.9}
    ) is True


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
