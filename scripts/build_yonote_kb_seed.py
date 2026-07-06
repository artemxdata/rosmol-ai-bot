from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from zipfile import ZipFile

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.index_kb import validate_seed_items
from src.kb.source_extractors import (
    extract_dates,
    extract_emails,
    extract_links,
    extract_phones,
    slugify,
)

DEFAULT_SOURCE_DIR = Path("data/private/yonote")
DEFAULT_BASE_KB = Path("data/knowledge_base_seed.json")
DEFAULT_OUTPUT = Path("data/knowledge_base_seed.json")

MIN_SECTION_CHARS = 20
MAX_CHUNK_CHARS = 2400

FORUM_NAME_ALIASES = {
    "иволга": "Иволга",
    "остроvа": "Островa",
    "российский север": "Российский Север",
    "утро": "Утро",
    "шум": "Шум",
    "форум молодых ученых полюс": "Полюс",
    "форум молодых учёных полюс": "Полюс",
    "экосистема заповедный край": "Экосистема",
    "дистанционная программа всероссийского молодежного образовательного форума шум": "Шум",
    "дистанционная программа всероссийского молодёжного образовательного форума шум": "Шум",
}


@dataclass(frozen=True)
class MarkdownDocument:
    zip_name: str
    md_name: str
    forum: str
    text: str


@dataclass(frozen=True)
class Section:
    title: str
    text: str
    index: int


def build_yonote_records(source_dir: Path, extraction_date: date) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    for document in read_yonote_markdown_documents(source_dir):
        content_hash = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)

        sections = parse_markdown_sections(document.text)
        for section in sections:
            for part_index, text in enumerate(split_section_text(section.text), start=1):
                title = section.title if part_index == 1 else f"{section.title}, часть {part_index}"
                if part_index > 1 and not text.startswith(section.title):
                    text = f"{section.title}\n\n{text}"
                clean_text = clean_markdown_text(text)
                if len(clean_text) < MIN_SECTION_CHARS:
                    continue
                records.append(
                    build_record(
                        document=document,
                        section=section,
                        title=title,
                        text_clean=clean_text,
                        extraction_date=extraction_date,
                    )
                )

    return records


def read_yonote_markdown_documents(source_dir: Path) -> list[MarkdownDocument]:
    if not source_dir.exists():
        raise FileNotFoundError(f"Yonote source directory not found: {source_dir}")

    documents: list[MarkdownDocument] = []
    for archive_path in sorted(source_dir.glob("*.zip")):
        with ZipFile(archive_path) as archive:
            for name in sorted(archive.namelist()):
                if not name.casefold().endswith(".md"):
                    continue
                text = archive.read(name).decode("utf-8", errors="replace")
                forum = infer_forum_name(Path(name).stem)
                documents.append(
                    MarkdownDocument(
                        zip_name=archive_path.name,
                        md_name=name,
                        forum=forum,
                        text=text,
                    )
                )
    return documents


def infer_forum_name(document_name: str) -> str:
    value = document_name.strip()
    value = re.sub(r"\s*\(\d+\)\s*$", "", value)
    value = re.sub(r"\s+2026\s*$", "", value)
    normalized = normalize_key(value)

    if normalized in FORUM_NAME_ALIASES:
        return FORUM_NAME_ALIASES[normalized]

    for prefix in (
        "всероссийский форум ",
        "молодежный форум ",
        "молодёжный форум ",
        "форум ",
    ):
        if normalized.startswith(prefix):
            return value
    return value


def parse_markdown_sections(text: str) -> list[Section]:
    sections: list[Section] = []
    current_title = "Описание"
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body:
            sections.append(
                Section(
                    title=current_title.strip(),
                    text=f"{current_title}\n\n{body}",
                    index=len(sections) + 1,
                )
            )
        current_lines = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            flush()
            current_title = strip_markdown_markup(heading.group(2))
            continue
        current_lines.append(line)

    flush()
    return sections


def split_section_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    clean = text.strip()
    if len(clean) <= max_chars:
        return [clean]

    paragraphs = [
        piece
        for part in re.split(r"\n\s*\n", clean)
        for piece in split_long_paragraph(part.strip(), max_chars=max_chars)
        if piece.strip()
    ]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        extra = len(paragraph) + (2 if current else 0)
        if current and current_len + extra > max_chars:
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_len = len(paragraph)
        else:
            current.append(paragraph)
            current_len += extra
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]

    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph)
        if sentence.strip()
    ]
    if len(sentences) <= 1:
        return [
            paragraph[index : index + max_chars].strip()
            for index in range(0, len(paragraph), max_chars)
            if paragraph[index : index + max_chars].strip()
        ]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        extra = len(sentence) + (1 if current else 0)
        if current and current_len + extra > max_chars:
            chunks.append(" ".join(current))
            current = [sentence]
            current_len = len(sentence)
        else:
            current.append(sentence)
            current_len += extra
    if current:
        chunks.append(" ".join(current))
    return chunks


def clean_markdown_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\[([^\]]+)\]\(attachment:[^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 \2", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[^>]+>", "", text)
    text = strip_markdown_markup(text)
    text = re.sub(r"^\s*\|[-:\s|]+\|\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_markdown_markup(text: str) -> str:
    text = re.sub(r"[*_`]+", "", text)
    return text.strip()


def build_record(
    *,
    document: MarkdownDocument,
    section: Section,
    title: str,
    text_clean: str,
    extraction_date: date,
) -> dict[str, Any]:
    topic = slugify(title, max_length=60)
    forum_slug = slugify(document.forum, max_length=48)
    chunk_id = f"yonote_{forum_slug}_s{section.index:04d}_{topic}"

    return {
        "chunk_id": chunk_id,
        "text_raw": text_clean,
        "text_clean": text_clean,
        "status": "published",
        "category": "форумы",
        "forum": document.forum,
        "forum_normalized": document.forum,
        "topic": topic,
        "is_generic": False,
        "has_conditional_logic": False,
        "conditions_summary": None,
        "links": extract_links(text_clean),
        "emails": extract_emails(text_clean),
        "phones": extract_phones(text_clean),
        "dates_mentioned": extract_dates(text_clean),
        "valid_from": None,
        "valid_to": None,
        "source_type": "yonote",
        "source": "yonote",
        "source_file": f"{document.zip_name}::{document.md_name}",
        "source_url": None,
        "version": "yonote-2026",
        "extraction_date": extraction_date.isoformat(),
        "updated_at": extraction_date.isoformat(),
        "char_count": len(text_clean),
        "parent_chunk_id": None,
        "intent_name": title,
        "intent_examples": build_intent_examples(document.forum, title),
        "intent_examples_count": 3,
        "source_category": document.forum,
        "source_sheet": None,
        "source_row": section.index,
        "source_columns": ["markdown_heading", "markdown_body"],
    }


def build_intent_examples(forum: str, title: str) -> list[str]:
    title_clean = title.strip()
    title_lower = title_clean.casefold()
    return [
        f"{forum}: {title_clean}",
        f"Расскажи про {title_lower} на форуме {forum}",
        f"Что известно про {forum}: {title_lower}?",
    ]


def normalize_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    normalized = "".join(ch if ch.isalnum() else " " for ch in normalized)
    return " ".join(normalized.split())


def load_json_array(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array")
    return raw


def merge_records(
    base_records: list[dict[str, Any]],
    yonote_records: list[dict[str, Any]],
    *,
    replace_existing_yonote: bool,
) -> list[dict[str, Any]]:
    if replace_existing_yonote:
        base_records = [
            record
            for record in base_records
            if str(record.get("source_type") or "") != "yonote"
        ]
    return [*base_records, *yonote_records]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build normalized KB records from private Yonote ZIP exports."
    )
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE_KB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--records-out", type=Path, default=None)
    parser.add_argument("--replace-existing-yonote", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--extraction-date",
        type=date.fromisoformat,
        default=date.today(),
        help="Extraction date in YYYY-MM-DD format.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    yonote_records = build_yonote_records(args.source_dir, args.extraction_date)
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

    forums = sorted({record["forum_normalized"] for record in yonote_records})
    print(
        "yonote_kb_built "
        f"yonote_records={len(yonote_records)} "
        f"base_records={len(base_records)} "
        f"merged_records={len(merged_records)} "
        f"forums={len(forums)} "
        f"out={args.out if not args.validate_only else 'validate-only'}"
    )


if __name__ == "__main__":
    main()
