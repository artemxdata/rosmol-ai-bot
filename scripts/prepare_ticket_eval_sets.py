from __future__ import annotations

# ruff: noqa: E402,I001

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.kb.source_extractors import clean_bot_text

DEFAULT_CANDIDATES = Path("data/private/tickets/analysis/golden_set_candidates.json")
DEFAULT_KB_SEED = Path("data/knowledge_base_seed.json")
DEFAULT_OUTPUT_DIR = Path("data/private/tickets/eval")

STOPWORDS = {
    "а",
    "без",
    "бы",
    "в",
    "во",
    "да",
    "для",
    "до",
    "его",
    "ее",
    "её",
    "если",
    "же",
    "за",
    "и",
    "или",
    "из",
    "к",
    "как",
    "ко",
    "ли",
    "на",
    "не",
    "но",
    "о",
    "об",
    "от",
    "по",
    "при",
    "с",
    "со",
    "то",
    "у",
    "что",
    "это",
    "я",
}


def prepare_eval_sets(
    candidates_path: Path,
    kb_seed_path: Path,
    output_dir: Path,
    *,
    max_cases: int = 800,
    smoke_size: int = 40,
    min_chunk_score: float = 0.18,
    top_matches: int = 5,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = read_json(candidates_path)
    kb_records = read_json(kb_seed_path)
    if not isinstance(candidates, list):
        raise ValueError("golden candidates must be a JSON array")
    if not isinstance(kb_records, list):
        raise ValueError("KB seed must be a JSON array")
    if any(candidate.get("deprecated_for_product_eval") for candidate in candidates):
        raise ValueError(
            "ticket candidates contain deprecated operator copy; "
            "use reviewed query-only product cases instead"
        )

    chunk_index = build_chunk_index(kb_records)
    prepared = [
        prepare_case(
            raw,
            chunk_index,
            min_chunk_score=min_chunk_score,
            top_matches=top_matches,
        )
        for raw in candidates[:max_cases]
    ]
    retrieval_cases = [
        retrieval_case(item)
        for item in prepared
        if item["expected_chunk_ids"] and not item["expected_escalated"]
    ]
    smoke_cases = select_balanced_cases(prepared, smoke_size)
    review_rows = build_review_rows(prepared)
    report = build_report(
        prepared,
        retrieval_cases,
        smoke_cases,
        candidates_path=candidates_path,
        kb_seed_path=kb_seed_path,
        min_chunk_score=min_chunk_score,
    )

    write_json(output_dir / "ticket_ask_eval_candidates.json", prepared)
    write_json(output_dir / "ticket_ask_eval_smoke.json", smoke_cases)
    write_json(output_dir / "ticket_retrieval_golden_candidates.json", retrieval_cases)
    write_csv(output_dir / "ticket_manual_review_sample.csv", review_rows)
    (output_dir / "ticket_eval_report.md").write_text(report, encoding="utf-8")

    return {
        "output_dir": str(output_dir),
        "cases_total": len(prepared),
        "smoke_cases": len(smoke_cases),
        "retrieval_cases": len(retrieval_cases),
        "chunk_matched_cases": sum(bool(item["expected_chunk_ids"]) for item in prepared),
        "needs_review_cases": sum(item["needs_review"] for item in prepared),
        "manual_review_csv": str(output_dir / "ticket_manual_review_sample.csv"),
        "ask_eval_candidates": str(output_dir / "ticket_ask_eval_candidates.json"),
        "ask_eval_smoke": str(output_dir / "ticket_ask_eval_smoke.json"),
        "retrieval_golden_candidates": str(
            output_dir / "ticket_retrieval_golden_candidates.json"
        ),
    }


def build_chunk_index(kb_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for record in kb_records:
        if record.get("status") not in (None, "", "published"):
            continue
        text = clean_bot_text(str(record.get("text_clean") or record.get("text") or ""))
        metadata_text = " ".join(
            str(record.get(key) or "")
            for key in ("category", "topic", "forum_normalized", "intent", "source_file")
        )
        chunks.append(
            {
                "chunk_id": str(record.get("chunk_id") or ""),
                "text": text,
                "category": record.get("category"),
                "topic": record.get("topic"),
                "forum_normalized": record.get("forum_normalized"),
                "tokens": tokenize(f"{text} {metadata_text}"),
            }
        )
    return [chunk for chunk in chunks if chunk["chunk_id"] and chunk["tokens"]]


def prepare_case(
    raw: dict[str, Any],
    chunks: list[dict[str, Any]],
    *,
    min_chunk_score: float,
    top_matches: int,
) -> dict[str, Any]:
    query = clean_bot_text(str(raw.get("query") or ""))
    expected_answer = clean_bot_text(str(raw.get("expected_answer") or ""))
    expected_escalated = bool(raw.get("expected_escalated"))
    matches = []
    if not expected_escalated:
        matches = match_chunks(raw, chunks, top_matches=top_matches)
    expected_chunk_ids = [
        match["chunk_id"] for match in matches if float(match["score"]) >= min_chunk_score
    ][:3]
    top_score = float(matches[0]["score"]) if matches else 0.0
    needs_review = expected_escalated or not expected_chunk_ids or top_score < min_chunk_score
    tags = [
        "ticket_analysis",
        f"category:{raw.get('category') or 'unknown'}",
        f"topic:{raw.get('topic') or 'unknown'}",
        f"difficulty:{raw.get('difficulty') or 'unknown'}",
    ]
    if raw.get("forum_normalized"):
        tags.append(f"forum:{raw['forum_normalized']}")
    if needs_review:
        tags.append("needs_review")

    return {
        "id": str(raw.get("id") or query),
        "query": query,
        "user_id": "ticket-eval",
        "channel": "api",
        "expected_chunk_ids": expected_chunk_ids,
        "expected_cited_chunk_ids": expected_chunk_ids if not expected_escalated else [],
        "expected_answer_contains": normalize_answer_contains(
            raw.get("expected_answer_contains") or []
        ),
        "expected_escalated": expected_escalated,
        "expected_escalation_reason": raw.get("expected_escalation_reason"),
        "expected_generator_model": None,
        "tags": tags,
        "category": raw.get("category"),
        "topic": raw.get("topic"),
        "forum_normalized": raw.get("forum_normalized"),
        "difficulty": raw.get("difficulty"),
        "source_ticket_ids": raw.get("source_ticket_ids") or [],
        "reference_answer": expected_answer,
        "candidate_chunk_matches": matches,
        "best_chunk_match_score": round(top_score, 6),
        "needs_review": needs_review,
        "review_reason": review_reason(
            expected_escalated=expected_escalated,
            expected_chunk_ids=expected_chunk_ids,
            top_score=top_score,
            min_chunk_score=min_chunk_score,
        ),
    }


def match_chunks(
    case: dict[str, Any],
    chunks: list[dict[str, Any]],
    *,
    top_matches: int,
) -> list[dict[str, Any]]:
    query = str(case.get("query") or "")
    expected_answer = str(case.get("expected_answer") or "")
    query_tokens = tokenize(query)
    answer_tokens = tokenize(expected_answer)
    combined_tokens = query_tokens | answer_tokens
    if not combined_tokens:
        return []

    rows = []
    for chunk in chunks:
        if case.get("category") and chunk.get("category") != case.get("category"):
            category_penalty = 0.75
        else:
            category_penalty = 1.0
        if case.get("forum_normalized") and chunk.get("forum_normalized"):
            forum_bonus = 1.2 if chunk["forum_normalized"] == case["forum_normalized"] else 0.55
        else:
            forum_bonus = 1.0

        chunk_tokens = chunk["tokens"]
        query_overlap = weighted_overlap(query_tokens, chunk_tokens)
        answer_overlap = weighted_overlap(answer_tokens, chunk_tokens)
        combined_overlap = weighted_overlap(combined_tokens, chunk_tokens)
        score = (0.25 * query_overlap + 0.55 * answer_overlap + 0.20 * combined_overlap)
        score *= category_penalty * forum_bonus
        if score <= 0:
            continue
        rows.append(
            {
                "chunk_id": chunk["chunk_id"],
                "score": round(min(score, 1.0), 6),
                "category": chunk.get("category"),
                "topic": chunk.get("topic"),
                "forum_normalized": chunk.get("forum_normalized"),
                "text_preview": preview(chunk["text"], 260),
            }
        )
    rows.sort(key=lambda item: item["score"], reverse=True)
    return rows[:top_matches]


def retrieval_case(item: dict[str, Any]) -> dict[str, Any]:
    filters = retrieval_filters(item)
    return {
        "id": item["id"],
        "query": item["query"],
        "filters": filters,
        "expected_chunk_ids": item["expected_chunk_ids"],
        "source_ticket_ids": item.get("source_ticket_ids") or [],
        "best_chunk_match_score": item.get("best_chunk_match_score"),
    }


def retrieval_filters(item: dict[str, Any]) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    expected = set(item.get("expected_chunk_ids") or [])
    expected_matches = [
        match
        for match in item.get("candidate_chunk_matches") or []
        if match.get("chunk_id") in expected
    ]
    if expected_matches:
        for key in ("forum_normalized", "category"):
            value = expected_matches[0].get(key)
            if value:
                filters[key] = value
        return filters

    for key in ("forum_normalized", "category"):
        if item.get(key):
            filters[key] = item[key]
    return filters


def select_balanced_cases(cases: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    groups: dict[tuple[str, str, bool], list[dict[str, Any]]] = defaultdict(list)
    for item in cases:
        groups[
            (
                str(item.get("category") or "unknown"),
                str(item.get("difficulty") or "unknown"),
                bool(item.get("expected_escalated")),
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


def build_review_rows(cases: list[dict[str, Any]], limit: int = 300) -> list[dict[str, Any]]:
    priority = sorted(
        cases,
        key=lambda item: (
            not item["needs_review"],
            item["expected_escalated"],
            -float(item.get("best_chunk_match_score") or 0.0),
        ),
    )
    rows: list[dict[str, Any]] = []
    for item in priority[:limit]:
        rows.append(
            {
                "id": item["id"],
                "query": item["query"],
                "category": item.get("category") or "",
                "topic": item.get("topic") or "",
                "forum_normalized": item.get("forum_normalized") or "",
                "difficulty": item.get("difficulty") or "",
                "expected_escalated": item["expected_escalated"],
                "expected_escalation_reason": item.get("expected_escalation_reason") or "",
                "expected_chunk_ids": ", ".join(item["expected_chunk_ids"]),
                "expected_cited_chunk_ids": ", ".join(item["expected_cited_chunk_ids"]),
                "best_chunk_match_score": item.get("best_chunk_match_score"),
                "needs_review": item["needs_review"],
                "review_reason": item["review_reason"],
                "source_ticket_ids": ", ".join(item.get("source_ticket_ids") or []),
                "reference_answer": item.get("reference_answer") or "",
                "top_chunk_preview": (
                    item["candidate_chunk_matches"][0]["text_preview"]
                    if item["candidate_chunk_matches"]
                    else ""
                ),
            }
        )
    return rows


def build_report(
    cases: list[dict[str, Any]],
    retrieval_cases: list[dict[str, Any]],
    smoke_cases: list[dict[str, Any]],
    *,
    candidates_path: Path,
    kb_seed_path: Path,
    min_chunk_score: float,
) -> str:
    category_counts = Counter(str(item.get("category") or "unknown") for item in cases)
    difficulty_counts = Counter(str(item.get("difficulty") or "unknown") for item in cases)
    review_counts = Counter(str(item["review_reason"]) for item in cases)
    matched = [item for item in cases if item["expected_chunk_ids"]]
    scores = [float(item.get("best_chunk_match_score") or 0.0) for item in cases]
    lines = [
        "# Ticket Eval Preparation Report",
        "",
        f"- Source candidates: `{candidates_path}`",
        f"- KB seed: `{kb_seed_path}`",
        f"- Cases total: `{len(cases)}`",
        f"- Smoke cases: `{len(smoke_cases)}`",
        f"- Retrieval cases with weak chunk labels: `{len(retrieval_cases)}`",
        f"- Cases with matched chunks: `{len(matched)}`",
        f"- Needs review: `{sum(item['needs_review'] for item in cases)}`",
        f"- Min chunk score: `{min_chunk_score}`",
        f"- Best chunk score p50: `{percentile(scores, 0.50)}`",
        f"- Best chunk score p90: `{percentile(scores, 0.90)}`",
        "",
        "## Category Counts",
        "",
        *counter_lines(category_counts),
        "",
        "## Difficulty Counts",
        "",
        *counter_lines(difficulty_counts),
        "",
        "## Review Reasons",
        "",
        *counter_lines(review_counts),
        "",
        "## Next Steps",
        "",
        "- Open `ticket_manual_review_sample.csv` and confirm/reject weak chunk matches.",
        "- Use `ticket_ask_eval_smoke.json` for cheap `/ask` smoke checks before full eval.",
        "- Use `ticket_retrieval_golden_candidates.json` only as weak labels until reviewed.",
        "- Do not copy these private eval files into public `data/` without sanitization.",
    ]
    return "\n".join(lines) + "\n"


def review_reason(
    *,
    expected_escalated: bool,
    expected_chunk_ids: list[str],
    top_score: float,
    min_chunk_score: float,
) -> str:
    if expected_escalated:
        return "escalation_case_review"
    if not expected_chunk_ids:
        return "no_kb_chunk_match"
    if top_score < min_chunk_score:
        return "weak_kb_chunk_match"
    return "auto_matched"


def normalize_answer_contains(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [value]
    else:
        raw = [str(item) for item in value]
    return [clean_bot_text(item)[:180] for item in raw if clean_bot_text(item)]


def tokenize(text: str) -> set[str]:
    normalized = clean_bot_text(text).casefold().replace("ё", "е")
    tokens = re.findall(r"[a-zа-я0-9]{3,}", normalized)
    return {token for token in tokens if token not in STOPWORDS}


def weighted_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = left & right
    if not intersection:
        return 0.0
    return len(intersection) / math.sqrt(len(left) * len(right))


def percentile(values: list[float], quantile: float) -> float | None:
    numeric = sorted(value for value in values if math.isfinite(value))
    if not numeric:
        return None
    index = round((len(numeric) - 1) * quantile)
    return round(numeric[max(0, min(len(numeric) - 1, index))], 6)


def preview(text: str, limit: int) -> str:
    compact = " ".join(clean_bot_text(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def counter_lines(counter: Counter[str], limit: int = 20) -> list[str]:
    return [f"- `{count}` {name}" for name, count in counter.most_common(limit)]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare private ticket-derived eval sets for RAG quality checks."
    )
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--kb-seed", type=Path, default=DEFAULT_KB_SEED)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-cases", type=int, default=800)
    parser.add_argument("--smoke-size", type=int, default=40)
    parser.add_argument("--min-chunk-score", type=float, default=0.18)
    parser.add_argument("--top-matches", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    summary = prepare_eval_sets(
        candidates_path=args.candidates,
        kb_seed_path=args.kb_seed,
        output_dir=args.out_dir,
        max_cases=args.max_cases,
        smoke_size=args.smoke_size,
        min_chunk_score=args.min_chunk_score,
        top_matches=args.top_matches,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
