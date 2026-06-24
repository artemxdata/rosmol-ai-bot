from __future__ import annotations

import json
from pathlib import Path

from eval.audit_expected_chunks import (
    audit_expected_chunks,
    filter_cases_passing_audit,
    write_markdown,
)


def test_audit_expected_chunks_accepts_exact_intent_label(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    kb = tmp_path / "kb.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "case-1",
                    "query": "Can I upload a grant report after correction?",
                    "expected_chunk_ids": ["chunk_exact"],
                }
            ]
        ),
        encoding="utf-8",
    )
    kb.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "chunk_exact",
                    "status": "published",
                    "text_clean": "Use the personal account for the exact report case.",
                    "intent_examples": ["Can I upload a grant report after correction?"],
                }
            ]
        ),
        encoding="utf-8",
    )

    report = audit_expected_chunks(cases, kb)

    assert report["pass_rate"] == 1.0
    assert report["reason_counts"] == {"ok": 1}
    assert report["rows"][0]["rank"] == 1


def test_audit_expected_chunks_reports_missing_and_weak_labels(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    kb = tmp_path / "kb.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "missing",
                    "query": "Where is the report section?",
                    "expected_chunk_ids": ["missing_chunk"],
                },
                {
                    "id": "weak",
                    "query": "Where is the report section?",
                    "expected_chunk_ids": ["unrelated"],
                },
            ]
        ),
        encoding="utf-8",
    )
    kb.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "unrelated",
                    "status": "published",
                    "text_clean": "Travel and hotel logistics.",
                    "intent_examples": ["Is transfer available?"],
                }
            ]
        ),
        encoding="utf-8",
    )

    report = audit_expected_chunks(cases, kb)

    assert report["passed"] == 0
    assert report["reason_counts"] == {
        "missing_expected_chunk_in_kb": 1,
        "expected_chunk_has_no_query_overlap": 1,
    }


def test_write_markdown_does_not_include_raw_query_text(tmp_path: Path) -> None:
    output = tmp_path / "audit.md"
    write_markdown(
        {
            "cases_path": "cases.json",
            "kb_seed_path": "kb.json",
            "checked_expected_chunks": 1,
            "passed": 0,
            "pass_rate": 0.0,
            "reason_counts": {"expected_chunk_rank_too_low": 1},
            "rows": [
                {
                    "case_id": "case-1",
                    "expected_id": "chunk_a",
                    "ok": False,
                    "reason": "expected_chunk_rank_too_low",
                    "rank": 99,
                    "score": 0.1,
                    "top_chunk_ids": ["chunk_b"],
                }
            ],
        },
        output,
    )

    text = output.read_text(encoding="utf-8")
    assert "case-1" in text
    assert "chunk_a" in text
    assert "Where is the report section?" not in text


def test_filter_cases_passing_audit_drops_failed_case() -> None:
    cases = [
        {"id": "good", "query": "good", "expected_chunk_ids": ["a"]},
        {"id": "bad", "query": "bad", "expected_chunk_ids": ["b"]},
    ]
    report = {
        "rows": [
            {"case_id": "good", "ok": True},
            {"case_id": "bad", "ok": False},
        ]
    }

    filtered = filter_cases_passing_audit(cases, report)

    assert filtered == [cases[0]]
