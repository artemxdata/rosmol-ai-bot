from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.ask_cases import seed_smoke_query, write_cases


def build_forum_smoke_set(
    kb_seed_path: Path,
    output_path: Path,
    *,
    per_forum: int = 1,
    user_prefix: str = "forum-smoke",
) -> dict[str, Any]:
    records = json.loads(kb_seed_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("KB seed must be a JSON array")

    by_forum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        forum = str(record.get("forum_normalized") or "").strip()
        if not forum or record.get("status") != "published" or not seed_smoke_query(record):
            continue
        by_forum[forum].append(record)

    cases: list[dict[str, Any]] = []
    for forum in sorted(by_forum):
        selected = _select_forum_records(by_forum[forum], limit=per_forum)
        for record in selected:
            cases.append(_case_from_record(record, index=len(cases) + 1, user_prefix=user_prefix))

    write_cases(output_path, cases)
    return _summary(cases, output_path)


def _select_forum_records(records: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_topics: set[str] = set()
    for record in sorted(records, key=_record_priority):
        topic = str(record.get("topic") or "")
        if topic and topic in seen_topics:
            continue
        selected.append(record)
        if topic:
            seen_topics.add(topic)
        if len(selected) >= limit:
            break
    return selected


def _record_priority(record: dict[str, Any]) -> tuple[int, int, str]:
    has_examples = 0 if record.get("intent_examples") else 1
    category_penalty = 0 if record.get("category") == "форумы" else 1
    chunk_id = str(record.get("chunk_id") or "")
    return (has_examples, category_penalty, chunk_id)


def _case_from_record(record: dict[str, Any], *, index: int, user_prefix: str) -> dict[str, Any]:
    chunk_id = str(record["chunk_id"])
    forum = str(record.get("forum_normalized") or "")
    category = str(record.get("category") or "unknown")
    tags = ["forum_smoke", f"category:{category}", f"forum:{forum}"]
    if record.get("topic"):
        tags.append(f"topic:{record['topic']}")

    return {
        "id": f"forum_smoke::{chunk_id}",
        "query": seed_smoke_query(record),
        "user_id": f"{user_prefix}-{index}",
        "channel": "api",
        "expected_chunk_ids": [chunk_id],
        "expected_answer_contains": [],
        "expected_escalated": False,
        "expected_escalation_reason": None,
        "expected_generator_model": None,
        "tags": tags,
    }


def _summary(cases: list[dict[str, Any]], output_path: Path) -> dict[str, Any]:
    forum_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    for case in cases:
        for tag in case.get("tags") or []:
            if tag.startswith("forum:"):
                forum_counts[tag.removeprefix("forum:")] += 1
            elif tag.startswith("category:"):
                category_counts[tag.removeprefix("category:")] += 1
    return {
        "cases_total": len(cases),
        "forums_total": len(forum_counts),
        "category_counts": dict(category_counts),
        "forum_counts": dict(sorted(forum_counts.items())),
        "output": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
    parser.add_argument("--output", default="reports/forum_smoke_set.json")
    parser.add_argument("--per-forum", type=int, default=1)
    parser.add_argument("--user-prefix", default="forum-smoke")
    args = parser.parse_args()

    summary = build_forum_smoke_set(
        kb_seed_path=Path(args.kb_seed),
        output_path=Path(args.output),
        per_forum=args.per_forum,
        user_prefix=args.user_prefix,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
