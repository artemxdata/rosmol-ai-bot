from __future__ import annotations

import json
from pathlib import Path

from eval.build_kb_cleanup_report import build_cleanup_report, write_markdown


def test_build_cleanup_report_classifies_actionable_failure_types(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    kb_path = tmp_path / "kb.json"
    _write_json(
        audit_path,
        {
            "rows": [
                _audit_row(
                    "case-generic",
                    "xlsx_specific",
                    "xlsx_generic",
                    same_topic=False,
                    same_forum=True,
                    text_similarity=0.1,
                    token_jaccard=0.05,
                ),
                _audit_row(
                    "case-topic",
                    "xlsx_transfer",
                    "xlsx_food",
                    same_topic=False,
                    same_forum=True,
                    text_similarity=0.4,
                    token_jaccard=0.25,
                ),
                _audit_row(
                    "case-conflict",
                    "docx_forum",
                    "ticket_platform",
                    same_topic=False,
                    same_forum=False,
                    text_similarity=0.4,
                    token_jaccard=0.25,
                ),
                _audit_row(
                    "case-wrong",
                    "xlsx_status",
                    "xlsx_grants",
                    same_topic=False,
                    same_forum=False,
                    text_similarity=0.1,
                    token_jaccard=0.05,
                ),
            ]
        },
    )
    _write_json(
        kb_path,
        [
            _record("xlsx_specific", "xlsx", "forum", "transfer", "Forum A"),
            _record("xlsx_generic", "xlsx", "forum", "general", "Forum A", is_generic=True),
            _record("xlsx_transfer", "xlsx", "forum", "transfer", "Forum A"),
            _record("xlsx_food", "xlsx", "forum", "food", "Forum A"),
            _record("docx_forum", "docx", "forum", "program", "Forum B"),
            _record("ticket_platform", "ticket_answer_bank", "platform", "profile", ""),
            _record("xlsx_status", "xlsx", "platform", "status", ""),
            _record("xlsx_grants", "xlsx", "grants", "report", ""),
        ],
    )

    report = build_cleanup_report(audit_path=audit_path, kb_seed_path=kb_path)

    assert report["analyzed_pairs"] == 4
    assert report["classification_counts"] == {
        "generic_fallback_competes_with_specific": 1,
        "same_forum_different_topic": 1,
        "cross_source_conflict": 1,
        "wrong_source_selected": 1,
    }
    assert report["severity_counts"] == {"high": 3, "medium": 1}


def test_build_cleanup_report_defaults_to_needs_review_rows(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    kb_path = tmp_path / "kb.json"
    _write_json(
        audit_path,
        {
            "rows": [
                _audit_row("case-1", "xlsx_expected", "xlsx_cited"),
                {
                    **_audit_row("case-2", "xlsx_expected", "xlsx_cited"),
                    "decision": "auto_equivalent",
                },
            ]
        },
    )
    _write_json(
        kb_path,
        [
            _record("xlsx_expected", "xlsx", "forum", "transfer", "Forum A"),
            _record("xlsx_cited", "xlsx", "forum", "food", "Forum A"),
        ],
    )

    report = build_cleanup_report(audit_path=audit_path, kb_seed_path=kb_path)

    assert report["input_candidate_pairs"] == 2
    assert report["analyzed_pairs"] == 1
    assert report["decision_filter"] == ["needs_review"]


def test_build_cleanup_report_does_not_emit_raw_text(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    kb_path = tmp_path / "kb.json"
    markdown_path = tmp_path / "cleanup.md"
    _write_json(
        audit_path,
        {"rows": [_audit_row("case-1", "xlsx_expected", "xlsx_cited")]},
    )
    _write_json(
        kb_path,
        [
            {
                **_record("xlsx_expected", "xlsx", "forum", "transfer", "Forum A"),
                "text_clean": "SECRET RAW TEXT",
                "text": "ANOTHER SECRET TEXT",
            },
            {
                **_record("xlsx_cited", "xlsx", "forum", "food", "Forum A"),
                "text_clean": "CITED RAW TEXT",
            },
        ],
    )

    report = build_cleanup_report(audit_path=audit_path, kb_seed_path=kb_path)
    write_markdown(report, markdown_path)

    payload = json.dumps(report, ensure_ascii=False)
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "SECRET RAW TEXT" not in payload
    assert "ANOTHER SECRET TEXT" not in payload
    assert "CITED RAW TEXT" not in payload
    assert "SECRET RAW TEXT" not in markdown
    assert "CITED RAW TEXT" not in markdown


def _audit_row(
    case_id: str,
    expected_id: str,
    cited_id: str,
    *,
    same_topic: bool = False,
    same_forum: bool = True,
    text_similarity: float = 0.2,
    token_jaccard: float = 0.1,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "expected_id": expected_id,
        "cited_id": cited_id,
        "decision": "needs_review",
        "same_category": False,
        "same_topic": same_topic,
        "same_forum": same_forum,
        "exact_text_match": False,
        "text_similarity": text_similarity,
        "token_jaccard": token_jaccard,
    }


def _record(
    chunk_id: str,
    source_type: str,
    category: str,
    topic: str,
    forum: str,
    *,
    is_generic: bool = False,
) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "source_type": source_type,
        "category": category,
        "topic": topic,
        "forum_normalized": forum,
        "status": "published",
        "is_generic": is_generic,
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
