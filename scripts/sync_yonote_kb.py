from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.build_yonote_kb_seed import (
    clean_markdown_text,
    merge_records,
    split_section_text_with_heading,
)
from scripts.index_kb import validate_seed_items
from src.config import get_settings
from src.kb.source_extractors import (
    refresh_text_derived_metadata,
    slugify,
)

DEFAULT_BASE_KB = Path("data/knowledge_base_seed.json")
DEFAULT_OUTPUT = Path("data/knowledge_base_seed.json")
DEFAULT_COLLECTION_NAMES = (
    "Росмолодёжь: общее, структура, направления",
    "Росмолодёжь: мероприятия",
)
TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
DEFAULT_MAX_YONOTE_RESPONSE_BYTES = 32 * 1024 * 1024
_UTF8_SIZE_CHUNK_CHARACTERS = 64 * 1024
_DOCUMENT_SCOPE_HASH_LENGTH = 12
ROOT_TITLES = {
    "росмолодёжь: общее, структура, направления",
    "росмолодёжь: мероприятия",
    "форумная дирекция",
    "всероссийские форумы 2026",
    "окружные форумы 2026",
    "программы и события",
    "общие программы и события",
    "цсмс",
    "кмоц",
    "роспатриотцентр",
}


@dataclass(frozen=True)
class YonoteCollection:
    id: str
    name: str
    url: str | None = None
    url_id: str | None = None


@dataclass(frozen=True)
class YonoteDocument:
    id: str
    title: str
    text: str
    collection_id: str
    collection_name: str
    url: str | None
    url_id: str | None
    parent_document_id: str | None
    path_titles: tuple[str, ...]
    updated_at: str | None
    created_at: str | None
    document_type: str | None


@dataclass(frozen=True)
class TextSection:
    title: str
    text: str
    index: int


class YonoteApiError(RuntimeError):
    pass


class YonoteOperationTimeout(YonoteApiError):
    pass


class YonoteDataTooLarge(YonoteApiError):
    pass


class _YonoteResponseStreamGuard(httpx.SyncByteStream):
    def __init__(
        self,
        stream: httpx.SyncByteStream,
        *,
        max_bytes: int,
        ensure_operation_active: Callable[[], None],
    ) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._ensure_operation_active = ensure_operation_active
        self._received_bytes = 0

    def __iter__(self) -> Iterator[bytes]:
        iterator = iter(self._stream)
        while True:
            self._ensure_operation_active()
            try:
                chunk = next(iterator)
            except StopIteration:
                self._ensure_operation_active()
                return
            self._ensure_operation_active()
            self._received_bytes += len(chunk)
            if self._received_bytes > self._max_bytes:
                raise YonoteDataTooLarge(
                    "Yonote API response exceeded the configured size limit"
                )
            yield chunk

    def close(self) -> None:
        self._stream.close()


class YonoteClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        timeout_seconds: float,
        max_retries: int = 2,
        min_request_interval_seconds: float = 0.15,
        max_duration_seconds: float | None = None,
        max_response_bytes: int = DEFAULT_MAX_YONOTE_RESPONSE_BYTES,
    ) -> None:
        if not api_token.strip():
            raise YonoteApiError("YONOTE_API_TOKEN is required")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self.min_request_interval_seconds = max(
            0.0,
            float(min_request_interval_seconds),
        )
        if max_duration_seconds is not None and max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self.max_response_bytes = int(max_response_bytes)
        self._operation_deadline = (
            time.monotonic() + float(max_duration_seconds)
            if max_duration_seconds is not None
            else None
        )
        self._last_request_at = 0.0
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                self.timeout_seconds,
                connect=min(self.timeout_seconds, 10.0),
            ),
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
                "User-Agent": "rosmol-ai-bot/yonote-sync",
            },
            event_hooks={"response": [self._install_response_guard]},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> YonoteClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def collections(self) -> list[YonoteCollection]:
        records = self._get_paginated("/api/collections.list")
        return [
            YonoteCollection(
                id=str(item["id"]),
                name=str(item.get("name") or ""),
                url=item.get("url"),
                url_id=item.get("urlId"),
            )
            for item in records
            if item.get("id") and item.get("name")
        ]

    def documents(self, collection_id: str) -> list[dict[str, Any]]:
        return self._get_paginated(
            "/api/documents.list",
            params={"collectionId": collection_id},
        )

    def document_info(self, document_id: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/documents.info",
            json={"id": document_id, "apiVersion": 2},
        )
        payload = self._json_object(response, "/api/documents.info")
        data = payload.get("data")
        document = data.get("document") if isinstance(data, dict) else None
        if not isinstance(document, dict):
            raise YonoteApiError(f"Malformed documents.info response for {document_id}")
        return document

    def _get_paginated(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        offset = 0
        records: list[dict[str, Any]] = []
        while True:
            request_params = {**params, "limit": limit, "offset": offset}
            response = self._request("GET", path, params=request_params)
            payload = self._json_object(response, path)
            data = payload.get("data")
            if not isinstance(data, list):
                raise YonoteApiError(f"Malformed paginated response from {path}")
            if any(not isinstance(item, dict) for item in data):
                raise YonoteApiError(f"Malformed paginated response from {path}")
            records.extend(data)
            if len(data) < limit:
                break
            offset += limit
        return records

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            self._ensure_operation_active()
            self._wait_for_request_slot()
            remaining_seconds = self._remaining_operation_seconds()
            if remaining_seconds is not None:
                kwargs["timeout"] = min(self.timeout_seconds, remaining_seconds)
            try:
                response = self._client.request(method, path, **kwargs)
                self._finish_response(response)
            except httpx.TransportError as exc:
                self._last_request_at = time.monotonic()
                self._ensure_operation_active()
                if attempt >= self.max_retries:
                    raise YonoteApiError(
                        f"Yonote API {path} temporarily unavailable after {attempts} attempts"
                    ) from exc
                self._sleep_with_operation_deadline(self._retry_delay(attempt))
                continue

            self._last_request_at = time.monotonic()
            self._ensure_operation_active()
            if response.status_code in TRANSIENT_STATUS_CODES and attempt < self.max_retries:
                self._sleep_with_operation_deadline(
                    self._retry_delay(attempt, response)
                )
                continue
            self._raise_for_error(response, path)
            return response

        raise YonoteApiError(f"Yonote API {path} request failed")

    def _install_response_guard(self, response: httpx.Response) -> None:
        self._ensure_operation_active()
        self._validate_response_headers(response)
        if response.is_stream_consumed:
            self._validate_buffered_response(response)
            return
        if not isinstance(response.stream, httpx.SyncByteStream):
            raise YonoteApiError("Yonote API returned an invalid response stream")
        if not isinstance(response.stream, _YonoteResponseStreamGuard):
            response.stream = _YonoteResponseStreamGuard(
                response.stream,
                max_bytes=self.max_response_bytes,
                ensure_operation_active=self._ensure_operation_active,
            )

    def _finish_response(self, response: httpx.Response) -> None:
        try:
            if not response.is_stream_consumed:
                self._install_response_guard(response)
                response.read()
            self._ensure_operation_active()
            self._validate_response_headers(response)
            self._validate_buffered_response(response)
        except BaseException:
            response.close()
            raise

    def _validate_response_headers(self, response: httpx.Response) -> None:
        content_encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if content_encoding not in {"", "identity"}:
            raise YonoteApiError(
                "Yonote API returned unsupported compressed content"
            )
        content_length = response.headers.get("Content-Length", "").strip()
        try:
            declared_bytes = int(content_length)
        except ValueError:
            return
        if declared_bytes > self.max_response_bytes:
            raise YonoteDataTooLarge(
                "Yonote API response exceeded the configured size limit"
            )

    def _validate_buffered_response(self, response: httpx.Response) -> None:
        if len(response.content) > self.max_response_bytes:
            raise YonoteDataTooLarge(
                "Yonote API response exceeded the configured size limit"
            )

    def _wait_for_request_slot(self) -> None:
        if self.min_request_interval_seconds <= 0 or self._last_request_at <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_request_interval_seconds - elapsed
        if remaining > 0:
            self._sleep_with_operation_deadline(remaining)

    def _remaining_operation_seconds(self) -> float | None:
        if self._operation_deadline is None:
            return None
        remaining = self._operation_deadline - time.monotonic()
        if remaining <= 0:
            raise YonoteOperationTimeout(
                "Yonote read exceeded the configured operation deadline"
            )
        return remaining

    def _ensure_operation_active(self) -> None:
        self._remaining_operation_seconds()

    def _sleep_with_operation_deadline(self, delay_seconds: float) -> None:
        remaining = self._remaining_operation_seconds()
        if remaining is None:
            time.sleep(delay_seconds)
            return
        time.sleep(min(delay_seconds, remaining))
        self._ensure_operation_active()

    @staticmethod
    def _retry_delay(attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "").strip()
            try:
                return min(max(float(retry_after), 0.0), 10.0)
            except ValueError:
                pass
        return min(2.0**attempt, 4.0)

    @staticmethod
    def _raise_for_error(response: httpx.Response, path: str) -> None:
        if response.status_code < 400:
            return
        raise YonoteApiError(
            f"Yonote API {path} failed with HTTP {response.status_code}"
        )

    @staticmethod
    def _json_object(response: httpx.Response, path: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise YonoteApiError(
                f"Yonote API {path} returned malformed JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise YonoteApiError(
                f"Yonote API {path} returned malformed JSON"
            )
        return payload


def selected_collection_names() -> tuple[str, ...]:
    settings = get_settings()
    configured = os.getenv("YONOTE_COLLECTION_NAMES", "").strip()
    if configured:
        return split_collection_selectors(configured)
    configured = getattr(settings, "yonote_collection_names", "").strip()
    if configured:
        return split_collection_selectors(configured)
    return DEFAULT_COLLECTION_NAMES


def split_collection_selectors(value: str) -> tuple[str, ...]:
    delimiter = ";" if ";" in value else "|"
    return tuple(item.strip() for item in value.split(delimiter) if item.strip())


def load_yonote_documents(
    client: YonoteClient,
    collection_selectors: tuple[str, ...],
    *,
    limit_documents: int | None = None,
    include_empty: bool = False,
    max_total_text_bytes: int | None = None,
) -> list[YonoteDocument]:
    if not collection_selectors:
        raise YonoteApiError("No Yonote collection selectors are configured")
    if max_total_text_bytes is not None and max_total_text_bytes <= 0:
        raise ValueError("max_total_text_bytes must be positive")
    collections = client.collections()
    selected = match_collections(collections, collection_selectors)
    unmatched_selectors = [
        selector
        for selector in collection_selectors
        if not any(
            _collection_matches_selector(collection, selector)
            for collection in collections
        )
    ]
    if unmatched_selectors:
        raise YonoteApiError(
            "Yonote collections not found: " + ", ".join(unmatched_selectors)
        )

    documents: list[YonoteDocument] = []
    total_text_bytes = 0
    for collection in selected:
        document_refs = client.documents(collection.id)
        by_id = {str(item["id"]): item for item in document_refs if item.get("id")}
        for item in document_refs:
            if limit_documents is not None and len(documents) >= limit_documents:
                return documents
            document_id = str(item.get("id") or "")
            if not document_id:
                continue
            details = client.document_info(document_id)
            text = str(details.get("text") or "").strip()
            if not text and not include_empty:
                continue
            if max_total_text_bytes is not None:
                remaining_text_bytes = max_total_text_bytes - total_text_bytes
                text_bytes = _bounded_utf8_size(text, remaining_text_bytes)
                if text_bytes > remaining_text_bytes:
                    raise YonoteDataTooLarge(
                        "Yonote text exceeded the configured aggregate size limit"
                    )
                total_text_bytes += text_bytes
            documents.append(
                YonoteDocument(
                    id=document_id,
                    title=str(details.get("title") or item.get("title") or "").strip(),
                    text=text,
                    collection_id=collection.id,
                    collection_name=collection.name,
                    url=details.get("url") or item.get("url"),
                    url_id=details.get("urlId") or item.get("urlId"),
                    parent_document_id=details.get("parentDocumentId")
                    or item.get("parentDocumentId"),
                    path_titles=build_document_path(item, by_id),
                    updated_at=details.get("updatedAt") or item.get("updatedAt"),
                    created_at=details.get("createdAt") or item.get("createdAt"),
                    document_type=details.get("type") or item.get("type"),
                )
            )
    return documents


def _bounded_utf8_size(value: str, limit: int) -> int:
    total = 0
    for start in range(0, len(value), _UTF8_SIZE_CHUNK_CHARACTERS):
        total += len(
            value[start : start + _UTF8_SIZE_CHUNK_CHARACTERS].encode("utf-8")
        )
        if total > limit:
            return total
    return total


def match_collections(
    collections: list[YonoteCollection],
    selectors: tuple[str, ...],
) -> list[YonoteCollection]:
    matched: list[YonoteCollection] = []
    for selector in selectors:
        found = next(
            (
                collection
                for collection in collections
                if _collection_matches_selector(collection, selector)
            ),
            None,
        )
        if found and found.id not in {collection.id for collection in matched}:
            matched.append(found)
    return matched


def _collection_matches_selector(
    collection: YonoteCollection,
    selector: str,
) -> bool:
    normalized_selector = normalize_key(selector)
    return (
        selector == collection.id
        or selector == collection.url_id
        or normalized_selector == normalize_key(collection.name)
        or normalized_selector == normalize_key(collection.url or "")
    )


def build_document_path(item: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    path: list[str] = []
    current = item
    seen: set[str] = set()
    while current:
        current_id = str(current.get("id") or "")
        if not current_id or current_id in seen:
            break
        seen.add(current_id)
        title = str(current.get("title") or "").strip()
        if title:
            path.append(title)
        parent_id = current.get("parentDocumentId")
        current = by_id.get(str(parent_id)) if parent_id else {}
    return tuple(reversed(path))


def build_records_from_api_documents(
    documents: list[YonoteDocument],
    *,
    base_url: str,
    extraction_date: date,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    document_slugs = _document_scope_slugs(documents)
    for document in documents:
        event_name = infer_event_name(document)
        category = infer_category(document, event_name)
        sections = parse_text_sections(document.title, document.text)
        for section in sections:
            clean_section_text = clean_markdown_text(section.text)
            for part_index, clean_text in enumerate(
                split_section_text_with_heading(
                    clean_section_text,
                    section.title,
                ),
                start=1,
            ):
                title = section.title
                if part_index > 1:
                    title = f"{section.title}, часть {part_index}"
                if len(clean_text) < 20:
                    continue
                records.append(
                    build_record(
                        document=document,
                        section=section,
                        title=title,
                        text_clean=clean_text,
                        event_name=event_name,
                        category=category,
                        base_url=base_url,
                        extraction_date=extraction_date,
                        document_slug=document_slugs[_document_scope(document)],
                        part_index=part_index,
                    )
                )
    return records


def _document_scope_slugs(
    documents: list[YonoteDocument],
) -> dict[tuple[str, str], str]:
    """Keep legacy slugs unless distinct source documents collide.

    ``urlId`` is normally unique and existing chunk IDs depend on its exact
    slug. The title fallback and truncated URL IDs are not unique, however.
    Resolve only real cross-document collisions so an unrelated new document
    cannot silently reuse another document's chunk IDs, while ordinary
    snapshots retain their existing IDs byte-for-byte.
    """

    base_slug_by_scope: dict[tuple[str, str], str] = {}
    scopes_by_base_slug: dict[str, set[tuple[str, str]]] = {}
    for document in documents:
        scope = _document_scope(document)
        identity = document.url_id or document.title or document.id
        base_slug = slugify(identity, max_length=48)
        full_slug = slugify(identity, max_length=4096)
        # A provider urlId is the existing stable identity and stays byte-for-byte
        # compatible. Title/id fallbacks and truncated identities are ambiguous by
        # construction, so bind them to the immutable document scope immediately;
        # their IDs then stay stable when a similarly named document appears later.
        if not document.url_id or full_slug != base_slug:
            base_slug = f"doc_{_document_scope_hash(scope)}"
        base_slug_by_scope[scope] = base_slug
        scopes_by_base_slug.setdefault(base_slug, set()).add(scope)

    resolved: dict[tuple[str, str], str] = {}
    for scope, base_slug in base_slug_by_scope.items():
        if len(scopes_by_base_slug[base_slug]) == 1:
            resolved[scope] = base_slug
            continue
        resolved[scope] = f"{base_slug}_{_document_scope_hash(scope)}"
    return resolved


def _document_scope(document: YonoteDocument) -> tuple[str, str]:
    return document.collection_id, document.id


def _document_scope_hash(scope: tuple[str, str]) -> str:
    collection_id, document_id = scope
    payload = f"{collection_id}\0{document_id}".encode()
    return sha256(payload).hexdigest()[:_DOCUMENT_SCOPE_HASH_LENGTH]


def parse_text_sections(document_title: str, text: str) -> list[TextSection]:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    heading_indexes = [
        index
        for index, line in enumerate(lines)
        if _looks_like_heading(line) and index + 1 < len(lines)
    ]
    if not heading_indexes:
        clean = text.strip()
        return [TextSection(title=document_title.strip() or "Описание", text=clean, index=1)]

    sections: list[TextSection] = []
    for position, start in enumerate(heading_indexes):
        end = heading_indexes[position + 1] if position + 1 < len(heading_indexes) else len(lines)
        title = lines[start].strip("# ").strip()
        body = "\n".join(lines[start + 1 : end]).strip()
        if body:
            sections.append(
                TextSection(
                    title=title,
                    text=f"{title}\n\n{body}",
                    index=len(sections) + 1,
                )
            )
    if sections:
        return sections
    return [TextSection(title=document_title.strip() or "Описание", text=text.strip(), index=1)]


def _looks_like_heading(line: str) -> bool:
    value = line.strip()
    if not value:
        return False
    if value.startswith("#"):
        return True
    if len(value) > 90:
        return False
    if re.search(r"[.!?;:]$", value):
        return False
    if re.match(r"^[\-*•]\s+", value):
        return False
    if re.match(r"^\d+[\).]\s+", value):
        return False
    words = value.split()
    if len(words) > 8:
        return False
    return bool(re.search(r"[А-Яа-яA-Za-z]", value))


def build_record(
    *,
    document: YonoteDocument,
    section: TextSection,
    title: str,
    text_clean: str,
    event_name: str | None,
    category: str,
    base_url: str,
    extraction_date: date,
    document_slug: str,
    part_index: int,
) -> dict[str, Any]:
    full_topic = slugify(title, max_length=4096)
    topic = slugify(title, max_length=60)
    if part_index > 1 and len(full_topic) > 60:
        topic = f"{topic}_p{part_index:04d}"
    chunk_id = f"yonote_api_{document_slug}_s{section.index:04d}_{topic}"
    source_url = f"{base_url.rstrip('/')}{document.url}" if document.url else None
    forum = event_name if category in {"форумы", "гранты"} else None

    record = {
        "chunk_id": chunk_id,
        "text_raw": text_clean,
        "text_clean": text_clean,
        "status": "published",
        "category": category,
        "forum": forum,
        "forum_normalized": forum,
        "topic": topic,
        "is_generic": forum is None,
        "has_conditional_logic": False,
        "conditions_summary": None,
        "links": [],
        "emails": [],
        "phones": [],
        "dates_mentioned": [],
        "valid_from": None,
        "valid_to": None,
        "source_type": "yonote",
        "source": "yonote_api",
        "source_file": f"{document.collection_name}::{format_path(document.path_titles)}",
        "source_url": source_url,
        "version": "yonote-api-v1",
        "extraction_date": extraction_date.isoformat(),
        "updated_at": document.updated_at or extraction_date.isoformat(),
        "char_count": 0,
        "parent_chunk_id": None,
        "intent_name": title,
        "intent_examples": build_intent_examples(event_name, title, document.title),
        "intent_examples_count": 4,
        "source_category": event_name or document.collection_name,
        "source_sheet": None,
        "source_row": section.index,
        "source_columns": ["document_title", "document_text"],
        "source_document_id": document.id,
        "source_collection_id": document.collection_id,
        "source_collection_name": document.collection_name,
        "source_heading_path": list(document.path_titles) + [title],
        "source_document_updated_at": document.updated_at,
    }
    refresh_text_derived_metadata(record, text_clean)
    return record


def build_intent_examples(
    event_name: str | None,
    section_title: str,
    document_title: str,
) -> list[str]:
    subject = event_name or document_title
    title = section_title.strip()
    title_lower = title.casefold()
    return [
        f"{subject}: {title}",
        f"Расскажи про {title_lower} по {subject}",
        f"Что известно про {subject}: {title_lower}?",
        f"Подскажи {title_lower}",
    ]


def infer_category(document: YonoteDocument, event_name: str | None) -> str:
    haystack = normalize_key(
        " ".join((*document.path_titles, document.title, document.collection_name))
    )
    path_keys = {normalize_key(title) for title in document.path_titles}
    if path_keys & {"о росмолодежи", "структура и направления"}:
        return "общее"
    if "грант" in haystack:
        return "гранты"
    if any(token in haystack for token in ("фгаис", "профиль", "заявк", "авторизац")):
        return "платформа_фгаис"
    event_tokens = ("форум", "мероприят", "фестиваль", "премия", "день молодежи")
    if any(token in haystack for token in event_tokens):
        return "форумы"
    if event_name:
        return "форумы"
    return "общее"


def infer_event_name(document: YonoteDocument) -> str | None:
    candidates = [
        title
        for title in reversed(document.path_titles or (document.title,))
        if normalize_key(title) not in ROOT_TITLES
    ]
    if not candidates:
        return None
    candidate = candidates[0]
    candidate = re.sub(r"\s+20\d{2}\s*$", "", candidate).strip()
    candidate = re.sub(r"^форум\s+", "", candidate, flags=re.IGNORECASE).strip()
    return candidate or None


def format_path(path_titles: tuple[str, ...]) -> str:
    return " > ".join(path_titles)


def normalize_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    normalized = "".join(ch if ch.isalnum() else " " for ch in normalized)
    return " ".join(normalized.split())


def load_json_array(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array")
    return raw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize read-only Yonote documents into normalized KB seed records."
    )
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE_KB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--records-out", type=Path, default=None)
    parser.add_argument("--collection", action="append", default=[])
    parser.add_argument("--replace-existing-yonote", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--limit-documents", type=int, default=None)
    parser.add_argument(
        "--extraction-date",
        type=date.fromisoformat,
        default=date.today(),
        help="Extraction date in YYYY-MM-DD format.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    base_url = str(getattr(settings, "yonote_base_url", "") or "").strip()
    api_token = str(getattr(settings, "yonote_api_token", "") or "").strip()
    timeout_seconds = float(getattr(settings, "yonote_request_timeout_seconds", 30.0))
    selectors = tuple(args.collection) or selected_collection_names()

    with YonoteClient(
        base_url=base_url,
        api_token=api_token,
        timeout_seconds=timeout_seconds,
    ) as client:
        documents = load_yonote_documents(
            client,
            selectors,
            limit_documents=args.limit_documents,
        )

    yonote_records = build_records_from_api_documents(
        documents,
        base_url=base_url,
        extraction_date=args.extraction_date,
    )
    validate_seed_items(yonote_records)

    base_records = load_json_array(args.base)
    merged_records = merge_records(
        base_records,
        yonote_records,
        replace_existing_yonote=args.replace_existing_yonote,
    )
    validate_seed_items(merged_records)

    if args.records_out:
        args.records_out.parent.mkdir(parents=True, exist_ok=True)
        args.records_out.write_text(
            json.dumps(yonote_records, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if not args.validate_only:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(merged_records, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    categories = sorted({str(record.get("category")) for record in yonote_records})
    events = sorted(
        {
            str(record.get("forum_normalized"))
            for record in yonote_records
            if record.get("forum_normalized")
        }
    )
    print(
        "yonote_api_sync_ok "
        f"documents={len(documents)} "
        f"yonote_records={len(yonote_records)} "
        f"base_records={len(base_records)} "
        f"merged_records={len(merged_records)} "
        f"events={len(events)} "
        f"categories={','.join(categories)} "
        f"out={args.out if not args.validate_only else 'validate-only'}"
    )


if __name__ == "__main__":
    main()
