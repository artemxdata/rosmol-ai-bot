from __future__ import annotations

# ruff: noqa: E402,I001

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.prepare_ticket_eval_sets import build_chunk_index, match_chunks
from src.kb.source_extractors import clean_bot_text
from src.security.pii_masker import PIIMasker

DEFAULT_TICKETS = Path("data/private/tickets/analysis/tickets_normalized.jsonl")
DEFAULT_KB_SEED = Path("data/knowledge_base_seed.json")
DEFAULT_OUTPUT = Path("data/private/tickets/curation/ideal_answer_bank_candidates.json")

PERSONAL_LETTER_MARKERS = (
    "меня зовут",
    "прошу рассмотреть",
    "во вложении",
    "прикрепляю",
    "с уважением",
    "отправлено из",
    "я являюсь",
    "моя заявка",
    "мой проект",
    "мой профиль",
)
ESCALATION_MARKERS = (
    "передали ваше обращение",
    "передаем ваше обращение",
    "передаю специалист",
    "перевожу на оператора",
    "ожидайте ответа",
    "потребуется некоторое время",
)
LOW_VALUE_MARKERS = (
    "уточните, пожалуйста",
    "опишите ваш вопрос",
    "чем я могу быть полезен",
    "напишите нам подробнее",
)
GOOD_ANSWER_MARKERS = (
    "можно",
    "необходимо",
    "нужно",
    "регистрация",
    "заявк",
    "перейд",
    "доступ",
    "профил",
    "форум",
    "грант",
    "обрати внимание",
    "если",
)
SENSITIVE_PLACEHOLDERS = ("[EMAIL]", "[ТЕЛЕФОН]", "[ДОКУМЕНТ]", "[ДАТА]", "[ИМЯ]")
UNSAFE_PLACEHOLDERS = (*SENSITIVE_PLACEHOLDERS, "[ID]", "[URL]")
DOCUMENT_NUMBER_RE = re.compile(r"\b\d{4}\s?\d{6}\b")
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
ID_LABEL_RE = re.compile(r"\bID\s*[:№#-]?\s*[A-Za-zА-Яа-я0-9_-]{6,}\b", re.IGNORECASE)
LONG_ID_RE = re.compile(r"\b\d{7,}\b")
LEADING_ADDRESSEE_RE = re.compile(
    r"^\s*[А-ЯЁ][а-яё]{2,}(?:\s+[А-ЯЁ][а-яё]{2,}){0,2},\s+"
)
ANSWER_FIRST_PERSON_RE = re.compile(
    r"\b(я|мне|меня|мой|моя|моё|мои|помогите|подскажите)\b",
    re.IGNORECASE,
)
PRIVATE_REQUEST_RE = re.compile(
    r"\b("
    r"прошу|"
    r"возможно\s+ли|"
    r"очень\s+хочу|"
    r"можете\s+пожалуйста|"
    r"пожалуйста\s+открыть|"
    r"не\s+успел[аи]?|"
    r"не\s+смог[уы]?|"
    r"приболел[аи]?|"
    r"почта\s+та\s+же|"
    r"не\s+копируется|"
    r"возник(?:ла|ли|ший|шую|шее)?\s+(?:техническая\s+)?(?:неполадка|вопрос|проблема)|"
    r"почему\s+|"
    r"не\s+хватило|"
    r"не\s+вижу|"
    r"личн(?:ые|ым|ыми)\s+обстоятельств"
    r")",
    re.IGNORECASE,
)
LATIN_ALPHA_RE = re.compile(r"[A-Za-z]")
LATIN_NOISE_RE = re.compile(r"\bcommented\b|\u200b|\u200c|\u200d", re.IGNORECASE)
THREAD_ARTIFACT_RE = re.compile(
    r"служб[аы]\s+заботы\s+росмолод[ёе]жи\s*:|"
    r"запрос\s+отправлял[аи]?|"
    r"отправлено\s+из|"
    r"\b\d{4}\s*г\.\s*,?\s*\d{1,2}:\d{2}\b",
    re.IGNORECASE,
)
USER_FRAGMENT_START_RE = re.compile(
    r"^\s*(?:"
    r"\(|"
    r"и\s+|"
    r"а\s+|"
    r"не\s+могу|"
    r"не\s+успел[аи]?|"
    r"не\s+получил[аи]?|"
    r"к\s+сожалению,\s+не\s+смогу|"
    r"возник(?:ла|ли|ший|шую|шее)?\s+|"
    r"подскажите|"
    r"прошу|"
    r"хочу|"
    r"можете\s+пожалуйста"
    r")",
    re.IGNORECASE,
)


def build_answer_bank(
    tickets_path: Path = DEFAULT_TICKETS,
    kb_seed_path: Path = DEFAULT_KB_SEED,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    limit: int = 300,
    min_quality_score: int = 9,
    top_matches: int = 3,
    use_natasha_names: bool = False,
) -> dict[str, Any]:
    masker: PIIMasker = PIIMasker() if use_natasha_names else RegexOnlyPIIMasker()
    tickets = read_jsonl(tickets_path)
    kb_records = read_json(kb_seed_path)
    chunks = build_chunk_index(kb_records)

    candidates = [
        candidate
        for ticket in tickets
        if (
            candidate := build_candidate(
                ticket,
                chunks,
                masker,
                min_quality_score=min_quality_score,
                top_matches=top_matches,
            )
        )
    ]
    selected = assign_candidate_ids(select_balanced(candidates, limit))
    report = build_report(
        selected,
        candidates,
        tickets_path=tickets_path,
        kb_seed_path=kb_seed_path,
        min_quality_score=min_quality_score,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, selected)
    markdown_path = output_path.with_suffix(".md")
    markdown_path.write_text(report, encoding="utf-8")
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "tickets_path": str(tickets_path),
        "kb_seed_path": str(kb_seed_path),
        "output": str(output_path),
        "markdown": str(markdown_path),
        "candidates_scored": len(candidates),
        "selected": len(selected),
        "limit": limit,
        "min_quality_score": min_quality_score,
        "use_natasha_names": use_natasha_names,
        "category_counts": dict(Counter(item["category"] for item in selected).most_common()),
        "difficulty_counts": dict(Counter(item["difficulty"] for item in selected).most_common()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


class RegexOnlyPIIMasker(PIIMasker):
    """Private offline batch mode; never use this class in webhook/runtime paths."""

    def _mask_names(self, text: str) -> tuple[str, list[str]]:
        return text, []


def build_candidate(
    ticket: dict[str, Any],
    chunks: list[dict[str, Any]],
    masker: PIIMasker,
    *,
    min_quality_score: int,
    top_matches: int,
) -> dict[str, Any] | None:
    if ticket.get("should_escalate") or not ticket.get("answerable_by_kb"):
        return None

    question = sanitize_text(str(ticket.get("question_candidate") or ""), masker)
    answer = sanitize_text(str(ticket.get("answer_candidate") or ""), masker)
    if not question or not answer:
        return None
    if (
        has_disallowed_markers(question, answer)
        or has_unsafe_question_shape(question)
        or has_unsafe_answer_shape(answer)
    ):
        return None

    score, reasons = quality_score(ticket, question, answer)
    if score < min_quality_score:
        return None

    matches = safe_chunk_matches(ticket, question, answer, chunks, top_matches=top_matches)
    return {
        "id": "ticket_answer_bank::pending",
        "review_status": "candidate",
        "source": "sanitized_hde_ticket_answer_bank",
        "query": question,
        "answer": answer,
        "category": ticket.get("category"),
        "topic": ticket.get("topic"),
        "forum_normalized": ticket.get("forum_normalized"),
        "difficulty": ticket.get("difficulty"),
        "intent": ticket.get("intent"),
        "tags": candidate_tags(ticket, score),
        "quality_score": score,
        "quality_reasons": reasons,
        "candidate_chunk_ids": [match["chunk_id"] for match in matches],
        "candidate_chunk_matches": matches,
        "rag_action": "review_then_promote_to_kb_seed",
        "notes": "Sanitized candidate from private tickets. Review factuality before indexing.",
    }


def sanitize_text(text: str, masker: PIIMasker) -> str:
    cleaned = clean_bot_text(text)
    if not cleaned:
        return ""
    masked, _mapping = masker.mask(cleaned)
    masked = DOCUMENT_NUMBER_RE.sub("[ДОКУМЕНТ]", masked)
    masked = UUID_RE.sub("[ID]", masked)
    masked = ID_LABEL_RE.sub("ID [ID]", masked)
    masked = LONG_ID_RE.sub("[ID]", masked)
    masked = mask_name_phrases(masked)
    masked = strip_signature(masked)
    masked = strip_greeting(masked)
    masked = strip_leading_addressee(masked)
    return compact(masked)


def mask_name_phrases(text: str) -> str:
    text = re.sub(
        r"(меня\s+зовут)\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){0,2}",
        r"\1 [ИМЯ]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(здравствуйте|добрый\s+день|добрый\s+вечер),?\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?",
        r"\1, [ИМЯ]",
        text,
        flags=re.IGNORECASE,
    )
    return text


def strip_signature(text: str) -> str:
    return re.split(r"\s+С\s+уважением[,:\s]", text, maxsplit=1, flags=re.IGNORECASE)[0]


def strip_greeting(text: str) -> str:
    return re.sub(
        r"^\s*(здравствуйте|добрый\s+день|добрый\s+вечер|доброе\s+утро)"
        r"(?:,\s*\[ИМЯ\])?[!.,:\s-]*",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )


def strip_leading_addressee(text: str) -> str:
    return LEADING_ADDRESSEE_RE.sub("", text, count=1)


def has_disallowed_markers(question: str, answer: str) -> bool:
    combined_text = f"{question} {answer}"
    combined = normalize(combined_text)
    if THREAD_ARTIFACT_RE.search(combined_text):
        return True
    return any(
        marker in combined
        for marker in (*PERSONAL_LETTER_MARKERS, *ESCALATION_MARKERS, *LOW_VALUE_MARKERS)
    )


def has_unsafe_answer_shape(answer: str) -> bool:
    normalized = normalize(answer)
    folded = answer.casefold()
    has_placeholder = any(
        placeholder.casefold() in answer.casefold() for placeholder in UNSAFE_PLACEHOLDERS
    )
    has_raw_id = bool(UUID_RE.search(answer) or ID_LABEL_RE.search(answer))
    latin_alpha_count = len(LATIN_ALPHA_RE.findall(answer))
    has_latin_noise = latin_alpha_count > max(40, int(len(answer) * 0.2)) or bool(
        LATIN_NOISE_RE.search(answer)
    )
    return (
        has_placeholder
        or has_raw_id
        or "?" in answer
        or has_latin_noise
        or bool(THREAD_ARTIFACT_RE.search(folded))
        or bool(USER_FRAGMENT_START_RE.search(folded))
        or bool(
            ANSWER_FIRST_PERSON_RE.search(normalized)
            or ANSWER_FIRST_PERSON_RE.search(folded)
            or PRIVATE_REQUEST_RE.search(normalized)
            or PRIVATE_REQUEST_RE.search(folded)
        )
    )


def has_unsafe_question_shape(question: str) -> bool:
    folded = question.casefold()
    if any(placeholder.casefold() in folded for placeholder in UNSAFE_PLACEHOLDERS):
        return True
    return bool(
        UUID_RE.search(question)
        or ID_LABEL_RE.search(question)
        or THREAD_ARTIFACT_RE.search(question)
    )


def quality_score(ticket: dict[str, Any], question: str, answer: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    normalized_question = normalize(question)
    normalized_answer = normalize(answer)

    answer_len = len(answer)
    question_len = len(question)
    if 80 <= answer_len <= 900:
        score += 3
        reasons.append("answer_length_good")
    elif 50 <= answer_len <= 1400:
        score += 1
        reasons.append("answer_length_acceptable")
    else:
        score -= 4
        reasons.append("answer_length_bad")

    if 10 <= question_len <= 280:
        score += 2
        reasons.append("question_length_good")
    elif question_len > 500:
        score -= 3
        reasons.append("question_too_long")

    if ticket.get("category") in {"форумы", "гранты", "платформа_фгаис", "навигация"}:
        score += 2
        reasons.append("supported_category")
    if ticket.get("topic") and ticket.get("topic") != "прочее":
        score += 1
        reasons.append("topic_detected")
    if ticket.get("forum_normalized"):
        score += 1
        reasons.append("forum_detected")
    if ticket.get("difficulty") in {"medium", "complex"}:
        score += 1
        reasons.append("nontrivial_case")

    good_markers = sum(1 for marker in GOOD_ANSWER_MARKERS if marker in normalized_answer)
    if good_markers:
        score += min(3, good_markers)
        reasons.append("answer_has_instruction_markers")

    placeholders = sum(answer.count(placeholder) for placeholder in SENSITIVE_PLACEHOLDERS)
    if placeholders:
        score -= 8
        reasons.append("contains_masked_sensitive_tokens")
    if has_unsafe_answer_shape(answer):
        score -= 8
        reasons.append("unsafe_answer_shape")
    if has_unsafe_question_shape(question):
        score -= 8
        reasons.append("unsafe_question_shape")

    if any(marker in normalized_answer for marker in PERSONAL_LETTER_MARKERS):
        score -= 6
        reasons.append("looks_like_user_letter")
    if any(marker in normalized_answer for marker in ESCALATION_MARKERS):
        score -= 4
        reasons.append("looks_like_escalation_reply")
    if any(marker in normalized_answer for marker in LOW_VALUE_MARKERS):
        score -= 3
        reasons.append("low_value_clarification_reply")
    if normalized_question == normalized_answer:
        score -= 6
        reasons.append("question_equals_answer")

    return score, reasons


def safe_chunk_matches(
    ticket: dict[str, Any],
    question: str,
    answer: str,
    chunks: list[dict[str, Any]],
    *,
    top_matches: int,
) -> list[dict[str, Any]]:
    raw_matches = match_chunks(
        {
            "query": question,
            "expected_answer": answer,
            "category": ticket.get("category"),
            "forum_normalized": ticket.get("forum_normalized"),
        },
        chunks,
        top_matches=top_matches,
    )
    return [
        {
            "chunk_id": match["chunk_id"],
            "score": match["score"],
            "category": match.get("category"),
            "topic": match.get("topic"),
            "forum_normalized": match.get("forum_normalized"),
        }
        for match in raw_matches
    ]


def candidate_tags(ticket: dict[str, Any], score: int) -> list[str]:
    tags = [
        "answer_bank_candidate",
        f"quality:{score}",
        f"category:{ticket.get('category') or 'unknown'}",
        f"topic:{ticket.get('topic') or 'unknown'}",
        f"difficulty:{ticket.get('difficulty') or 'unknown'}",
    ]
    if ticket.get("forum_normalized"):
        tags.append(f"forum:{ticket['forum_normalized']}")
    return tags


def select_balanced(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in sorted(candidates, key=lambda row: row["quality_score"], reverse=True):
        groups[
            (
                str(item.get("category") or "unknown"),
                str(item.get("topic") or "unknown"),
                str(item.get("forum_normalized") or "unknown"),
            )
        ].append(item)

    selected: list[dict[str, Any]] = []
    while len(selected) < limit and any(groups.values()):
        for key in sorted(groups):
            if not groups[key]:
                continue
            selected.append(groups[key].pop(0))
            if len(selected) >= limit:
                break
    return selected


def assign_candidate_ids(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    numbered = []
    for index, item in enumerate(candidates, start=1):
        numbered_item = dict(item)
        numbered_item["id"] = f"ticket_answer_bank::{index:03d}"
        numbered.append(numbered_item)
    return numbered


def build_report(
    selected: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    tickets_path: Path,
    kb_seed_path: Path,
    min_quality_score: int,
) -> str:
    category_counts = Counter(item["category"] for item in selected)
    topic_counts = Counter(item["topic"] for item in selected)
    forum_counts = Counter(item.get("forum_normalized") or "unknown" for item in selected)
    lines = [
        "# Sanitized Ticket Answer Bank Candidates",
        "",
        f"- Generated: `{datetime.now(UTC).isoformat()}`",
        f"- Private source: `{tickets_path}`",
        f"- KB seed for match hints: `{kb_seed_path}`",
        f"- Scored candidates: `{len(candidates)}`",
        f"- Selected candidates: `{len(selected)}`",
        f"- Min quality score: `{min_quality_score}`",
        "",
        "## Category Mix",
        "",
        *counter_lines(category_counts),
        "",
        "## Top Topics",
        "",
        *counter_lines(topic_counts, limit=25),
        "",
        "## Top Forums",
        "",
        *counter_lines(forum_counts, limit=25),
        "",
        "## Review Protocol",
        "",
        "1. Проверить фактическую актуальность ответа по официальной KB.",
        "2. Оставить только ответы, которые можно показывать пользователю без контекста тикета.",
        "3. Перенести подтверждённые записи в `knowledge_base_seed.json` как `status=draft`.",
        "4. После редакторской проверки перевести записи в `published` и переиндексировать RAG.",
        "",
        "## Sample Candidates",
        "",
    ]
    for item in selected[:30]:
        lines.extend(
            [
                f"### {item['id']}",
                "",
                f"- Category: `{item.get('category')}`",
                f"- Topic: `{item.get('topic')}`",
                f"- Forum: `{item.get('forum_normalized') or 'unknown'}`",
                f"- Quality score: `{item.get('quality_score')}`",
                f"- Candidate chunks: `{', '.join(item.get('candidate_chunk_ids') or [])}`",
                "",
                f"**Query:** {item['query']}",
                "",
                f"**Answer:** {item['answer']}",
                "",
            ]
        )
    return "\n".join(lines)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL line {line_number} must be an object")
            records.append(value)
    return records


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize(text: str) -> str:
    return clean_bot_text(text).casefold().replace("ё", "е")


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def counter_lines(counter: Counter[Any], limit: int = 20) -> list[str]:
    return [f"- `{count}` {name}" for name, count in counter.most_common(limit)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build sanitized ideal answer candidates from private ticket analysis."
    )
    parser.add_argument("--tickets", type=Path, default=DEFAULT_TICKETS)
    parser.add_argument("--kb-seed", type=Path, default=DEFAULT_KB_SEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--min-quality-score", type=int, default=9)
    parser.add_argument("--top-matches", type=int, default=3)
    parser.add_argument(
        "--use-natasha-names",
        action="store_true",
        help="Enable Natasha NER for names. Slower; regex scrubbers stay enabled either way.",
    )
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    build_answer_bank(
        tickets_path=args.tickets,
        kb_seed_path=args.kb_seed,
        output_path=args.output,
        limit=args.limit,
        min_quality_score=args.min_quality_score,
        top_matches=args.top_matches,
        use_natasha_names=args.use_natasha_names,
    )


if __name__ == "__main__":
    main()
