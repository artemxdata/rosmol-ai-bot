from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.index_kb import KBSeedRecord, build_qdrant_payload
from src.admin.kb_status import build_runtime_kb_status


class FakeQdrant:
    def __init__(
        self,
        payloads: list[dict[str, Any] | None],
        *,
        cache_count: int = 0,
    ) -> None:
        self.payloads = payloads
        self.cache_count = cache_count

    async def scroll(self, **kwargs: Any) -> tuple[list[Any], None]:
        assert kwargs["collection_name"] == "knowledge_base"
        assert kwargs["with_payload"] is True
        assert kwargs["with_vectors"] is False
        return [SimpleNamespace(payload=payload) for payload in self.payloads], None

    async def collection_exists(self, collection: str) -> bool:
        assert collection == "response_cache"
        return self.cache_count > 0

    async def count(self, **kwargs: Any) -> Any:
        assert kwargs == {"collection_name": "response_cache", "exact": True}
        return SimpleNamespace(count=self.cache_count)


def _write_seed(path: Path) -> dict[str, Any]:
    record = {
        "chunk_id": "published-source",
        "text_clean": "Подтверждённый опубликованный ответ.",
        "status": "published",
        "category": "general",
        "forum_normalized": "Амур",
        "source_type": "xlsx",
        "topic": "published_answer",
    }
    path.write_text(
        json.dumps([record], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return record


def _indexed_payload(record: dict[str, Any]) -> dict[str, Any]:
    return build_qdrant_payload(KBSeedRecord.model_validate(record))


@pytest.mark.asyncio
async def test_runtime_status_proves_seed_and_qdrant_payload_match(tmp_path: Path) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    record = _write_seed(seed_path)
    qdrant_payload = _indexed_payload(record)

    report = await build_runtime_kb_status(
        FakeQdrant([qdrant_payload], cache_count=3),  # type: ignore[arg-type]
        seed_path=seed_path,
        knowledge_collection="knowledge_base",
    )

    assert report["status"] == "GO"
    assert report["seed"]["published"] == 1
    assert report["qdrant"]["points"] == 1
    assert report["qdrant"]["exact_payload_match"] is True
    assert report["seed"]["payload_fingerprint_sha256"] == (
        report["qdrant"]["payload_fingerprint_sha256"]
    )
    assert report["response_cache"]["points"] == 3


@pytest.mark.asyncio
async def test_runtime_status_rechecks_seed_after_comparing_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    record = _write_seed(seed_path)
    original_read_bytes = Path.read_bytes
    seed_reads = 0

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal seed_reads
        if path == seed_path:
            seed_reads += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)

    report = await build_runtime_kb_status(
        FakeQdrant([_indexed_payload(record)]),  # type: ignore[arg-type]
        seed_path=seed_path,
        knowledge_collection="knowledge_base",
    )

    assert report["status"] == "GO"
    assert seed_reads == 2


@pytest.mark.asyncio
async def test_runtime_status_stops_when_seed_changes_during_qdrant_scan(
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    record = _write_seed(seed_path)

    class MutatingQdrant(FakeQdrant):
        async def scroll(self, **kwargs: Any) -> tuple[list[Any], None]:
            seed_path.write_text(
                json.dumps(
                    [
                        {
                            **record,
                            "text_clean": "Seed изменился во время проверки.",
                        }
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return await super().scroll(**kwargs)

    report = await build_runtime_kb_status(
        MutatingQdrant([_indexed_payload(record)]),  # type: ignore[arg-type]
        seed_path=seed_path,
        knowledge_collection="knowledge_base",
    )

    assert report["status"] == "STOP"
    assert report["ok"] is False
    assert report["failure_reasons"] == ["seed_changed_during_scan"]
    assert report["seed"]["changed_during_scan"] is True
    assert report["seed"]["post_scan_sha256"] != report["seed"]["sha256"]
    assert report["qdrant"]["snapshot_payload_match"] is True
    assert report["qdrant"]["exact_payload_match"] is False


@pytest.mark.asyncio
async def test_runtime_status_reports_missing_stale_and_changed_chunks(
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    record = _write_seed(seed_path)
    changed_payload = _indexed_payload(record)
    changed_payload["text"] = "Устаревший ответ"
    qdrant_payloads = [
        changed_payload,
        {
            "chunk_id": "stale-source",
            "text": "Удалённый из seed ответ",
            "status": "published",
        },
    ]

    report = await build_runtime_kb_status(
        FakeQdrant(qdrant_payloads),  # type: ignore[arg-type]
        seed_path=seed_path,
        knowledge_collection="knowledge_base",
    )

    assert report["status"] == "STOP"
    assert report["qdrant"]["missing"] == 0
    assert report["qdrant"]["stale"] == 1
    assert report["qdrant"]["changed"] == 1
    assert report["qdrant"]["stale_sample"] == ["stale-source"]
    assert report["qdrant"]["changed_sample"] == ["published-source"]


@pytest.mark.asyncio
async def test_runtime_status_rejects_duplicate_or_unbound_qdrant_points(
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    record = _write_seed(seed_path)
    expected_payload = _indexed_payload(record)
    qdrant_payloads = [
        expected_payload,
        dict(expected_payload),
        {"text": "Point without a chunk binding"},
        None,
    ]

    report = await build_runtime_kb_status(
        FakeQdrant(qdrant_payloads),  # type: ignore[arg-type]
        seed_path=seed_path,
        knowledge_collection="knowledge_base",
    )

    assert report["status"] == "STOP"
    assert report["qdrant"]["exact_payload_match"] is False
    assert report["qdrant"]["invalid_or_duplicate_points"] == 3


@pytest.mark.asyncio
async def test_runtime_status_rejects_wrong_qdrant_forum_key(tmp_path: Path) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    record = _write_seed(seed_path)
    qdrant_payload = _indexed_payload(record)
    qdrant_payload["forum_key"] = "h_wrong_forum_key"

    report = await build_runtime_kb_status(
        FakeQdrant([qdrant_payload]),  # type: ignore[arg-type]
        seed_path=seed_path,
        knowledge_collection="knowledge_base",
    )

    assert report["status"] == "STOP"
    assert report["qdrant"]["exact_payload_match"] is False
    assert report["qdrant"]["changed"] == 1
    assert report["qdrant"]["changed_sample"] == ["published-source"]
