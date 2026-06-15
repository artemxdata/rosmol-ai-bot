from __future__ import annotations

import json
from pathlib import Path

from eval.summarize_forum_ask import summarize_forum_ask


def test_summarize_forum_ask_groups_and_flags_foreign_chunks(tmp_path: Path) -> None:
    metrics = tmp_path / "ask.json"
    kb_seed = tmp_path / "kb.json"
    output = tmp_path / "summary.json"
    markdown = tmp_path / "summary.md"
    metrics.write_text(
        json.dumps(
            {
                "cases_total": 2,
                "pass_rate": 0.5,
                "expected_chunk_hit_rate": 1.0,
                "http_success_rate": 1.0,
                "escalation_rate": 0.0,
                "llm_estimated_cost_rub": 1.23,
                "results": [
                    {
                        "id": "mashuk",
                        "tags": ["forum:Машук"],
                        "passed": True,
                        "expected_chunk_hit": True,
                        "http_success": True,
                        "was_escalated": False,
                        "latency_ms": 100,
                        "trace_total_latency_ms": 90,
                        "max_reranker_score": 0.9,
                        "observed_chunk_ids": ["mashuk_docs"],
                    },
                    {
                        "id": "utro",
                        "tags": ["forum:Утро"],
                        "passed": False,
                        "expected_chunk_hit": True,
                        "http_success": True,
                        "was_escalated": False,
                        "latency_ms": 200,
                        "trace_total_latency_ms": 180,
                        "max_reranker_score": 0.4,
                        "observed_chunk_ids": ["utro_docs", "mashuk_docs"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    kb_seed.write_text(
        json.dumps(
            [
                {"chunk_id": "mashuk_docs", "forum_normalized": "Машук"},
                {"chunk_id": "utro_docs", "forum_normalized": "Утро"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = summarize_forum_ask(metrics, kb_seed, output, markdown)

    assert summary["forums_total"] == 2
    utro = next(row for row in summary["forums"] if row["forum"] == "Утро")
    assert utro["pass_rate"] == 0.0
    assert utro["foreign_observed_forums"] == {"Машук": 1}
    assert "Утро" in markdown.read_text(encoding="utf-8")
    assert json.loads(output.read_text(encoding="utf-8"))["problem_forums"][0]["forum"] == "Утро"
