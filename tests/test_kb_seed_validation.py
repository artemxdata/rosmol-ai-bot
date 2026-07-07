from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts.index_kb import (
    build_embedding_text,
    collect_existing_chunk_ids,
    prune_stale_points,
    select_records_for_indexing,
    validate_only,
    validate_quality_gate,
    validate_seed_items,
)


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


def test_validate_quality_gate_accepts_passed_report(tmp_path) -> None:
    path = tmp_path / "quality_gate.json"
    path.write_text(
        json.dumps({"passed": True, "failed_checks": 0}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert validate_quality_gate(path)["passed"] is True


def test_validate_quality_gate_rejects_missing_report(tmp_path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        validate_quality_gate(tmp_path / "missing.json")


def test_validate_quality_gate_rejects_failed_report(tmp_path) -> None:
    path = tmp_path / "quality_gate.json"
    path.write_text(
        json.dumps({"passed": False, "failed_checks": 2}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="failed_checks=2"):
        validate_quality_gate(path)


def test_build_embedding_text_includes_intent_examples_without_changing_answer() -> None:
    record = validate_seed_items(
        [
            {
                "chunk_id": "travel",
                "text_clean": "Билеты до Пятигорска оплачиваются самостоятельно.",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Машук",
                "topic": "oplata_proezda",
                "intent_name": "Оплата проезда",
                "intent_examples": ["кто оплачивает проезд", "финансируют ли дорогу"],
            }
        ]
    )[0]

    embedding_text = build_embedding_text(record)

    assert "кто оплачивает проезд" in embedding_text
    assert "Интент: Оплата проезда" in embedding_text
    assert "Ответ:\nБилеты до Пятигорска оплачиваются самостоятельно." in embedding_text
    assert record.content == "Билеты до Пятигорска оплачиваются самостоятельно."


def test_select_records_for_indexing_keeps_full_allowed_set_for_prune() -> None:
    records = validate_seed_items(
        [
            {"chunk_id": "existing", "text": "Старый чанк"},
            {"chunk_id": "missing", "text": "Новый чанк"},
        ]
    )

    selected, skipped, allowed_chunk_ids = select_records_for_indexing(
        records,
        existing_chunk_ids={"existing"},
        only_missing=True,
    )

    assert [record.chunk_id for record in selected] == ["missing"]
    assert skipped == 1
    assert allowed_chunk_ids == {"existing", "missing"}


class FakeQdrantForPrune:
    def __init__(self) -> None:
        self.scroll_calls = []
        self.delete_calls = []
        self.pages = [
            [
                SimpleNamespace(id="keep-point", payload={"chunk_id": "keep"}),
                SimpleNamespace(id="stale-point", payload={"chunk_id": "stale"}),
            ],
            [
                SimpleNamespace(id="missing-chunk-id", payload={}),
            ],
        ]

    async def scroll(self, **kwargs):
        self.scroll_calls.append(kwargs)
        page_index = len(self.scroll_calls) - 1
        next_page_offset = "next" if page_index + 1 < len(self.pages) else None
        return self.pages[page_index], next_page_offset

    async def delete(self, **kwargs):
        self.delete_calls.append(kwargs)


@pytest.mark.asyncio
async def test_collect_existing_chunk_ids_reads_all_pages() -> None:
    qdrant = FakeQdrantForPrune()

    chunk_ids = await collect_existing_chunk_ids(
        qdrant,  # type: ignore[arg-type]
        "knowledge_base",
        scroll_limit=2,
    )

    assert chunk_ids == {"keep", "stale"}
    assert len(qdrant.scroll_calls) == 2
    assert qdrant.scroll_calls[0]["with_payload"] == ["chunk_id"]
    assert qdrant.scroll_calls[0]["with_vectors"] is False


@pytest.mark.asyncio
async def test_prune_stale_points_removes_only_chunks_missing_from_seed() -> None:
    qdrant = FakeQdrantForPrune()

    deleted = await prune_stale_points(
        qdrant,  # type: ignore[arg-type]
        "knowledge_base",
        {"keep"},
        scroll_limit=2,
        delete_batch_size=1,
    )

    assert deleted == 2
    assert len(qdrant.scroll_calls) == 2
    assert qdrant.scroll_calls[0]["with_payload"] == ["chunk_id"]
    assert qdrant.scroll_calls[0]["with_vectors"] is False
    assert [call["points_selector"].points for call in qdrant.delete_calls] == [
        ["stale-point"],
        ["missing-chunk-id"],
    ]
