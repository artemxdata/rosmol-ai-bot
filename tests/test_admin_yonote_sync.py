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


def test_preview_sync_rejects_semantic_conflict_without_changing_seed(
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

    with pytest.raises(ValueError, match="forum_text_conflict=1"):
        yonote_sync.preview_sync(
            seed_path,
            SimpleNamespace(),
            receipt_dir=tmp_path / "receipts",
        )

    assert seed_path.read_text(encoding="utf-8") == original
    assert not seed_path.with_name(f"{seed_path.name}.tmp").exists()
    assert not (tmp_path / "receipts").exists()


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
    assert report["index_projection"] == {
        "current_published_points": 3,
        "expected_published_points": 2,
        "stale_prune_required": True,
        "full_reindex_required": True,
    }


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
    active_receipt = json.loads(next(receipt_dir.glob("*.json")).read_text("utf-8"))
    stored_yonote = next(
        record
        for record in active_receipt["merged_records"]
        if record["chunk_id"] == current_yonote["chunk_id"]
    )
    assert stored_yonote["extraction_date"] == "2026-08-19"
    assert stored_yonote["updated_at"] == "2026-08-19"


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
