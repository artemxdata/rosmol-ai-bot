from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from src.admin import yonote_sync


def _write_seed(path: Path) -> None:
    records = [
        {
            "chunk_id": "xlsx_base",
            "text_clean": "Base answer",
            "status": "published",
            "category": "general",
            "source_type": "xlsx",
        },
        {
            "chunk_id": "yonote_old_changed",
            "text_clean": "Old Yonote answer",
            "status": "published",
            "category": "forums",
            "forum_normalized": "Amur",
            "source_type": "yonote",
        },
        {
            "chunk_id": "yonote_removed",
            "text_clean": "Removed Yonote answer",
            "status": "published",
            "category": "forums",
            "forum_normalized": "Amur",
            "source_type": "yonote",
        },
    ]
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def test_preview_sync_does_not_write_seed_and_counts_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    original = seed_path.read_text(encoding="utf-8")

    def fake_load(_settings, *, limit_documents):
        assert limit_documents == 2
        return [object(), object()], [
            {
                "chunk_id": "yonote_old_changed",
                "text_clean": "Fresh Yonote answer",
                "status": "published",
                "category": "forums",
                "forum_normalized": "Amur",
                "source_type": "yonote",
            },
            {
                "chunk_id": "yonote_added",
                "text_clean": "Added Yonote answer",
                "status": "published",
                "category": "forums",
                "forum_normalized": "Amur",
                "source_type": "yonote",
                "intent_name": "How to join Amur",
                "source_collection_name": "Forum knowledge",
                "source_heading_path": ["Amur", "Registration"],
                "source_url": "https://example.test/amur",
            },
        ]

    monkeypatch.setattr(yonote_sync, "_load_fresh_yonote_records", fake_load)

    report = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        limit_documents=2,
    )

    assert report["applied"] is False
    assert report["documents"] == 2
    assert report["added"] == 1
    assert report["changed"] == 1
    assert report["removed"] == 1
    assert report["added_items"] == [
        {
            "chunk_id": "yonote_added",
            "title": "How to join Amur",
            "heading": "Amur / Registration",
            "collection": "Forum knowledge",
            "forum": "Amur",
            "category": "forums",
            "source_url": "https://example.test/amur",
            "updated_at": "",
            "text_preview": "Added Yonote answer",
        }
    ]
    assert report["changed_items"][0]["title"] == "yonote_old_changed"
    assert report["changed_items"][0]["changed_fields"] == ["text_clean"]
    assert report["changed_items"][0]["before_text"] == "Old Yonote answer"
    assert report["changed_items"][0]["after_text"] == "Fresh Yonote answer"
    assert report["removed_items"][0]["title"] == "yonote_removed"
    assert report["collection_counts"] == {"Forum knowledge": 1, "unknown": 1}
    assert seed_path.read_text(encoding="utf-8") == original


def test_apply_sync_writes_seed_and_keeps_non_yonote_records(
    monkeypatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)

    def fake_load(_settings, *, limit_documents):
        assert limit_documents is None
        return [object()], [
            {
                "chunk_id": "yonote_added",
                "text_clean": "Added Yonote answer",
                "status": "published",
                "category": "forums",
                "forum_normalized": "Amur",
                "source_type": "yonote",
            }
        ]

    monkeypatch.setattr(yonote_sync, "_load_fresh_yonote_records", fake_load)

    report = yonote_sync.apply_sync(seed_path, SimpleNamespace())
    stored = json.loads(seed_path.read_text(encoding="utf-8"))

    assert report["applied"] is True
    assert report["index_required"] is True
    assert [record["chunk_id"] for record in stored] == ["xlsx_base", "yonote_added"]
