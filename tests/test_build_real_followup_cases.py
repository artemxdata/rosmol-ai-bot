from __future__ import annotations

import json
from pathlib import Path

from scripts.build_real_followup_cases import build_real_followup_cases


def test_build_real_followup_cases_creates_safe_two_turn_dialog(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    evaluation = tmp_path / "evaluation.json"
    output = tmp_path / "followup.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "ticket",
                    "query": "Где мой билет?",
                    "reference_answer": "Для Дня молодёжи билет доступен в MAX.",
                    "expected_behavior": "answer",
                    "category": "навигация",
                    "department": "MAX Бот",
                    "golden_exclusion_reasons": ["unsupported_category"],
                },
                {
                    "id": "personal",
                    "query": "Где моя заявка [ID]?",
                    "reference_answer": "Статус заявки проверит оператор.",
                    "expected_behavior": "answer",
                    "golden_exclusion_reasons": ["personal_status_question"],
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    evaluation.write_text(
        json.dumps(
            {
                "results": [
                    {"id": "ticket", "observed_behavior": "clarify"},
                    {"id": "personal", "observed_behavior": "clarify"},
                ]
            }
        ),
        encoding="utf-8",
    )

    summary = build_real_followup_cases(cases, evaluation, output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert summary["conversations_created"] == 1
    assert payload[0]["turns"][0]["expected_behavior"] == "clarify"
    assert payload[0]["turns"][1]["query"] == "День молодёжи"
    assert payload[0]["turns"][1]["expected_behavior"] == "answer"


def test_build_real_followup_cases_requires_unique_reference_forum(
    tmp_path: Path,
) -> None:
    cases = tmp_path / "cases.json"
    evaluation = tmp_path / "evaluation.json"
    output = tmp_path / "followup.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "ambiguous",
                    "query": "Где билет?",
                    "reference_answer": "Это может быть Амур или Машук.",
                    "expected_behavior": "answer",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    evaluation.write_text(
        json.dumps(
            {"results": [{"id": "ambiguous", "observed_behavior": "clarify"}]}
        ),
        encoding="utf-8",
    )

    summary = build_real_followup_cases(cases, evaluation, output)

    assert summary["conversations_created"] == 0
    assert summary["skipped"]["reference_forum_not_unique"] == 1
