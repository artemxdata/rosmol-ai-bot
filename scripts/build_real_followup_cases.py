from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from src.kb.forum_registry import detect_forums_from_text

UNSAFE_EXCLUSION_REASONS = {
    "contains_sensitive_placeholder",
    "question_contains_sensitive_placeholder",
    "operator_join_placeholder",
    "personal_or_manual_answer",
    "personal_status_question",
    "routing_requires_operator",
}


def build_real_followup_cases(
    cases_path: Path,
    evaluation_path: Path,
    output_path: Path,
    *,
    forum: str | None = None,
    department: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    cases = _read_json_list(cases_path)
    evaluation = _read_json_object(evaluation_path)
    results = {
        str(item.get("id") or ""): item
        for item in evaluation.get("results") or []
        if isinstance(item, dict)
    }

    conversations: list[dict[str, Any]] = []
    skipped = Counter()
    forum_counts = Counter()
    for case in cases:
        case_id = str(case.get("id") or "")
        result = results.get(case_id)
        reason = _skip_reason(
            case,
            result,
            forum=forum,
            department=department,
        )
        if reason:
            skipped[reason] += 1
            continue

        resolved_forum = _reference_forum(case)
        if not resolved_forum:
            skipped["reference_forum_not_unique"] += 1
            continue
        conversations.append(_conversation(case, resolved_forum))
        forum_counts[resolved_forum] += 1
        if limit is not None and len(conversations) >= limit:
            break

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(conversations, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "source_cases": len(cases),
        "evaluation_results": len(results),
        "conversations_created": len(conversations),
        "turns_created": len(conversations) * 2,
        "forum_counts": dict(forum_counts.most_common()),
        "skipped": dict(skipped.most_common()),
        "output_path": str(output_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def _skip_reason(
    case: dict[str, Any],
    result: dict[str, Any] | None,
    *,
    forum: str | None,
    department: str | None,
) -> str | None:
    if result is None:
        return "result_missing"
    if case.get("expected_behavior") != "answer":
        return "expected_not_answer"
    if result.get("observed_behavior") != "clarify":
        return "observed_not_clarify"
    if forum and _normalize(case.get("forum_normalized")) not in {"", _normalize(forum)}:
        return "forum_filter_mismatch"
    if department and _normalize(case.get("department")) != _normalize(department):
        return "department_filter_mismatch"
    if detect_forums_from_text(str(case.get("query") or "")):
        return "query_already_has_forum"
    exclusion_reasons = {str(item) for item in case.get("golden_exclusion_reasons") or []}
    if exclusion_reasons & UNSAFE_EXCLUSION_REASONS:
        return "unsafe_or_manual"
    return None


def _reference_forum(case: dict[str, Any]) -> str | None:
    forums = detect_forums_from_text(str(case.get("reference_answer") or ""))
    unique = list(dict.fromkeys(forums))
    if len(unique) != 1:
        return None
    return unique[0]


def _conversation(case: dict[str, Any], forum: str) -> dict[str, Any]:
    case_id = str(case["id"])
    base_tags = [
        "real_june_followup",
        f"forum:{forum}",
        f"category:{case.get('category') or 'unknown'}",
    ]
    return {
        "id": f"real_followup_{case_id}",
        "source_case_id": case_id,
        "tags": base_tags,
        "turns": [
            {
                "id": f"real_followup_{case_id}_t1",
                "query": str(case.get("query") or ""),
                "channel": "api",
                "expected_behavior": "clarify",
                "expected_escalated": False,
                "tags": [*base_tags, "turn:1", "expected:clarify"],
            },
            {
                "id": f"real_followup_{case_id}_t2",
                "query": forum,
                "channel": "api",
                "expected_behavior": "answer",
                "expected_escalated": False,
                "tags": [*base_tags, "turn:2", "expected:answer"],
            },
        ],
    }


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON array")
    return [dict(item) for item in payload if isinstance(item, dict)]


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return dict(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build real two-turn clarification cases from operator QA data."
    )
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--forum")
    parser.add_argument("--department")
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build_real_followup_cases(
        args.cases,
        args.evaluation,
        args.output,
        forum=args.forum,
        department=args.department,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
