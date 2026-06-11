from __future__ import annotations

import json

import pytest

from scripts.index_kb import validate_only, validate_seed_items


def test_validate_seed_items_accepts_valid_records() -> None:
    records = validate_seed_items(
        [
            {
                "chunk_id": "ctx_1",
                "text_clean": "Проезд участник оплачивает самостоятельно.",
                "status": "published",
            }
        ]
    )

    assert len(records) == 1
    assert records[0].chunk_id == "ctx_1"


def test_validate_seed_items_requires_json_array() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        validate_seed_items({"chunk_id": "ctx_1"})


def test_validate_seed_items_rejects_duplicate_chunk_id() -> None:
    raw = [
        {"chunk_id": "ctx_1", "text": "Первый текст"},
        {"chunk_id": "ctx_1", "text": "Второй текст"},
    ]

    with pytest.raises(ValueError, match="duplicate_chunk_id"):
        validate_seed_items(raw)


def test_validate_seed_items_rejects_empty_text() -> None:
    with pytest.raises(ValueError, match="empty_text"):
        validate_seed_items([{"chunk_id": "ctx_1", "text_clean": "  "}])


def test_validate_seed_items_rejects_invalid_status() -> None:
    with pytest.raises(ValueError, match="status must be draft, published or archived"):
        validate_seed_items([{"chunk_id": "ctx_1", "text": "Текст", "status": "deleted"}])


def test_validate_seed_items_rejects_empty_chunk_id() -> None:
    with pytest.raises(ValueError, match="chunk_id must not be empty"):
        validate_seed_items([{"chunk_id": " ", "text": "Текст"}])


def test_validate_only_prints_record_count(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "knowledge_base_seed.json"
    path.write_text(
        json.dumps([{"chunk_id": "ctx_1", "text": "Текст"}], ensure_ascii=False),
        encoding="utf-8",
    )

    validate_only(path)

    assert "valid_records=1" in capsys.readouterr().out
