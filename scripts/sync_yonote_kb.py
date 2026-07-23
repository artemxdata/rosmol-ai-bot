from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.build_yonote_kb_seed import (
    clean_markdown_text,
    merge_records,
    split_section_text,
)
from scripts.index_kb import validate_seed_items
from src.config import get_settings
from src.kb.source_extractors import (
    extract_dates,
    extract_emails,
    extract_links,
    extract_phones,
    has_conditional_logic,
    slugify,
)
from src.kb.temporal import registration_deadline_iso

DEFAULT_BASE_KB = Path("data/knowledge_base_seed.json")
DEFAULT_OUTPUT = Path("data/knowledge_base_seed.json")
DEFAULT_COLLECTION_NAMES = (
    "Росмолодёжь: общее, структура, направления",
    "Росмолодёжь: мероприятия",
)
TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
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


class YonoteClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        timeout_seconds: float,
        max_retries: int = 2,
        min_request_interval_seconds: float = 0.15,
    ) -> None:
        if not api_token.strip():
            raise YonoteApiError("YONOTE_API_TOKEN is required")
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, int(max_retries))
        self.min_request_interval_seconds = max(
            0.0,
            float(min_request_interval_seconds),
        )
        self._last_request_at = 0.0
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0)),
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "rosmol-ai-bot/yonote-sync",
            },
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
        payload = response.json()
        document = (payload.get("data") or {}).get("document")
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
            payload = response.json()
            data = payload.get("data") or []
            if not isinstance(data, list):
                raise YonoteApiError(f"Malformed paginated response from {path}")
            records.extend(item for item in data if isinstance(item, dict))
            if len(data) < limit:
                break
            offset += limit
        return records

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            self._wait_for_request_slot()
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                self._last_request_at = time.monotonic()
                if attempt >= self.max_retries:
                    raise YonoteApiError(
                        f"Yonote API {path} temporarily unavailable after {attempts} attempts"
                    ) from exc
                time.sleep(self._retry_delay(attempt))
                continue

            self._last_request_at = time.monotonic()
            if response.status_code in TRANSIENT_STATUS_CODES and attempt < self.max_retries:
                time.sleep(self._retry_delay(attempt, response))
                continue
            self._raise_for_error(response, path)
            return response

        raise YonoteApiError(f"Yonote API {path} request failed")

    def _wait_for_request_slot(self) -> None:
        if self.min_request_interval_seconds <= 0 or self._last_request_at <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_request_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

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
        detail = ""
        try:
            payload = response.json()
            detail = str(payload.get("message") or payload.get("error") or "")
        except ValueError:
            detail = response.text[:200]
        raise YonoteApiError(f"Yonote API {path} failed: {response.status_code} {detail}")


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
) -> list[YonoteDocument]:
    if not collection_selectors:
        raise YonoteApiError("No Yonote collection selectors are configured")
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
            if not text:
                continue
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
    for document in documents:
        event_name = infer_event_name(document)
        category = infer_category(document, event_name)
        sections = parse_text_sections(document.title, document.text)
        for section in sections:
            for part_index, part in enumerate(split_section_text(section.text), start=1):
                title = section.title
                if part_index > 1:
                    title = f"{section.title}, часть {part_index}"
                    if not part.startswith(section.title):
                        part = f"{section.title}\n\n{part}"
                clean_text = clean_markdown_text(part)
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
                    )
                )
    return records


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
) -> dict[str, Any]:
    topic = slugify(title, max_length=60)
    doc_slug = slugify(document.url_id or document.title or document.id, max_length=48)
    chunk_id = f"yonote_api_{doc_slug}_s{section.index:04d}_{topic}"
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
        "has_conditional_logic": has_conditional_logic(text_clean),
        "conditions_summary": None,
        "links": extract_links(text_clean),
        "emails": extract_emails(text_clean),
        "phones": extract_phones(text_clean),
        "dates_mentioned": extract_dates(text_clean),
        "valid_from": None,
        "valid_to": None,
        "source_type": "yonote",
        "source": "yonote_api",
        "source_file": f"{document.collection_name}::{format_path(document.path_titles)}",
        "source_url": source_url,
        "version": "yonote-api-v1",
        "extraction_date": extraction_date.isoformat(),
        "updated_at": document.updated_at or extraction_date.isoformat(),
        "char_count": len(text_clean),
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
    registration_deadline = registration_deadline_iso(text_clean)
    if registration_deadline:
        record["registration_deadline"] = registration_deadline
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
