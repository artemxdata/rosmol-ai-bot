from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import zipfile
from collections import Counter, defaultdict, deque
from pathlib import Path
from posixpath import normpath
from typing import Any
from xml.etree import ElementTree

NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

EXPECTED_GROUPS = {
    "typical": "Типовой",
    "atypical": "Нетиповой",
}

QUESTION_SIGNALS = (
    "как ",
    "где ",
    "когда ",
    "куда ",
    "почему ",
    "что делать",
    "можно ли",
    "подскаж",
    "не могу",
    "не получается",
    "хочу",
    "нужно",
    "регистрац",
    "заявк",
    "форум",
    "грант",
    "сертификат",
    "документ",
    "проезд",
    "проживан",
    "трансфер",
    "питани",
    "фгаис",
    "госуслуг",
    "оператор",
    "специалист",
    "суицид",
    "самоуб",
    "буллинг",
    "травл",
)

SKIP_MARKERS = (
    "/start",
    "чем я могу быть полезен",
    "заботливый бот",
    "служба заботы",
    "благодарим",
    "для оценки качества",
    "надеюсь, мне удалось помочь",
    "сейчас служба заботы отдыхает",
    "мы вернёмся",
    "мы вернемся",
    "с уважением",
    "рады помочь",
    "передали ваше обращение",
    "ваше обращение передано",
    "обращение передано",
    "ожидайте ответа",
    "оцените качество",
    "спасибо за обращение",
)

PII_MARKERS = (
    "паспорт",
    "снилс",
    "инн",
    "дата рождения",
    "место рождения",
    "серия паспорта",
    "номер паспорта",
    "мой номер",
    "мой телефон",
    "моя почта",
    "мой email",
    "меня зовут",
    "фамили",
    "имя ",
    "свидетельство",
    "паспортные данные",
)

TOPIC_KEYWORDS = {
    "registration": ("регистрац", "зарегистр", "фгаис", "госуслуг"),
    "application": ("заявк", "подать", "отклон", "изменен"),
    "forum": ("форум", "мероприят"),
    "grant": ("грант", "отчет", "отчёт", "средств"),
    "documents": ("документ", "положен", "памятк", "письмо-вызов", "письмо вызов"),
    "travel": ("проезд", "билет", "трансфер", "дорог"),
    "accommodation": ("проживан", "питан", "заселен", "отель"),
    "certificate": ("сертификат",),
    "tech": ("ошибк", "не отображ", "не откры", "не могу войти", "личном кабинете"),
    "operator": ("оператор", "специалист", "живой человек"),
    "safety": ("суицид", "самоуб", "буллинг", "травл", "угрож", "насили"),
    "offtopic": ("погода", "курс доллара", "пробки", "такси до дома"),
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a sanitized 50/50 typical-atypical demo ask set "
            "from private RAG_Dataset.xlsx."
        )
    )
    parser.add_argument("--source", default="data/private/tickets/RAG_Dataset.xlsx")
    parser.add_argument("--output", default="reports/rag_dataset_demo_100_cases.json")
    parser.add_argument("--profile-output", default="reports/rag_dataset_demo_profile.json")
    parser.add_argument("--typical-source", default="reports/pre_channel_typical_50_cases.json")
    parser.add_argument("--atypical-source", default="reports/pre_channel_atypical_100_cases.json")
    parser.add_argument("--typical", type=int, default=50)
    parser.add_argument("--atypical", type=int, default=50)
    parser.add_argument(
        "--raw-ticket-candidates",
        action="store_true",
        help=(
            "Use sanitized snippets extracted from private ticket messages. "
            "Default is safer: use curated sanitized prompts and only profile the private dataset."
        ),
    )
    args = parser.parse_args()

    source = Path(args.source)
    rows = read_xlsx_rows(source)
    if args.raw_ticket_candidates:
        cases, profile = build_cases(
            rows,
            typical_limit=args.typical,
            atypical_limit=args.atypical,
        )
    else:
        cases, profile = build_curated_cases(
            rows,
            typical_source=Path(args.typical_source),
            atypical_source=Path(args.atypical_source),
            typical_limit=args.typical,
            atypical_limit=args.atypical,
        )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")

    profile_output = Path(args.profile_output)
    profile_output.parent.mkdir(parents=True, exist_ok=True)
    profile_output.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"cases={len(cases)} typical={profile['selected_counts'].get('typical', 0)} "
        f"atypical={profile['selected_counts'].get('atypical', 0)} output={output}"
    )


def read_xlsx_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"RAG dataset not found: {path}")

    with zipfile.ZipFile(path) as workbook:
        shared_strings = _read_shared_strings(workbook)
        sheet_name = _first_sheet_name(workbook)
        root = ElementTree.fromstring(workbook.read(sheet_name))

    raw_rows: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", NS):
        values: list[str] = []
        for cell in row.findall("x:c", NS):
            column_index = _column_index(cell.attrib.get("r", ""))
            while len(values) < column_index:
                values.append("")
            values.append(_cell_value(cell, shared_strings))
        raw_rows.append(values)

    if not raw_rows:
        return []

    headers = [header.strip() for header in raw_rows[0]]
    records: list[dict[str, str]] = []
    for raw in raw_rows[1:]:
        record = {
            header: raw[index] if index < len(raw) else ""
            for index, header in enumerate(headers)
        }
        records.append(record)
    return records


def build_cases(
    rows: list[dict[str, str]],
    *,
    typical_limit: int,
    atypical_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    dataset_label_counts = Counter(
        str(row.get("typical_atypical") or "empty").strip() for row in rows
    )
    buckets: dict[str, list[dict[str, Any]]] = {"typical": [], "atypical": []}
    skipped: Counter[str] = Counter()

    for row_number, row in enumerate(rows, 2):
        group = _group_from_label(row.get("typical_atypical"))
        if not group:
            skipped["label"] += 1
            continue

        for segment in _split_messages(row.get("messages") or ""):
            query = sanitize_query(segment)
            if not query:
                skipped["empty_after_sanitize"] += 1
                continue

            reason = _skip_reason(query)
            if reason:
                skipped[reason] += 1
                continue

            behavior = expected_behavior(query)
            if group == "typical" and behavior != "answer":
                skipped["typical_non_answer_behavior"] += 1
                continue

            topics = detect_topics(query)
            if not topics:
                skipped["topic"] += 1
                continue

            buckets[group].append(
                {
                    "query": query,
                    "row_number": row_number,
                    "ticket_hash": _ticket_hash(row),
                    "source_label": EXPECTED_GROUPS[group],
                    "topics": topics,
                    "expected_behavior": behavior,
                    "score": candidate_score(query, topics, group),
                }
            )

    selected_typical = select_diverse(buckets["typical"], typical_limit)
    selected_atypical = select_diverse(buckets["atypical"], atypical_limit)
    cases = [
        _case_from_candidate(candidate, "typical", index)
        for index, candidate in enumerate(selected_typical, 1)
    ]
    cases.extend(
        _case_from_candidate(candidate, "atypical", index)
        for index, candidate in enumerate(selected_atypical, 1)
    )

    profile = {
        "source": "data/private/tickets/RAG_Dataset.xlsx",
        "dataset_rows": len(rows),
        "dataset_label_counts": dict(dataset_label_counts),
        "candidate_counts": {key: len(value) for key, value in buckets.items()},
        "selected_counts": dict(Counter(case["demo_group"] for case in cases)),
        "selected_topic_counts": dict(
            Counter(topic for case in cases for topic in case.get("topics", []))
        ),
        "expected_behavior_counts": dict(
            Counter(case.get("expected_behavior") or "unscored" for case in cases)
        ),
        "skipped_counts": dict(skipped),
        "privacy_note": (
            "Cases are sanitized representatives generated from private tickets. "
            "Raw ticket text, IDs, names, contacts, and message history are not exported."
        ),
    }
    return cases, profile


def build_curated_cases(
    rows: list[dict[str, str]],
    *,
    typical_source: Path,
    atypical_source: Path,
    typical_limit: int,
    atypical_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    typical_cases = _load_json_cases(typical_source)[:typical_limit]
    atypical_cases = _load_json_cases(atypical_source)[:atypical_limit]
    if len(typical_cases) < typical_limit:
        raise RuntimeError(f"Not enough typical cases in {typical_source}: {len(typical_cases)}")
    if len(atypical_cases) < atypical_limit:
        raise RuntimeError(f"Not enough atypical cases in {atypical_source}: {len(atypical_cases)}")

    cases = [
        _curated_case(case, "typical", index)
        for index, case in enumerate(typical_cases, 1)
    ]
    cases.extend(
        _curated_case(case, "atypical", index)
        for index, case in enumerate(atypical_cases, 1)
    )

    dataset_label_counts = Counter(
        str(row.get("typical_atypical") or "empty").strip() for row in rows
    )
    profile = {
        "source": "data/private/tickets/RAG_Dataset.xlsx",
        "dataset_rows": len(rows),
        "dataset_label_counts": dict(dataset_label_counts),
        "selected_counts": dict(Counter(case["demo_group"] for case in cases)),
        "selected_topic_counts": dict(
            Counter(topic for case in cases for topic in case.get("topics", []))
        ),
        "expected_behavior_counts": dict(
            Counter(case.get("expected_behavior") or "unscored" for case in cases)
        ),
        "curated_sources": {
            "typical": str(typical_source),
            "atypical": str(atypical_source),
        },
        "privacy_note": (
            "RAG_Dataset.xlsx is used only to profile the real ticket mix. "
            "The exported demo cases are sanitized representative prompts, not raw ticket text."
        ),
    }
    return cases, profile


def _load_json_cases(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Case source not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _curated_case(source_case: dict[str, Any], group: str, index: int) -> dict[str, Any]:
    topics = _topics_from_case(source_case)
    tags = [*source_case.get("tags", [])]
    tags.extend(
        [
            "rag_dataset_demo",
            f"demo_group:{group}",
            "source_basis:rag_dataset_profile",
        ]
    )
    case = {
        **source_case,
        "id": f"rag_dataset_demo::{group}::{index:03d}",
        "user_id": f"rag-dataset-demo-{group}-{index:03d}",
        "channel": "api",
        "demo_group": group,
        "source_dataset": "RAG_Dataset.xlsx",
        "source_basis": (
            "sanitized representative prompt selected from local eval fixtures "
            "using the RAG_Dataset typical/atypical profile; raw ticket text is not exported"
        ),
        "topics": topics,
        "tags": tags,
    }
    return case


def _topics_from_case(case: dict[str, Any]) -> list[str]:
    topics = [
        tag.removeprefix("topic:")
        for tag in case.get("tags", [])
        if isinstance(tag, str) and tag.startswith("topic:")
    ]
    if topics:
        return topics
    return detect_topics(str(case.get("query") or ""))


def select_diverse(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = _dedupe_key(candidate["query"])
        current = unique.get(key)
        if not current or candidate["score"] > current["score"]:
            unique[key] = candidate

    topic_buckets: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for candidate in sorted(unique.values(), key=lambda item: (-item["score"], item["query"])):
        topic_buckets[candidate["topics"][0]].append(candidate)

    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    topic_order = sorted(topic_buckets, key=lambda topic: (-len(topic_buckets[topic]), topic))
    while len(selected) < limit and any(topic_buckets.values()):
        progressed = False
        for topic in topic_order:
            if len(selected) >= limit:
                break
            bucket = topic_buckets[topic]
            while bucket:
                candidate = bucket.popleft()
                key = _dedupe_key(candidate["query"])
                if key in selected_keys:
                    continue
                selected.append(candidate)
                selected_keys.add(key)
                progressed = True
                break
        if not progressed:
            break

    if len(selected) < limit:
        raise RuntimeError(
            f"Not enough sanitized candidates: selected={len(selected)} limit={limit}"
        )
    return selected[:limit]


def sanitize_query(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(
        r"\s*(?:--\s*)?Отправлено\s+(?:из|с)\s+.*$",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"\s+--\s+.*$", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+|www\.\S+", "[link]", text, flags=re.IGNORECASE)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}", "[email]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "[id]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:\+?\d[\s()\-]*){10,}",
        "[phone]",
        text,
    )
    text = re.sub(r"\b\d{4,}\b", "[number]", text)
    text = re.sub(r"\b[A-ZА-ЯЁ]{2,}-\d+\b", "[ticket]", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n\"'«»")
    text = _strip_greeting(text)
    text = re.sub(r"\s+[А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+$", "", text)
    return text.strip(" \t\r\n\"'«»()")


def expected_behavior(query: str) -> str:
    normalized = query.casefold().replace("ё", "е")
    if any(marker in normalized for marker in TOPIC_KEYWORDS["safety"]):
        return "escalate"
    if any(marker in normalized for marker in TOPIC_KEYWORDS["operator"]):
        return "escalate"
    if any(marker in normalized for marker in TOPIC_KEYWORDS["offtopic"]):
        return "scope_note"
    if normalized in {"подать заявку", "как подать заявку", "хочу подать заявку"}:
        return "clarify"
    return "answer"


def detect_topics(query: str) -> list[str]:
    normalized = query.casefold().replace("ё", "е")
    topics = [
        topic
        for topic, markers in TOPIC_KEYWORDS.items()
        if any(marker in normalized for marker in markers)
    ]
    return topics


def candidate_score(query: str, topics: list[str], group: str) -> int:
    normalized = query.casefold()
    score = 0
    if "?" in query:
        score += 4
    if 30 <= len(query) <= 160:
        score += 3
    if len(topics) >= 2:
        score += 3
    if group == "atypical" and len(topics) >= 2:
        score += 4
    if any(topic in {"registration", "application", "forum", "grant"} for topic in topics):
        score += 2
    if any(marker in normalized for marker in ("не могу", "не получается", "ошибка")):
        score += 2
    return score


def _skip_reason(query: str) -> str | None:
    normalized = query.casefold().replace("ё", "е")
    if len(query) < 12:
        return "too_short"
    if len(query) > 220:
        return "too_long"
    if "[email]" in query or "[phone]" in query or "[id]" in query:
        return "contacts"
    if any(marker in normalized for marker in SKIP_MARKERS):
        return "service_text"
    if any(marker in normalized for marker in PII_MARKERS):
        return "pii_marker"
    if not ("?" in query or any(marker in normalized for marker in QUESTION_SIGNALS)):
        return "no_question_signal"
    return None


def _split_messages(messages: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"(?:^|\n)\s*---+\s*(?:\n|$)", messages or "")
        if part.strip()
    ]


def _case_from_candidate(candidate: dict[str, Any], group: str, index: int) -> dict[str, Any]:
    behavior = candidate["expected_behavior"]
    return {
        "id": f"rag_dataset_demo::{group}::{index:03d}",
        "query": candidate["query"],
        "user_id": f"rag-dataset-demo-{group}-{index:03d}",
        "channel": "api",
        "expected_behavior": behavior,
        "expected_escalated": True if behavior == "escalate" else None,
        "expected_escalation_reason": None,
        "expected_chunk_ids": [],
        "expected_answer_contains": [],
        "expected_generator_model": None,
        "demo_group": group,
        "source_dataset": "RAG_Dataset.xlsx",
        "source_label": candidate["source_label"],
        "source_ticket_hash": candidate["ticket_hash"],
        "topics": candidate["topics"],
        "tags": [
            "rag_dataset_demo",
            f"demo_group:{group}",
            f"expected_behavior:{behavior}",
            *[f"topic:{topic}" for topic in candidate["topics"]],
        ],
    }


def _read_shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall("x:si", NS):
        strings.append("".join(text.text or "" for text in item.findall(".//x:t", NS)))
    return strings


def _first_sheet_name(workbook: zipfile.ZipFile) -> str:
    workbook_root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
    first_sheet = workbook_root.find(".//x:sheets/x:sheet", NS)
    if first_sheet is None:
        raise ValueError("Workbook has no sheets")
    relation_id = first_sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
    rels_root = ElementTree.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    for rel in rels_root:
        if rel.attrib.get("Id") == relation_id:
            target = rel.attrib["Target"].lstrip("/")
            if target.startswith("xl/"):
                return normpath(target)
            return normpath("xl/" + target)
    raise ValueError(f"Cannot resolve sheet relation: {relation_id}")


def _cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//x:t", NS))
    value = cell.find("x:v", NS)
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        index = int(value.text)
        return shared_strings[index] if index < len(shared_strings) else ""
    return value.text


def _column_index(cell_ref: str) -> int:
    letters = "".join(char for char in cell_ref if char.isalpha())
    if not letters:
        return 0
    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _group_from_label(label: str | None) -> str | None:
    normalized = str(label or "").strip().casefold()
    if normalized == "типовой":
        return "typical"
    if normalized == "нетиповой":
        return "atypical"
    return None


def _strip_greeting(text: str) -> str:
    text = re.sub(
        r"^(здравствуйте|добрый день|добрый вечер|привет)[,!.\s]+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _dedupe_key(query: str) -> str:
    return re.sub(r"[^а-яa-z0-9]+", "", query.casefold().replace("ё", "е"))


def _ticket_hash(row: dict[str, str]) -> str:
    value = str(row.get("id") or row.get("unique_id") or row.get("title") or "")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


if __name__ == "__main__":
    main()
