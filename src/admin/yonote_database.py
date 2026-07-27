from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from scripts.sync_yonote_kb import (
    YonoteDocument,
    parse_text_sections,
)
from src.admin.yonote_sync import load_yonote_documents_from_settings
from src.kb.source_extractors import extract_links

MAX_TEXT_EXPORT_BYTES = 25 * 1024 * 1024
MAX_DATABASE_TEXT_BYTES = 128 * 1024 * 1024
MAX_DATABASE_PULL_SECONDS = 240.0
_UTF8_SIZE_CHUNK_CHARACTERS = 64 * 1024
_WORD_RE = re.compile(r"[^\W_]+(?:[-‑–—'][^\W_]+)*", flags=re.UNICODE)
_PARAGRAPH_RE = re.compile(r"\n\s*\n")


class YonoteDatabaseExportTooLarge(ValueError):
    pass


def count_database(settings: Any) -> dict[str, Any]:
    documents = load_database_documents(settings)
    return build_statistics(documents)


def export_database(settings: Any) -> str:
    documents = load_database_documents(
        settings,
        max_total_text_bytes=MAX_TEXT_EXPORT_BYTES,
    )
    report = build_statistics(documents)
    return render_text_export(
        documents,
        report=report,
        base_url=_setting_text(settings, "yonote_base_url"),
        max_bytes=MAX_TEXT_EXPORT_BYTES,
    )


def load_database_documents(
    settings: Any,
    *,
    max_total_text_bytes: int = MAX_DATABASE_TEXT_BYTES,
) -> list[YonoteDocument]:
    return load_yonote_documents_from_settings(
        settings,
        include_empty=True,
        max_duration_seconds=MAX_DATABASE_PULL_SECONDS,
        max_total_text_bytes=max_total_text_bytes,
    )


def build_statistics(
    documents: Iterable[YonoteDocument],
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    items = list(documents)
    collection_documents: Counter[str] = Counter()
    collection_sections: Counter[str] = Counter()
    collection_characters: Counter[str] = Counter()
    document_type_counts: Counter[str] = Counter()

    documents_with_text = 0
    root_documents = 0
    sections_total = 0
    characters_with_spaces = 0
    characters_without_whitespace = 0
    words_total = 0
    paragraphs_total = 0
    links_total = 0
    updated_values: list[str] = []

    for document in items:
        collection = document.collection_name.strip() or "Без названия"
        document_type = str(document.document_type or "unknown").strip() or "unknown"
        text = _normalized_text(document.text)
        collection_documents[collection] += 1
        document_type_counts[document_type] += 1
        if document.parent_document_id is None:
            root_documents += 1
        if document.updated_at:
            updated_values.append(str(document.updated_at))
        if not text:
            continue

        documents_with_text += 1
        sections = parse_text_sections(document.title, text)
        section_count = len(sections)
        character_count = len(text)
        sections_total += section_count
        characters_with_spaces += character_count
        characters_without_whitespace += sum(
            1 for character in text if not character.isspace()
        )
        words_total += sum(1 for _match in _WORD_RE.finditer(text))
        paragraphs_total += sum(
            1 for part in _PARAGRAPH_RE.split(text) if part.strip()
        )
        links_total += len(extract_links(text))
        collection_sections[collection] += section_count
        collection_characters[collection] += character_count

    collections = [
        {
            "name": name,
            "documents": collection_documents[name],
            "sections": collection_sections[name],
            "characters_with_spaces": collection_characters[name],
        }
        for name in sorted(collection_documents)
    ]
    generated = generated_at or datetime.now(UTC)
    total_documents = len(items)
    return {
        "ok": True,
        "source": "yonote_live_api",
        "read_only": True,
        "scope_definition": "configured_collections_visible_to_service_account",
        "generated_at": generated.astimezone(UTC).isoformat(),
        "documents_total": total_documents,
        "documents_with_text": documents_with_text,
        "documents_without_text": total_documents - documents_with_text,
        "root_documents": root_documents,
        "nested_documents": total_documents - root_documents,
        "sections_total": sections_total,
        "sections_definition": "text_sections_detected_from_headings_or_single_body",
        "characters_with_spaces": characters_with_spaces,
        "characters_without_whitespace": characters_without_whitespace,
        "words_total": words_total,
        "paragraphs_total": paragraphs_total,
        "links_total": links_total,
        "collections_total": len(collection_documents),
        "collections": collections,
        "document_type_counts": dict(
            sorted(document_type_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        "latest_updated_at": max(updated_values, default=None),
    }


def render_text_export(
    documents: Iterable[YonoteDocument],
    *,
    report: dict[str, Any] | None = None,
    base_url: str,
    max_bytes: int | None = None,
) -> str:
    items = sorted(
        documents,
        key=lambda document: (
            document.collection_name.casefold(),
            tuple(part.casefold() for part in document.path_titles),
            document.title.casefold(),
            document.id,
        ),
    )
    stats = report or build_statistics(items)
    lines: list[str] = []
    byte_count = 0

    def append_lines(*values: str) -> None:
        nonlocal byte_count
        for value in values:
            separator_bytes = 1 if lines else 0
            remaining_bytes = (
                None
                if max_bytes is None
                else max_bytes - byte_count - separator_bytes
            )
            if remaining_bytes is not None and remaining_bytes < 0:
                raise _export_too_large(max_bytes)
            value_bytes = _bounded_utf8_size(value, remaining_bytes)
            if remaining_bytes is not None and value_bytes > remaining_bytes:
                raise _export_too_large(max_bytes)
            lines.append(value)
            byte_count += separator_bytes + value_bytes

    append_lines(
        "ВЫГРУЗКА ДОСТУПНОГО СОДЕРЖИМОГО YONOTE",
        "",
        "Источник: живой Yonote API, только чтение",
        (
            "Охват: разрешённые коллекции и страницы, доступные "
            "сервисной учётной записи"
        ),
        f"Сформировано (UTC): {stats['generated_at']}",
        f"Коллекций: {stats['collections_total']}",
        f"Документов/страниц: {stats['documents_total']}",
        f"Текстовых секций (оценка): {stats['sections_total']}",
        f"Символов с пробелами: {stats['characters_with_spaces']}",
        f"Слов: {stats['words_total']}",
        "",
        "Документы Yonote этим действием не изменялись.",
    )
    current_collection: str | None = None
    for index, document in enumerate(items, start=1):
        collection = document.collection_name.strip() or "Без названия"
        if collection != current_collection:
            append_lines(
                "",
                "=" * 80,
                f"КОЛЛЕКЦИЯ: {collection}",
                "=" * 80,
            )
            current_collection = collection

        path = " / ".join(part.strip() for part in document.path_titles if part.strip())
        title = document.title.strip() or f"Документ {index}"
        url = _absolute_document_url(base_url, document.url)
        text = _normalized_text(document.text)
        append_lines(
            "",
            "-" * 80,
            f"{index}. {title}",
            f"Путь: {path or title}",
            f"Обновлено: {document.updated_at or 'не указано'}",
        )
        if url:
            append_lines(f"Ссылка: {url}")
        append_lines(
            "",
            text or "[Документ не содержит текстового содержимого]",
        )
    append_lines("")
    return "\n".join(lines)


def _bounded_utf8_size(value: str, limit: int | None) -> int:
    total = 0
    for start in range(0, len(value), _UTF8_SIZE_CHUNK_CHARACTERS):
        total += len(
            value[start : start + _UTF8_SIZE_CHUNK_CHARACTERS].encode("utf-8")
        )
        if limit is not None and total > limit:
            return total
    return total


def _export_too_large(max_bytes: int) -> YonoteDatabaseExportTooLarge:
    limit_mib = max_bytes / (1024 * 1024)
    label = f"{limit_mib:g} МиБ" if limit_mib >= 1 else f"{max_bytes} байт"
    return YonoteDatabaseExportTooLarge(
        f"Текстовая выгрузка Yonote превышает безопасный лимит {label}."
    )


def _absolute_document_url(base_url: str, document_url: str | None) -> str:
    value = str(document_url or "").strip()
    if not value:
        return ""
    if value.startswith(("https://", "http://")):
        return value
    return f"{base_url.rstrip('/')}/{value.lstrip('/')}"


def _normalized_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _setting_text(settings: Any, key: str) -> str:
    return str(getattr(settings, key, "") or "").strip()
