from __future__ import annotations

import ast
import html
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from zipfile import ZipFile

SPREADSHEET_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}
WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

ALLOWED_STATUS = "published"
EXAMPLE_LIMIT = 30

CYRILLIC_TRANSLIT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "c",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)


@dataclass(frozen=True)
class SpreadsheetRow:
    sheet_name: str
    row_number: int
    cells: tuple[str, ...]

    def cell(self, index: int) -> str:
        if index >= len(self.cells):
            return ""
        return self.cells[index].strip()


@dataclass(frozen=True)
class DocxIntentBlock:
    intent: str
    answer: str
    paragraph_start: int
    paragraph_end: int
    block_index: int


def read_xlsx_sheets(path: Path) -> dict[str, list[SpreadsheetRow]]:
    with ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        sheet_targets = _read_sheet_targets(archive)
        sheets: dict[str, list[SpreadsheetRow]] = {}
        for sheet_name, target in sheet_targets:
            root = ET.fromstring(archive.read(target))
            rows: list[SpreadsheetRow] = []
            for row in root.findall(".//main:sheetData/main:row", SPREADSHEET_NS):
                row_number = int(row.attrib.get("r", len(rows) + 1))
                cells: list[str] = []
                for cell in row.findall("main:c", SPREADSHEET_NS):
                    index = _cell_column_index(cell.attrib.get("r", "A1"))
                    while len(cells) <= index:
                        cells.append("")
                    cells[index] = _cell_text(cell, shared_strings)
                if any(value.strip() for value in cells):
                    rows.append(
                        SpreadsheetRow(
                            sheet_name=sheet_name,
                            row_number=row_number,
                            cells=tuple(cells),
                        )
                    )
            sheets[sheet_name] = rows
    return sheets


def read_docx_paragraphs(path: Path) -> list[str]:
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))

    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", WORD_NS):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == _word_tag("t"):
                parts.append(node.text or "")
            elif node.tag == _word_tag("tab"):
                parts.append("\t")
            elif node.tag == _word_tag("br"):
                parts.append("\n")
        text = _normalize_plain_text("".join(parts))
        if text:
            paragraphs.append(text)
    return paragraphs


def parse_docx_intent_blocks(path: Path) -> list[DocxIntentBlock]:
    paragraphs = read_docx_paragraphs(path)
    blocks: list[DocxIntentBlock] = []
    current_intent: str | None = None
    answer_parts: list[str] = []
    answer_start = 0
    answer_end = 0

    def flush() -> None:
        nonlocal current_intent, answer_parts, answer_start, answer_end
        if current_intent and answer_parts:
            answer = "\n".join(part for part in answer_parts if part).strip()
            if answer:
                blocks.append(
                    DocxIntentBlock(
                        intent=current_intent,
                        answer=answer,
                        paragraph_start=answer_start,
                        paragraph_end=answer_end,
                        block_index=len(blocks) + 1,
                    )
                )
        current_intent = None
        answer_parts = []
        answer_start = 0
        answer_end = 0

    for paragraph_number, paragraph in enumerate(paragraphs, start=1):
        intent_match = re.match(r"^Интент\s*:\s*(.+)$", paragraph, flags=re.IGNORECASE)
        if intent_match:
            flush()
            current_intent = intent_match.group(1).strip()
            continue

        text_match = re.match(r"^Текст бота\s*:?\s*(.*)$", paragraph, flags=re.IGNORECASE)
        if text_match and current_intent:
            rest = text_match.group(1).strip()
            answer_start = paragraph_number
            answer_end = paragraph_number
            if rest:
                answer_parts.append(rest)
            continue

        if current_intent and answer_start:
            answer_parts.append(paragraph)
            answer_end = paragraph_number

    flush()
    return blocks


def extract_intent_examples(rows: list[SpreadsheetRow]) -> dict[str, list[str]]:
    examples: dict[str, list[str]] = {}
    current_intent = ""
    for row in rows:
        if row.row_number == 1 and row.cell(1).casefold() == "название интента":
            continue
        if row.cell(1):
            current_intent = row.cell(1)
            examples.setdefault(current_intent, [])
        example = row.cell(3)
        if current_intent and example:
            examples.setdefault(current_intent, []).append(example)
    return examples


def extract_event_registry(rows: list[SpreadsheetRow]) -> list[dict[str, Any]]:
    registry: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if row.row_number == 1 and row.cell(0).casefold() == "entity":
            continue
        entity = row.cell(0)
        if not entity:
            continue
        key = entity.casefold()
        if key in seen:
            continue
        seen.add(key)
        aliases = _split_aliases(row.cell(1))
        if entity not in aliases:
            aliases.insert(0, entity)
        registry.append(
            {
                "name": entity,
                "normalized": entity,
                "aliases": _dedupe_preserve_order(aliases),
            }
        )
    return registry


def build_seed_from_sources(
    xlsx_path: Path,
    docx_paths: list[Path],
    extraction_date: date | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    extraction_date = extraction_date or date.today()
    sheets = read_xlsx_sheets(xlsx_path)
    intent_examples = extract_intent_examples(sheets.get("Интенты", []))
    registry = extract_event_registry(sheets.get("Словарь_событий", []))

    records: list[dict[str, Any]] = []
    records.extend(
        build_excel_answer_records(
            rows=sheets.get("category", []),
            sheet_category=None,
            source_file=xlsx_path.name,
            intent_examples=intent_examples,
            registry=registry,
            extraction_date=extraction_date,
        )
    )
    records.extend(
        build_excel_answer_records(
            rows=sheets.get("FALLBACK", []),
            sheet_category="fallback",
            source_file=xlsx_path.name,
            intent_examples=intent_examples,
            registry=registry,
            extraction_date=extraction_date,
        )
    )

    for docx_path in docx_paths:
        records.extend(build_docx_records(docx_path, registry, extraction_date))

    registry = _extend_registry_from_records(registry, records)
    return records, registry


def build_excel_answer_records(
    rows: list[SpreadsheetRow],
    sheet_category: str | None,
    source_file: str,
    intent_examples: dict[str, list[str]],
    registry: list[dict[str, Any]],
    extraction_date: date,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    current_source_category = sheet_category or ""
    for row in rows:
        source_category = row.cell(0)
        intent = row.cell(1)
        answer_raw = row.cell(2)
        if source_category and source_category != "FALLBACK_CONDITION":
            current_source_category = source_category
        if not intent or not answer_raw:
            continue

        text_clean = clean_bot_text(answer_raw)
        text_clean = clean_excel_answer_text(intent, text_clean)
        if not text_clean:
            continue

        original_category = current_source_category or sheet_category or ""
        category = infer_category(original_category, intent, text_clean, row.sheet_name)
        forum = (
            None
            if sheet_category == "fallback" or row.sheet_name == "FALLBACK"
            else infer_forum(original_category, intent, text_clean, registry)
        )
        examples = intent_examples.get(intent, [])[:EXAMPLE_LIMIT]
        topic = slugify(intent)
        chunk_id = f"xlsx_{slugify(row.sheet_name)}_r{row.row_number:04d}_{topic}"
        records.append(
            base_record(
                chunk_id=chunk_id,
                text_clean=text_clean,
                text_raw=answer_raw,
                status=ALLOWED_STATUS,
                category=category,
                topic=topic,
                forum=forum,
                source_type="xlsx",
                source=f"{source_file}: {row.sheet_name}",
                source_file=source_file,
                extraction_date=extraction_date,
                extra={
                    "intent_name": intent,
                    "intent_examples": examples,
                    "intent_examples_count": len(intent_examples.get(intent, [])),
                    "source_category": original_category or None,
                    "source_sheet": row.sheet_name,
                    "source_row": row.row_number,
                    "source_columns": {
                        "category": "A",
                        "intent": "B",
                        "answer": "C",
                    },
                },
            )
        )
    return records


def build_docx_records(
    path: Path,
    registry: list[dict[str, Any]],
    extraction_date: date,
) -> list[dict[str, Any]]:
    forum = infer_docx_forum(path.name)
    records: list[dict[str, Any]] = []
    for block in parse_docx_intent_blocks(path):
        text_clean = clean_bot_text(block.answer)
        topic = slugify(block.intent)
        source_slug = slugify(path.stem)
        chunk_id = f"docx_{source_slug}_{block.block_index:03d}_{topic}"
        records.append(
            base_record(
                chunk_id=chunk_id,
                text_clean=text_clean,
                text_raw=block.answer,
                status=ALLOWED_STATUS,
                category="форумы",
                topic=topic,
                forum=forum or infer_forum(path.stem, block.intent, block.answer, registry),
                source_type="docx",
                source=path.stem.strip(),
                source_file=path.name,
                extraction_date=extraction_date,
                extra={
                    "intent_name": block.intent,
                    "source_heading_path": [block.intent],
                    "source_paragraph_start": block.paragraph_start,
                    "source_paragraph_end": block.paragraph_end,
                    "source_table_index": None,
                },
            )
        )
    return records


def base_record(
    *,
    chunk_id: str,
    text_clean: str,
    text_raw: str,
    status: str,
    category: str,
    topic: str,
    forum: str | None,
    source_type: str,
    source: str,
    source_file: str,
    extraction_date: date,
    extra: dict[str, Any],
) -> dict[str, Any]:
    emails = extract_emails(text_clean)
    phones = extract_phones(text_clean)
    links = extract_links(text_clean)
    dates_mentioned = extract_dates(text_clean)
    has_conditions = has_conditional_logic(text_clean)
    record: dict[str, Any] = {
        "chunk_id": chunk_id,
        "text_raw": text_raw,
        "text_clean": text_clean,
        "status": status,
        "category": category,
        "forum": forum,
        "forum_normalized": forum,
        "topic": topic,
        "is_generic": forum is None,
        "has_conditional_logic": has_conditions,
        "conditions_summary": None,
        "links": links,
        "emails": emails,
        "phones": phones,
        "dates_mentioned": dates_mentioned,
        "valid_from": None,
        "valid_to": None,
        "source_type": source_type,
        "source": source,
        "source_file": source_file,
        "source_url": None,
        "version": 1,
        "extraction_date": extraction_date.isoformat(),
        "updated_at": extraction_date.isoformat(),
        "char_count": len(text_clean),
        "parent_chunk_id": None,
    }
    record.update(extra)
    return record


def clean_bot_text(value: str) -> str:
    value = _render_known_random_template(value)
    text = html.unescape(value)
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_normalize_plain_text(line) for line in text.split("\n")]
    cleaned = "\n".join(line for line in lines if line).strip()
    cleaned = _strip_export_quote_artifact(cleaned)
    return _fix_known_text_artifacts(cleaned)


def clean_excel_answer_text(intent: str, text: str) -> str:
    if slugify(intent) == "gde_nayti_id_profilya":
        return text.split("\nОшибка входа.", 1)[0].strip()
    return text


def _fix_known_text_artifacts(value: str) -> str:
    replacements = (
        ("Паспорт иличные документы", "Паспорт и личные документы"),
        ("поздее", "позднее"),
        ("Актуалльные", "Актуальные"),
        ("момеент", "момент"),
        ("регисттрация", "регистрация"),
        ("регистрация форум", "регистрация на форум"),
        ("деньмолодёжи. рф", "деньмолодёжи.рф"),
        ("деньмолодежи. рф", "деньмолодежи.рф"),
    )
    for source, target in replacements:
        value = value.replace(source, target)
    return re.sub(r"(?<=[А-Яа-яЁё]):(?=[А-Яа-яЁё])", ": ", value)


def _render_known_random_template(value: str) -> str:
    match = re.fullmatch(r"\s*\{\{\s*(\[[^\]]+\])\s*\|\s*random\s*\}\}\s*", value)
    if not match:
        return value
    try:
        options = ast.literal_eval(match.group(1))
    except (SyntaxError, ValueError):
        return value
    if not isinstance(options, list) or not options:
        return value
    first = options[0]
    return str(first) if isinstance(first, str) and first.strip() else value


def infer_category(source_category: str, intent: str, text: str, sheet_name: str) -> str:
    source_category_normalized = source_category.casefold()
    intent_normalized = intent.casefold()
    text_normalized = text.casefold()
    haystack = f"{source_category} {intent} {text}".casefold()
    if sheet_name == "FALLBACK":
        if any(word in haystack for word in ("техничес", "ошиб", "кэш", "браузер")):
            return "техподдержка"
        if any(word in haystack for word in ("фгаис", "аккаунт", "профил", "заявк", "парол")):
            return "платформа_фгаис"
        if any(word in haystack for word in ("оператор", "привет", "прощан", "благодар")):
            return "навигация"
        return "общее"
    if "грант" in source_category_normalized or "грант" in intent_normalized:
        return "гранты"
    if source_category:
        return "форумы"
    if "грант" in text_normalized:
        return "гранты"
    return "общее"


def infer_forum(
    source_category: str,
    intent: str,
    text: str,
    registry: list[dict[str, Any]],
) -> str | None:
    if "грант" in source_category.casefold():
        return None
    source_category_match = _match_source_category_forum(source_category, registry)
    if source_category_match:
        return source_category_match

    haystack = _normalize_for_match(f"{source_category} {intent} {text}")
    for alias, normalized in _registry_forum_aliases(registry):
        if "грант" in alias:
            continue
        if alias and alias in haystack:
            return normalized
    return None


def _match_source_category_forum(value: str, registry: list[dict[str, Any]]) -> str | None:
    normalized_value = _normalize_for_match(value)
    if not normalized_value:
        return None

    exact_match = _match_forum_alias(value, registry)
    if exact_match:
        return exact_match

    for alias, normalized in _registry_forum_aliases(registry):
        if not alias or "грант" in alias:
            continue
        if len(normalized_value) >= 4 and (
            normalized_value in alias or alias in normalized_value
        ):
            return normalized

    return value.strip() or None


def _match_forum_alias(value: str, registry: list[dict[str, Any]]) -> str | None:
    normalized_value = _normalize_for_match(value)
    if not normalized_value:
        return None
    for alias, normalized in _registry_forum_aliases(registry):
        if alias == normalized_value:
            return normalized
    return None


def _registry_forum_aliases(registry: list[dict[str, Any]]) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    for event in registry:
        normalized = str(event["normalized"])
        aliases.append((_normalize_for_match(normalized), normalized))
        aliases.append((_normalize_for_match(str(event.get("name", normalized))), normalized))
        for alias in event.get("aliases", []):
            aliases.append((_normalize_for_match(str(alias)), normalized))
    return sorted(aliases, key=lambda item: len(item[0]), reverse=True)


def infer_docx_forum(file_name: str) -> str | None:
    normalized = _normalize_for_match(file_name)
    if "rossiyskiy sever" in normalized or "российский север" in normalized:
        return "Российский Север"
    if "bolshe chem puteshestvie" in normalized or "больше чем путешествие" in normalized:
        return "Больше, чем путешествие"
    return None


def has_conditional_logic(text: str) -> bool:
    return bool(
        re.search(
            r"\b(если|в случае|зависит|для вас|для тебя|участники|победители)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def extract_links(text: str) -> list[str]:
    links = re.findall(
        r"https?://[^\s<>)]+|(?:[\w-]+\.)+[\w-]+/[^\s<>)]+",
        text,
    )
    return _dedupe_preserve_order(links)


def extract_emails(text: str) -> list[str]:
    return _dedupe_preserve_order(
        re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text, flags=re.IGNORECASE)
    )


def extract_phones(text: str) -> list[str]:
    candidates = re.findall(
        r"(?:\+7|8)\s*\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}",
        text,
    )
    return _dedupe_preserve_order(_normalize_plain_text(phone) for phone in candidates)


def extract_dates(text: str) -> list[str]:
    month = (
        r"(?:январ[яе]|феврал[яе]|март[ае]?|апрел[яе]|ма[яе]|июн[яе]|июл[яе]|"
        r"август[ае]?|сентябр[яе]|октябр[яе]|ноябр[яе]|декабр[яе])"
    )
    patterns = [
        r"\b\d{1,2}\.\d{1,2}\.\d{4}\b",
        rf"\b\d{{1,2}}\s+{month}(?:\s+\d{{4}}\s*(?:года|г\.?)?)?\b",
    ]
    found: list[str] = []
    for pattern in patterns:
        found.extend(re.findall(pattern, text, flags=re.IGNORECASE))
    return _dedupe_preserve_order(_normalize_plain_text(item) for item in found)


def slugify(value: str, max_length: int = 80) -> str:
    lowered = unicodedata.normalize("NFKC", value).casefold().translate(CYRILLIC_TRANSLIT)
    slug = re.sub(r"[^a-z0-9]+", "_", lowered)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return (slug[:max_length].strip("_") or "chunk")


def _read_shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall("main:si", SPREADSHEET_NS):
        strings.append(
            "".join(node.text or "" for node in item.findall(".//main:t", SPREADSHEET_NS))
        )
    return strings


def _read_sheet_targets(archive: ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets_by_id = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
    targets: list[tuple[str, str]] = []
    for sheet in workbook.findall(".//main:sheet", SPREADSHEET_NS):
        name = sheet.attrib["name"]
        relationship_id = sheet.attrib[f"{{{OFFICE_REL_NS}}}id"]
        targets.append((name, _normalize_xlsx_target(targets_by_id[relationship_id])))
    return targets


def _normalize_xlsx_target(target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    if target.startswith("xl/"):
        return target
    return f"xl/{target.lstrip('/')}"


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return _normalize_plain_text(
            "".join(node.text or "" for node in cell.findall(".//main:t", SPREADSHEET_NS))
        )
    value = cell.find("main:v", SPREADSHEET_NS)
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        return _normalize_plain_text(shared_strings[int(value.text)])
    if cell_type == "b":
        return "true" if value.text == "1" else "false"
    return _normalize_plain_text(value.text)


def _cell_column_index(cell_ref: str) -> int:
    letters = "".join(char for char in cell_ref if char.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + ord(char.upper()) - ord("A") + 1
    return index - 1


def _word_tag(local_name: str) -> str:
    return f"{{{WORD_NS['w']}}}{local_name}"


def _normalize_plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _strip_export_quote_artifact(value: str) -> str:
    if value.endswith("'") and not value.startswith("'"):
        return value[:-1].rstrip()
    return value


def _normalize_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("ё", "е")
    value = value.translate(CYRILLIC_TRANSLIT)
    return re.sub(r"[^a-z0-9а-я]+", " ", value).strip()


def _split_aliases(value: str) -> list[str]:
    aliases = []
    for alias in re.split(r"[,;]", value):
        alias = alias.strip().strip('"').strip("'").strip("«»")
        if alias:
            aliases.append(alias)
    return aliases


def _dedupe_preserve_order(values: list[str] | Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        key = text.casefold()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _extend_registry_from_records(
    registry: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_name = {str(item["normalized"]).casefold(): item for item in registry}
    for record in records:
        forum = record.get("forum_normalized")
        if not forum:
            continue
        key = str(forum).casefold()
        if key not in by_name:
            by_name[key] = {"name": forum, "normalized": forum, "aliases": [forum]}
    return sorted(by_name.values(), key=lambda item: str(item["normalized"]).casefold())
