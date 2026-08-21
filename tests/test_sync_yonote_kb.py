from __future__ import annotations

from dataclasses import replace
from datetime import date
from hashlib import sha256

import httpx
import pytest

import scripts.sync_yonote_kb as sync_yonote_kb
from scripts.index_kb import validate_seed_items
from scripts.sync_yonote_kb import (
    YonoteApiError,
    YonoteClient,
    YonoteCollection,
    YonoteDataTooLarge,
    YonoteDocument,
    YonoteOperationTimeout,
    build_document_path,
    build_records_from_api_documents,
    infer_category,
    infer_event_name,
    match_collections,
    split_collection_selectors,
)


def test_yonote_client_stops_stream_after_response_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False

    class ChunkStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"data":'
            yield b"[]}"

        def close(self) -> None:
            nonlocal closed
            closed = True

    client = YonoteClient(
        base_url="https://example.test",
        api_token="test-token",
        timeout_seconds=5,
        max_retries=0,
        min_request_interval_seconds=0,
        max_response_bytes=8,
    )
    request = httpx.Request("GET", "https://example.test/api/collections.list")
    monkeypatch.setattr(
        client._client,
        "request",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            request=request,
            stream=ChunkStream(),
        ),
    )
    try:
        with pytest.raises(YonoteDataTooLarge, match="size limit"):
            client.collections()
    finally:
        client.close()

    assert closed is True


def test_yonote_client_checks_deadline_between_response_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    closed = False
    monkeypatch.setattr(sync_yonote_kb.time, "monotonic", lambda: clock[0])

    class TimedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"data":'
            clock[0] = 106.0
            yield b"[]}"

        def close(self) -> None:
            nonlocal closed
            closed = True

    client = YonoteClient(
        base_url="https://example.test",
        api_token="test-token",
        timeout_seconds=30,
        max_retries=0,
        min_request_interval_seconds=0,
        max_duration_seconds=5,
    )
    request = httpx.Request("GET", "https://example.test/api/collections.list")
    monkeypatch.setattr(
        client._client,
        "request",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            request=request,
            stream=TimedStream(),
        ),
    )
    try:
        with pytest.raises(YonoteOperationTimeout, match="operation deadline"):
            client.collections()
    finally:
        client.close()

    assert closed is True


def test_yonote_client_stops_before_request_after_operation_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(sync_yonote_kb.time, "monotonic", lambda: clock[0])
    client = YonoteClient(
        base_url="https://example.test",
        api_token="test-token",
        timeout_seconds=30,
        max_retries=2,
        min_request_interval_seconds=0,
        max_duration_seconds=5,
    )
    provider_called = False

    def fail_if_called(*_args: object, **_kwargs: object) -> httpx.Response:
        nonlocal provider_called
        provider_called = True
        raise AssertionError("provider request must not start after the deadline")

    monkeypatch.setattr(client._client, "request", fail_if_called)
    clock[0] = 106.0
    try:
        with pytest.raises(YonoteOperationTimeout, match="operation deadline"):
            client.collections()
    finally:
        client.close()

    assert provider_called is False


def test_yonote_client_rejects_malformed_json_without_echoing_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_marker = "read-only-yonote-token"
    client = YonoteClient(
        base_url="https://example.test",
        api_token="test-token",
        timeout_seconds=5,
        max_retries=0,
        min_request_interval_seconds=0,
    )
    request = httpx.Request("GET", "https://example.test/api/collections.list")
    monkeypatch.setattr(
        client._client,
        "request",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            request=request,
            content=f"not-json {secret_marker}".encode(),
        ),
    )
    try:
        with pytest.raises(YonoteApiError, match="malformed JSON") as exc_info:
            client.collections()
    finally:
        client.close()

    assert secret_marker not in str(exc_info.value)


def test_yonote_client_rejects_success_payload_without_data_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = YonoteClient(
        base_url="https://example.test",
        api_token="test-token",
        timeout_seconds=5,
        max_retries=0,
        min_request_interval_seconds=0,
    )
    request = httpx.Request("GET", "https://example.test/api/collections.list")
    monkeypatch.setattr(
        client._client,
        "request",
        lambda *_args, **_kwargs: httpx.Response(200, request=request, json={}),
    )
    try:
        with pytest.raises(YonoteApiError, match="Malformed paginated response"):
            client.collections()
    finally:
        client.close()


def test_yonote_client_rejects_non_object_page_item_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_marker = "read-only-yonote-token"
    client = YonoteClient(
        base_url="https://example.test",
        api_token="test-token",
        timeout_seconds=5,
        max_retries=0,
        min_request_interval_seconds=0,
    )
    request = httpx.Request("GET", "https://example.test/api/collections.list")
    monkeypatch.setattr(
        client._client,
        "request",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            request=request,
            json={"data": [{"id": "valid"}, secret_marker]},
        ),
    )
    try:
        with pytest.raises(
            YonoteApiError,
            match="Malformed paginated response",
        ) as exc_info:
            client.collections()
    finally:
        client.close()

    assert secret_marker not in str(exc_info.value)


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


def test_load_yonote_documents_can_include_empty_pages_for_inventory() -> None:
    class FakeClient:
        def collections(self) -> list[YonoteCollection]:
            return [
                YonoteCollection(
                    id="collection-id",
                    name="Collection",
                    url="/collection",
                    url_id="collection",
                )
            ]

        def documents(self, _collection_id: str) -> list[dict[str, object]]:
            return [
                {"id": "empty", "title": "Empty"},
                {"id": "filled", "title": "Filled"},
            ]

        def document_info(self, document_id: str) -> dict[str, object]:
            return {
                "id": document_id,
                "title": document_id.title(),
                "text": "" if document_id == "empty" else "Published content",
            }

    default_documents = sync_yonote_kb.load_yonote_documents(
        FakeClient(),  # type: ignore[arg-type]
        ("Collection",),
    )
    inventory_documents = sync_yonote_kb.load_yonote_documents(
        FakeClient(),  # type: ignore[arg-type]
        ("Collection",),
        include_empty=True,
    )

    assert [document.id for document in default_documents] == ["filled"]
    assert [document.id for document in inventory_documents] == ["empty", "filled"]


def test_load_yonote_documents_stops_at_aggregate_utf8_text_limit() -> None:
    class FakeClient:
        def collections(self) -> list[YonoteCollection]:
            return [
                YonoteCollection(
                    id="collection-id",
                    name="Collection",
                    url="/collection",
                    url_id="collection",
                )
            ]

        def documents(self, _collection_id: str) -> list[dict[str, object]]:
            return [{"id": "filled", "title": "Filled"}]

        def document_info(self, document_id: str) -> dict[str, object]:
            return {
                "id": document_id,
                "title": "Filled",
                "text": "аб",
            }

    with pytest.raises(YonoteDataTooLarge, match="aggregate size limit"):
        sync_yonote_kb.load_yonote_documents(
            FakeClient(),  # type: ignore[arg-type]
            ("Collection",),
            max_total_text_bytes=3,
        )


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


def _chunk_identity_document(
    *,
    document_id: str,
    url_id: str | None,
    title: str = "Одинаковый заголовок",
    collection_id: str = "collection-1",
) -> YonoteDocument:
    return YonoteDocument(
        id=document_id,
        title=title,
        text="Описание\nДостаточно длинный подтверждённый текст для базы знаний.",
        collection_id=collection_id,
        collection_name="Росмолодёжь: мероприятия",
        url=f"/doc/{document_id}",
        url_id=url_id,
        parent_document_id=None,
        path_titles=(title,),
        updated_at="2026-08-20T12:00:00.000Z",
        created_at="2026-08-20T11:00:00.000Z",
        document_type="document",
    )


def _document_scope_suffix(collection_id: str, document_id: str) -> str:
    payload = f"{collection_id}\0{document_id}".encode()
    return sha256(payload).hexdigest()[:12]


def test_unique_url_id_preserves_legacy_chunk_id() -> None:
    document = _chunk_identity_document(document_id="doc-1", url_id="legacy-url-id")

    records = build_records_from_api_documents(
        [document],
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )

    assert [record["chunk_id"] for record in records] == [
        "yonote_api_legacy_url_id_s0001_opisanie"
    ]


def test_unique_title_fallback_uses_stable_document_scope_id() -> None:
    document = _chunk_identity_document(
        document_id="doc-1",
        url_id=None,
        title="Уникальный заголовок",
    )

    records = build_records_from_api_documents(
        [document],
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )

    assert [record["chunk_id"] for record in records] == [
        "yonote_api_doc_"
        f"{_document_scope_suffix('collection-1', 'doc-1')}_s0001_opisanie"
    ]


def test_same_title_without_url_id_gets_unique_document_scope_ids() -> None:
    documents = [
        _chunk_identity_document(document_id="doc-1", url_id=None),
        _chunk_identity_document(document_id="doc-2", url_id=None),
    ]

    records = build_records_from_api_documents(
        documents,
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )

    assert {record["chunk_id"] for record in records} == {
        "yonote_api_doc_"
        f"{_document_scope_suffix('collection-1', 'doc-1')}_s0001_opisanie",
        "yonote_api_doc_"
        f"{_document_scope_suffix('collection-1', 'doc-2')}_s0001_opisanie",
    }
    validate_seed_items(records)


def test_document_scope_collision_ids_do_not_depend_on_api_order() -> None:
    documents = [
        _chunk_identity_document(document_id="doc-1", url_id=None),
        _chunk_identity_document(document_id="doc-2", url_id=None),
    ]

    forward = build_records_from_api_documents(
        documents,
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )
    reversed_records = build_records_from_api_documents(
        list(reversed(documents)),
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )

    assert {record["chunk_id"] for record in forward} == {
        record["chunk_id"] for record in reversed_records
    }


def test_title_fallback_id_stays_stable_when_collision_appears_and_disappears() -> None:
    first = _chunk_identity_document(document_id="doc-1", url_id=None)
    second = _chunk_identity_document(document_id="doc-2", url_id=None)

    only_first = build_records_from_api_documents(
        [first],
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )[0]["chunk_id"]
    with_collision = build_records_from_api_documents(
        [first, second],
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )[0]["chunk_id"]
    after_removal = build_records_from_api_documents(
        [first],
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )[0]["chunk_id"]

    assert only_first == with_collision == after_removal


def test_title_fallback_id_stays_stable_when_document_is_renamed() -> None:
    original = _chunk_identity_document(
        document_id="doc-1",
        url_id=None,
        title="Первоначальный заголовок",
    )
    renamed = replace(original, title="Полностью новый заголовок")

    original_id = build_records_from_api_documents(
        [original],
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )[0]["chunk_id"]
    renamed_id = build_records_from_api_documents(
        [renamed],
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )[0]["chunk_id"]

    assert original_id == renamed_id


@pytest.mark.parametrize(
    ("first_url_id", "second_url_id"),
    [
        ("shared-url-id", "shared-url-id"),
        ("x" * 48 + "-first", "x" * 48 + "-second"),
    ],
)
def test_duplicate_or_truncated_url_ids_are_disambiguated(
    first_url_id: str,
    second_url_id: str,
) -> None:
    documents = [
        _chunk_identity_document(document_id="doc-1", url_id=first_url_id),
        _chunk_identity_document(document_id="doc-2", url_id=second_url_id),
    ]

    records = build_records_from_api_documents(
        documents,
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )

    assert len({record["chunk_id"] for record in records}) == 2
    assert all(
        _document_scope_suffix("collection-1", document_id) in record["chunk_id"]
        for document_id, record in zip(("doc-1", "doc-2"), records, strict=True)
    )
    validate_seed_items(records)


def test_long_split_section_keeps_unique_part_ids_after_topic_truncation() -> None:
    title = (
        "Сверхдлинный заголовок подробного информационного раздела "
        "мероприятия часть 2"
    )
    document = _chunk_identity_document(
        document_id="doc-long",
        url_id="stable-long-doc",
        title="Документ с большим разделом",
    )
    document = replace(
        document,
        text=f"{title}\n" + ("Подтверждённый длинный текст. " * 360),
    )

    records = build_records_from_api_documents(
        [document],
        base_url="https://rossmol.yonote.ru",
        extraction_date=date(2026, 8, 20),
    )

    assert len(records) > 1
    assert len({record["chunk_id"] for record in records}) == len(records)
    assert not records[0]["chunk_id"].endswith("_p0001")
    assert records[1]["chunk_id"].endswith("_p0002")
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
