from __future__ import annotations

# ruff: noqa: E402,I001

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.kb.source_extractors import read_xlsx_sheets  # noqa: E402


DEFAULT_INPUT = Path("data/private/tickets/RAG_Dataset.xlsx")
DEFAULT_OUTPUT_DIR = Path("data/private/tickets/analysis")
DEFAULT_FORUMS = Path("data/forums_registry.json")

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s\-()]?\d{3}[\s\-()]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)")
PASSPORT_RE = re.compile(r"(?<!\d)\d{4}\s?\d{6}(?!\d)")
DATE_RE = re.compile(r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4})(?!\d)")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")

BOILERPLATE_PHRASES = [
    "заботливый бот росмолодёжи",
    "заботливый бот росмолодежи",
    "чем я могу быть полезен",
    "перевожу на оператора",
    "пожалуйста, ожидайте",
    "служба заботы отдыхает",
    "присоединюсь к диалогу",
    "благодарим за обращение в службу заботы",
    "если у вас есть к нам вопросы",
]

PROFANITY_MARKERS = [
    "хер",
    "пизд",
    "дерьм",
    "мраз",
    "урод",
    "бред",
    "маразм",
]

CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "гранты",
        (
            "грант",
            "средств",
            "субсид",
            "проект",
            "отчет",
            "отчёт",
            "договор",
            "смет",
        ),
    ),
    (
        "платформа_фгаис",
        (
            "фгаис",
            "myrosmol",
            "личный кабинет",
            "профил",
            "id проф",
            "авторизац",
            "регистрац",
            "заявк",
        ),
    ),
    (
        "техподдержка",
        (
            "ошибк",
            "не работает",
            "не могу войти",
            "парол",
            "код",
            "кнопк",
            "доступ",
            "сайт",
            "техничес",
        ),
    ),
    (
        "форумы",
        (
            "форум",
            "смен",
            "мероприят",
            "участ",
            "прожив",
            "проезд",
            "трансфер",
            "питани",
            "письмо-вызов",
            "письмо вызов",
        ),
    ),
    (
        "навигация",
        (
            "оператор",
            "куда обратиться",
            "контакт",
            "телефон",
            "почт",
            "статус",
            "специалист",
        ),
    ),
]

TOPIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("регистрация_и_заявка", ("регистрац", "подать заяв", "заявк", "дедлайн")),
    ("статус_заявки", ("статус", "результат", "отбор", "прошел", "прошёл", "резерв")),
    ("письмо_и_уведомления", ("письмо", "уведомлен", "не пришло", "почт")),
    ("личный_кабинет_и_профиль", ("профил", "личный кабинет", "id проф", "аккаунт")),
    ("доступ_и_техническая_ошибка", ("не могу войти", "ошибк", "парол", "код", "кнопк")),
    ("возраст_и_условия_участия", ("возраст", "лет", "ограничен", "кто может")),
    ("проезд_и_трансфер", ("проезд", "дорог", "трансфер", "перелет", "перелёт")),
    ("проживание_и_питание", ("прожив", "питани", "гостиниц", "общежит")),
    ("документы", ("документ", "паспорт", "справк", "согласие")),
    ("грантовая_отчетность", ("отчет", "отчёт", "договор", "смет", "вернуть средств")),
    ("контакты_и_оператор", ("оператор", "контакт", "телефон", "куда писать")),
]

ESCALATION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("personal_status", ("мой статус", "статус заявки", "почему отказ", "результат отбора")),
    ("technical_issue", ("не могу войти", "не работает", "ошибка", "код не приходит")),
    ("operator_requested", ("оператор", "специалист", "живой человек")),
    ("legal_or_financial_risk", ("вернуть средства", "суд", "жалоб", "прокурат", "мвд")),
    ("unsafe_or_abusive", tuple(PROFANITY_MARKERS)),
]


@dataclass(frozen=True)
class ForumAlias:
    normalized: str
    aliases: tuple[str, ...]


def analyze_dataset(
    input_path: Path,
    output_dir: Path,
    forums_path: Path,
    *,
    max_golden: int,
    max_pairs: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    forums = load_forum_aliases(forums_path)
    rows = load_ticket_rows(input_path)
    normalized = [normalize_ticket(row, forums) for row in rows]

    profile = build_profile(normalized, input_path)
    taxonomy_rows = build_taxonomy(normalized)
    top_questions = build_top_questions(normalized)
    golden = build_golden_candidates(normalized, max_items=max_golden)
    pairs = build_reranker_pairs(golden, normalized, max_pairs=max_pairs)
    gap_report = build_gap_report(profile, taxonomy_rows, top_questions)

    write_json(output_dir / "dataset_profile.json", profile)
    write_jsonl(output_dir / "tickets_normalized.jsonl", normalized)
    write_csv(output_dir / "intent_taxonomy.csv", taxonomy_rows)
    write_markdown(output_dir / "top_questions.md", top_questions_to_markdown(top_questions))
    write_json(output_dir / "golden_set_candidates.json", golden)
    write_jsonl(output_dir / "reranker_calibration_pairs.jsonl", pairs)
    (output_dir / "kb_gap_report.md").write_text(gap_report, encoding="utf-8")
    (output_dir / "analysis_summary.md").write_text(
        build_summary(profile, golden, pairs, output_dir),
        encoding="utf-8",
    )

    return {
        "input": str(input_path),
        "output_dir": str(output_dir),
        "tickets_total": profile["tickets_total"],
        "golden_candidates": len(golden),
        "reranker_pairs": len(pairs),
        "top_categories": profile["category_counts"],
        "top_escalation_reasons": profile["escalation_reason_counts"],
    }


def load_ticket_rows(path: Path) -> list[dict[str, str]]:
    sheets = read_xlsx_sheets(path)
    if not sheets:
        raise ValueError("xlsx workbook has no readable sheets")
    first_sheet = next(iter(sheets.values()))
    if not first_sheet:
        raise ValueError("xlsx first sheet is empty")

    headers = [cell.strip() for cell in first_sheet[0].cells]
    rows: list[dict[str, str]] = []
    for source_row in first_sheet[1:]:
        row = {header: source_row.cell(index) for index, header in enumerate(headers)}
        if any(value for value in row.values()):
            rows.append(row)
    return rows


def normalize_ticket(row: dict[str, str], forums: list[ForumAlias]) -> dict[str, Any]:
    ticket_id = row.get("id") or row.get("unique_id") or ""
    title_raw = compact_text(row.get("title") or "")
    messages_raw = compact_text(row.get("messages") or "")
    segments = split_message_segments(messages_raw)
    meaningful_segments = [segment for segment in segments if not is_boilerplate(segment)]
    text_for_classification = " ".join([title_raw, *meaningful_segments[:8]])

    title_masked, title_pii = mask_pii(title_raw)
    messages_masked, messages_pii = mask_pii(messages_raw)
    question_candidate = choose_question_candidate(title_raw, meaningful_segments)
    answer_candidate = choose_answer_candidate(meaningful_segments, question_candidate)
    question_masked, question_pii = mask_pii(question_candidate)
    answer_masked, answer_pii = mask_pii(answer_candidate)

    category = classify_category(text_for_classification)
    topic = classify_topic(text_for_classification)
    forum = detect_forum(text_for_classification, forums)
    escalation_reason = classify_escalation(text_for_classification, row)
    should_escalate = escalation_reason is not None
    difficulty = classify_difficulty(
        text_for_classification,
        typical=row.get("typical_atypical") or "",
        should_escalate=should_escalate,
    )
    answerable_by_kb = (
        bool(answer_candidate)
        and not should_escalate
        and category in {"форумы", "гранты", "платформа_фгаис", "навигация"}
    )

    pii_types = sorted(set(title_pii + messages_pii + question_pii + answer_pii))
    return {
        "ticket_id": ticket_id,
        "ticket_hash": sha1_short(ticket_id or messages_raw or title_raw),
        "unique_id": row.get("unique_id") or "",
        "created_at": row.get("date_created") or "",
        "updated_at": row.get("date_updated") or "",
        "closed_at": row.get("date_closed") or "",
        "channel": row.get("department") or "",
        "status": row.get("status") or "",
        "typical_atypical": row.get("typical_atypical") or "",
        "responsible_present": bool(row.get("responsible_name")),
        "title_masked": title_masked,
        "question_candidate": question_masked,
        "answer_candidate": answer_masked,
        "messages_masked": messages_masked,
        "message_segments_count": len(segments),
        "meaningful_segments_count": len(meaningful_segments),
        "category": category,
        "topic": topic,
        "intent": build_intent(category, topic),
        "subintent": "",
        "forum_normalized": forum,
        "has_pii": bool(pii_types),
        "pii_types": pii_types,
        "answerable_by_kb": answerable_by_kb,
        "needs_clarification": needs_clarification(text_for_classification),
        "should_escalate": should_escalate,
        "escalation_reason": escalation_reason,
        "difficulty": difficulty,
        "quality_notes": build_quality_notes(row, question_candidate, answer_candidate),
    }


def split_message_segments(messages: str) -> list[str]:
    if not messages:
        return []
    segments = [compact_text(segment) for segment in re.split(r"\s+---\s+", messages)]
    return [segment for segment in segments if segment]


def is_boilerplate(text: str) -> bool:
    normalized = normalize_for_match(text)
    if len(normalized) < 4:
        return True
    return any(phrase in normalized for phrase in BOILERPLATE_PHRASES)


def choose_question_candidate(title: str, segments: list[str]) -> str:
    if 8 <= len(title) <= 500:
        return title
    question_segments = [
        segment
        for segment in segments
        if "?" in segment
        or any(
            marker in normalize_for_match(segment)
            for marker in ("как ", "где ", "что ", "когда ", "куда ")
        )
    ]
    if question_segments:
        return max(question_segments[:5], key=len)[:1000]
    return max(segments[:5], key=len, default="")[:1000]


def choose_answer_candidate(segments: list[str], question: str) -> str:
    candidates = []
    question_norm = normalize_for_match(question)
    for segment in segments:
        norm = normalize_for_match(segment)
        if norm == question_norm or is_boilerplate(segment):
            continue
        if len(segment) < 50:
            continue
        candidates.append(segment)
    if not candidates:
        return ""
    return candidates[-1][:2000]


def classify_category(text: str) -> str:
    normalized = normalize_for_match(text)
    scores: Counter[str] = Counter()
    for category, markers in CATEGORY_RULES:
        for marker in markers:
            if marker in normalized:
                scores[category] += 1
    if not scores:
        return "другое"
    return scores.most_common(1)[0][0]


def classify_topic(text: str) -> str:
    normalized = normalize_for_match(text)
    scores: Counter[str] = Counter()
    for topic, markers in TOPIC_RULES:
        for marker in markers:
            if marker in normalized:
                scores[topic] += 1
    if not scores:
        return "прочее"
    return scores.most_common(1)[0][0]


def classify_escalation(text: str, row: dict[str, str]) -> str | None:
    normalized = normalize_for_match(text)
    if (row.get("status") or "").strip() in {"open", "v-processe"}:
        return "open_or_in_progress"
    for reason, markers in ESCALATION_RULES:
        if any(marker in normalized for marker in markers):
            return reason
    return None


def classify_difficulty(text: str, *, typical: str, should_escalate: bool) -> str:
    normalized = normalize_for_match(text)
    multi_topic_markers = sum(
        1 for _topic, markers in TOPIC_RULES if any(marker in normalized for marker in markers)
    )
    if should_escalate or "нетиповой" in normalize_for_match(typical) or multi_topic_markers >= 3:
        return "complex"
    if len(text) > 350 or multi_topic_markers == 2:
        return "medium"
    return "simple"


def detect_forum(text: str, forums: list[ForumAlias]) -> str | None:
    normalized = normalize_for_match(text)
    matches: list[tuple[int, str]] = []
    for forum in forums:
        for alias in forum.aliases:
            alias_norm = normalize_for_match(alias)
            if alias_norm and alias_norm in normalized:
                matches.append((len(alias_norm), forum.normalized))
    if not matches:
        return None
    return sorted(matches, reverse=True)[0][1]


def needs_clarification(text: str) -> bool:
    normalized = normalize_for_match(text)
    if len(normalized) < 20:
        return True
    vague = ("вопрос", "помогите", "не понятно", "что делать", "как быть")
    return any(marker == normalized for marker in vague)


def build_profile(records: list[dict[str, Any]], input_path: Path) -> dict[str, Any]:
    return {
        "input_file": str(input_path),
        "tickets_total": len(records),
        "status_counts": dict(Counter(item["status"] or "empty" for item in records).most_common()),
        "channel_counts": dict(
            Counter(item["channel"] or "empty" for item in records).most_common()
        ),
        "typical_counts": dict(
            Counter(item["typical_atypical"] or "empty" for item in records).most_common()
        ),
        "category_counts": dict(Counter(item["category"] for item in records).most_common()),
        "topic_counts": dict(Counter(item["topic"] for item in records).most_common()),
        "forum_counts": dict(
            Counter(item["forum_normalized"] or "unknown" for item in records).most_common(50)
        ),
        "difficulty_counts": dict(Counter(item["difficulty"] for item in records).most_common()),
        "escalation_reason_counts": dict(
            Counter(item["escalation_reason"] or "none" for item in records).most_common()
        ),
        "answerable_by_kb_count": sum(1 for item in records if item["answerable_by_kb"]),
        "should_escalate_count": sum(1 for item in records if item["should_escalate"]),
        "has_pii_count": sum(1 for item in records if item["has_pii"]),
        "segment_counts": {
            "avg": round(
                sum(item["message_segments_count"] for item in records) / len(records), 2
            )
            if records
            else 0,
            "max": max((item["message_segments_count"] for item in records), default=0),
        },
    }


def build_taxonomy(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        key = (
            item["category"],
            item["topic"],
            item["intent"],
            item["forum_normalized"] or "",
        )
        grouped[key].append(item)

    rows: list[dict[str, Any]] = []
    for (category, topic, intent, forum), items in sorted(
        grouped.items(), key=lambda pair: len(pair[1]), reverse=True
    ):
        examples = [
            item["question_candidate"]
            for item in items
            if item.get("question_candidate")
        ][:5]
        escalation_rate = sum(1 for item in items if item["should_escalate"]) / len(items)
        rows.append(
            {
                "category": category,
                "topic": topic,
                "intent": intent,
                "subintent": "",
                "forum_normalized": forum,
                "frequency": len(items),
                "example_queries": " || ".join(examples),
                "typical_answer_summary": summarize_answers(items),
                "should_escalate_default": escalation_rate >= 0.5,
            }
        )
    return rows


def build_top_questions(records: list[dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        question_key = normalize_question_key(
            item.get("question_candidate") or item["title_masked"]
        )
        if not question_key:
            continue
        key = (question_key, item["category"], item["topic"], item["forum_normalized"] or "")
        grouped[key].append(item)

    results: list[dict[str, Any]] = []
    for (question_key, category, topic, forum), items in sorted(
        grouped.items(), key=lambda pair: len(pair[1]), reverse=True
    )[:limit]:
        best = max(items, key=lambda item: len(item.get("answer_candidate") or ""))
        results.append(
            {
                "normalized_question": question_key,
                "example_query": best.get("question_candidate") or "",
                "frequency": len(items),
                "category": category,
                "topic": topic,
                "forum_normalized": forum,
                "typical_answer": best.get("answer_candidate") or "",
                "should_escalate": (
                    sum(1 for item in items if item["should_escalate"]) >= len(items) / 2
                ),
            }
        )
    return results


def build_golden_candidates(
    records: list[dict[str, Any]],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    scored = []
    for item in records:
        query = item.get("question_candidate") or ""
        answer = item.get("answer_candidate") or ""
        if len(query) < 8:
            continue
        score = 0
        if item["answerable_by_kb"] and len(answer) >= 40:
            score += 5
        if item["should_escalate"]:
            score += 3
        if item["forum_normalized"]:
            score += 2
        if item["difficulty"] == "complex":
            score += 2
        if item["category"] != "другое":
            score += 1
        if len(query) > 500:
            score -= 2
        scored.append((score, item))

    selected: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True):
        dedupe_key = normalize_question_key(item["question_candidate"])
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        selected.append(
            {
                "id": f"ticket::{item['ticket_hash']}",
                "query": item["question_candidate"],
                "query_variants": [],
                "expected_answer": "" if item["should_escalate"] else item["answer_candidate"],
                "expected_answer_contains": extract_key_phrases(item["answer_candidate"]),
                "forbidden_answer_contains": ["не найдено в источниках", "я думаю", "скорее всего"],
                "forum_normalized": item["forum_normalized"],
                "category": item["category"],
                "topic": item["topic"],
                "intent": item["intent"],
                "expected_escalated": item["should_escalate"],
                "expected_escalation_reason": item["escalation_reason"],
                "difficulty": item["difficulty"],
                "source_ticket_ids": [item["ticket_id"]],
                "source_refs": ["RAG_Dataset.xlsx"],
                "notes": item["quality_notes"],
            }
        )
        if len(selected) >= max_items:
            break
    return selected


def build_reranker_pairs(
    golden: list[dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    max_pairs: int,
) -> list[dict[str, Any]]:
    answer_pool = [
        item
        for item in records
        if item.get("answer_candidate") and len(item["answer_candidate"]) >= 40
    ]
    pairs: list[dict[str, Any]] = []
    for case in golden:
        if case["expected_escalated"] or not case["expected_answer"]:
            continue
        negatives = []
        for item in answer_pool:
            if item["ticket_id"] in case["source_ticket_ids"]:
                continue
            same_topic = item["topic"] == case["topic"]
            other_forum = (item["forum_normalized"] or "") != (case["forum_normalized"] or "")
            same_category = item["category"] == case["category"]
            if (same_topic and other_forum) or same_category:
                negatives.append(item["answer_candidate"])
            if len(negatives) >= 3:
                break
        if negatives:
            pairs.append(
                {
                    "query": case["query"],
                    "positive_text": case["expected_answer"],
                    "hard_negative_texts": negatives,
                    "forum_normalized": case["forum_normalized"],
                    "category": case["category"],
                    "topic": case["topic"],
                    "relevance_positive": 3,
                    "relevance_negatives": [1 for _ in negatives],
                    "source_ticket_ids": case["source_ticket_ids"],
                }
            )
        if len(pairs) >= max_pairs:
            break
    return pairs


def build_gap_report(
    profile: dict[str, Any],
    taxonomy_rows: list[dict[str, Any]],
    top_questions: list[dict[str, Any]],
) -> str:
    lines = [
        "# KB Gap Report",
        "",
        "## Executive Summary",
        "",
        f"- Tickets total: `{profile['tickets_total']}`",
        f"- Answerable by KB candidates: `{profile['answerable_by_kb_count']}`",
        f"- Escalation candidates: `{profile['should_escalate_count']}`",
        f"- Tickets with detected PII: `{profile['has_pii_count']}`",
        "",
        "## Category Counts",
        "",
        *_counter_lines(profile["category_counts"]),
        "",
        "## Top Forums",
        "",
        *_counter_lines(profile["forum_counts"], limit=30),
        "",
        "## Escalation Reasons",
        "",
        *_counter_lines(profile["escalation_reason_counts"]),
        "",
        "## High-Frequency Intent Groups",
        "",
    ]
    for row in taxonomy_rows[:30]:
        lines.append(
            f"- `{row['frequency']}` {row['category']} / {row['topic']} / "
            f"{row['forum_normalized'] or 'unknown'}"
        )

    lines.extend(
        [
            "",
            "## Top Questions For Review",
            "",
        ]
    )
    for item in top_questions[:30]:
        lines.append(
            f"- `{item['frequency']}` {item['category']} / {item['topic']} / "
            f"{item['forum_normalized'] or 'unknown'}: {item['example_query'][:180]}"
        )

    lines.extend(
        [
            "",
            "## Recommendations",
            "",
            "- Сначала вручную проверить top intent groups и подтвердить реальные "
            "`expected_answer`.",
            "- Для форумов с похожими вопросами добавить `forum_normalized` и aliases "
            "в KB metadata.",
            "- Для `personal_status` и открытых статусов оставить controlled escalation.",
            "- По reranker calibration pairs прогнать threshold sweep до изменения `.env`.",
            "- Сырые и нормализованные приватные данные не коммитить.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_summary(
    profile: dict[str, Any],
    golden: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    output_dir: Path,
) -> str:
    return "\n".join(
        [
            "# Ticket Dataset Analysis Summary",
            "",
            f"- Tickets total: `{profile['tickets_total']}`",
            f"- Golden candidates: `{len(golden)}`",
            f"- Reranker pairs: `{len(pairs)}`",
            f"- Output dir: `{output_dir}`",
            f"- Answerable by KB candidates: `{profile['answerable_by_kb_count']}`",
            f"- Escalation candidates: `{profile['should_escalate_count']}`",
            f"- Tickets with detected PII: `{profile['has_pii_count']}`",
            "",
            "## Generated Files",
            "",
            "- `dataset_profile.json`",
            "- `tickets_normalized.jsonl`",
            "- `intent_taxonomy.csv`",
            "- `top_questions.md`",
            "- `golden_set_candidates.json`",
            "- `reranker_calibration_pairs.jsonl`",
            "- `kb_gap_report.md`",
            "",
        ]
    )


def top_questions_to_markdown(items: list[dict[str, Any]]) -> str:
    lines = [
        "# Top Questions",
        "",
        "| Rank | Frequency | Category | Topic | Forum | Escalate | Example Query |",
        "|---:|---:|---|---|---|---|---|",
    ]
    for index, item in enumerate(items, start=1):
        lines.append(
            "| "
            f"{index} | {item['frequency']} | {item['category']} | {item['topic']} | "
            f"{item['forum_normalized'] or 'unknown'} | {item['should_escalate']} | "
            f"{markdown_cell(item['example_query'][:220])} |"
        )
    return "\n".join(lines) + "\n"


def load_forum_aliases(path: Path) -> list[ForumAlias]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    forums = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        normalized = str(item.get("normalized") or item.get("name") or "").strip()
        aliases = [str(alias).strip() for alias in item.get("aliases") or [] if str(alias).strip()]
        if normalized:
            aliases.append(normalized)
            forums.append(ForumAlias(normalized=normalized, aliases=tuple(sorted(set(aliases)))))
    return forums


def mask_pii(text: str) -> tuple[str, list[str]]:
    masked = text or ""
    pii_types: list[str] = []
    for pii_type, regex, placeholder in (
        ("email", EMAIL_RE, "[EMAIL]"),
        ("phone", PHONE_RE, "[ТЕЛЕФОН]"),
        ("passport", PASSPORT_RE, "[ДОКУМЕНТ]"),
        ("date", DATE_RE, "[ДАТА]"),
        ("url", URL_RE, "[URL]"),
    ):
        masked, count = regex.subn(placeholder, masked)
        if count:
            pii_types.append(pii_type)
    return compact_text(masked), pii_types


def compact_text(text: str) -> str:
    text = str(text or "").replace("\xa0", " ")
    return WHITESPACE_RE.sub(" ", text).strip()


def normalize_for_match(text: str) -> str:
    return compact_text(text).casefold().replace("ё", "е")


def normalize_question_key(text: str) -> str:
    normalized = normalize_for_match(text)
    normalized = URL_RE.sub("[url]", normalized)
    normalized = EMAIL_RE.sub("[email]", normalized)
    normalized = PHONE_RE.sub("[phone]", normalized)
    normalized = re.sub(r"[^\wа-яА-ЯёЁ\s\[\]]+", " ", normalized)
    normalized = WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized[:240]


def build_intent(category: str, topic: str) -> str:
    return f"{category}.{topic}"


def summarize_answers(items: list[dict[str, Any]]) -> str:
    answers = [item["answer_candidate"] for item in items if item.get("answer_candidate")]
    if not answers:
        return ""
    best = max(answers, key=len)
    return best[:300]


def extract_key_phrases(text: str, limit: int = 3) -> list[str]:
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    phrases = [sentence.strip() for sentence in sentences if 20 <= len(sentence.strip()) <= 180]
    return phrases[:limit]


def build_quality_notes(row: dict[str, str], question: str, answer: str) -> str:
    notes = []
    if not question:
        notes.append("missing_question_candidate")
    if not answer:
        notes.append("missing_answer_candidate")
    if (row.get("typical_atypical") or "").casefold() == "нетиповой":
        notes.append("marked_atypical")
    return ", ".join(notes)


def sha1_short(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


def markdown_cell(value: str) -> str:
    return compact_text(value).replace("|", "\\|").replace("\n", " ")


def _counter_lines(counter: dict[str, int], limit: int | None = None) -> list[str]:
    items = list(counter.items())
    if limit is not None:
        items = items[:limit]
    return [f"- `{count}` {name}" for name, count in items]


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_markdown(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze private support tickets for RAG quality.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--forums", type=Path, default=DEFAULT_FORUMS)
    parser.add_argument("--max-golden", type=int, default=800)
    parser.add_argument("--max-pairs", type=int, default=1000)
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    summary = analyze_dataset(
        input_path=args.input,
        output_dir=args.out_dir,
        forums_path=args.forums,
        max_golden=args.max_golden,
        max_pairs=args.max_pairs,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
