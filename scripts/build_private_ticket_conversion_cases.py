from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

TYPICAL_LABEL = "Типовой"
ATYPICAL_LABEL = "Нетиповой"
IN_SCOPE_CATEGORIES = {
    "форумы",
    "гранты",
    "платформа_фгаис",
    "техподдержка",
    "навигация",
}
ESCALATION_REASONS = {
    "operator_requested",
    "technical_issue",
    "legal_or_financial_risk",
    "personal_status",
    "open_or_in_progress",
    "unsafe_or_abusive",
}
GENERIC_APPLICATION_QUERIES = {
    "подать заявку на участие",
    "как подать заявку",
    "хочу подать заявку",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a private 2026 ticket conversion eval set."
    )
    parser.add_argument(
        "--input",
        default="data/private/tickets/analysis_2026_full/tickets_normalized.jsonl",
    )
    parser.add_argument(
        "--output",
        default=(
            "data/private/tickets/eval_2026_full/"
            "conversion_2026_50_typical_100_atypical.json"
        ),
    )
    parser.add_argument("--typical", type=int, default=50)
    parser.add_argument("--atypical", type=int, default=100)
    args = parser.parse_args()

    records = load_records(Path(args.input))
    typical_pool = [
        item
        for item in records
        if item.get("typical_atypical") == TYPICAL_LABEL
        and item.get("answerable_by_kb")
        and not item.get("should_escalate")
    ]
    if len(typical_pool) < args.typical:
        seen_ids = {id(item) for item in typical_pool}
        typical_pool.extend(
            item
            for item in records
            if item.get("typical_atypical") == TYPICAL_LABEL
            and not item.get("should_escalate")
            and id(item) not in seen_ids
        )

    atypical_pool = [
        item for item in records if item.get("typical_atypical") == ATYPICAL_LABEL
    ]
    cases = stratified_cases(typical_pool, args.typical, "typical")
    cases.extend(stratified_cases(atypical_pool, args.atypical, "atypical"))

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "output": str(output_path),
                "cases": len(cases),
                "pools": {
                    "typical": len(typical_pool),
                    "atypical": len(atypical_pool),
                },
                "by_type": Counter(case["_typical_atypical"] for case in cases),
                "expected_behavior": Counter(case["expected_behavior"] for case in cases),
                "by_type_behavior": _string_key_counter(
                    Counter(
                        (case["_typical_atypical"], case["expected_behavior"])
                        for case in cases
                    )
                ),
                "categories": Counter(case["_category"] for case in cases),
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )


def load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            query = compact_text(item.get("question_candidate") or "")
            if len(query) < 8:
                continue
            if len(query) > 900:
                item["question_candidate"] = query[:900]
            records.append(item)
    return records


def stratified_cases(
    items: list[dict[str, Any]],
    limit: int,
    prefix: str,
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        key = (
            str(item.get("category") or "unknown"),
            str(item.get("topic") or "unknown"),
            str(item.get("escalation_reason") or "none"),
        )
        buckets[key].append(item)

    ordered_keys = sorted(buckets, key=lambda key: (-len(buckets[key]), key))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    while len(selected) < limit:
        progressed = False
        for key in ordered_keys:
            bucket = buckets[key]
            while bucket:
                item = bucket.pop(0)
                fingerprint = query_fingerprint(item.get("question_candidate") or "")
                if fingerprint in seen:
                    continue
                selected.append(item)
                seen.add(fingerprint)
                progressed = True
                break
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return [build_case(item, prefix, index) for index, item in enumerate(selected, 1)]


def build_case(item: dict[str, Any], prefix: str, index: int) -> dict[str, Any]:
    behavior, expected_escalated = expected_behavior(item)
    return {
        "id": f"{prefix}-{index:03d}-{item.get('ticket_hash')}",
        "query": compact_text(item.get("question_candidate") or item.get("title_masked") or ""),
        "user_id": f"private-2026-{prefix}-{index:03d}",
        "channel": "api",
        "expected_behavior": behavior,
        "expected_escalated": expected_escalated,
        "tags": [
            "private_2026",
            f"type:{item.get('typical_atypical')}",
            f"category:{item.get('category')}",
            f"topic:{item.get('topic')}",
            f"difficulty:{item.get('difficulty')}",
            f"answerable:{bool(item.get('answerable_by_kb'))}",
            f"escalation_reason:{item.get('escalation_reason') or 'none'}",
        ],
        "source_ticket_ids": [item.get("ticket_hash")],
        "_typical_atypical": item.get("typical_atypical"),
        "_category": item.get("category"),
        "_topic": item.get("topic"),
        "_difficulty": item.get("difficulty"),
        "_answerable_by_kb": bool(item.get("answerable_by_kb")),
        "_source_escalation_reason": item.get("escalation_reason"),
    }


def expected_behavior(item: dict[str, Any]) -> tuple[str, bool]:
    reason = item.get("escalation_reason")
    if item.get("should_escalate") or reason in ESCALATION_REASONS:
        return "escalate", True

    query_normalized = normalize_text(item.get("question_candidate") or "")
    if item.get("category") not in IN_SCOPE_CATEGORIES:
        return "scope_note", False
    if item.get("needs_clarification") or query_normalized in GENERIC_APPLICATION_QUERIES:
        return "clarify", False
    if len(query_normalized) < 18 and not item.get("forum_normalized"):
        return "clarify", False
    return "answer", False


def compact_text(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_text(text: object) -> str:
    return compact_text(text).casefold().replace("ё", "е")


def query_fingerprint(text: object) -> str:
    return re.sub(r"[^\w]+", " ", normalize_text(text))[:180]


def _json_default(value: object) -> object:
    if isinstance(value, Counter):
        return dict(value)
    return str(value)


def _string_key_counter(counter: Counter[tuple[object, ...]]) -> dict[str, int]:
    return {" / ".join(str(part) for part in key): value for key, value in counter.items()}


if __name__ == "__main__":
    main()
