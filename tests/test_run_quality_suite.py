from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from eval import run_quality_suite as suite


@pytest.mark.asyncio
async def test_run_quality_suite_writes_all_reports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_retrieval_eval(*args, **kwargs):
        await asyncio.to_thread(
            Path(kwargs["markdown_path"]).write_text,
            "# retrieval\n",
            encoding="utf-8",
        )
        await asyncio.to_thread(Path(args[1]).write_text, "{}", encoding="utf-8")
        return {
            "backend": kwargs["backend"],
            "cases_total": 2,
            "cases_scored": 2,
            "recall_at_5": 1.0,
            "recall_at_k": 1.0,
            "generated_smoke_cases": True,
        }

    async def fake_ask_eval(**kwargs):
        await asyncio.to_thread(
            kwargs["markdown_path"].write_text,
            "# ask\n",
            encoding="utf-8",
        )
        await asyncio.to_thread(kwargs["output_path"].write_text, "{}", encoding="utf-8")
        return {
            "cases_total": 2,
            "pass_rate": 1.0,
            "expected_chunk_hit_rate": 1.0,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "low_confidence_expected_chunk_hit_rate": 0.0,
            "llm_estimated_cost_rub": 0.01,
            "results": [
                {
                    "expected_chunk_ids": ["chunk_1"],
                    "expected_chunk_hit": True,
                    "max_reranker_score": 0.8,
                }
            ],
        }

    monkeypatch.setattr(suite, "run_retrieval_eval", fake_retrieval_eval)
    monkeypatch.setattr(suite, "run_ask_eval", fake_ask_eval)

    summary = await suite.run_quality_suite(
        output_dir=tmp_path,
        golden_path=Path("golden.json"),
        ask_cases_path=Path("ask.json"),
        kb_seed_path=Path("kb.json"),
        retrieval_backend="lexical",
        auto_smoke_cases=True,
        max_smoke_cases=2,
    )

    assert summary["passed"] is True
    assert summary["retrieval"]["backend"] == "lexical"
    assert summary["ask"]["llm_estimated_cost_rub"] == 0.01
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["passed"] is True
    assert (tmp_path / "retrieval_eval.md").exists()
    assert (tmp_path / "ask_eval.md").exists()
    assert (tmp_path / "rag_threshold_suggestions.json").exists()
    assert (tmp_path / "quality_gate.json").exists()
    assert "Quality Suite Summary" in (tmp_path / "summary.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_quality_suite_can_include_forum_smoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_retrieval_eval(*args, **kwargs):
        await asyncio.to_thread(Path(args[1]).write_text, "{}", encoding="utf-8")
        await asyncio.to_thread(
            kwargs["markdown_path"].write_text,
            "# retrieval\n",
            encoding="utf-8",
        )
        return {
            "backend": kwargs["backend"],
            "cases_total": 1,
            "cases_scored": 1,
            "recall_at_5": 1.0,
            "recall_at_k": 1.0,
        }

    ask_calls: list[Path] = []

    async def fake_ask_eval(**kwargs):
        ask_calls.append(kwargs["output_path"])
        await asyncio.to_thread(kwargs["output_path"].write_text, "{}", encoding="utf-8")
        await asyncio.to_thread(
            kwargs["markdown_path"].write_text,
            "# ask\n",
            encoding="utf-8",
        )
        return {
            "cases_total": 1,
            "pass_rate": 1.0,
            "expected_chunk_hit_rate": 1.0,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "low_confidence_expected_chunk_hit_rate": 0.0,
            "llm_estimated_cost_rub": 0.0,
            "results": [],
        }

    def fake_build_forum_smoke_set(
        kb_seed_path,
        output_path,
        *,
        per_forum,
        user_prefix="forum-smoke",
    ):
        output_path.write_text("[]", encoding="utf-8")
        return {"cases_total": 1, "forums_total": 1}

    def fake_summarize_forum_ask(ask_metrics_path, kb_seed_path, output_path, markdown_path=None):
        summary = {
            "cases_total": 1,
            "forums_total": 1,
            "pass_rate": 1.0,
            "expected_chunk_hit_rate": 1.0,
            "escalation_rate": 0.0,
            "problem_forums": [],
            "forums": [],
        }
        output_path.write_text(json.dumps(summary), encoding="utf-8")
        if markdown_path:
            markdown_path.write_text("# forum\n", encoding="utf-8")
        return summary

    monkeypatch.setattr(suite, "run_retrieval_eval", fake_retrieval_eval)
    monkeypatch.setattr(suite, "run_ask_eval", fake_ask_eval)
    monkeypatch.setattr(suite, "build_forum_smoke_set", fake_build_forum_smoke_set)
    monkeypatch.setattr(suite, "summarize_forum_ask", fake_summarize_forum_ask)

    summary = await suite.run_quality_suite(
        output_dir=tmp_path,
        golden_path=Path("golden.json"),
        ask_cases_path=Path("ask.json"),
        kb_seed_path=Path("kb.json"),
        retrieval_backend="lexical",
        auto_smoke_cases=True,
        max_smoke_cases=1,
        forum_smoke=True,
    )

    assert summary["passed"] is True
    assert summary["forum_smoke"]["pass_rate"] == 1.0
    assert ask_calls == [tmp_path / "ask_eval.json", tmp_path / "forum_ask_eval.json"]
    assert (tmp_path / "forum_smoke_set.json").exists()
    assert (tmp_path / "forum_ask_summary.json").exists()
    assert "Forum smoke" in (tmp_path / "summary.md").read_text(encoding="utf-8")
