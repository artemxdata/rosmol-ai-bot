from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.audit_kb_seed import audit_kb_seed
from src.kb.audit import audit_seed_records


def test_audit_seed_records_detects_errors_and_warnings() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "bad_quote",
                "text_clean": "Ответ'",
                "category": "форумы",
                "topic": "topic",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            },
            {
                "chunk_id": "template",
                "text_clean": "{{ ['Всегда рад!']|random}}",
                "category": "навигация",
                "topic": "thanks",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            },
            {
                "chunk_id": "missing",
                "text_clean": "Текст",
                "category": "гранты",
                "forum_normalized": "Гранты для физических лиц",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            },
            {
                "chunk_id": "duplicate_1",
                "text_clean": "Одинаковый текст",
                "category": "общее",
                "topic": "same",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            },
            {
                "chunk_id": "duplicate_2",
                "text_clean": "Одинаковый  текст",
                "category": "общее",
                "topic": "same",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            },
        ]
    )

    codes = {finding["code"] for finding in report["findings"]}
    assert report["errors"] == 2
    assert report["warnings"] == 3
    assert {
        "trailing_export_quote",
        "template_artifact",
        "missing_topic",
        "grant_record_has_forum",
        "duplicate_text",
    } <= codes


def test_audit_seed_records_includes_quality_summary() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "forum",
                "text_clean": "Forum answer",
                "category": "forums",
                "forum_normalized": "Forum A",
                "topic": "travel",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
                "status": "published",
                "char_count": 12,
            },
            {
                "chunk_id": "generic",
                "text_clean": "Generic answer text",
                "category": "general",
                "topic": "fallback",
                "source_type": "docx",
                "source_file": "source.docx",
                "status": "draft",
            },
        ]
    )

    summary = report["summary"]
    assert summary["category_counts"] == {"forums": 1, "general": 1}
    assert summary["status_counts"] == {"published": 1, "draft": 1}
    assert summary["source_type_counts"] == {"xlsx": 1, "docx": 1}
    assert summary["forum_counts_top"] == {"Forum A": 1}
    assert summary["forums_total"] == 1
    assert summary["generic_records_count"] == 1
    assert summary["char_count"]["min"] == 12
    assert summary["char_count"]["max"] == len("Generic answer text")


def test_audit_kb_seed_can_fail_on_errors(tmp_path: Path) -> None:
    path = tmp_path / "knowledge_base_seed.json"
    path.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "bad",
                    "text_clean": "Ответ'",
                    "category": "общее",
                    "topic": "topic",
                    "source_type": "xlsx",
                    "source_file": "source.xlsx",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        audit_kb_seed(path, None, "error")


def test_audit_kb_seed_writes_report(tmp_path: Path) -> None:
    path = tmp_path / "knowledge_base_seed.json"
    output = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "ok",
                    "text_clean": "Ответ",
                    "category": "общее",
                    "topic": "topic",
                    "source_type": "xlsx",
                    "source_file": "source.xlsx",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    audit_kb_seed(path, output, None)

    assert json.loads(output.read_text(encoding="utf-8"))["records_total"] == 1
