from __future__ import annotations

import json
from pathlib import Path

from eval.run_generation import (
    load_generation_cases,
    run_generation_eval,
    score_generation_case,
    summarize_generation_results,
)


def test_score_generation_case_passes_grounded_answer() -> None:
    result = score_generation_case(
        {
            "id": "docs",
            "response": "Нужен паспорт и согласие. [src:docs_1]",
            "sources": [{"chunk_id": "docs_1", "text": "Нужен паспорт и согласие."}],
            "expected_chunk_ids": ["docs_1"],
            "expected_answer_contains": ["паспорт"],
            "verifier_result": {"has_hallucination": False},
        }
    )

    assert result["passed"] is True
    assert result["expected_chunk_hit"] is True
    assert result["unknown_source_markers"] == []
    assert result["checks"]["verifier_passed"] is True


def test_score_generation_case_fails_unknown_source_and_forbidden_text() -> None:
    result = score_generation_case(
        {
            "id": "bad",
            "response": "Ответ из головы, точно приезжайте завтра. [src:unknown]",
            "sources": [{"chunk_id": "docs_1", "text": "Нужен паспорт."}],
            "expected_chunk_ids": ["docs_1"],
            "forbidden_phrases": ["точно приезжайте завтра"],
            "verifier_result": {"has_hallucination": True},
        }
    )

    assert result["passed"] is False
    assert result["checks"]["unknown_source_markers"] is False
    assert result["checks"]["forbidden_text_absent"] is False
    assert result["checks"]["verifier_passed"] is False


def test_score_generation_case_uses_observed_chunk_ids_from_ask_eval() -> None:
    result = score_generation_case(
        {
            "id": "ask-eval-case",
            "response": "Ответ без src-маркера, но trace содержит источник.",
            "observed_chunk_ids": ["chunk_1"],
            "expected_chunk_ids": ["chunk_1"],
            "was_escalated": False,
        }
    )

    assert result["source_context_present"] is True
    assert result["source_context_ids"] == ["chunk_1"]
    assert result["expected_chunk_hit"] is True
    assert result["checks"]["source_context_present"] is True


def test_load_generation_cases_accepts_ask_eval_results(tmp_path: Path) -> None:
    path = tmp_path / "ask_eval.json"
    path.write_text(
        json.dumps({"results": [{"id": "case_1", "response": "Ответ"}]}),
        encoding="utf-8",
    )

    assert load_generation_cases(path) == [{"id": "case_1", "response": "Ответ"}]


def test_run_generation_eval_writes_safe_report(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    output = tmp_path / "generation.json"
    markdown = tmp_path / "generation.md"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "ok",
                    "response": "Ответ [src:chunk_1]",
                    "sources": [{"chunk_id": "chunk_1", "text": "Ответ"}],
                    "expected_chunk_ids": ["chunk_1"],
                    "expected_escalated": False,
                    "was_escalated": False,
                },
                {
                    "id": "miss",
                    "response": "Ответ без источника",
                    "expected_chunk_ids": ["chunk_2"],
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = run_generation_eval(cases, output, markdown)

    assert report["cases_total"] == 2
    assert report["pass_rate"] == 0.5
    assert report["expected_chunk_hit_rate"] == 0.5
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["results"][0]["id"] == "ok"
    assert "Ответ без источника" not in output.read_text(encoding="utf-8")
    assert "Generation Eval Report" in markdown.read_text(encoding="utf-8")


def test_summarize_generation_results_excludes_escalations_from_source_context_rate() -> None:
    report = summarize_generation_results(
        [
            {
                "id": "answered",
                "passed": True,
                "source_context_present": True,
                "expected_chunk_ids": [],
                "expected_chunk_hit": None,
                "expected_escalated": False,
                "was_escalated": False,
                "verifier_hallucination": None,
                "checks": {},
            },
            {
                "id": "escalated",
                "passed": True,
                "source_context_present": False,
                "expected_chunk_ids": [],
                "expected_chunk_hit": None,
                "expected_escalated": True,
                "was_escalated": True,
                "verifier_hallucination": None,
                "checks": {},
            },
        ],
        Path("cases.json"),
    )

    assert report["source_context_rate"] == 1.0
