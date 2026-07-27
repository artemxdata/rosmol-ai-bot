from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts.sync_yonote_kb import YonoteDocument
from src.admin import yonote_database


def _document(
    *,
    document_id: str,
    title: str,
    text: str,
    collection: str,
    parent_document_id: str | None,
    path_titles: tuple[str, ...],
    updated_at: str | None,
) -> YonoteDocument:
    return YonoteDocument(
        id=document_id,
        title=title,
        text=text,
        collection_id=f"collection-{collection}",
        collection_name=collection,
        url=f"/doc/{document_id}",
        url_id=document_id,
        parent_document_id=parent_document_id,
        path_titles=path_titles,
        updated_at=updated_at,
        created_at=None,
        document_type="document",
    )


def _documents() -> list[YonoteDocument]:
    return [
        _document(
            document_id="root",
            title="Главная статья",
            text=(
                "# Первый раздел\n"
                "Первый абзац.\n\n"
                "## Второй раздел\n"
                "Ссылка: https://example.test/page"
            ),
            collection="Общая база",
            parent_document_id=None,
            path_titles=("Главная статья",),
            updated_at="2026-07-22T10:00:00Z",
        ),
        _document(
            document_id="empty",
            title="Пустая страница",
            text="",
            collection="Общая база",
            parent_document_id="root",
            path_titles=("Главная статья", "Пустая страница"),
            updated_at="2026-07-21T10:00:00Z",
        ),
        _document(
            document_id="event",
            title="Мероприятие",
            text="Один текстовый блок",
            collection="Мероприятия",
            parent_document_id=None,
            path_titles=("Мероприятие",),
            updated_at="2026-07-23T10:00:00Z",
        ),
    ]


def test_build_statistics_counts_live_yonote_documents_and_text() -> None:
    documents = _documents()
    generated_at = datetime(2026, 7, 23, 12, 30, tzinfo=UTC)

    report = yonote_database.build_statistics(
        documents,
        generated_at=generated_at,
    )

    assert report["ok"] is True
    assert report["source"] == "yonote_live_api"
    assert report["read_only"] is True
    assert (
        report["scope_definition"]
        == "configured_collections_visible_to_service_account"
    )
    assert report["generated_at"] == "2026-07-23T12:30:00+00:00"
    assert report["documents_total"] == 3
    assert report["documents_with_text"] == 2
    assert report["documents_without_text"] == 1
    assert report["root_documents"] == 2
    assert report["nested_documents"] == 1
    assert report["sections_total"] == 3
    assert (
        report["sections_definition"]
        == "text_sections_detected_from_headings_or_single_body"
    )
    assert report["characters_with_spaces"] == sum(
        len(document.text.strip()) for document in documents
    )
    assert report["characters_without_whitespace"] < report["characters_with_spaces"]
    assert report["words_total"] > 0
    assert report["paragraphs_total"] == 3
    assert report["links_total"] == 1
    assert report["collections_total"] == 2
    assert report["latest_updated_at"] == "2026-07-23T10:00:00Z"
    assert report["collections"] == [
        {
            "name": "Мероприятия",
            "documents": 1,
            "sections": 1,
            "characters_with_spaces": len("Один текстовый блок"),
        },
        {
            "name": "Общая база",
            "documents": 2,
            "sections": 2,
            "characters_with_spaces": len(documents[0].text),
        },
    ]


def test_render_text_export_contains_structure_text_and_no_secret() -> None:
    documents = _documents()
    report = yonote_database.build_statistics(
        documents,
        generated_at=datetime(2026, 7, 23, 12, 30, tzinfo=UTC),
    )

    rendered = yonote_database.render_text_export(
        documents,
        report=report,
        base_url="https://rossmol.example",
    )

    assert "ВЫГРУЗКА ДОСТУПНОГО СОДЕРЖИМОГО YONOTE" in rendered
    assert "доступные сервисной учётной записи" in rendered
    assert "Документов/страниц: 3" in rendered
    assert "КОЛЛЕКЦИЯ: Общая база" in rendered
    assert "Главная статья / Пустая страница" in rendered
    assert "https://rossmol.example/doc/root" in rendered
    assert "Первый абзац." in rendered
    assert "[Документ не содержит текстового содержимого]" in rendered
    assert "read-only-yonote-token" not in rendered


def test_export_database_rejects_oversized_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load(
        _settings: object,
        *,
        max_total_text_bytes: int,
    ) -> list[YonoteDocument]:
        assert max_total_text_bytes == 16
        return _documents()

    monkeypatch.setattr(
        yonote_database,
        "load_database_documents",
        fake_load,
    )
    monkeypatch.setattr(yonote_database, "MAX_TEXT_EXPORT_BYTES", 16)

    with pytest.raises(
        yonote_database.YonoteDatabaseExportTooLarge,
        match="безопасный лимит 16 байт",
    ):
        yonote_database.export_database(
            type("Settings", (), {"yonote_base_url": "https://rossmol.example"})()
        )


def test_render_text_export_enforces_utf8_byte_limit_for_multibyte_text() -> None:
    documents = _documents()
    report = yonote_database.build_statistics(
        documents,
        generated_at=datetime(2026, 7, 23, 12, 30, tzinfo=UTC),
    )
    rendered = yonote_database.render_text_export(
        documents,
        report=report,
        base_url="https://rossmol.example",
    )
    character_count = len(rendered)
    byte_count = len(rendered.encode("utf-8"))
    assert byte_count > character_count
    byte_limit_between_counts = (character_count + byte_count) // 2

    with pytest.raises(
        yonote_database.YonoteDatabaseExportTooLarge,
        match="безопасный лимит",
    ):
        yonote_database.render_text_export(
            documents,
            report=report,
            base_url="https://rossmol.example",
            max_bytes=byte_limit_between_counts,
        )


def test_database_reader_requests_empty_pages_with_bounded_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = object()
    expected_documents = _documents()

    def fake_load(
        received_settings: object,
        *,
        include_empty: bool,
        max_duration_seconds: float,
        max_total_text_bytes: int,
    ) -> list[YonoteDocument]:
        assert received_settings is settings
        assert include_empty is True
        assert max_duration_seconds == yonote_database.MAX_DATABASE_PULL_SECONDS
        assert max_total_text_bytes == yonote_database.MAX_DATABASE_TEXT_BYTES
        return expected_documents

    monkeypatch.setattr(
        yonote_database,
        "load_yonote_documents_from_settings",
        fake_load,
    )

    assert yonote_database.load_database_documents(settings) is expected_documents
