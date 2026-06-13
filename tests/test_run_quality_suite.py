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
