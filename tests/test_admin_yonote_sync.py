from __future__ import annotations

import json
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.admin import yonote_sync


def test_common_reader_uses_only_passed_settings_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    expected_documents = [object()]

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            captured["closed"] = True

    def fake_load(
        client: object,
        selectors: tuple[str, ...],
        *,
        limit_documents: int | None,
        include_empty: bool,
        max_total_text_bytes: int | None,
    ) -> list[object]:
        assert isinstance(client, FakeClient)
        captured["selectors"] = selectors
        captured["limit_documents"] = limit_documents
        captured["include_empty"] = include_empty
        captured["max_total_text_bytes"] = max_total_text_bytes
        return expected_documents

    monkeypatch.setattr(yonote_sync, "YonoteClient", FakeClient)
    monkeypatch.setattr(yonote_sync, "load_yonote_documents", fake_load)
    settings = SimpleNamespace(
        yonote_base_url="https://yonote.example",
        yonote_api_token="placeholder-token",
        yonote_collection_names="Первая коллекция;Вторая коллекция",
        yonote_request_timeout_seconds=17,
        yonote_max_retries=4,
        yonote_min_request_interval_seconds=0.25,
    )

    result = yonote_sync.load_yonote_documents_from_settings(
        settings,
        limit_documents=7,
        include_empty=True,
        max_duration_seconds=240,
        max_total_text_bytes=1024,
    )

    assert result is expected_documents
    assert captured["selectors"] == ("Первая коллекция", "Вторая коллекция")
    assert captured["limit_documents"] == 7
    assert captured["include_empty"] is True
    assert captured["max_total_text_bytes"] == 1024
    assert captured["closed"] is True
    assert captured["client_kwargs"] == {
        "base_url": "https://yonote.example",
        "api_token": "placeholder-token",
        "timeout_seconds": 17.0,
        "max_retries": 4,
        "min_request_interval_seconds": 0.25,
        "max_duration_seconds": 240,
    }


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


def _scoped_yonote_record(
    chunk_id: str,
    text: str,
    *,
    document_id: str = "doc-1",
    source_row: int = 1,
) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "text_clean": text,
        "status": "published",
        "category": "general",
        "source_type": "yonote",
        "source_url": f"https://rossmol.yonote.ru/doc/{document_id}",
        "source_collection_id": "collection-1",
        "source_document_id": document_id,
        "source_document_updated_at": "2026-08-20T12:00:00Z",
        "source_row": source_row,
    }


def _seal_fresh_receipt(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed_path: Path,
    receipt_dir: Path,
) -> dict[str, object]:
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: (
            [object()],
            [
                {
                    "chunk_id": "yonote_fresh",
                    "text_clean": "Fresh grounded answer",
                    "status": "published",
                    "category": "general",
                    "source_type": "yonote",
                    "source_url": "https://rossmol.yonote.ru/doc/fresh",
                    "source_document_id": "doc-fresh",
                    "source_document_updated_at": "2026-08-20T12:00:00Z",
                }
            ],
        ),
    )
    return yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )


def _write_receipt_cleanup_sentinels(
    receipt_dir: Path,
) -> tuple[Path, Path, Path]:
    receipt_dir.mkdir(parents=True, exist_ok=True)
    old_applied = receipt_dir / f"{'0' * 32}.{'1' * 64}.applied"
    invalid_name = receipt_dir / "not-a-receipt.applied"
    receipt_shaped_directory = receipt_dir / f"{'2' * 32}.{'3' * 64}.applied"
    old_applied.write_bytes(b"superseded")
    invalid_name.write_bytes(b"keep invalid names")
    receipt_shaped_directory.mkdir()
    return old_applied, invalid_name, receipt_shaped_directory


def test_preview_blocks_unresolved_applying_before_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    preview = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    receipt = preview["receipt"]
    assert isinstance(receipt, dict)
    active_path = next(receipt_dir.glob("*.json"))
    applying_path = active_path.with_suffix(".applying")
    active_path.replace(applying_path)
    network_calls = 0

    def reject_network(*_args: object, **_kwargs: object) -> object:
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("blocked Preview must not call Yonote")

    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        reject_network,
    )

    with pytest.raises(
        yonote_sync.YonoteReceiptConflict,
        match="requires exact receipt recovery",
    ):
        yonote_sync.preview_sync(
            seed_path,
            SimpleNamespace(),
            receipt_dir=receipt_dir,
        )

    assert network_calls == 0
    assert applying_path.exists()


def test_new_preview_removes_only_superseded_regular_applied_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    old_applied, invalid_name, receipt_shaped_directory = (
        _write_receipt_cleanup_sentinels(receipt_dir)
    )

    preview = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )

    current_receipts = list(receipt_dir.glob("*.json"))
    assert isinstance(preview["receipt"], dict)
    assert len(current_receipts) == 1
    assert current_receipts[0].name.startswith(str(preview["receipt"]["id"]))
    assert not old_applied.exists()
    assert invalid_name.exists()
    assert receipt_shaped_directory.is_dir()


def test_finalize_removes_only_superseded_regular_applied_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    preview = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    receipt = preview["receipt"]
    assert isinstance(receipt, dict)
    old_applied, invalid_name, receipt_shaped_directory = (
        _write_receipt_cleanup_sentinels(receipt_dir)
    )

    applied = yonote_sync.apply_sync(
        seed_path,
        receipt_dir=receipt_dir,
        receipt_id=str(receipt["id"]),
        receipt_sha256=str(receipt["sha256"]),
    )

    current_applied = receipt_dir / (
        f"{receipt['id']}.{receipt['sha256']}.applied"
    )
    assert applied["receipt"]["state"] == "applied"
    assert current_applied.is_file()
    assert not old_applied.exists()
    assert invalid_name.exists()
    assert receipt_shaped_directory.is_dir()


def test_receipt_cleanup_never_unlinks_symlink() -> None:
    class SymlinkCandidate:
        name = f"{'4' * 32}.{'5' * 64}.applied"

        @staticmethod
        def is_symlink() -> bool:
            return True

        @staticmethod
        def is_file() -> bool:
            raise AssertionError("symlink must be rejected before file inspection")

        @staticmethod
        def unlink() -> None:
            raise AssertionError("receipt cleanup must never unlink symlinks")

    class ReceiptDirectory:
        @staticmethod
        def is_symlink() -> bool:
            return False

        @staticmethod
        def iterdir() -> list[SymlinkCandidate]:
            return [SymlinkCandidate()]

    yonote_sync._remove_superseded_receipts(
        ReceiptDirectory(),  # type: ignore[arg-type]
        keep=f"{'6' * 32}.{'7' * 64}.applied",
        removable_states=frozenset({"applied"}),
    )


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
    assert report["snapshot_scope"] == "partial"
    assert report["receipt"]["apply_ready"] is False
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


def test_preview_reconciles_exact_content_rekeys_before_removal_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    current = [
        _scoped_yonote_record(
            f"old-{index}",
            f"Подтверждённый неизменный факт номер {index}.",
            source_row=index + 1,
        )
        for index in range(40)
    ]
    seed_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    fresh = [
        _scoped_yonote_record(
            f"fresh-{index}",
            f"Подтверждённый неизменный факт номер {index}.",
            source_row=index + 2,
        )
        for index in range(40)
    ]
    fresh.append(
        _scoped_yonote_record(
            "fresh-new",
            "Новый подтверждённый опубликованный факт.",
            source_row=1,
        )
    )
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: ([object()], fresh),
    )

    report = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    assert report["identity_reconciliation"] == {
        "raw_id_added": 41,
        "raw_id_removed": 40,
        "exact_content_rekeys": 40,
        "same_set_identity_rotations": 0,
        "ambiguous_exact_content_groups": 0,
        "logical_added": 1,
        "logical_removed": 0,
    }
    assert report["added"] == 1
    assert report["removed"] == 0
    assert report["changed"] == 40
    assert report["change_classification"] == {
        "metadata_only": 40,
        "content_or_source": 0,
        "field_counts": {"source_row": 40},
    }
    assert report["snapshot_safety"]["status"] == "GO"
    assert report["snapshot_safety"]["removal_ratio"] == 0.0
    assert report["snapshot_safety"]["raw_id_removed"] == 40
    assert report["snapshot_safety"]["exact_content_rekeys"] == 40
    assert report["receipt"]["apply_ready"] is True


def test_preview_does_not_reconcile_identical_text_across_document_scopes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    current = [
        _scoped_yonote_record(
            f"old-{index}",
            f"Подтверждённый факт номер {index} для проверки области.",
            document_id="old-document",
            source_row=index + 1,
        )
        for index in range(40)
    ]
    fresh = [
        _scoped_yonote_record(
            f"fresh-{index}",
            f"Подтверждённый факт номер {index} для проверки области.",
            document_id="new-document",
            source_row=index + 1,
        )
        for index in range(40)
    ]
    seed_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: ([object()], fresh),
    )

    report = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    assert report["identity_reconciliation"]["exact_content_rekeys"] == 0
    assert report["identity_reconciliation"]["raw_id_removed"] == 40
    assert report["identity_reconciliation"]["logical_removed"] == 40
    assert report["removed"] == 40
    assert report["snapshot_safety"]["status"] == "STOP"
    assert report["snapshot_safety"]["reasons"] == [
        "removal_ratio_limit_exceeded"
    ]
    assert report["receipt"]["apply_ready"] is False
    assert not receipt_dir.exists()


def test_identity_reconciliation_preserves_insertion_chain_and_displaces_new_content() -> None:
    old = [
        _scoped_yonote_record("section-1", "Факт A."),
        _scoped_yonote_record("section-2", "Факт B.", source_row=2),
    ]
    fresh = [
        _scoped_yonote_record("section-1", "Новый факт."),
        _scoped_yonote_record("section-2", "Факт A.", source_row=2),
        _scoped_yonote_record("section-3", "Факт B.", source_row=3),
    ]

    reconciled, identity = yonote_sync._reconcile_exact_content_chunk_ids(
        old,
        fresh,
    )

    by_text = {str(record["text_clean"]): record["chunk_id"] for record in reconciled}
    assert by_text == {
        "Факт A.": "section-1",
        "Факт B.": "section-2",
        "Новый факт.": "section-3",
    }
    assert identity == {
        "raw_id_added": 1,
        "raw_id_removed": 0,
        "exact_content_rekeys": 0,
        "same_set_identity_rotations": 2,
        "ambiguous_exact_content_groups": 0,
    }


def test_identity_reconciliation_never_allocates_vacated_id_across_documents() -> None:
    old = [
        _scoped_yonote_record(
            "a-old",
            "Неизменный факт документа A.",
            document_id="doc-a",
        ),
        _scoped_yonote_record(
            "b-1",
            "Неизменный факт документа B.",
            document_id="doc-b",
        ),
    ]
    fresh = [
        _scoped_yonote_record(
            "a-new",
            "Неизменный факт документа A.",
            document_id="doc-a",
        ),
        _scoped_yonote_record(
            "b-1",
            "Новый факт документа B.",
            document_id="doc-b",
        ),
        _scoped_yonote_record(
            "b-2",
            "Неизменный факт документа B.",
            document_id="doc-b",
            source_row=2,
        ),
    ]

    reconciled, identity = yonote_sync._reconcile_exact_content_chunk_ids(
        old,
        fresh,
    )

    by_text = {str(record["text_clean"]): record["chunk_id"] for record in reconciled}
    assert by_text == {
        "Неизменный факт документа A.": "a-old",
        "Неизменный факт документа B.": "b-1",
        "Новый факт документа B.": "b-2",
    }
    assert identity == {
        "raw_id_added": 2,
        "raw_id_removed": 1,
        "exact_content_rekeys": 1,
        "same_set_identity_rotations": 1,
        "ambiguous_exact_content_groups": 0,
    }


def test_identity_reconciliation_preserves_swapped_exact_contents() -> None:
    old = [
        _scoped_yonote_record("section-1", "Факт A."),
        _scoped_yonote_record("section-2", "Факт B.", source_row=2),
    ]
    fresh = [
        _scoped_yonote_record("section-1", "Факт B."),
        _scoped_yonote_record("section-2", "Факт A.", source_row=2),
    ]

    reconciled, identity = yonote_sync._reconcile_exact_content_chunk_ids(
        old,
        fresh,
    )

    assert {
        str(record["text_clean"]): record["chunk_id"] for record in reconciled
    } == {"Факт A.": "section-1", "Факт B.": "section-2"}
    assert identity["same_set_identity_rotations"] == 2
    assert identity["exact_content_rekeys"] == 0
    assert len({record["chunk_id"] for record in reconciled}) == 2


def test_identity_reconciliation_is_order_independent_and_skips_ambiguity() -> None:
    old = [
        _scoped_yonote_record("old-a", "Уникальный факт A."),
        _scoped_yonote_record("old-b1", "Повторяемый факт.", source_row=2),
        _scoped_yonote_record("old-b2", "Повторяемый факт.", source_row=3),
    ]
    fresh = [
        _scoped_yonote_record("fresh-a", "Уникальный факт A."),
        _scoped_yonote_record("fresh-b1", "Повторяемый факт.", source_row=2),
        _scoped_yonote_record("fresh-b2", "Повторяемый факт.", source_row=3),
    ]

    forward, forward_identity = yonote_sync._reconcile_exact_content_chunk_ids(
        old,
        fresh,
    )
    reversed_result, reversed_identity = (
        yonote_sync._reconcile_exact_content_chunk_ids(
            list(reversed(old)),
            list(reversed(fresh)),
        )
    )

    forward_mapping = {
        str(record["text_clean"]) + ":" + str(record.get("source_row")): record[
            "chunk_id"
        ]
        for record in forward
    }
    reversed_mapping = {
        str(record["text_clean"]) + ":" + str(record.get("source_row")): record[
            "chunk_id"
        ]
        for record in reversed_result
    }
    assert forward_mapping == reversed_mapping
    assert forward_identity == reversed_identity
    assert forward_identity["exact_content_rekeys"] == 1
    assert forward_identity["ambiguous_exact_content_groups"] == 1
    ambiguous_ids = {
        record["chunk_id"]
        for record in forward
        if record["text_clean"] == "Повторяемый факт."
    }
    assert ambiguous_ids == {
        "fresh-b1",
        "fresh-b2",
    }


def test_identity_reconciliation_never_matches_across_document_scope() -> None:
    old = [_scoped_yonote_record("old", "Одинаковый текст.", document_id="old-doc")]
    fresh = [
        _scoped_yonote_record("fresh", "Одинаковый текст.", document_id="new-doc")
    ]

    reconciled, identity = yonote_sync._reconcile_exact_content_chunk_ids(
        old,
        fresh,
    )

    assert [record["chunk_id"] for record in reconciled] == ["fresh"]
    assert identity["exact_content_rekeys"] == 0
    assert identity["same_set_identity_rotations"] == 0


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
                "text_clean": "Added grounded Yonote answer",
                "status": "published",
                "category": "forums",
                "forum_normalized": "Amur",
                "source_type": "yonote",
                "source_url": "https://rossmol.yonote.ru/doc/added",
                "source_document_id": "doc-added",
                "source_document_updated_at": "2026-08-20T12:00:00Z",
            }
        ]

    monkeypatch.setattr(yonote_sync, "_load_fresh_yonote_records", fake_load)

    receipt_dir = tmp_path / "receipts"
    preview = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda *_args, **_kwargs: pytest.fail("Apply must not fetch Yonote again"),
    )
    receipt = preview["receipt"]
    report = yonote_sync.apply_sync(
        seed_path,
        receipt_dir=receipt_dir,
        receipt_id=receipt["id"],
        receipt_sha256=receipt["sha256"],
    )
    stored = json.loads(seed_path.read_text(encoding="utf-8"))

    assert report["applied"] is True
    assert report["index_required"] is True
    assert report["receipt"]["consumed"] is True
    assert [record["chunk_id"] for record in stored] == ["xlsx_base", "yonote_added"]
    assert not list(receipt_dir.glob("*.json"))
    assert len(list(receipt_dir.glob("*.applied"))) == 1


def test_apply_sync_rejects_receipt_from_pre_audit_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    original_seed = seed_path.read_bytes()
    preview = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    receipt = preview["receipt"]
    assert isinstance(receipt, dict)
    active_path = next(receipt_dir.glob("*.json"))
    payload = json.loads(active_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "yonote-sync-receipt-v1"
    rendered = yonote_sync._canonical_json_bytes(payload)
    legacy_sha256 = sha256(rendered).hexdigest()
    legacy_path = receipt_dir / f"{receipt['id']}.{legacy_sha256}.json"
    active_path.unlink()
    legacy_path.write_bytes(rendered)

    with pytest.raises(
        yonote_sync.YonoteReceiptError,
        match="unsupported preview receipt schema",
    ):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=str(receipt["id"]),
            receipt_sha256=legacy_sha256,
        )

    assert seed_path.read_bytes() == original_seed
    assert legacy_path.is_file()


def test_apply_sync_treats_post_replace_write_exception_as_committed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    preview = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    receipt = preview["receipt"]
    assert isinstance(receipt, dict)
    real_write = yonote_sync.write_seed_records
    write_calls = 0

    def fail_after_replace(path: Path, records: list[dict[str, object]]) -> None:
        nonlocal write_calls
        write_calls += 1
        real_write(path, records)
        raise OSError("simulated post-replace durability failure")

    monkeypatch.setattr(yonote_sync, "write_seed_records", fail_after_replace)

    applied = yonote_sync.apply_sync(
        seed_path,
        receipt_dir=receipt_dir,
        receipt_id=str(receipt["id"]),
        receipt_sha256=str(receipt["sha256"]),
    )
    replayed = yonote_sync.apply_sync(
        seed_path,
        receipt_dir=receipt_dir,
        receipt_id=str(receipt["id"]),
        receipt_sha256=str(receipt["sha256"]),
    )

    assert write_calls == 1
    assert yonote_sync._file_sha256(seed_path) == preview["hashes"][
        "merged_seed_sha256"
    ]
    assert applied["applied"] is True
    assert applied["receipt"]["state"] == "applied"
    assert applied["receipt"]["finalization_pending"] is False
    assert replayed["receipt"]["idempotent"] is True
    assert len(list(receipt_dir.glob("*.applied"))) == 1
    assert not list(receipt_dir.glob("*.json"))
    assert not list(receipt_dir.glob("*.applying"))


def test_apply_sync_recovers_after_finalize_rename_failure_without_rewrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    preview = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    receipt = preview["receipt"]
    assert isinstance(receipt, dict)
    real_write = yonote_sync.write_seed_records
    real_replace = yonote_sync.os.replace
    write_calls = 0
    finalize_failures = 0

    def counted_write(path: Path, records: list[dict[str, object]]) -> None:
        nonlocal write_calls
        write_calls += 1
        real_write(path, records)

    def fail_first_finalize(source: object, target: object) -> None:
        nonlocal finalize_failures
        source_path = Path(source)  # type: ignore[arg-type]
        target_path = Path(target)  # type: ignore[arg-type]
        if (
            source_path.suffix == ".applying"
            and target_path.suffix == ".applied"
            and finalize_failures == 0
        ):
            finalize_failures += 1
            raise OSError("simulated finalize rename failure")
        real_replace(source, target)

    monkeypatch.setattr(yonote_sync, "write_seed_records", counted_write)
    monkeypatch.setattr(yonote_sync.os, "replace", fail_first_finalize)

    applied = yonote_sync.apply_sync(
        seed_path,
        receipt_dir=receipt_dir,
        receipt_id=str(receipt["id"]),
        receipt_sha256=str(receipt["sha256"]),
    )

    assert applied["applied"] is True
    assert applied["receipt"]["state"] == "applying"
    assert applied["receipt"]["finalization_pending"] is True
    assert len(list(receipt_dir.glob("*.applying"))) == 1

    monkeypatch.setattr(
        yonote_sync,
        "_validate_merged_seed",
        lambda *_args, **_kwargs: {
            "status": "STOP",
            "codes": {"forum_text_conflict": 1},
            "errors_total": 1,
        },
    )

    recovered = yonote_sync.apply_sync(
        seed_path,
        receipt_dir=receipt_dir,
        receipt_id=str(receipt["id"]),
        receipt_sha256=str(receipt["sha256"]),
    )

    assert write_calls == 1
    assert finalize_failures == 1
    assert recovered["applied"] is True
    assert recovered["receipt"]["idempotent"] is True
    assert recovered["receipt"]["state"] == "applied"
    assert recovered["receipt"]["finalization_pending"] is False
    assert len(list(receipt_dir.glob("*.applied"))) == 1
    assert not list(receipt_dir.glob("*.applying"))


def test_apply_sync_recovers_active_receipt_when_seed_is_already_committed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    preview = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    receipt = preview["receipt"]
    assert isinstance(receipt, dict)
    active_path = next(receipt_dir.glob("*.json"))
    sealed = json.loads(active_path.read_text(encoding="utf-8"))
    yonote_sync.write_seed_records(seed_path, sealed["merged_records"])

    def reject_second_write(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("committed seed must not be written again")

    monkeypatch.setattr(yonote_sync, "write_seed_records", reject_second_write)

    recovered = yonote_sync.apply_sync(
        seed_path,
        receipt_dir=receipt_dir,
        receipt_id=str(receipt["id"]),
        receipt_sha256=str(receipt["sha256"]),
    )

    assert recovered["applied"] is True
    assert recovered["receipt"]["idempotent"] is True
    assert recovered["receipt"]["state"] == "applied"
    assert len(list(receipt_dir.glob("*.applied"))) == 1
    assert not list(receipt_dir.glob("*.json"))


def test_apply_sync_restores_active_receipt_when_write_fails_before_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    original_seed = seed_path.read_bytes()
    preview = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    receipt = preview["receipt"]
    assert isinstance(receipt, dict)

    def fail_before_commit(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated pre-commit failure")

    monkeypatch.setattr(yonote_sync, "write_seed_records", fail_before_commit)

    with pytest.raises(OSError, match="pre-commit"):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=str(receipt["id"]),
            receipt_sha256=str(receipt["sha256"]),
        )

    assert seed_path.read_bytes() == original_seed
    assert len(list(receipt_dir.glob("*.json"))) == 1
    assert not list(receipt_dir.glob("*.applying"))
    assert not list(receipt_dir.glob("*.applied"))


@pytest.mark.parametrize("durable_state", ["applying", "applied"])
def test_apply_sync_rejects_tampered_durable_receipt_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    durable_state: str,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    original_seed = seed_path.read_bytes()
    preview = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    receipt = preview["receipt"]
    assert isinstance(receipt, dict)
    active_path = next(receipt_dir.glob("*.json"))
    durable_path = active_path.with_suffix(f".{durable_state}")
    active_path.replace(durable_path)
    durable_path.write_bytes(durable_path.read_bytes() + b"tampered")

    with pytest.raises(yonote_sync.YonoteReceiptError, match="hash mismatch"):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=str(receipt["id"]),
            receipt_sha256=str(receipt["sha256"]),
        )

    assert seed_path.read_bytes() == original_seed


@pytest.mark.parametrize("durable_state", ["applying", "applied"])
def test_apply_sync_rejects_mismatched_id_in_durable_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    durable_state: str,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    original_seed = seed_path.read_bytes()
    preview = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    receipt = preview["receipt"]
    assert isinstance(receipt, dict)
    active_path = next(receipt_dir.glob("*.json"))
    payload = json.loads(active_path.read_text(encoding="utf-8"))
    payload["receipt_id"] = "f" * 32
    rendered = yonote_sync._canonical_json_bytes(payload)
    tampered_sha256 = sha256(rendered).hexdigest()
    durable_path = receipt_dir / (
        f"{receipt['id']}.{tampered_sha256}.{durable_state}"
    )
    active_path.unlink()
    durable_path.write_bytes(rendered)

    with pytest.raises(yonote_sync.YonoteReceiptError, match="id mismatch"):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=str(receipt["id"]),
            receipt_sha256=tampered_sha256,
        )

    assert seed_path.read_bytes() == original_seed


def test_preview_sync_reports_semantic_stop_without_receipt_or_seed_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    registry_path = tmp_path / "forums_registry.json"
    _write_seed(seed_path)
    registry_path.write_text(
        json.dumps(
            [
                {"name": "Амур", "normalized": "Амур", "aliases": []},
                {"name": "Ростов", "normalized": "Ростов", "aliases": []},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    original = seed_path.read_text(encoding="utf-8")

    def fake_load(_settings, *, limit_documents):
        assert limit_documents is None
        return [object()], [
            {
                "chunk_id": "yonote_wrong_event",
                "text_clean": "Регистрация на форуме «Ростов» закрыта.",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Амур",
                "source_type": "yonote",
            }
        ]

    monkeypatch.setattr(yonote_sync, "_load_fresh_yonote_records", fake_load)

    receipt_dir = tmp_path / "receipts"
    report = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    assert report["snapshot_scope"] == "full"
    assert report["semantic_integrity"] == {
        "status": "STOP",
        "codes": {"forum_text_conflict": 1},
        "errors_total": 1,
        "affected_chunk_ids": {"forum_text_conflict": ["yonote_wrong_event"]},
    }
    assert report["snapshot_safety"]["status"] == "GO"
    assert report["snapshot_safety"]["reasons"] == []
    assert report["receipt"] == {
        "apply_ready": False,
        "reason": "semantic_integrity_failed",
    }
    assert set(report["hashes"]) == {
        "current_seed_sha256",
        "yonote_snapshot_sha256",
        "merged_seed_sha256",
    }
    assert seed_path.read_text(encoding="utf-8") == original
    assert not seed_path.with_name(f"{seed_path.name}.tmp").exists()
    assert not receipt_dir.exists()


def test_full_semantic_stop_invalidates_older_active_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    first = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    first_receipt = first["receipt"]
    assert isinstance(first_receipt, dict)
    assert len(list(receipt_dir.glob("*.json"))) == 1

    monkeypatch.setattr(
        yonote_sync,
        "_validate_merged_seed",
        lambda *_args, **_kwargs: {
            "status": "STOP",
            "codes": {"forum_text_conflict": 1},
            "errors_total": 1,
        },
    )
    stopped = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    assert stopped["receipt"] == {
        "apply_ready": False,
        "reason": "semantic_integrity_failed",
    }
    assert not list(receipt_dir.glob("*.json"))
    with pytest.raises(yonote_sync.YonoteReceiptNotFound):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=str(first_receipt["id"]),
            receipt_sha256=str(first_receipt["sha256"]),
        )


def test_full_chunk_audit_stop_invalidates_older_active_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    first = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    first_receipt = first["receipt"]
    assert isinstance(first_receipt, dict)

    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: (
            [object()],
            [
                {
                    "chunk_id": "yonote_incomplete",
                    "text_clean": "Опубликованный, но неполный чанк Yonote.",
                    "status": "published",
                    "category": "general",
                    "source_type": "yonote",
                    "source_document_id": "doc-incomplete",
                    "source_document_updated_at": "2026-08-20T12:00:00Z",
                }
            ],
        ),
    )
    stopped = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    assert stopped["semantic_integrity"]["status"] == "GO"
    assert stopped["snapshot_safety"]["status"] == "GO"
    assert stopped["chunk_audit"]["findings"]["missing_source_url"] == 1
    assert stopped["receipt"] == {
        "apply_ready": False,
        "reason": "chunk_audit_failed",
    }
    assert not list(receipt_dir.glob("*.json"))
    with pytest.raises(yonote_sync.YonoteReceiptNotFound):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=str(first_receipt["id"]),
            receipt_sha256=str(first_receipt["sha256"]),
        )


def test_preview_sync_keeps_structural_validation_as_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    original = seed_path.read_bytes()
    duplicate = {
        "chunk_id": "yonote_duplicate",
        "text_clean": "Опубликованный подтверждённый факт.",
        "status": "published",
        "category": "general",
        "source_type": "yonote",
    }
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: (
            [object(), object()],
            [duplicate, dict(duplicate)],
        ),
    )

    with pytest.raises(ValueError, match="duplicate_chunk_id"):
        yonote_sync.preview_sync(
            seed_path,
            SimpleNamespace(),
            receipt_dir=receipt_dir,
        )

    assert seed_path.read_bytes() == original
    assert not receipt_dir.exists()


def test_invalid_full_snapshot_invalidates_older_active_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    first = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    first_receipt = first["receipt"]
    assert isinstance(first_receipt, dict)
    assert len(list(receipt_dir.glob("*.json"))) == 1
    duplicate = {
        "chunk_id": "yonote_duplicate",
        "text_clean": "Опубликованный подтверждённый факт.",
        "status": "published",
        "category": "general",
        "source_type": "yonote",
    }
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: (
            [object(), object()],
            [duplicate, dict(duplicate)],
        ),
    )

    with pytest.raises(ValueError, match="duplicate_chunk_id"):
        yonote_sync.preview_sync(
            seed_path,
            SimpleNamespace(),
            receipt_dir=receipt_dir,
        )

    assert not list(receipt_dir.glob("*.json"))
    with pytest.raises(yonote_sync.YonoteReceiptNotFound):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=str(first_receipt["id"]),
            receipt_sha256=str(first_receipt["sha256"]),
        )


def test_real_built_duplicate_snapshot_invalidates_active_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    real_load_fresh = yonote_sync._load_fresh_yonote_records
    first = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    first_receipt = first["receipt"]
    assert isinstance(first_receipt, dict)
    duplicate_document = yonote_sync.YonoteDocument(
        id="duplicate-document",
        title="Опубликованный документ",
        text="Описание\nПодтверждённый опубликованный факт для проверки снимка.",
        collection_id="collection-1",
        collection_name="Росмолодёжь: мероприятия",
        url="/doc/duplicate-document",
        url_id="duplicate-document",
        parent_document_id=None,
        path_titles=("Опубликованный документ",),
        updated_at="2026-08-21T00:00:00Z",
        created_at="2026-08-20T00:00:00Z",
        document_type="document",
    )
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        real_load_fresh,
    )
    monkeypatch.setattr(
        yonote_sync,
        "load_yonote_documents_from_settings",
        lambda *_args, **_kwargs: [duplicate_document, duplicate_document],
    )

    with pytest.raises(ValueError, match="duplicate_chunk_id"):
        yonote_sync.preview_sync(
            seed_path,
            SimpleNamespace(yonote_base_url="https://rossmol.yonote.ru"),
            receipt_dir=receipt_dir,
        )

    assert not list(receipt_dir.glob("*.json"))
    with pytest.raises(yonote_sync.YonoteReceiptNotFound):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=str(first_receipt["id"]),
            receipt_sha256=str(first_receipt["sha256"]),
        )


def test_apply_sync_rechecks_semantic_integrity_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    original = seed_path.read_bytes()
    preview = _seal_fresh_receipt(
        monkeypatch,
        seed_path=seed_path,
        receipt_dir=receipt_dir,
    )
    receipt = preview["receipt"]
    assert isinstance(receipt, dict)
    monkeypatch.setattr(
        yonote_sync,
        "_validate_merged_seed",
        lambda *_args, **_kwargs: {
            "status": "STOP",
            "codes": {"forum_text_conflict": 1},
            "errors_total": 1,
        },
    )

    with pytest.raises(
        yonote_sync.YonoteReceiptError,
        match="failed semantic integrity",
    ):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=str(receipt["id"]),
            receipt_sha256=str(receipt["sha256"]),
        )

    assert seed_path.read_bytes() == original
    assert len(list(receipt_dir.glob("*.json"))) == 1
    assert not list(receipt_dir.glob("*.applying"))


def test_preview_rejects_concurrent_seed_change_without_sealing_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)

    def change_seed_during_pull(_settings, *, limit_documents):
        assert limit_documents is None
        records = json.loads(seed_path.read_text(encoding="utf-8"))
        records[0]["text_clean"] = "Manual editor change"
        seed_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        return [object()], [records[1]]

    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        change_seed_during_pull,
    )

    with pytest.raises(yonote_sync.YonoteReceiptConflict, match="during preview"):
        yonote_sync.preview_sync(
            seed_path,
            SimpleNamespace(),
            receipt_dir=receipt_dir,
        )

    assert json.loads(seed_path.read_text(encoding="utf-8"))[0]["text_clean"] == (
        "Manual editor change"
    )
    assert not receipt_dir.exists()


def test_semantic_stop_rechecks_concurrent_seed_change_before_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)

    def change_seed_during_pull(_settings, *, limit_documents):
        assert limit_documents is None
        records = json.loads(seed_path.read_text(encoding="utf-8"))
        records[0]["text_clean"] = "Concurrent editor change"
        seed_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        return [object()], [records[1]]

    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        change_seed_during_pull,
    )
    monkeypatch.setattr(
        yonote_sync,
        "_validate_merged_seed",
        lambda *_args, **_kwargs: {
            "status": "STOP",
            "codes": {"forum_text_conflict": 1},
            "errors_total": 1,
        },
    )

    with pytest.raises(yonote_sync.YonoteReceiptConflict, match="during preview"):
        yonote_sync.preview_sync(
            seed_path,
            SimpleNamespace(),
            receipt_dir=receipt_dir,
        )

    assert json.loads(seed_path.read_text(encoding="utf-8"))[0]["text_clean"] == (
        "Concurrent editor change"
    )
    assert not receipt_dir.exists()


def test_preview_reader_has_server_side_duration_and_size_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_load(_settings: object, **kwargs: object) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        yonote_sync,
        "load_yonote_documents_from_settings",
        fake_load,
    )
    monkeypatch.setattr(
        yonote_sync,
        "build_records_from_api_documents",
        lambda *_args, **_kwargs: [],
    )

    documents, records = yonote_sync._load_fresh_yonote_records(
        SimpleNamespace(yonote_base_url="https://yonote.example"),
        limit_documents=None,
    )

    assert documents == []
    assert records == []
    assert captured == {
        "limit_documents": None,
        "include_empty": True,
        "max_duration_seconds": yonote_sync.MAX_PREVIEW_DURATION_SECONDS,
        "max_total_text_bytes": yonote_sync.MAX_PREVIEW_TEXT_BYTES,
    }


def test_full_preview_blocks_empty_yonote_snapshot_without_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    original = seed_path.read_bytes()
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: ([object()], []),
    )

    report = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    assert report["snapshot_scope"] == "full"
    assert report["snapshot_safety"]["status"] == "STOP"
    assert report["snapshot_safety"]["reasons"] == ["yonote_snapshot_empty"]
    assert report["receipt"] == {
        "apply_ready": False,
        "reason": "destructive_snapshot_requires_owner_waiver",
    }
    assert seed_path.read_bytes() == original
    assert not receipt_dir.exists()


def test_full_preview_blocks_mass_removal_without_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    records = [
        {
            "chunk_id": f"yonote-{index}",
            "text_clean": f"Published fact {index}",
            "status": "published",
            "category": "general",
            "source_type": "yonote",
        }
        for index in range(40)
    ]
    seed_path.write_text(json.dumps(records), encoding="utf-8")
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: ([object()] * 20, records[:20]),
    )

    report = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    assert report["removed"] == 20
    assert report["snapshot_safety"]["status"] == "STOP"
    assert report["snapshot_safety"]["reasons"] == [
        "removal_ratio_limit_exceeded"
    ]
    assert report["receipt"]["apply_ready"] is False
    assert not receipt_dir.exists()


def test_apply_sync_rejects_seed_changed_after_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)

    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: (
            [object()],
            [
                {
                    "chunk_id": "yonote_fresh",
                    "text_clean": "Fresh grounded answer",
                    "status": "published",
                    "category": "general",
                    "source_type": "yonote",
                    "source_url": "https://rossmol.yonote.ru/doc/fresh",
                    "source_document_id": "doc-fresh",
                    "source_document_updated_at": "2026-08-20T12:00:00Z",
                }
            ],
        ),
    )
    preview = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )
    original_preview_hash = preview["hashes"]["current_seed_sha256"]
    seed_path.write_text(seed_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    receipt = preview["receipt"]

    with pytest.raises(yonote_sync.YonoteReceiptConflict, match="changed after preview"):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=receipt["id"],
            receipt_sha256=receipt["sha256"],
        )

    assert yonote_sync._file_sha256(seed_path) != original_preview_hash
    assert len(list(receipt_dir.glob("*.json"))) == 1


def test_full_preview_reports_hashes_chunk_audit_and_index_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    fresh = {
        "chunk_id": "yonote_fresh",
        "text_clean": "Новый подтверждённый ответ из опубликованного документа.",
        "status": "published",
        "category": "general",
        "source_type": "yonote",
        "source_url": "https://example.test/source",
        "source_document_id": "doc-1",
        "source_document_updated_at": "2026-08-20T00:00:00Z",
    }
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: ([object()], [fresh]),
    )

    report = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=tmp_path / "receipts",
    )

    assert report["snapshot_scope"] == "full"
    assert report["receipt"]["apply_ready"] is True
    assert len(report["receipt"]["id"]) == 32
    assert len(report["receipt"]["sha256"]) == 64
    assert len(report["hashes"]["current_seed_sha256"]) == 64
    assert len(report["hashes"]["yonote_snapshot_sha256"]) == 64
    assert len(report["hashes"]["merged_seed_sha256"]) == 64
    assert report["chunk_audit"]["findings"] == {
        "empty_text": 0,
        "too_short_under_20_chars": 0,
        "oversized_over_max_chars": 0,
        "duplicate_text_groups": 0,
        "missing_source_url": 0,
        "missing_source_document_id": 0,
        "missing_source_updated_at": 0,
    }
    assert report["chunk_audit"]["policy_version"] == "yonote-chunk-audit-v1"
    assert report["chunk_audit"]["status"] == "GO"
    assert report["chunk_audit"]["blocking"] == {
        "total": 0,
        "findings": {
            "empty_text": 0,
            "oversized_over_max_chars": 0,
            "missing_source_url": 0,
                "missing_source_document_id": 0,
                "missing_source_updated_at": 0,
                "existing_documents_without_chunks": 0,
                "new_substantive_documents_without_chunks": 0,
                "unclassified_documents_without_chunks": 0,
        },
    }
    assert report["index_projection"] == {
        "current_published_points": 3,
        "expected_published_points": 2,
        "stale_prune_required": True,
        "full_reindex_required": True,
    }


def test_chunk_audit_allows_new_empty_document_but_blocks_existing_loss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    existing = _scoped_yonote_record(
        "existing",
        "Существующий подтверждённый опубликованный факт.",
        document_id="existing-document",
    )
    seed_path.write_text(json.dumps([existing], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: (
            [
                SimpleNamespace(
                    id="existing-document",
                    collection_id="collection-1",
                    text=str(existing["text_clean"]),
                ),
                SimpleNamespace(
                    id="new-empty-document",
                    collection_id="collection-1",
                    text="",
                ),
            ],
            [dict(existing)],
        ),
    )

    advisory = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    assert advisory["chunk_audit"]["status"] == "GO"
    assert advisory["chunk_audit"]["documents"]["new_without_chunks"] == 1
    assert advisory["chunk_audit"]["documents"][
        "new_substantive_without_chunks"
    ] == 0
    assert advisory["chunk_audit"]["advisory"]["findings"][
        "new_documents_without_chunks"
    ] == 1
    assert advisory["receipt"]["apply_ready"] is True

    second = _scoped_yonote_record(
        "lost",
        "Другой существующий подтверждённый опубликованный факт.",
        document_id="lost-document",
    )
    seed_path.write_text(
        json.dumps([existing, second], ensure_ascii=False),
        encoding="utf-8",
    )
    receipt_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: (
            [
                SimpleNamespace(
                    id="existing-document",
                    collection_id="collection-1",
                    text=str(existing["text_clean"]),
                ),
                SimpleNamespace(
                    id="lost-document",
                    collection_id="collection-1",
                    text=str(second["text_clean"]),
                ),
            ],
            [dict(existing)],
        ),
    )

    blocked = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    assert blocked["snapshot_safety"]["status"] == "GO"
    assert blocked["chunk_audit"]["status"] == "STOP"
    assert blocked["chunk_audit"]["documents"]["existing_without_chunks"] == 1
    assert blocked["chunk_audit"]["blocking"]["findings"][
        "existing_documents_without_chunks"
    ] == 1
    assert blocked["receipt"] == {
        "apply_ready": False,
        "reason": "chunk_audit_failed",
    }


def test_chunk_audit_blocks_substantive_new_document_without_chunks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    seed_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: (
            [
                SimpleNamespace(
                    id="substantive-new",
                    collection_id="collection-1",
                    text="Новый опубликованный документ содержит важный подтверждённый факт.",
                )
            ],
            [],
        ),
    )

    report = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    audit = report["chunk_audit"]
    assert audit["status"] == "STOP"
    assert audit["blocking"]["findings"][
        "new_substantive_documents_without_chunks"
    ] == 1
    assert audit["documents"]["without_chunks"] == 1
    assert audit["documents"]["existing_without_chunks"] == 0
    assert audit["documents"]["new_without_chunks"] == 0
    assert audit["documents"]["new_substantive_without_chunks"] == 1
    assert audit["documents"]["unclassified_without_chunks"] == 0
    sample = audit["documents"]["without_chunks_sample"]
    assert sample[0]["source_collection_id"] == "collection-1"
    assert sample[0]["source_document_id"] == "substantive-new"
    assert sample[0]["reason"] == "new_substantive_document_without_chunks"
    assert sample[0]["cleaned_chars"] >= 20
    assert report["receipt"] == {
        "apply_ready": False,
        "reason": "chunk_audit_failed",
    }
    assert not receipt_dir.exists()


def test_chunk_audit_keeps_same_document_id_isolated_by_collection() -> None:
    first = {
        **_scoped_yonote_record("first", "Факт первой коллекции."),
        "source_collection_id": "collection-a",
    }
    second = {
        **_scoped_yonote_record("second", "Факт второй коллекции."),
        "source_collection_id": "collection-b",
    }

    audit = yonote_sync._chunk_audit(
        documents_count=2,
        loaded_documents=[
            SimpleNamespace(
                id="doc-1",
                collection_id="collection-a",
                text=first["text_clean"],
            ),
            SimpleNamespace(
                id="doc-1",
                collection_id="collection-b",
                text=second["text_clean"],
            ),
        ],
        current_yonote_records=[first, second],
        fresh_yonote_records=[second],
        merged_records=[second],
    )

    assert audit["status"] == "STOP"
    assert audit["documents"]["with_chunks"] == 1
    assert audit["documents"]["existing_without_chunks"] == 1
    assert audit["documents"]["without_chunks"] == 1
    assert audit["documents"]["without_chunks_sample"][0][
        "source_collection_id"
    ] == "collection-a"


def test_full_preview_replaces_superseded_active_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: (
            [object()],
            [
                {
                    "chunk_id": "yonote_fresh",
                    "text_clean": "Fresh grounded answer",
                    "status": "published",
                    "category": "general",
                    "source_type": "yonote",
                    "source_url": "https://rossmol.yonote.ru/doc/fresh",
                    "source_document_id": "doc-fresh",
                    "source_document_updated_at": "2026-08-20T12:00:00Z",
                }
            ],
        ),
    )

    first = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )
    second = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    active_receipts = list(receipt_dir.glob("*.json"))
    assert first["receipt"]["id"] != second["receipt"]["id"]
    assert len(active_receipts) == 1
    assert active_receipts[0].name.startswith(second["receipt"]["id"])
    with pytest.raises(yonote_sync.YonoteReceiptNotFound):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=first["receipt"]["id"],
            receipt_sha256=first["receipt"]["sha256"],
        )


def test_apply_sync_rejects_expired_receipt_without_changing_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    original = seed_path.read_bytes()
    monkeypatch.setattr(yonote_sync, "RECEIPT_TTL", timedelta(seconds=-1))
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: (
            [object()],
            [
                {
                    "chunk_id": "yonote_fresh",
                    "text_clean": "Fresh grounded answer",
                    "status": "published",
                    "category": "general",
                    "source_type": "yonote",
                    "source_url": "https://rossmol.yonote.ru/doc/fresh",
                    "source_document_id": "doc-fresh",
                    "source_document_updated_at": "2026-08-20T12:00:00Z",
                }
            ],
        ),
    )
    preview = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    with pytest.raises(yonote_sync.YonoteReceiptExpired):
        yonote_sync.apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=preview["receipt"]["id"],
            receipt_sha256=preview["receipt"]["sha256"],
        )

    assert seed_path.read_bytes() == original
    assert len(list(receipt_dir.glob("*.json"))) == 0


def test_preview_marks_embedding_and_source_metadata_change_for_reindex(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    current = json.loads(seed_path.read_text(encoding="utf-8"))
    current[1].update(
        {
            "intent_examples": ["Старая формулировка"],
            "source_category": "Старый раздел",
            "source_document_id": "doc-1",
            "source_heading_path": ["Амур", "Старый раздел"],
        }
    )
    seed_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    fresh = {
        **current[1],
        "intent_examples": ["Новая формулировка"],
        "source_category": "Новый раздел",
        "source_heading_path": ["Амур", "Новый раздел"],
    }
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: ([object()], [fresh]),
    )

    report = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=tmp_path / "receipts",
    )

    assert report["changed"] == 1
    assert report["index_required"] is True
    assert report["changed_items"][0]["changed_fields"] == [
        "intent_examples",
        "source_category",
        "source_heading_path",
    ]
    assert report["index_projection"]["full_reindex_required"] is True


def test_preview_marks_unlisted_behavior_metadata_change_for_reindex(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    current = json.loads(seed_path.read_text(encoding="utf-8"))
    current[1]["has_conditional_logic"] = False
    current[1]["conditions_summary"] = ["Старая привязка условия"]
    seed_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    fresh = {
        **current[1],
        "has_conditional_logic": True,
        "conditions_summary": ["Новая привязка условия"],
    }
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: ([object()], [fresh]),
    )

    report = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=tmp_path / "receipts",
    )

    assert report["changed"] == 1
    assert report["changed_items"][0]["changed_fields"] == [
        "conditions_summary",
        "has_conditional_logic",
    ]
    assert report["index_required"] is True


def test_next_day_preview_preserves_unchanged_yonote_record_and_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    current = json.loads(seed_path.read_text(encoding="utf-8"))[:2]
    current_yonote = current[1]
    current_yonote.update(
        {
            "source_url": "https://yonote.example/doc-1",
            "source_document_id": "doc-1",
            "source_document_updated_at": None,
            "extraction_date": "2026-08-19",
            "updated_at": "2026-08-19",
        }
    )
    seed_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    fresh = {
        **current_yonote,
        "extraction_date": "2026-08-20",
        "updated_at": "2026-08-20",
    }
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: ([object()], [dict(fresh)]),
    )

    first = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )
    second = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    assert first["changed"] == second["changed"] == 0
    assert first["index_required"] is second["index_required"] is False
    assert first["hashes"]["yonote_snapshot_sha256"] == second["hashes"][
        "yonote_snapshot_sha256"
    ]
    assert first["hashes"]["merged_seed_sha256"] == second["hashes"][
        "merged_seed_sha256"
    ]
    assert first["chunk_audit"]["findings"]["missing_source_updated_at"] == 1
    assert first["receipt"] == {
        "apply_ready": False,
        "reason": "chunk_audit_failed",
    }
    assert not list(receipt_dir.glob("*.json"))


def test_provider_snapshot_hash_is_independent_of_current_ids_and_pull_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_seed = tmp_path / "first.json"
    second_seed = tmp_path / "second.json"
    stable_text = "Один и тот же опубликованный подтверждённый факт."
    first_seed.write_text(
        json.dumps(
            [_scoped_yonote_record("old-a", stable_text)],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    second_seed.write_text(
        json.dumps(
            [_scoped_yonote_record("old-b", stable_text)],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    call_count = 0

    def fake_load(_settings: object, *, limit_documents: int | None):
        nonlocal call_count
        assert limit_documents is None
        call_count += 1
        extraction_date = f"2026-08-{20 + call_count}"
        fresh = {
            **_scoped_yonote_record("fresh-provider-id", stable_text),
            "extraction_date": extraction_date,
        }
        return [SimpleNamespace(id="doc-1", text=stable_text)], [fresh]

    monkeypatch.setattr(yonote_sync, "_load_fresh_yonote_records", fake_load)

    first = yonote_sync.preview_sync(
        first_seed,
        SimpleNamespace(),
        receipt_dir=tmp_path / "first-receipts",
    )
    second = yonote_sync.preview_sync(
        second_seed,
        SimpleNamespace(),
        receipt_dir=tmp_path / "second-receipts",
    )

    assert first["hashes"]["yonote_snapshot_sha256"] == second["hashes"][
        "yonote_snapshot_sha256"
    ]
    for report, receipt_dir in (
        (first, tmp_path / "first-receipts"),
        (second, tmp_path / "second-receipts"),
    ):
        receipt = json.loads(next(receipt_dir.glob("*.json")).read_text("utf-8"))
        assert receipt["bindings"]["yonote_snapshot_sha256"] == report["hashes"][
            "yonote_snapshot_sha256"
        ]


def test_provider_snapshot_hash_ignores_fallback_dates_but_tracks_source_change() -> None:
    base = {
        **_scoped_yonote_record("provider-id", "Подтверждённый факт."),
        "source_document_updated_at": None,
        "extraction_date": "2026-08-20",
        "updated_at": "2026-08-20",
    }
    next_day = {
        **base,
        "extraction_date": "2026-08-21",
        "updated_at": "2026-08-21",
    }
    changed_source = {
        **next_day,
        "source_url": "https://rossmol.yonote.ru/doc/changed",
    }
    second_record = {
        **_scoped_yonote_record(
            "provider-id-2",
            "Другой подтверждённый факт.",
            document_id="doc-2",
        ),
        "extraction_date": "2026-08-20",
    }

    baseline_hash = yonote_sync._provider_snapshot_sha256([base])

    assert yonote_sync._provider_snapshot_sha256([next_day]) == baseline_hash
    assert yonote_sync._provider_snapshot_sha256([changed_source]) != baseline_hash
    assert yonote_sync._provider_snapshot_sha256(
        [base, second_record]
    ) == yonote_sync._provider_snapshot_sha256([second_record, base])


def test_next_day_preview_keeps_real_content_and_source_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    receipt_dir = tmp_path / "receipts"
    _write_seed(seed_path)
    current = json.loads(seed_path.read_text(encoding="utf-8"))[:2]
    current_yonote = current[1]
    current_yonote.update(
        {
            "source_url": "https://yonote.example/old",
            "source_document_id": "doc-1",
            "source_document_updated_at": "2026-08-19T10:00:00Z",
            "extraction_date": "2026-08-19",
            "updated_at": "2026-08-19T10:00:00Z",
        }
    )
    seed_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    fresh = {
        **current_yonote,
        "text_clean": "New grounded Yonote answer",
        "source_url": "https://yonote.example/new",
        "source_document_updated_at": "2026-08-20T10:00:00Z",
        "extraction_date": "2026-08-20",
        "updated_at": "2026-08-20T10:00:00Z",
    }
    monkeypatch.setattr(
        yonote_sync,
        "_load_fresh_yonote_records",
        lambda _settings, *, limit_documents: ([object()], [fresh]),
    )

    report = yonote_sync.preview_sync(
        seed_path,
        SimpleNamespace(),
        receipt_dir=receipt_dir,
    )

    assert report["changed"] == 1
    assert report["index_required"] is True
    assert {
        "text_clean",
        "source_url",
        "source_document_updated_at",
        "extraction_date",
        "updated_at",
    }.issubset(report["changed_items"][0]["changed_fields"])
    active_receipt = json.loads(next(receipt_dir.glob("*.json")).read_text("utf-8"))
    stored_yonote = next(
        record
        for record in active_receipt["merged_records"]
        if record["chunk_id"] == current_yonote["chunk_id"]
    )
    assert stored_yonote["text_clean"] == "New grounded Yonote answer"
    assert stored_yonote["source_url"] == "https://yonote.example/new"
    assert stored_yonote["extraction_date"] == "2026-08-20"
