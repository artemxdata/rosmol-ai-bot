from __future__ import annotations

from datetime import date

from scripts.index_kb import validate_seed_items
from scripts.sync_yonote_kb import (
    YonoteCollection,
    YonoteDocument,
    build_document_path,
    build_records_from_api_documents,
    infer_category,
    infer_event_name,
    match_collections,
    split_collection_selectors,
)


def test_split_collection_selectors_keeps_commas_inside_collection_names() -> None:
    selectors = split_collection_selectors(
        "Росмолодёжь: общее, структура, направления;Росмолодёжь: мероприятия"
    )

    assert selectors == (
        "Росмолодёжь: общее, структура, направления",
        "Росмолодёжь: мероприятия",
    )


def test_match_collections_by_name_id_or_url_id() -> None:
    collections = [
        YonoteCollection(
            id="first-id",
            name="Росмолодёжь: общее, структура, направления",
            url="/collection/general",
            url_id="general-id",
        ),
        YonoteCollection(
            id="second-id",
            name="Росмолодёжь: мероприятия",
            url="/collection/events",
            url_id="events-id",
        ),
    ]

    matched = match_collections(
        collections,
        ("Росмолодёжь: общее, структура, направления", "events-id"),
    )

    assert [collection.id for collection in matched] == ["first-id", "second-id"]


def test_build_document_path_uses_parent_chain() -> None:
    by_id = {
        "root": {"id": "root", "title": "Форумная дирекция", "parentDocumentId": None},
        "forums": {
            "id": "forums",
            "title": "Всероссийские форумы 2026",
            "parentDocumentId": "root",
        },
        "amur": {"id": "amur", "title": "Амур 2026", "parentDocumentId": "forums"},
    }

    assert build_document_path(by_id["amur"], by_id) == (
        "Форумная дирекция",
        "Всероссийские форумы 2026",
        "Амур 2026",
    )


def test_build_records_from_api_documents_creates_valid_yonote_seed_records() -> None:
    document = YonoteDocument(
        id="doc-1",
        title="Амур 2026",
        text=(
            "Регистрация\n"
            "Сейчас регистрация на форум закрыта. Даты объявят в 2026 году.\n\n"
            "Проезд\n"
            "Проезд оплачивает направляющая сторона или участник самостоятельно."
        ),
        collection_id="collection-1",
        collection_name="Росмолодёжь: мероприятия",
        url="/doc/amur-abc",
        url_id="abc",
        parent_document_id="forums",
        path_titles=("Форумная дирекция", "Всероссийские форумы 2026", "Амур 2026"),
        updated_at="2026-07-05T12:00:00.000Z",
        created_at="2026-07-01T12:00:00.000Z",
        document_type="document",
    )

    records = build_records_from_api_documents(
        [document],
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 7, 6),
    )

    assert len(records) == 2
    assert {record["source_type"] for record in records} == {"yonote"}
    assert {record["source"] for record in records} == {"yonote_api"}
    assert {record["category"] for record in records} == {"форумы"}
    assert {record["forum_normalized"] for record in records} == {"Амур"}
    assert all(
        record["source_url"] == "https://rossmol.yonote.ru/doc/amur-abc"
        for record in records
    )
    assert any(record["topic"] == "registraciya" for record in records)
    validate_seed_items(records)


def test_infer_category_handles_general_platform_and_grants() -> None:
    fgais = YonoteDocument(
        id="doc-fgais",
        title="ФГАИС «Молодёжь России»",
        text="Профиль и заявки",
        collection_id="c",
        collection_name="Росмолодёжь: общее, структура, направления",
        url=None,
        url_id=None,
        parent_document_id=None,
        path_titles=("ФГАИС «Молодёжь России»",),
        updated_at=None,
        created_at=None,
        document_type="document",
    )
    grants = YonoteDocument(
        id="doc-grants",
        title="Гранты для физических лиц",
        text="Грантовый конкурс",
        collection_id="c",
        collection_name="Росмолодёжь: мероприятия",
        url=None,
        url_id=None,
        parent_document_id=None,
        path_titles=("Росмолодёжь.Гранты", "Гранты для физических лиц"),
        updated_at=None,
        created_at=None,
        document_type="document",
    )

    assert infer_category(fgais, infer_event_name(fgais)) == "платформа_фгаис"
    assert infer_category(grants, infer_event_name(grants)) == "гранты"
