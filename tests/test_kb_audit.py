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
    assert report["warnings"] == 4
    assert {
        "trailing_export_quote",
        "template_artifact",
        "missing_topic",
        "short_published_text",
        "grant_record_has_forum",
        "duplicate_text",
    } <= codes


def test_audit_seed_records_detects_private_source_references_and_offtopic_context() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "private",
                "text_clean": "Private source answer",
                "category": "общее",
                "topic": "answer_bank",
                "source_type": "ticket_answer_bank",
                "source_file": "data/private/tickets/export.json",
            },
            {
                "chunk_id": "offtopic",
                "text_clean": "Я отвечаю только по Росмолодёжи",
                "category": "общее",
                "topic": "offtop_ne_po_rosmolodezhi",
                "forum": "Гранты для физических лиц",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            },
        ]
    )

    findings = {finding["code"]: finding for finding in report["findings"]}
    assert report["errors"] == 1
    assert report["warnings"] == 1
    assert findings["private_source_reference"]["chunk_ids"] == ["private"]
    assert findings["offtopic_record_has_context"]["chunk_ids"] == ["offtopic"]


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


def test_audit_seed_records_detects_forum_registry_coverage_gaps() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "forum_a_1",
                "text_clean": "Forum coverage answer",
                "category": "forums",
                "forum_normalized": "Forum A",
                "topic": "documents",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
                "status": "published",
            },
            {
                "chunk_id": "forum_c_1",
                "text_clean": "Another answer",
                "category": "forums",
                "forum_normalized": "Forum C",
                "topic": "transfer",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
                "status": "published",
            },
        ],
        forum_registry=[
            {"name": "Forum A", "normalized": "Forum A"},
            {"name": "Forum B", "normalized": "Forum B"},
            {"name": "Гранты 1 сезон", "normalized": "Гранты 1 сезон"},
        ],
        min_forum_chunks=2,
        min_forum_topics=2,
    )

    findings = {finding["code"]: finding for finding in report["findings"]}
    assert report["warnings"] == 4
    assert findings["registry_forum_without_published_chunks"]["forums"] == ["Forum B"]
    assert findings["forum_not_in_registry"]["forums"] == ["Forum C"]
    assert findings["low_forum_chunk_coverage"]["count"] == 2
    assert findings["low_forum_topic_coverage"]["count"] == 2


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
    markdown = tmp_path / "report.md"
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

    audit_kb_seed(path, output, None, markdown_path=markdown)

    assert json.loads(output.read_text(encoding="utf-8"))["records_total"] == 1
    assert "KB Seed Audit" in markdown.read_text(encoding="utf-8")


def test_audit_kb_seed_reads_forum_registry_for_coverage(tmp_path: Path) -> None:
    path = tmp_path / "knowledge_base_seed.json"
    registry = tmp_path / "forums_registry.json"
    output = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "forum_a_1",
                    "text_clean": "Forum coverage answer",
                    "category": "forums",
                    "forum_normalized": "Forum A",
                    "topic": "documents",
                    "source_type": "xlsx",
                    "source_file": "source.xlsx",
                    "status": "published",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    registry.write_text(
        json.dumps(
            [
                {"name": "Forum A", "normalized": "Forum A"},
                {"name": "Forum B", "normalized": "Forum B"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = audit_kb_seed(
        path,
        output,
        None,
        forums_registry_path=registry,
        min_forum_chunks=2,
        min_forum_topics=1,
    )

    codes = {finding["code"] for finding in report["findings"]}
    assert {"registry_forum_without_published_chunks", "low_forum_chunk_coverage"} <= codes
    assert json.loads(output.read_text(encoding="utf-8"))["warnings"] == 2


def test_audit_blocks_unresolved_social_link_placeholders() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "contacts_without_urls",
                "text_clean": "Соцсети: VK TG",
                "status": "published",
                "category": "форумы",
                "topic": "contacts",
                "source_type": "yonote",
                "source_file": "yonote",
            }
        ]
    )

    finding = next(
        item
        for item in report["findings"]
        if item["code"] == "unresolved_social_link_placeholder"
    )
    assert finding["severity"] == "error"
    assert finding["chunk_ids"] == ["contacts_without_urls"]


def test_audit_detects_published_forum_text_conflict() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "wrong_event",
                "text_clean": "Регистрация на форум «Ростов» уже закрыта.",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Добрино",
                "topic": "registration",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            }
        ],
        forum_registry=[
            {"name": "Добрино", "normalized": "Добрино", "aliases": []},
            {"name": "Ростов", "normalized": "Ростов", "aliases": []},
        ],
    )

    finding = next(item for item in report["findings"] if item["code"] == "forum_text_conflict")
    assert finding["severity"] == "error"
    assert finding["records"][0]["chunk_id"] == "wrong_event"


def test_audit_warns_but_does_not_block_unknown_forum_cross_reference() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "new_event_overview",
                "text_clean": "В программе также расскажут о форуме «Машук».",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Горизонт будущего 2027",
                "topic": "overview",
                "source_type": "yonote",
                "source_file": "yonote",
            }
        ],
        forum_registry=[
            {"name": "Машук", "normalized": "Машук", "aliases": []},
        ],
    )

    findings = {item["code"]: item for item in report["findings"]}
    assert "forum_text_conflict" not in findings
    assert findings["forum_not_in_registry"]["severity"] == "warning"
    assert findings["forum_not_in_registry"]["forums"] == [
        "Горизонт будущего 2027"
    ]


def test_audit_blocks_unknown_forum_whose_main_text_is_about_reviewed_event() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "wrong_new_event_scope",
                "text_clean": "Форум «Машук» пройдёт с 9 по 23 августа.",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Горизонт будущего 2027",
                "topic": "dates",
                "source_type": "yonote",
                "source_file": "yonote",
            }
        ],
        forum_registry=[
            {"name": "Машук", "normalized": "Машук", "aliases": []},
        ],
    )

    findings = {item["code"]: item for item in report["findings"]}
    assert findings["forum_text_conflict"]["severity"] == "error"
    assert findings["forum_text_conflict"]["records"] == [
        {
            "chunk_id": "wrong_new_event_scope",
            "record_forum": "Горизонт будущего 2027",
            "mentioned_forum": "Машук",
        }
    ]
    assert findings["forum_not_in_registry"]["severity"] == "warning"


@pytest.mark.parametrize(
    "text",
    [
        "Форум «Машук» пройдёт с 9 по 23 августа. Также будет трансляция.",
        "Также форум «Машук» пройдёт с 9 по 23 августа.",
        "На форуме «Машук» расскажут о программе развития.",
        "Форум «Машук» упоминает новые направления.",
        "Расскажем о форуме «Машук»: он пройдёт с 9 по 23 августа.",
    ],
)
def test_audit_does_not_mistake_main_event_statement_for_secondary_reference(
    text: str,
) -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "wrong_new_event_scope",
                "text_clean": text,
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Горизонт будущего 2027",
                "topic": "dates",
                "source_type": "yonote",
                "source_file": "yonote",
            }
        ],
        forum_registry=[
            {"name": "Машук", "normalized": "Машук", "aliases": []},
        ],
    )

    findings = {item["code"]: item for item in report["findings"]}
    assert findings["forum_text_conflict"]["severity"] == "error"


def test_audit_allows_explicit_secondary_reference_between_reviewed_forums() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "reviewed_event_cross_reference",
                "text_clean": "В программе также расскажут о форуме «Машук».",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Амур",
                "topic": "program",
                "source_type": "yonote",
                "source_file": "yonote",
            }
        ],
        forum_registry=[
            {"name": "Амур", "normalized": "Амур", "aliases": []},
            {"name": "Машук", "normalized": "Машук", "aliases": []},
        ],
    )

    assert not any(
        item["code"] == "forum_text_conflict" for item in report["findings"]
    )


def test_audit_keeps_main_event_mismatch_after_forum_review() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "reviewed_event_overview",
                "text_clean": "Форум «Машук» пройдёт с 9 по 23 августа.",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Горизонт будущего 2027",
                "topic": "overview",
                "source_type": "yonote",
                "source_file": "yonote",
            }
        ],
        forum_registry=[
            {
                "name": "Горизонт будущего 2027",
                "normalized": "Горизонт будущего 2027",
                "aliases": [],
            },
            {"name": "Машук", "normalized": "Машук", "aliases": []},
        ],
    )

    finding = next(
        item for item in report["findings"] if item["code"] == "forum_text_conflict"
    )
    assert finding["severity"] == "error"
    assert finding["records"] == [
        {
            "chunk_id": "reviewed_event_overview",
            "record_forum": "Горизонт будущего 2027",
            "mentioned_forum": "Машук",
        }
    ]


def test_audit_detects_inflected_forum_alias_and_slet_conflicts() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "wrong_alias",
                "text_clean": "Регистрация на форуме «ОстроVа» уже закрыта.",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Ладога",
                "topic": "registration",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            },
            {
                "chunk_id": "wrong_slet",
                "text_clean": "Критерии опубликованы в Положении слёта «Спецназ».",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Волга",
                "topic": "selection",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            },
        ],
        forum_registry=[
            {"name": "Ладога", "normalized": "Ладога", "aliases": []},
            {"name": "Острова", "normalized": "Острова", "aliases": ["ОстроVа"]},
            {"name": "Волга", "normalized": "Волга", "aliases": []},
            {"name": "Спецназ", "normalized": "Спецназ", "aliases": []},
        ],
    )

    finding = next(item for item in report["findings"] if item["code"] == "forum_text_conflict")
    assert {item["chunk_id"] for item in finding["records"]} == {
        "wrong_alias",
        "wrong_slet",
    }


def test_audit_allows_reviewed_cross_event_overview() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "xlsx_category_r0662_o_meropriyatii",
                "text_clean": "Одна из программ проходит на форуме «Бирюса».",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Ямолод",
                "topic": "overview",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            }
        ],
        forum_registry=[
            {"name": "Ямолод", "normalized": "Ямолод", "aliases": []},
            {"name": "Бирюса", "normalized": "Бирюса", "aliases": []},
        ],
    )

    assert "forum_text_conflict" not in {item["code"] for item in report["findings"]}


def test_audit_ignores_archived_conflict_and_nested_parent_event() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "archived",
                "text_clean": "Регистрация на форум «Ростов» закрыта.",
                "status": "archived",
                "category": "форумы",
                "forum_normalized": "Добрино",
                "topic": "registration",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            },
            {
                "chunk_id": "nested",
                "text_clean": "Регистрация на форум «Истоки» закрыта.",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Истоки Школа",
                "topic": "registration",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            },
        ],
        forum_registry=[
            {"name": "Добрино", "normalized": "Добрино", "aliases": []},
            {"name": "Ростов", "normalized": "Ростов", "aliases": []},
            {"name": "Истоки", "normalized": "Истоки", "aliases": []},
            {"name": "Истоки Школа", "normalized": "Истоки Школа", "aliases": []},
        ],
    )

    assert "forum_text_conflict" not in {item["code"] for item in report["findings"]}


def test_audit_detects_malformed_link_metadata_and_known_domain_typo() -> None:
    report = audit_seed_records(
        [
            {
                "chunk_id": "broken_links",
                "text_clean": (
                    "Подать заявку: https://events.myrosmol.rru/ "
                    "[https://example.org/apply|Подать] "
                    "https://max.ru/example'Контакты"
                ),
                "links": ["https://example.org/apply|Подать"],
                "status": "published",
                "category": "форумы",
                "topic": "registration",
                "source_type": "xlsx",
                "source_file": "source.xlsx",
            }
        ]
    )

    findings = {item["code"]: item for item in report["findings"]}
    assert findings["malformed_link"]["severity"] == "error"
    assert findings["suspicious_link_domain"]["severity"] == "error"
