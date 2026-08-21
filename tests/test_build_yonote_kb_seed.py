from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from zipfile import ZipFile

from scripts.build_yonote_kb_seed import (
    MAX_CHUNK_CHARS,
    build_yonote_records,
    clean_markdown_text,
    merge_records,
    split_long_paragraph,
    split_section_text_with_heading,
)
from scripts.index_kb import validate_seed_items


def test_build_yonote_records_splits_markdown_sections_and_skips_duplicate_exports(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "yonote"
    source_dir.mkdir()
    markdown = """# Описание

Форум пройдет в 2026 году.

# Регистрация

Регистрация идет на сайте https://example.test/events.

# Финансирование

Проезд оплачивает направляющая сторона.
"""
    for archive_name in ("Волга 2026.zip", "Волга 2026 copy.zip"):
        with ZipFile(source_dir / archive_name, "w") as archive:
            archive.writestr("Волга 2026.md", markdown)

    records = build_yonote_records(source_dir, date(2026, 7, 4))

    assert len(records) == 3
    assert {record["source_type"] for record in records} == {"yonote"}
    assert {record["forum_normalized"] for record in records} == {"Волга"}
    assert all(record["status"] == "published" for record in records)
    assert any("Регистрация" in record["intent_name"] for record in records)
    validate_seed_items(records)


def test_merge_records_can_replace_existing_yonote_records() -> None:
    base = [
        {
            "chunk_id": "xlsx_base",
            "text_clean": "Base answer",
            "status": "published",
            "source_type": "xlsx",
        },
        {
            "chunk_id": "yonote_old",
            "text_clean": "Old Yonote answer",
            "status": "published",
            "source_type": "yonote",
        },
    ]
    fresh = [
        {
            "chunk_id": "yonote_new",
            "text_clean": "Fresh Yonote answer",
            "status": "published",
            "source_type": "yonote",
        }
    ]

    merged = merge_records(base, fresh, replace_existing_yonote=True)

    assert [record["chunk_id"] for record in merged] == ["xlsx_base", "yonote_new"]
    json.dumps(merged, ensure_ascii=False)


def test_merge_records_rebuilds_clean_link_metadata_from_text() -> None:
    base = [
        {
            "chunk_id": "xlsx_base",
            "text_clean": "Кабинет: myrosmol.ru/profile. Почта: help@example.org.",
            "links": ["myrosmol.ru/profile."],
            "status": "published",
            "source_type": "xlsx",
        }
    ]

    merged = merge_records(base, [], replace_existing_yonote=False)

    assert merged[0]["links"] == ["https://myrosmol.ru/profile"]


def test_clean_markdown_text_removes_unresolved_social_link_labels() -> None:
    assert clean_markdown_text("Соцсети: VK TG\n\nСайт") == "Сайт"
    assert clean_markdown_text(
        "Сайт VK TG\n\nЭлектронная почта: test@example.ru"
    ) == "Электронная почта: test@example.ru"
    assert clean_markdown_text(
        "Контакты: test@example.ru, VK TG"
    ) == "Контакты: test@example.ru,"


def test_split_long_paragraph_bounds_one_oversized_sentence_among_others() -> None:
    paragraph = "Краткое введение. " + ("подтверждённый факт " * 80) + "Конец."

    parts = split_long_paragraph(paragraph, max_chars=120)

    assert len(parts) > 2
    assert all(0 < len(part) <= 120 for part in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(paragraph.split())


def test_split_section_text_with_heading_reserves_context_budget() -> None:
    heading = "Документы для подачи заявки"
    text = f"{heading}\n\n" + ("Подтверждённый текст без точек " * 80)

    parts = split_section_text_with_heading(text, heading, max_chars=180)

    assert len(parts) > 1
    assert all(part.startswith(f"{heading}\n\n") for part in parts)
    assert all(len(part) <= 180 for part in parts)
    assert heading not in {part.strip() for part in parts}


def test_markdown_builder_never_emits_chunks_over_configured_maximum(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "yonote"
    source_dir.mkdir()
    heading = "Обязательные документы"
    markdown = f"# {heading}\n\nВведение. " + ("длинный факт " * 600)
    with ZipFile(source_dir / "Волга 2026.zip", "w") as archive:
        archive.writestr("Волга 2026.md", markdown)

    records = build_yonote_records(source_dir, date(2026, 8, 21))

    assert len(records) > 1
    assert all(20 <= len(record["text_clean"]) <= MAX_CHUNK_CHARS for record in records)
    assert all(record["text_clean"].startswith(heading) for record in records)
    assert heading not in {record["text_clean"].strip() for record in records}
