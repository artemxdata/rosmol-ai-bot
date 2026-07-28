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
from src.graph.response_profiles import infer_response_profile  # noqa: E402
from src.models import QueryAnalysis  # noqa: E402
from src.response_contract import ResponseProfileName  # noqa: E402


DEFAULT_INPUT = Path("data/private/tickets/RAG_Dataset.xlsx")
DEFAULT_OUTPUT_DIR = Path("data/private/tickets/analysis")
DEFAULT_FORUMS = Path("data/forums_registry.json")

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s\-()]?\d{3}[\s\-()]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)")
PASSPORT_RE = re.compile(r"(?<!\d)\d{4}\s?\d{6}(?!\d)")
SNILS_RE = re.compile(r"(?<!\d)\d{3}[-\s]\d{3}[-\s]\d{3}\s?\d{2}(?!\d)")
VK_ID_RE = re.compile(r"(?<!\w)(?:vk[_\s-]?id|id)\s*[:=]?\s*\d{4,}(?!\w)", re.IGNORECASE)
HANDLE_RE = re.compile(r"(?<![\w@])@[a-zа-яё0-9_][a-zа-яё0-9_.-]{2,}", re.IGNORECASE)
LONG_ID_RE = re.compile(r"(?<!\d)\d{11,20}(?!\d)")
FIO_CONTEXT_RE = re.compile(
    r"\b(?:фио|ф\.?\s*и\.?\s*о\.?|меня зовут)\s*[:=-]?\s*"
    r"[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,2}",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4})(?!\d)")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")

BOILERPLATE_PHRASES = [
    "заботливый бот росмолодёжи",
    "заботливый бот росмолодежи",
    "заботливый бот создан",
    "чем я могу быть полезен",
    "перевожу на оператора",
    "пожалуйста, ожидайте",
    "служба заботы отдыхает",
    "присоединюсь к диалогу",
    "благодарим за обращение в службу заботы",
    "если у вас есть к нам вопросы",
    "для оценки качества обслуживания",
    "мы уже занимаемся вашим вопросом",
    "вернемся с ответом в течение 15 минут",
    "вернёмся с ответом в течение 15 минут",
    "выбери, что тебя интересует",
    "все мероприятия росмолодёжи собраны",
    "все мероприятия росмолодежи собраны",
    "надеюсь, мне удалось помочь",
    "я люблю быть полезным",
    "ваше сообщение не доставлено",
    "недоставленное сообщение",
    "mail failure",
    "recent login from a new device",
    "вход с нового устройства",
    "код проверки для двухфакторной авторизации",
    "ответ на форму",
    "ваш запрос направлен в службу заботы",
    "мы рады, что вопрос решён",
    "мы рады, что вопрос решен",
    "приветствуем вас и благодарим за обратную связь",
    "в списке нет нужного мероприятия? мы поможем",
    "я помощник росмолодёжи",
    "я помощник росмолодежи",
    "я не совсем понял вопрос",
    "вот актуальные контакты",
    "служба заботы всегда рядом",
    "кто-то входит в ваш аккаунт",
    "2 new comments in",
    "[support] null",
]

SUPPORT_ANSWER_MARKERS = [
    "можно",
    "необходимо",
    "нужно",
    "для этого",
    "для того",
    "чтобы",
    "перейдите",
    "проверьте",
    "обратитесь",
    "подать заяв",
    "подается",
    "подаётся",
    "регистрация",
    "личный кабинет",
    "поступил ответ",
    "обрати внимание",
    "если",
]

ANSWER_OPENING_MARKERS = [
    "здесь все зависит",
    "здесь всё зависит",
    "для этого",
    "для того чтобы",
    "обрати внимание",
    "подать заявку можно",
    "регистрация проходит",
    "перейдите",
    "проверьте",
    "необходимо",
    "поступил ответ",
    "к сожалению",
]

USER_REQUEST_MARKERS = [
    " я ",
    " мне ",
    " меня ",
    " мой ",
    " моя ",
    " мои ",
    " моего ",
    " моему ",
    "прошу",
    "подскажите",
    "помогите",
    "не могу",
    "не успел",
    "не пришло",
    "не получил",
    "почему",
    "можете пожалуйста",
    "хочу",
    "возник",
    "буду ждать",
]

QUESTION_MARKERS = [
    "как ",
    "где ",
    "что ",
    "когда ",
    "куда ",
    "почему ",
    "можно ли",
    "что делать",
    "как быть",
    "подскажите",
    "помогите",
]

THREAD_ARTIFACT_MARKERS = [
    "commented",
    "служба заботы росмолодёжи:",
    "служба заботы росмолодежи:",
    "запрос отправлял",
    "запрос отправляла",
    "отправлено из",
    "с уважением",
]
THREAD_TIMESTAMP_RE = re.compile(r"\b\d{4}\s*г\.\s*,?\s*\d{1,2}:\d{2}\b", re.IGNORECASE)

LOW_SIGNAL_TITLE_MARKERS = [
    "личное сообщение",
    "входящий вызов",
    "служба заботы",
    "обращение",
    "без темы",
    "image-",
    ".jpeg",
    ".jpg",
    ".png",
    "re:",
    "fw:",
    "fwd:",
    "ответ на форму",
    "ваше сообщение не доставлено",
    "недоставленное сообщение",
    "mail failure",
    "recent login from a new device",
    "вход с нового устройства",
    "код проверки",
    "двухфакторной авторизации",
    "request from",
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
    product_splits, product_split_summary = build_product_eval_splits(normalized)
    gap_report = build_gap_report(profile, taxonomy_rows, top_questions)

    write_json(output_dir / "dataset_profile.json", profile)
    write_jsonl(output_dir / "tickets_normalized.jsonl", normalized)
    write_csv(output_dir / "intent_taxonomy.csv", taxonomy_rows)
    write_markdown(output_dir / "top_questions.md", top_questions_to_markdown(top_questions))
    write_json(output_dir / "golden_set_candidates.json", golden)
    write_jsonl(output_dir / "reranker_calibration_pairs.jsonl", pairs)
    for split_name, cases in product_splits.items():
        write_jsonl(output_dir / f"product_{split_name}_cases.jsonl", cases)
    write_json(output_dir / "product_split_summary.json", product_split_summary)
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
        "product_eval_candidates": product_split_summary["total"],
        "product_split_counts": product_split_summary["split_counts"],
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
        if not any(value for value in row.values()):
            continue
        if rows and _is_ticket_continuation(row, rows[-1]):
            rows[-1]["messages"] = _join_message_parts(
                rows[-1].get("messages") or "",
                row.get("messages") or "",
            )
            continue
        rows.append(row)
    return rows


def _is_ticket_continuation(row: dict[str, str], previous: dict[str, str]) -> bool:
    ticket_id = str(row.get("id") or "").strip()
    if not ticket_id or ticket_id != str(previous.get("id") or "").strip():
        return False
    if str(row.get("unique_id") or "").strip():
        return False
    return not any(
        str(value or "").strip()
        for key, value in row.items()
        if key not in {"id", "messages"}
    )


def _join_message_parts(left: str, right: str) -> str:
    parts = [str(part or "").strip() for part in (left, right)]
    return "\n".join(part for part in parts if part)


def normalize_ticket(row: dict[str, str], forums: list[ForumAlias]) -> dict[str, Any]:
    ticket_id_raw = row.get("id") or row.get("unique_id") or ""
    title_raw = compact_text(row.get("title") or "")
    messages_raw = compact_text(row.get("messages") or "")
    ticket_hash = private_id_hash(ticket_id_raw or messages_raw or title_raw)
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
    query_category = classify_category(question_candidate)
    query_topic = classify_topic(question_candidate)
    query_forum = detect_forum(question_candidate, forums)
    query_escalation_reason = classify_escalation(question_candidate, {})
    query_should_escalate = query_escalation_reason is not None
    query_needs_clarification = needs_clarification(question_candidate)
    response_profile = (
        infer_response_profile(
            QueryAnalysis(
                category=query_category,
                is_technical=query_category == "техподдержка",
            ),
            question_candidate,
        ).value
        if question_candidate
        else "unresolved"
    )
    forum = detect_forum(text_for_classification, forums)
    escalation_reason = classify_escalation(text_for_classification, row)
    should_escalate = escalation_reason is not None
    difficulty = classify_difficulty(
        text_for_classification,
        typical=row.get("typical_atypical") or "",
        should_escalate=should_escalate,
    )
    answerable_by_kb = (
        bool(question_candidate)
        and
        bool(answer_candidate)
        and not should_escalate
        and category in {"форумы", "гранты", "платформа_фгаис", "навигация"}
    )

    pii_types = sorted(set(title_pii + messages_pii + question_pii + answer_pii))
    return {
        "ticket_id": f"ticket::{ticket_hash}",
        "ticket_hash": ticket_hash,
        "unique_id": (
            f"ticket::{private_id_hash(row['unique_id'])}"
            if row.get("unique_id")
            else ""
        ),
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
        "query_category": query_category,
        "query_topic": query_topic,
        "query_forum_normalized": query_forum,
        "query_needs_clarification": query_needs_clarification,
        "query_should_escalate": query_should_escalate,
        "query_escalation_reason": query_escalation_reason,
        "response_profile": response_profile,
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
    if URL_RE.fullmatch(text.strip()):
        return True
    return any(phrase in normalized for phrase in BOILERPLATE_PHRASES)


def choose_question_candidate(title: str, segments: list[str]) -> str:
    title_score = score_question_segment(title)
    if (
        8 <= len(title) <= 500
        and not is_low_signal_title(title)
        and (
            title_score > 0
            or title_score == 0
            and _is_request_like_title(title)
        )
    ):
        return title

    candidates: list[tuple[int, int, str]] = []
    for index, segment in enumerate(segments[:8]):
        score = score_question_segment(segment)
        if score > 0:
            candidates.append((score, -index, segment))
    if candidates:
        return max(candidates, key=lambda item: (item[0], item[1], -len(item[2])))[2][:1000]
    return ""


def score_question_segment(segment: str) -> int:
    normalized = normalize_for_match(segment)
    padded_prefix = f" {normalized[:240]} "
    if not normalized or is_boilerplate(segment):
        return -100

    score = 0
    user_signal = sum(marker in padded_prefix for marker in USER_REQUEST_MARKERS)
    leading = re.sub(
        r"^(?:здравствуйте|добрый день|добрый вечер|привет)[,!.:\s-]*",
        "",
        normalized,
    )
    leading_question_signal = sum(
        leading.startswith(marker.strip())
        for marker in QUESTION_MARKERS
    )
    if leading.startswith("вопрос ") or leading.startswith("вопрос по"):
        leading_question_signal += 1

    score += 8 * user_signal
    score += 7 * leading_question_signal
    if "?" in segment:
        score += 6
    if re.search(r"\b(?:не могу|не получается|не пришло|не получил|не успел)\b", normalized):
        score += 4
    if len(segment) > 220 and not user_signal and not leading_question_signal:
        score -= 12

    score -= 9 * sum(marker in normalized for marker in ANSWER_OPENING_MARKERS)
    score -= 12 * sum(marker in normalized for marker in THREAD_ARTIFACT_MARKERS)
    if THREAD_TIMESTAMP_RE.search(normalized):
        score -= 12
    if len(segment) > 500:
        score -= 4
    if len(segment) > 1000:
        score -= 8
    if len(segment) < 8:
        score -= 8
    return score


def is_low_signal_title(title: str) -> bool:
    normalized = normalize_for_match(title)
    if not normalized:
        return True
    if any(marker in normalized for marker in LOW_SIGNAL_TITLE_MARKERS):
        return True
    words = re.findall(r"[a-zа-я0-9]{3,}", normalized)
    return len(words) <= 1


def _is_request_like_title(title: str) -> bool:
    normalized = normalize_for_match(title)
    if len(normalized) > 180:
        return False
    return any(
        marker in normalized
        for marker in (
            "вопрос",
            "заявк",
            "ошиб",
            "не работ",
            "регистрац",
            "статус",
            "документ",
            "трансфер",
            "проезд",
            "прожив",
            "питан",
            "грант",
            "помощ",
            "участ",
            "доступ",
            "парол",
            "письм",
            "результат",
            "срок",
            "дата",
            "как ",
            "когда ",
            "где ",
            "почему ",
            "можно ",
        )
    )


def choose_answer_candidate(segments: list[str], question: str) -> str:
    candidates: list[tuple[int, int, str]] = []
    question_norm = normalize_for_match(question)
    for index, segment in enumerate(segments):
        norm = normalize_for_match(segment)
        if norm == question_norm or is_boilerplate(segment):
            continue
        if len(segment) < 50:
            continue
        score = score_answer_segment(segment)
        if score > 0:
            candidates.append((score, index, segment))
    if not candidates:
        return ""
    best = max(candidates, key=lambda item: (item[0], item[1]))
    return best[2][:2000]


def score_answer_segment(segment: str) -> int:
    normalized = f" {normalize_for_match(segment)} "
    score = min(4, len(segment) // 180)
    score += 4 * sum(marker in normalized for marker in SUPPORT_ANSWER_MARKERS)
    score -= 6 * sum(marker in normalized for marker in USER_REQUEST_MARKERS)
    score -= 8 * sum(marker in normalized for marker in THREAD_ARTIFACT_MARKERS)
    if THREAD_TIMESTAMP_RE.search(normalized):
        score -= 8
    if "?" in segment:
        score -= 4
    user_fragment_start = r"^\s*(?:\(|и\s+|а\s+|не\s+могу|не\s+успел|прошу|подскажите|хочу)"
    if re.match(user_fragment_start, segment, re.IGNORECASE):
        score -= 5
    return score


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
        "response_profile_counts": dict(
            Counter(
                item.get("response_profile") or "generic"
                for item in records
            ).most_common()
        ),
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
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        key = (
            item["category"],
            item["topic"],
            item["intent"],
            item.get("response_profile") or "generic",
            item["forum_normalized"] or "",
        )
        grouped[key].append(item)

    rows: list[dict[str, Any]] = []
    for (category, topic, intent, response_profile, forum), items in sorted(
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
                "response_profile": response_profile,
                "forum_normalized": forum,
                "frequency": len(items),
                "example_queries": " || ".join(examples),
                "operator_copy_summary": summarize_answers(items),
                "operator_copy_status": "weak_unreviewed",
                "should_escalate_default": escalation_rate >= 0.5,
            }
        )
    return rows


def build_top_questions(records: list[dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        question_key = normalize_question_key(item.get("question_candidate") or "")
        if not question_key:
            continue
        key = (
            question_key,
            item["category"],
            item["topic"],
            item.get("response_profile") or "generic",
            item["forum_normalized"] or "",
        )
        grouped[key].append(item)

    results: list[dict[str, Any]] = []
    for (question_key, category, topic, response_profile, forum), items in sorted(
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
                "response_profile": response_profile,
                "forum_normalized": forum,
                "operator_copy_candidate": best.get("answer_candidate") or "",
                "operator_copy_status": "weak_unreviewed",
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
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in records:
        query = item.get("question_candidate") or ""
        answer = item.get("answer_candidate") or ""
        if len(query) < 8:
            continue
        if not item["should_escalate"] and not (item["answerable_by_kb"] and len(answer) >= 40):
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
    for item in balanced_scored_records(scored):
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
                "response_profile": item.get("response_profile") or "generic",
                "expected_escalated": item["should_escalate"],
                "expected_escalation_reason": item["escalation_reason"],
                "difficulty": item["difficulty"],
                "source_ticket_ids": [item["ticket_id"]],
                "source_refs": ["RAG_Dataset.xlsx"],
                "notes": item["quality_notes"],
                "label_status": "legacy_weak_operator_copy",
                "operator_answer_used_as_fact": True,
                "deprecated_for_product_eval": True,
                "requires_human_review": True,
            }
        )
        if len(selected) >= max_items:
            break
    return selected


def balanced_scored_records(scored: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    groups: dict[
        tuple[str, str, str, bool],
        list[tuple[int, int, dict[str, Any]]],
    ] = defaultdict(list)
    for index, (score, item) in enumerate(scored):
        groups[golden_group_key(item)].append((score, index, item))

    for rows in groups.values():
        rows.sort(key=lambda pair: (-pair[0], pair[1]))

    ordered_keys = sorted(
        groups,
        key=lambda key: (
            max(score for score, _index, _item in groups[key]),
            key[0],
            key[1],
            key[2],
            key[3],
        ),
        reverse=True,
    )
    ordered: list[dict[str, Any]] = []
    while any(groups.values()):
        for key in ordered_keys:
            if not groups[key]:
                continue
            _score, _index, item = groups[key].pop(0)
            ordered.append(item)
    return ordered


def golden_group_key(item: dict[str, Any]) -> tuple[str, str, str, bool]:
    return (
        str(item.get("category") or "unknown"),
        str(item.get("response_profile") or "generic"),
        str(item.get("difficulty") or "unknown"),
        bool(item.get("should_escalate")),
    )


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
                    "response_profile": case.get("response_profile") or "generic",
                    "relevance_positive": 3,
                    "relevance_negatives": [1 for _ in negatives],
                    "source_ticket_ids": case["source_ticket_ids"],
                    "label_status": "legacy_weak_operator_copy",
                    "operator_answer_used_as_fact": True,
                    "deprecated_for_product_eval": True,
                    "requires_human_review": True,
                }
            )
        if len(pairs) >= max_pairs:
            break
    return pairs


def build_product_eval_splits(
    records: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Build private, query-only review queues without treating operator copy as gold."""

    candidates = [
        item
        for item in records
        if item.get("question_candidate")
        and item.get("response_profile") not in {None, "", "unresolved"}
    ]
    dated_keys = sorted(
        key
        for item in candidates
        if (key := _product_available_at_key(item)) is not None
    )
    validation_cutoff = _quantile_key(dated_keys, 0.70)
    holdout_cutoff = _quantile_key(dated_keys, 0.85)

    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        families[_ticket_query_family(item)].append(item)

    splits: dict[str, list[dict[str, Any]]] = {
        "calibration": [],
        "validation": [],
        "holdout": [],
    }
    crossing_family_count = 0
    for family_key, items in sorted(families.items()):
        item_dates = [
            key
            for item in items
            if (key := _product_available_at_key(item)) is not None
        ]
        split, crosses_boundary = _assign_product_split(
            item_dates,
            validation_cutoff=validation_cutoff,
            holdout_cutoff=holdout_cutoff,
        )
        crossing_family_count += int(crosses_boundary)
        cluster_id = sha1_short(family_key)
        for item in sorted(
            items,
            key=lambda row: (
                _product_available_at_key(row)
                or (0, 0, 0, 0, 0, 0),
                str(row.get("ticket_hash") or ""),
            ),
        ):
            splits[split].append(
                _build_product_eval_case(
                    item,
                    split=split,
                    duplicate_cluster_id=cluster_id,
                )
            )

    for cases in splits.values():
        cases.sort(key=lambda item: (item["available_at"], item["id"]))

    summary = {
        "schema_version": "1.0.0",
        "total": sum(len(cases) for cases in splits.values()),
        "split_counts": {
            split: len(cases)
            for split, cases in splits.items()
        },
        "unique_duplicate_clusters": len(families),
        "crossing_families_forced_to_calibration": crossing_family_count,
        "validation_cutoff": _format_sort_key(validation_cutoff),
        "holdout_cutoff": _format_sort_key(holdout_cutoff),
        "input_tickets_total": len(records),
        "excluded_without_query_or_profile": len(records) - len(candidates),
        "candidate_coverage_ratio": (
            round(len(candidates) / len(records), 4)
            if records
            else 0.0
        ),
        "unit": "merged_ticket_query_candidate",
        "split_timestamp": "latest_of_created_updated_closed",
        "label_status": "weak_unreviewed",
        "deidentification_status": "best_effort_private_only",
        "operator_answers_used_as_facts": False,
        "factual_ground_truth_present": False,
        "sealed_holdout_ready": False,
        "limitations": [
            "Every case requires human review before it can become gold.",
            "Operator replies are excluded from cases and are not factual ground truth.",
            (
                "Cases are query-only: dialogue roles and full multi-turn context "
                "are not reconstructed."
            ),
            (
                "Best-effort masking is not anonymization; every artifact must remain "
                "under data/private."
            ),
            "Time-sensitive facts require an approved as-of release snapshot.",
            (
                "Lexical template families crossing time boundaries stay in calibration; "
                "semantic paraphrase leakage still requires human review."
            ),
        ],
    }
    return splits, summary


def _build_product_eval_case(
    item: dict[str, Any],
    *,
    split: str,
    duplicate_cluster_id: str,
) -> dict[str, Any]:
    query = str(item.get("question_candidate") or "")
    query_category = str(item.get("query_category") or classify_category(query))
    query_topic = str(item.get("query_topic") or classify_topic(query))
    profile = infer_response_profile(
        QueryAnalysis(
            category=query_category,
            is_technical=query_category == "техподдержка",
        ),
        query,
    ).value
    query_escalation_reason = (
        item.get("query_escalation_reason")
        if "query_escalation_reason" in item
        else classify_escalation(query, {})
    )
    query_should_escalate = bool(
        item.get("query_should_escalate")
        if "query_should_escalate" in item
        else query_escalation_reason
    )
    query_needs_clarification = bool(
        item.get("query_needs_clarification")
        if "query_needs_clarification" in item
        else needs_clarification(query)
    )
    available_at = _format_sort_key(_product_available_at_key(item)) or ""
    return {
        "schema_version": "1.0.0",
        "id": f"ticket::{item['ticket_hash']}",
        "ticket_id_hash": item["ticket_hash"],
        "query": query,
        "first_timestamp": str(item.get("created_at") or ""),
        "available_at": available_at,
        "channel": str(item.get("channel") or ""),
        "category": query_category,
        "topic": query_topic,
        "entity": str(item.get("query_forum_normalized") or ""),
        "expected_response_profile": profile,
        "expected_route": (
            "escalate"
            if query_should_escalate
            else "clarify"
            if query_needs_clarification
            else "answer"
        ),
        "needs_clarification": query_needs_clarification,
        "needs_escalation": query_should_escalate,
        "expected_escalation_reason": query_escalation_reason,
        "time_sensitive": _is_time_sensitive_case(profile, query),
        "answerable_from_snapshot": None,
        "approved_chunk_ids": [],
        "forbidden_response_profiles": [],
        "duplicate_cluster_id": duplicate_cluster_id,
        "split": split,
        "label_status": "weak_unreviewed",
        "label_provenance": "deterministic_query_only_v2",
        "requires_human_review": True,
        "operator_answer_included": False,
        "operator_answer_used_as_fact": False,
    }


def _ticket_query_family(item: dict[str, Any]) -> str:
    query = normalize_question_key(str(item.get("question_candidate") or ""))
    forum = normalize_for_match(str(item.get("query_forum_normalized") or ""))
    if forum:
        query = query.replace(forum, "[event]")
    query = re.sub(r"\b\d+\b", "[num]", query)
    query = re.sub(r"(?:\[num\]\s*){2,}", "[num] ", query)
    return WHITESPACE_RE.sub(" ", query).strip()


def _product_available_at_key(
    item: dict[str, Any],
) -> tuple[int, int, int, int, int, int] | None:
    timestamps = [
        key
        for field in ("created_at", "updated_at", "closed_at")
        if (key := _created_at_sort_key(str(item.get(field) or ""))) is not None
    ]
    return max(timestamps, default=None)


def _created_at_sort_key(value: str) -> tuple[int, int, int, int, int, int] | None:
    match = re.search(
        r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"
        r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        value,
    )
    if not match:
        return None
    parts = [int(part or 0) for part in match.groups()]
    year, month, day, hour, minute, second = parts
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year, month, day, hour, minute, second


def _quantile_key(
    values: list[tuple[int, int, int, int, int, int]],
    quantile: float,
) -> tuple[int, int, int, int, int, int] | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, int(len(values) * quantile)))
    return values[index]


def _assign_product_split(
    item_dates: list[tuple[int, int, int, int, int, int]],
    *,
    validation_cutoff: tuple[int, int, int, int, int, int] | None,
    holdout_cutoff: tuple[int, int, int, int, int, int] | None,
) -> tuple[str, bool]:
    if not item_dates or validation_cutoff is None or holdout_cutoff is None:
        return "calibration", False
    earliest = min(item_dates)
    latest = max(item_dates)
    if earliest >= holdout_cutoff:
        return "holdout", False
    if earliest >= validation_cutoff and latest < holdout_cutoff:
        return "validation", False
    crosses_boundary = (
        earliest < validation_cutoff <= latest
        or earliest < holdout_cutoff <= latest
    )
    return "calibration", crosses_boundary


def _format_sort_key(
    value: tuple[int, int, int, int, int, int] | None,
) -> str | None:
    if value is None:
        return None
    year, month, day, hour, minute, second = value
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"


def _is_time_sensitive_case(profile: str, query: str) -> bool:
    if profile in {
        ResponseProfileName.DATES.value,
        ResponseProfileName.SELECTION_STATUS.value,
    }:
        return True
    normalized = normalize_for_match(query)
    return any(
        marker in normalized
        for marker in (
            "срок",
            "дедлайн",
            "прием заяв",
            "приём заяв",
            "регистрац",
            "статус",
            "результат",
        )
    )


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
        "## Requested Response Profiles",
        "",
        *_counter_lines(profile["response_profile_counts"]),
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
            f"{row['response_profile']} / {row['forum_normalized'] or 'unknown'}"
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
            f"{item['response_profile']} / {item['forum_normalized'] or 'unknown'}: "
            f"{item['example_query'][:180]}"
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
            (
                "- Legacy operator-copy reranker pairs не использовать для threshold sweep; "
                "сначала сопоставить запросы с approved KB chunks."
            ),
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
    golden_difficulty = Counter(item.get("difficulty") or "unknown" for item in golden)
    golden_category = Counter(item.get("category") or "unknown" for item in golden)
    golden_profile = Counter(
        item.get("response_profile") or "generic"
        for item in golden
    )
    golden_escalation = Counter(
        "escalated" if item.get("expected_escalated") else "answerable" for item in golden
    )
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
            "> `golden_set_candidates` and reranker pairs contain legacy, weak "
            "operator copy. They are deprecated for product evaluation and must not "
            "be used as factual ground truth.",
            "",
            "## Golden Candidate Mix",
            "",
            *_counter_lines(dict(golden_difficulty.most_common())),
            "",
            "## Golden Candidate Categories",
            "",
            *_counter_lines(dict(golden_category.most_common())),
            "",
            "## Golden Candidate Response Profiles",
            "",
            *_counter_lines(dict(golden_profile.most_common())),
            "",
            "## Golden Candidate Expected Routing",
            "",
            *_counter_lines(dict(golden_escalation.most_common())),
            "",
            "## Generated Files",
            "",
            "- `dataset_profile.json`",
            "- `tickets_normalized.jsonl`",
            "- `intent_taxonomy.csv`",
            "- `top_questions.md`",
            "- `golden_set_candidates.json`",
            "- `reranker_calibration_pairs.jsonl`",
            "- `product_calibration_cases.jsonl`",
            "- `product_validation_cases.jsonl`",
            "- `product_holdout_cases.jsonl`",
            "- `product_split_summary.json`",
            "- `kb_gap_report.md`",
            "",
        ]
    )


def top_questions_to_markdown(items: list[dict[str, Any]]) -> str:
    lines = [
        "# Top Questions",
        "",
        "| Rank | Frequency | Category | Topic | Profile | Forum | Escalate | Example Query |",
        "|---:|---:|---|---|---|---|---|---|",
    ]
    for index, item in enumerate(items, start=1):
        lines.append(
            "| "
            f"{index} | {item['frequency']} | {item['category']} | {item['topic']} | "
            f"{item['response_profile']} | "
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
        ("url", URL_RE, "[URL]"),
        ("phone", PHONE_RE, "[ТЕЛЕФОН]"),
        ("snils", SNILS_RE, "[СНИЛС]"),
        ("passport", PASSPORT_RE, "[ДОКУМЕНТ]"),
        ("vk_id", VK_ID_RE, "[VK_ID]"),
        ("handle", HANDLE_RE, "[HANDLE]"),
        ("fio_context", FIO_CONTEXT_RE, "[ФИО]"),
        ("long_id", LONG_ID_RE, "[ID]"),
        ("date", DATE_RE, "[ДАТА]"),
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


def private_id_hash(value: str) -> str:
    namespaced = f"rosmol-private-ticket-v1\0{value}"
    return hashlib.sha256(namespaced.encode("utf-8", errors="ignore")).hexdigest()[:24]


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
