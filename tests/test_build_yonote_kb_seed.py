from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from zipfile import ZipFile

from scripts.build_yonote_kb_seed import (
    build_yonote_records,
    merge_records,
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
