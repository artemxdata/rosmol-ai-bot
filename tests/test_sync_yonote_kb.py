from __future__ import annotations

from datetime import date

import httpx
import pytest

import scripts.sync_yonote_kb as sync_yonote_kb
from scripts.index_kb import validate_seed_items
from scripts.sync_yonote_kb import (
    YonoteApiError,
    YonoteClient,
    YonoteCollection,
    YonoteDocument,
    build_document_path,
    build_records_from_api_documents,
    infer_category,
    infer_event_name,
    match_collections,
    split_collection_selectors,
)


def test_yonote_client_retries_transient_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = YonoteClient(
        base_url="https://example.test",
        api_token="test-token",
        timeout_seconds=5,
        max_retries=2,
        min_request_interval_seconds=0,
    )
    request = httpx.Request("POST", "https://example.test/api/documents.info")
    responses: list[httpx.Response | Exception] = [
        httpx.RemoteProtocolError("connection dropped", request=request),
        httpx.Response(
            200,
            request=request,
            json={"data": {"document": {"id": "doc-1", "text": "Ответ"}}},
        ),
    ]
    sleeps: list[float] = []

    def fake_request(*_args: object, **_kwargs: object) -> httpx.Response:
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(client._client, "request", fake_request)
    monkeypatch.setattr(sync_yonote_kb.time, "sleep", sleeps.append)
    try:
        document = client.document_info("doc-1")
    finally:
        client.close()

    assert document["id"] == "doc-1"
    assert sleeps == [1.0]


def test_yonote_client_retries_429_using_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = YonoteClient(
        base_url="https://example.test",
        api_token="test-token",
        timeout_seconds=5,
        max_retries=1,
        min_request_interval_seconds=0,
    )
    request = httpx.Request("GET", "https://example.test/api/collections.list")
    responses = [
        httpx.Response(429, request=request, headers={"Retry-After": "2"}),
        httpx.Response(200, request=request, json={"data": []}),
    ]
    sleeps: list[float] = []
    monkeypatch.setattr(client._client, "request", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr(sync_yonote_kb.time, "sleep", sleeps.append)
    try:
        assert client.collections() == []
    finally:
        client.close()

    assert sleeps == [2.0]


def test_yonote_client_wraps_terminal_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = YonoteClient(
        base_url="https://example.test",
        api_token="test-token",
        timeout_seconds=5,
        max_retries=1,
        min_request_interval_seconds=0,
    )
    request = httpx.Request("POST", "https://example.test/api/documents.info")
    monkeypatch.setattr(
        client._client,
        "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            httpx.RemoteProtocolError("connection dropped", request=request)
        ),
    )
    monkeypatch.setattr(sync_yonote_kb.time, "sleep", lambda _value: None)
    try:
        with pytest.raises(YonoteApiError, match="temporarily unavailable"):
            client.document_info("doc-1")
    finally:
        client.close()


def test_yonote_client_reads_every_paginated_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = YonoteClient(
        base_url="https://example.test",
        api_token="test-token",
        timeout_seconds=5,
        max_retries=0,
        min_request_interval_seconds=0,
    )
    offsets: list[int] = []

    def fake_request(
        _method: str,
        _path: str,
        **kwargs: object,
    ) -> httpx.Response:
        params = kwargs["params"]
        assert isinstance(params, dict)
        offset = int(params["offset"])
        offsets.append(offset)
        page_size = 100 if offset == 0 else 1
        request = httpx.Request("GET", "https://example.test/api/collections.list")
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"id": f"item-{offset + index}"} for index in range(page_size)]},
        )

    monkeypatch.setattr(client._client, "request", fake_request)
    try:
        records = client._get_paginated("/api/collections.list")
    finally:
        client.close()

    assert len(records) == 101
    assert offsets == [0, 100]


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


def test_load_yonote_documents_rejects_partially_matched_collection_set() -> None:
    class FakeClient:
        def collections(self) -> list[YonoteCollection]:
            return [
                YonoteCollection(
                    id="first-id",
                    name="First collection",
                    url="/collection/first",
                    url_id="first",
                )
            ]

    with pytest.raises(
        YonoteApiError,
        match="Yonote collections not found: Missing collection",
    ):
        sync_yonote_kb.load_yonote_documents(
            FakeClient(),  # type: ignore[arg-type]
            ("First collection", "Missing collection"),
        )


def test_load_yonote_documents_rejects_empty_collection_selector_set() -> None:
    class FakeClient:
        def collections(self) -> list[YonoteCollection]:
            raise AssertionError("provider must not be called without collection selectors")

    with pytest.raises(
        YonoteApiError,
        match="No Yonote collection selectors are configured",
    ):
        sync_yonote_kb.load_yonote_documents(
            FakeClient(),  # type: ignore[arg-type]
            (),
        )


def test_load_yonote_documents_reads_every_selected_collection_without_limit() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.requested_collections: list[str] = []

        def collections(self) -> list[YonoteCollection]:
            return [
                YonoteCollection(
                    id="first-id",
                    name="First collection",
                    url="/collection/first",
                    url_id="first",
                ),
                YonoteCollection(
                    id="second-id",
                    name="Second collection",
                    url="/collection/second",
                    url_id="second",
                ),
            ]

        def documents(self, collection_id: str) -> list[dict[str, object]]:
            self.requested_collections.append(collection_id)
            return [
                {
                    "id": f"doc-{collection_id}",
                    "title": collection_id,
                }
            ]

        def document_info(self, document_id: str) -> dict[str, object]:
            return {
                "id": document_id,
                "title": document_id,
                "text": f"Published content for {document_id}",
            }

    client = FakeClient()
    documents = sync_yonote_kb.load_yonote_documents(
        client,  # type: ignore[arg-type]
        ("First collection", "Second collection"),
    )

    assert client.requested_collections == ["first-id", "second-id"]
    assert [document.id for document in documents] == [
        "doc-first-id",
        "doc-second-id",
    ]


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


@pytest.mark.parametrize("title", ["О Росмолодёжи", "Структура и направления"])
def test_infer_category_keeps_general_yonote_sections_out_of_forum_taxonomy(
    title: str,
) -> None:
    document = YonoteDocument(
        id="doc-general",
        title=title,
        text="Справочная информация Росмолодёжи",
        collection_id="c",
        collection_name="Росмолодёжь: общее, структура, направления",
        url=None,
        url_id=None,
        parent_document_id=None,
        path_titles=(title,),
        updated_at=None,
        created_at=None,
        document_type="document",
    )

    records = build_records_from_api_documents(
        [document],
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 7, 14),
    )

    assert records
    assert {record["category"] for record in records} == {"общее"}
    assert {record["forum_normalized"] for record in records} == {None}
    assert {record["is_generic"] for record in records} == {True}
