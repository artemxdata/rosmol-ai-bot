from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.admin.kb_store import update_chunk
from src.kb.source_extractors import (
    extract_dates,
    extract_emails,
    extract_links,
    extract_phones,
    has_conditional_logic,
)


def _write_seed(path: Path, record: dict[str, object]) -> None:
    path.write_text(
        json.dumps([record], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    path.with_name("forums_registry.json").write_text("[]", encoding="utf-8")


def test_text_edit_refreshes_all_deterministic_metadata(tmp_path: Path) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    old_text = (
        "Подай заявку до 20 августа 2026 года: https://old.example. "
        "Почта old@example.ru, телефон +7 999 000-00-00."
    )
    record: dict[str, object] = {
        "chunk_id": "editable",
        "text_raw": old_text,
        "text_clean": old_text,
        "status": "published",
        "category": "general",
        "source_type": "yonote",
        "has_conditional_logic": True,
        "conditions_summary": None,
        "links": ["https://old.example"],
        "emails": ["old@example.ru"],
        "phones": ["+7 999 000-00-00"],
        "dates_mentioned": ["20 августа 2026 года"],
        "dates": ["20 августа 2026 года"],
        "registration_deadline": "2026-08-20",
        "char_count": len(old_text),
    }
    _write_seed(seed_path, record)
    new_text = (
        "Если регистрация закрыта, проверь https://new.example. "
        "Напиши new@example.ru или позвони +7 999 111-22-33. "
        "Информация обновлена 21 августа 2026 года."
    )

    updated = update_chunk(seed_path, "editable", text_clean=new_text)

    assert updated["text_clean"] == new_text
    assert updated["text_raw"] == old_text
    assert updated["links"] == extract_links(new_text)
    assert updated["emails"] == extract_emails(new_text)
    assert updated["phones"] == extract_phones(new_text)
    assert updated["dates_mentioned"] == extract_dates(new_text)
    assert updated["dates"] == extract_dates(new_text)
    assert updated["char_count"] == len(new_text)
    assert updated["has_conditional_logic"] is has_conditional_logic(new_text)
    assert "registration_deadline" not in updated
    assert json.loads(seed_path.read_text(encoding="utf-8"))[0] == updated


def test_text_edit_rejects_unrebuildable_curated_condition_summary(
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    old_text = "Если заявка одобрена, дождись письма."
    record: dict[str, object] = {
        "chunk_id": "curated-condition",
        "text_clean": old_text,
        "status": "published",
        "category": "general",
        "source_type": "yonote",
        "has_conditional_logic": True,
        "conditions_summary": "Только после одобрения заявки",
        "links": [],
        "emails": [],
        "phones": [],
        "dates_mentioned": [],
        "char_count": len(old_text),
    }
    _write_seed(seed_path, record)
    original = seed_path.read_bytes()

    with pytest.raises(
        ValueError,
        match="conditions_summary is curated",
    ):
        update_chunk(
            seed_path,
            "curated-condition",
            text_clean="Заявка уже одобрена.",
        )

    assert seed_path.read_bytes() == original
