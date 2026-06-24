from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))


def build_cleanup_report(
    *,
    audit_path: Path,
    kb_seed_path: Path,
    include_decisions: set[str] | None = None,
) -> dict[str, Any]:
    audit = _read_json_object(audit_path)
    records = _records_by_chunk_id(kb_seed_path)
    decision_filter = include_decisions or {"needs_review"}

    rows: list[dict[str, Any]] = []
    for row in audit.get("rows") or []:
        if not isinstance(row, dict):
            continue
        decision = str(row.get("decision") or "")
        if decision not in decision_filter:
            continue
        rows.append(_cleanup_row(row, records))

    classification_counts = Counter(row["classification"] for row in rows)
    action_counts = Counter(row["suggested_action"] for row in rows)
    severity_counts = Counter(row["severity"] for row in rows)
    source_type_pair_counts = Counter(
        f"{row['expected']['source_type']}->{row['cited']['source_type']}"
        for row in rows
    )

    return {
        "audit_path": str(audit_path),
        "kb_seed_path": str(kb_seed_path),
        "input_candidate_pairs": len(audit.get("rows") or []),
        "analyzed_pairs": len(rows),
        "decision_filter": sorted(decision_filter),
        "classification_counts": dict(classification_counts),
        "suggested_action_counts": dict(action_counts),
        "severity_counts": dict(severity_counts),
        "source_type_pair_counts": dict(source_type_pair_counts),
        "rows": rows,
    }


def write_markdown(report: dict[str, Any], output_path: Path) -> None:
    lines = [
        "# KB Cleanup Report",
        "",
        f"- Audit: `{report['audit_path']}`",
        f"- KB seed: `{report['kb_seed_path']}`",
        f"- Input candidate pairs: `{report['input_candidate_pairs']}`",
        f"- Analyzed pairs: `{report['analyzed_pairs']}`",
        f"- Decisions: `{', '.join(report['decision_filter'])}`",
        "",
        "## Classification Counts",
        "",
    ]
    lines.extend(_counter_lines(report["classification_counts"]))
    lines.extend(["", "## Suggested Actions", ""])
    lines.extend(_counter_lines(report["suggested_action_counts"]))
    lines.extend(["", "## Severity", ""])
    lines.extend(_counter_lines(report["severity_counts"]))

    rows = report.get("rows") or []
    if rows:
        lines.extend(["", "## Pairs", ""])
        for row in rows[:200]:
            lines.append(
                f"- `{row['classification']}` severity=`{row['severity']}` "
                f"case=`{row['case_id']}` expected=`{row['expected_id']}` "
                f"cited=`{row['cited_id']}` action=`{row['suggested_action']}` "
                f"similarity=`{row['signals']['text_similarity']}` "
                f"jaccard=`{row['signals']['token_jaccard']}` "
                f"same_topic=`{row['signals']['same_topic']}` "
                f"same_forum=`{row['signals']['same_forum']}` "
                f"source_pair=`{row['expected']['source_type']}->{row['cited']['source_type']}`"
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cleanup_row(
    row: dict[str, Any],
    records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    expected_id = str(row.get("expected_id") or "")
    cited_id = str(row.get("cited_id") or "")
    expected = records.get(expected_id) or {}
    cited = records.get(cited_id) or {}
    classification = _classify(row, expected, cited)
    return {
        "case_id": str(row.get("case_id") or ""),
        "expected_id": expected_id,
        "cited_id": cited_id,
        "classification": classification["classification"],
        "severity": classification["severity"],
        "suggested_action": classification["suggested_action"],
        "signals": {
            "same_category": _bool(row.get("same_category")),
            "same_topic": _bool(row.get("same_topic")),
            "same_forum": _bool(row.get("same_forum")),
            "exact_text_match": _bool(row.get("exact_text_match")),
            "text_similarity": _float(row.get("text_similarity")),
            "token_jaccard": _float(row.get("token_jaccard")),
            "expected_generic": _is_generic(expected),
            "cited_generic": _is_generic(cited),
            "metadata_conflict": _metadata_conflict(expected, cited),
        },
        "expected": _safe_metadata(expected, expected_id),
        "cited": _safe_metadata(cited, cited_id),
    }


def _classify(
    row: dict[str, Any],
    expected: dict[str, Any],
    cited: dict[str, Any],
) -> dict[str, str]:
    text_similarity = _float(row.get("text_similarity"))
    token_jaccard = _float(row.get("token_jaccard"))
    exact_text_match = _bool(row.get("exact_text_match"))
    same_topic = _bool(row.get("same_topic"))
    same_forum = _bool(row.get("same_forum"))
    expected_generic = _is_generic(expected)
    cited_generic = _is_generic(cited)

    if exact_text_match or (text_similarity >= 0.98 and token_jaccard >= 0.95):
        return _decision("duplicate_exact", "low", "merge_or_mark_equivalent")
    if text_similarity >= 0.92 or (same_topic and token_jaccard >= 0.72):
        return _decision("duplicate_equivalent", "low", "mark_equivalent_after_review")
    if cited_generic and not expected_generic:
        return _decision(
            "generic_fallback_competes_with_specific",
            "high",
            "lower_generic_priority_or_add_specific_filter",
        )
    if same_forum and not same_topic:
        return _decision(
            "same_forum_different_topic",
            "medium",
            "improve_topic_disambiguation",
        )
    if _metadata_conflict(expected, cited) and _source_type(expected) != _source_type(cited):
        return _decision(
            "cross_source_conflict",
            "high",
            "review_source_priority_and_metadata",
        )
    if text_similarity < 0.35 and token_jaccard < 0.20:
        return _decision(
            "wrong_source_selected",
            "high",
            "tighten_retrieval_or_rerank_signals",
        )
    if same_topic:
        return _decision(
            "same_topic_neighbor",
            "medium",
            "review_neighbor_chunk_boundaries",
        )
    return _decision("needs_manual_review", "medium", "manual_review")


def _decision(
    classification: str,
    severity: str,
    suggested_action: str,
) -> dict[str, str]:
    return {
        "classification": classification,
        "severity": severity,
        "suggested_action": suggested_action,
    }


def _safe_metadata(record: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    return {
        "chunk_id": str(record.get("chunk_id") or fallback_id),
        "source_type": _source_type(record) or _source_type_from_id(fallback_id),
        "source_category": _str(record.get("source_category")),
        "category": _str(record.get("category")),
        "topic": _str(record.get("topic")),
        "forum_normalized": _str(record.get("forum_normalized") or record.get("forum")),
        "status": _str(record.get("status")),
        "is_generic": _is_generic(record),
        "source_specificity": _source_specificity(record, fallback_id),
    }


def _metadata_conflict(expected: dict[str, Any], cited: dict[str, Any]) -> bool:
    comparable_keys = ("category", "topic", "forum_normalized")
    for key in comparable_keys:
        left = _str(expected.get(key))
        right = _str(cited.get(key))
        if left and right and left != right:
            return True
    return False


def _source_specificity(record: dict[str, Any], chunk_id: str) -> int:
    source_type = _source_type(record) or _source_type_from_id(chunk_id)
    is_generic = _is_generic(record)
    if source_type in {"docx", "xlsx"} and not is_generic:
        return 4
    if source_type == "ticket_answer_bank" and not is_generic:
        return 3
    if source_type in {"docx", "xlsx"}:
        return 2
    if source_type == "ticket_answer_bank":
        return 1
    return 0


def _records_by_chunk_id(path: Path) -> dict[str, dict[str, Any]]:
    return {
        str(record.get("chunk_id")): record
        for record in _read_json_array(path)
        if record.get("chunk_id")
    }


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"file must contain a JSON array of objects: {path}")
    return payload


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"file must contain a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _counter_lines(counter: dict[str, int]) -> list[str]:
    if not counter:
        return ["- none"]
    return [
        f"- `{key}`: `{value}`"
        for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y"}
    return bool(value)


def _float(value: Any) -> float:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return 0.0


def _is_generic(record: dict[str, Any]) -> bool:
    value = record.get("is_generic")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y"}
    return False


def _source_type(record: dict[str, Any]) -> str:
    return _str(record.get("source_type"))


def _source_type_from_id(chunk_id: str) -> str:
    if chunk_id.startswith("ticket_answer_bank_"):
        return "ticket_answer_bank"
    if chunk_id.startswith("xlsx_"):
        return "xlsx"
    if chunk_id.startswith("docx_"):
        return "docx"
    return "unknown"


def _str(value: Any) -> str:
    return str(value or "").strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a safe KB cleanup report from equivalent chunk audit rows."
    )
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--kb-seed", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument(
        "--include-decision",
        action="append",
        default=None,
        help="Audit decision to include. Defaults to needs_review.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    include_decisions = set(args.include_decision) if args.include_decision else None
    report = build_cleanup_report(
        audit_path=args.audit,
        kb_seed_path=args.kb_seed,
        include_decisions=include_decisions,
    )
    if args.output:
        _write_json(args.output, report)
    if args.markdown:
        write_markdown(report, args.markdown)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "rows"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
