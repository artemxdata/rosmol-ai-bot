from __future__ import annotations

# ruff: noqa: E402,I001

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.index_kb import validate_seed_items

DEFAULT_ANSWER_BANK = Path("data/private/tickets/curation/ideal_answer_bank_candidates.json")
DEFAULT_OUTPUT = Path("data/private/tickets/curation/kb_draft_answer_bank.json")
DEFAULT_BASE_KB = Path("data/knowledge_base_seed.json")

SAFE_SOURCE_FIELDS = {
    "id",
    "review_status",
    "source",
    "query",
    "answer",
    "category",
    "topic",
    "forum_normalized",
    "difficulty",
    "intent",
    "tags",
    "quality_score",
    "quality_reasons",
    "candidate_chunk_ids",
    "rag_action",
    "notes",
}
CONDITIONAL_RE = re.compile(r"\bесли\b|в\s+случае|при\s+условии", re.IGNORECASE)
UNSAFE_PLACEHOLDERS = ("[EMAIL]", "[ТЕЛЕФОН]", "[ДОКУМЕНТ]", "[ДАТА]", "[ИМЯ]", "[ID]", "[URL]")
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
ID_LABEL_RE = re.compile(r"\bID\s*[:№#-]?\s*[A-Za-zА-Яа-я0-9_-]{6,}\b", re.IGNORECASE)
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


def promote_answer_bank(
    answer_bank_path: Path = DEFAULT_ANSWER_BANK,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    base_kb_path: Path | None = None,
    merged_output_path: Path | None = None,
    include_candidates: bool = False,
    publish_for_sandbox: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    answer_bank = read_json_array(answer_bank_path)
    base_records = read_json_array(base_kb_path) if base_kb_path else []
    existing_chunk_ids = {
        str(record.get("chunk_id")) for record in base_records if record.get("chunk_id")
    }
    selected = select_promotable(answer_bank, include_candidates=include_candidates)
    if limit is not None:
        selected = selected[:limit]
    draft_chunks = [
        build_draft_chunk(
            candidate,
            index=index,
            existing_chunk_ids=existing_chunk_ids,
            publish_for_sandbox=publish_for_sandbox,
        )
        for index, candidate in enumerate(selected, start=1)
    ]
    validate_seed_items(draft_chunks)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, draft_chunks)
    markdown_path = output_path.with_suffix(".md")
    markdown_path.write_text(
        build_report(
            draft_chunks,
            selected,
            answer_bank_path=answer_bank_path,
            base_kb_path=base_kb_path,
            include_candidates=include_candidates,
            publish_for_sandbox=publish_for_sandbox,
        ),
        encoding="utf-8",
    )

    if merged_output_path:
        merged = [*base_records, *draft_chunks]
        validate_seed_items(merged)
        merged_output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(merged_output_path, merged)

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "answer_bank": str(answer_bank_path),
        "output": str(output_path),
        "markdown": str(markdown_path),
        "base_kb": str(base_kb_path) if base_kb_path else None,
        "merged_output": str(merged_output_path) if merged_output_path else None,
        "include_candidates": include_candidates,
        "publish_for_sandbox": publish_for_sandbox,
        "answer_bank_total": len(answer_bank),
        "promoted_chunks": len(draft_chunks),
        "status_counts": status_counts(answer_bank),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def select_promotable(
    answer_bank: list[dict[str, Any]],
    *,
    include_candidates: bool,
) -> list[dict[str, Any]]:
    allowed = {"approved"}
    if include_candidates:
        allowed.add("candidate")
    return [
        sanitize_candidate(candidate)
        for candidate in answer_bank
        if str(candidate.get("review_status") or "").strip() in allowed
        and is_safe_candidate_texts(
            query=str(candidate.get("query") or ""),
            answer=str(candidate.get("answer") or ""),
        )
    ]


def sanitize_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {key: candidate.get(key) for key in SAFE_SOURCE_FIELDS if key in candidate}


def is_safe_answer_text(text: str) -> bool:
    normalized = clean_optional(text)
    if not normalized:
        return False
    folded = normalized.casefold()
    if any(placeholder.casefold() in folded for placeholder in UNSAFE_PLACEHOLDERS):
        return False
    if UUID_RE.search(folded) or ID_LABEL_RE.search(folded):
        return False
    latin_alpha_count = len(LATIN_ALPHA_RE.findall(normalized))
    if "?" in normalized or latin_alpha_count > max(40, int(len(normalized) * 0.2)):
        return False
    if LATIN_NOISE_RE.search(normalized):
        return False
    if THREAD_ARTIFACT_RE.search(folded):
        return False
    if USER_FRAGMENT_START_RE.search(folded):
        return False
    return (
        ANSWER_FIRST_PERSON_RE.search(folded) is None
        and PRIVATE_REQUEST_RE.search(folded) is None
    )


def is_safe_query_text(text: str) -> bool:
    normalized = clean_optional(text)
    if not normalized:
        return False
    folded = normalized.casefold()
    if any(placeholder.casefold() in folded for placeholder in UNSAFE_PLACEHOLDERS):
        return False
    return (
        UUID_RE.search(folded) is None
        and ID_LABEL_RE.search(folded) is None
        and THREAD_ARTIFACT_RE.search(folded) is None
    )


def is_safe_candidate_texts(*, query: str, answer: str) -> bool:
    return is_safe_query_text(query) and is_safe_answer_text(answer)


def build_draft_chunk(
    candidate: dict[str, Any],
    *,
    index: int,
    existing_chunk_ids: set[str],
    publish_for_sandbox: bool = False,
) -> dict[str, Any]:
    text = clean_required(candidate.get("answer"), "answer")
    query = clean_required(candidate.get("query"), "query")
    candidate_id = str(candidate.get("id") or f"{index:03d}")
    chunk_id = unique_chunk_id(candidate_id, index=index, existing_chunk_ids=existing_chunk_ids)
    now = date.today().isoformat()
    forum = clean_optional(candidate.get("forum_normalized"))
    category = clean_optional(candidate.get("category"))
    topic = clean_optional(candidate.get("topic"))
    review_status = clean_optional(candidate.get("review_status")) or "candidate"

    return {
        "chunk_id": chunk_id,
        "text_raw": text,
        "text_clean": text,
        "status": "published" if publish_for_sandbox else "draft",
        "category": category,
        "forum": forum,
        "forum_normalized": forum,
        "topic": topic,
        "is_generic": forum is None,
        "has_conditional_logic": bool(CONDITIONAL_RE.search(text)),
        "conditions_summary": None,
        "links": [],
        "emails": [],
        "phones": [],
        "dates_mentioned": [],
        "valid_from": None,
        "valid_to": None,
        "source_type": "ticket_answer_bank",
        "source": "sanitized_hde_ticket_answer_bank",
        "source_file": "ideal_answer_bank_candidates.json",
        "source_url": None,
        "version": 1,
        "extraction_date": now,
        "updated_at": now,
        "char_count": len(text),
        "parent_chunk_id": None,
        "intent_name": clean_optional(candidate.get("intent")) or topic,
        "intent_examples": [query],
        "intent_examples_count": 1,
        "source_category": "Sanitized ticket answer bank",
        "review_status": review_status,
        "answer_bank_id": candidate_id,
        "answer_bank_quality_score": candidate.get("quality_score"),
        "answer_bank_quality_reasons": list(candidate.get("quality_reasons") or []),
        "answer_bank_candidate_chunk_ids": list(candidate.get("candidate_chunk_ids") or []),
        "answer_bank_tags": list(candidate.get("tags") or []),
        "sandbox_published": publish_for_sandbox,
        "promotion_notes": candidate.get("notes") or "",
    }


def unique_chunk_id(candidate_id: str, *, index: int, existing_chunk_ids: set[str]) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9_]+", "_", candidate_id).strip("_").lower()
    suffix = suffix.removeprefix("ticket_answer_bank_")
    if not suffix:
        suffix = f"{index:03d}"
    chunk_id = f"ticket_answer_bank_{suffix}"
    if chunk_id not in existing_chunk_ids:
        existing_chunk_ids.add(chunk_id)
        return chunk_id

    counter = 2
    while f"{chunk_id}_{counter}" in existing_chunk_ids:
        counter += 1
    unique = f"{chunk_id}_{counter}"
    existing_chunk_ids.add(unique)
    return unique


def build_report(
    draft_chunks: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    *,
    answer_bank_path: Path,
    base_kb_path: Path | None,
    include_candidates: bool,
    publish_for_sandbox: bool,
) -> str:
    lines = [
        "# KB Drafts From Ticket Answer Bank",
        "",
        f"- Generated: `{datetime.now(UTC).isoformat()}`",
        f"- Answer bank: `{answer_bank_path}`",
        f"- Base KB: `{base_kb_path or 'not used'}`",
        f"- Include candidates: `{include_candidates}`",
        f"- Publish for sandbox: `{publish_for_sandbox}`",
        f"- Draft chunks: `{len(draft_chunks)}`",
        "",
        "## Review Protocol",
        "",
        "1. Проверить каждый draft на актуальность по официальной базе знаний.",
        "2. Отредактировать `text_clean` только если нужно убрать контекст тикета.",
        "3. После проверки перенести запись в основной `knowledge_base_seed.json`.",
        "4. Перевести `status` из `draft` в `published` только после редакторского подтверждения.",
        "5. Запустить `python scripts/index_kb.py --validate-only` и переиндексацию.",
        "",
        "## Sample Drafts",
        "",
    ]
    for chunk, candidate in zip(draft_chunks[:30], selected[:30], strict=False):
        candidate_chunk_ids = ", ".join(chunk.get("answer_bank_candidate_chunk_ids") or [])
        lines.extend(
            [
                f"### {chunk['chunk_id']}",
                "",
                f"- Category: `{chunk.get('category')}`",
                f"- Topic: `{chunk.get('topic')}`",
                f"- Forum: `{chunk.get('forum_normalized') or 'unknown'}`",
                f"- Review status: `{chunk.get('review_status')}`",
                f"- Quality score: `{candidate.get('quality_score')}`",
                f"- Candidate chunks: `{candidate_chunk_ids}`",
                "",
                f"**Intent example:** {chunk['intent_examples'][0]}",
                "",
                f"**Draft answer:** {chunk['text_clean']}",
                "",
            ]
        )
    return "\n".join(lines)


def status_counts(answer_bank: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in answer_bank:
        status = str(item.get("review_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def clean_required(value: Any, field: str) -> str:
    cleaned = clean_optional(value)
    if not cleaned:
        raise ValueError(f"answer bank candidate has empty {field}")
    return cleaned


def clean_optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def read_json_array(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"file must contain a JSON array: {path}")
    if not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"JSON array must contain only objects: {path}")
    return payload


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert reviewed answer bank candidates into KB-compatible draft chunks."
    )
    parser.add_argument("--answer-bank", type=Path, default=DEFAULT_ANSWER_BANK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-kb", type=Path, default=DEFAULT_BASE_KB)
    parser.add_argument("--merged-output", type=Path, default=None)
    parser.add_argument(
        "--include-candidates",
        action="store_true",
        help="Promote review_status=candidate records as draft chunks for private review.",
    )
    parser.add_argument(
        "--publish-for-sandbox",
        action="store_true",
        help=(
            "Write promoted answer-bank records as status=published for private local "
            "sandbox retrieval only. Do not copy this output into the main KB."
        ),
    )
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    promote_answer_bank(
        answer_bank_path=args.answer_bank,
        output_path=args.output,
        base_kb_path=args.base_kb,
        merged_output_path=args.merged_output,
        include_candidates=args.include_candidates,
        publish_for_sandbox=args.publish_for_sandbox,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
