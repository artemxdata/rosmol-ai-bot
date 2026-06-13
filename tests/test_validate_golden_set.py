from __future__ import annotations

import json
from pathlib import Path

from eval.validate_golden_set import (
    GoldenValidationConfig,
    validate_golden_set,
    write_markdown,
    write_report,
)


def test_validate_golden_set_accepts_well_formed_cases() -> None:
    report = validate_golden_set(
        [
            {
                "id": "mashuk_travel",
                "question": "Кто оплачивает проезд на Машук?",
                "expected_chunks": ["chunk_1"],
                "expected_forum": "Машук",
                "category": "форумы",
                "difficulty": "simple",
                "source": "manual",
                "reference_answer": "Проезд оплачивает участник.",
            }
        ],
        kb_records=[{"chunk_id": "chunk_1"}],
        config=GoldenValidationConfig(min_cases=1),
    )

    assert report["valid"] is True
    assert report["errors_total"] == 0
    assert report["warnings_total"] == 0
    assert report["category_counts"] == {"форумы": 1}
    assert report["forum_counts_top"] == {"Машук": 1}
    assert report["difficulty_counts"] == {"simple": 1}


def test_validate_golden_set_reports_blocking_errors_and_warnings() -> None:
    report = validate_golden_set(
        [
            {
                "id": "duplicate",
                "question": "Есть ли регистрация?",
                "expected_chunks": ["missing"],
                "difficulty": "hard",
            },
            {
                "id": "duplicate",
                "query": "",
                "expected_chunks": [],
            },
        ],
        kb_records=[{"chunk_id": "known"}],
        config=GoldenValidationConfig(min_cases=3),
    )

    assert report["valid"] is False
    error_codes = {item["code"] for item in report["errors"]}
    warning_codes = {item["code"] for item in report["warnings"]}
    assert "min_cases" in error_codes
    assert "unknown_expected_chunks" in error_codes
    assert "duplicate_id" in error_codes
    assert "missing_query" in error_codes
    assert "missing_expected_chunks" in error_codes
    assert "unknown_difficulty" in warning_codes
    assert "missing_reference_answer" in warning_codes


def test_validate_golden_set_writes_json_and_markdown(tmp_path: Path) -> None:
    report = validate_golden_set(
        [
            {
                "id": "case_1",
                "query": "Где найти ID?",
                "expected_chunk_ids": ["chunk_1"],
                "reference_answer": "ID доступен в профиле.",
            }
        ],
        kb_records=[{"chunk_id": "chunk_1"}],
        config=GoldenValidationConfig(min_cases=1),
    )
    output = tmp_path / "golden_validation.json"
    markdown = tmp_path / "golden_validation.md"

    write_report(output, report)
    write_markdown(markdown, report)

    assert json.loads(output.read_text(encoding="utf-8"))["valid"] is True
    assert "Golden Set Validation" in markdown.read_text(encoding="utf-8")
