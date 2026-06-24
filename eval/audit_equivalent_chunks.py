from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.run_ask import _normalize_case, score_case, summarize_results

TOKEN_RE = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)


def audit_equivalent_chunks(
    *,
    cases_path: Path,
    metrics_path: Path,
    kb_seed_path: Path,
    min_text_similarity: float = 0.92,
    min_token_jaccard: float = 0.72,
) -> dict[str, Any]:
    cases = _read_json_array(cases_path)
    metrics = _read_json_object(metrics_path)
    records = _records_by_chunk_id(kb_seed_path)
    case_by_id = {str(case.get("id")): case for case in cases}

    rows: list[dict[str, Any]] = []
    for result in metrics.get("results") or []:
        if "expected_chunk_not_cited" not in (result.get("failure_reasons") or []):
            continue
        case_id = str(result.get("id") or "")
        case = case_by_id.get(case_id)
        if not case:
            continue
        expected_ids = _string_list(
            case.get("expected_cited_chunk_ids") or case.get("expected_chunk_ids") or []
        )
        cited_ids = _string_list(result.get("cited_source_ids") or [])
        for expected_id in expected_ids:
            for cited_id in cited_ids:
                if expected_id == cited_id:
                    continue
                row = _audit_pair(
                    case_id=case_id,
                    expected_id=expected_id,
                    cited_id=cited_id,
                    records=records,
                    min_text_similarity=min_text_similarity,
                    min_token_jaccard=min_token_jaccard,
                )
                rows.append(row)

    decision_counts = Counter(row["decision"] for row in rows)
    return {
        "cases_path": str(cases_path),
        "metrics_path": str(metrics_path),
        "kb_seed_path": str(kb_seed_path),
        "candidate_pairs": len(rows),
        "auto_equivalent_pairs": decision_counts.get("auto_equivalent", 0),
        "needs_review_pairs": decision_counts.get("needs_review", 0),
        "decision_counts": dict(decision_counts),
        "rows": rows,
    }


def build_cases_with_equivalents(
    cases: list[dict[str, Any]],
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    auto_pairs: dict[str, dict[str, set[str]]] = {}
    for row in report.get("rows") or []:
        if row.get("decision") != "auto_equivalent":
            continue
        case_id = str(row.get("case_id") or "")
        expected_id = str(row.get("expected_id") or "")
        cited_id = str(row.get("cited_id") or "")
        if not case_id or not expected_id or not cited_id:
            continue
        auto_pairs.setdefault(case_id, {}).setdefault(expected_id, set()).add(cited_id)

    enhanced: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case.get("id") or "")
        item = dict(case)
        equivalent_map = _existing_equivalent_map(item)
        for expected_id, cited_ids in auto_pairs.get(case_id, {}).items():
            equivalent_map.setdefault(expected_id, set()).update(cited_ids)
        if equivalent_map:
            item["equivalent_chunk_ids"] = {
                expected_id: sorted(cited_ids)
                for expected_id, cited_ids in sorted(equivalent_map.items())
            }
        enhanced.append(item)
    return enhanced


def rescore_metrics_with_equivalents(
    *,
    enhanced_cases: list[dict[str, Any]],
    metrics: dict[str, Any],
    target: str | None = None,
) -> dict[str, Any]:
    normalized_cases = {
        case["id"]: case for case in (_normalize_case(item) for item in enhanced_cases)
    }
    results = []
    for old_result in metrics.get("results") or []:
        case = normalized_cases.get(str(old_result.get("id") or ""))
        if not case:
            continue
        http_result = {
            "http_status": old_result.get("http_status"),
            "request_id": old_result.get("request_id"),
            "response": old_result.get("response") or "",
            "latency_ms": old_result.get("latency_ms"),
            "error": old_result.get("error"),
        }
        trace = {
            "cited_sources": old_result.get("cited_source_ids") or [],
            "retrieved_chunks": [
                {"chunk_id": chunk_id}
                for chunk_id in (old_result.get("observed_chunk_ids") or [])
            ],
            "reranker_scores": [],
            "was_escalated": old_result.get("was_escalated"),
            "escalation_reason": old_result.get("escalation_reason"),
            "generator_model": old_result.get("generator_model"),
            "cache_hit": old_result.get("cache_hit"),
            "max_reranker_score": old_result.get("max_reranker_score"),
            "total_latency_ms": old_result.get("trace_total_latency_ms"),
            "llm_usage": old_result.get("llm_usage") or [],
            "llm_prompt_tokens": old_result.get("llm_prompt_tokens") or 0,
            "llm_completion_tokens": old_result.get("llm_completion_tokens") or 0,
            "llm_total_tokens": old_result.get("llm_total_tokens") or 0,
            "llm_estimated_cost_rub": old_result.get("llm_estimated_cost_rub") or 0.0,
        }
        results.append(score_case(case, http_result, trace))

    return summarize_results(
        results,
        target=target or str(metrics.get("target") or "rescore"),
        cases_path=Path(str(metrics.get("cases_path") or "enhanced_cases.json")),
        generated_smoke_cases=bool(metrics.get("generated_smoke_cases")),
        trace_lookup_error=metrics.get("trace_lookup_error"),
    )


def write_markdown(report: dict[str, Any], output_path: Path) -> None:
    lines = [
        "# Equivalent Chunk Audit",
        "",
        f"- Cases: `{report['cases_path']}`",
        f"- Metrics: `{report['metrics_path']}`",
        f"- KB seed: `{report['kb_seed_path']}`",
        f"- Candidate pairs: `{report['candidate_pairs']}`",
        f"- Auto-equivalent pairs: `{report['auto_equivalent_pairs']}`",
        f"- Needs review pairs: `{report['needs_review_pairs']}`",
        "",
        "## Decision Counts",
        "",
    ]
    for decision, count in sorted(
        report["decision_counts"].items(),
        key=lambda item: (-item[1], item[0]),
    ):
        lines.append(f"- `{decision}`: `{count}`")

    rows = report.get("rows") or []
    if rows:
        lines.extend(["", "## Candidate Pairs", ""])
        for row in rows[:200]:
            lines.append(
                f"- `{row['decision']}` case=`{row['case_id']}` "
                f"expected=`{row['expected_id']}` cited=`{row['cited_id']}` "
                f"text_similarity=`{row['text_similarity']}` "
                f"token_jaccard=`{row['token_jaccard']}` "
                f"same_topic=`{row['same_topic']}` same_forum=`{row['same_forum']}`"
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _audit_pair(
    *,
    case_id: str,
    expected_id: str,
    cited_id: str,
    records: dict[str, dict[str, Any]],
    min_text_similarity: float,
    min_token_jaccard: float,
) -> dict[str, Any]:
    expected = records.get(expected_id) or {}
    cited = records.get(cited_id) or {}
    expected_text = _record_text(expected)
    cited_text = _record_text(cited)
    text_similarity = _text_similarity(expected_text, cited_text)
    token_jaccard = _token_jaccard(expected_text, cited_text)
    same_topic = _field(expected, "topic") and _field(expected, "topic") == _field(cited, "topic")
    same_forum = _field(expected, "forum_normalized") == _field(cited, "forum_normalized")
    same_category = _field(expected, "category") == _field(cited, "category")
    exact_text_match = bool(_normalize_text(expected_text)) and (
        _normalize_text(expected_text) == _normalize_text(cited_text)
    )
    metadata_aligned = bool(same_topic or (same_forum and same_category))

    decision = "needs_review"
    if exact_text_match:
        decision = "auto_equivalent"
    elif text_similarity >= min_text_similarity:
        decision = "auto_equivalent"
    elif metadata_aligned and token_jaccard >= min_token_jaccard:
        decision = "auto_equivalent"

    return {
        "case_id": case_id,
        "expected_id": expected_id,
        "cited_id": cited_id,
        "decision": decision,
        "expected_source_type": (
            _field(expected, "source_type") or _source_type_from_id(expected_id)
        ),
        "cited_source_type": _field(cited, "source_type") or _source_type_from_id(cited_id),
        "same_category": bool(same_category),
        "same_topic": bool(same_topic),
        "same_forum": bool(same_forum),
        "exact_text_match": exact_text_match,
        "text_similarity": round(text_similarity, 6),
        "token_jaccard": round(token_jaccard, 6),
    }


def _existing_equivalent_map(case: dict[str, Any]) -> dict[str, set[str]]:
    raw = case.get("equivalent_chunk_ids") or {}
    if isinstance(raw, dict):
        return {
            str(chunk_id): set(_string_list(equivalents))
            for chunk_id, equivalents in raw.items()
            if _string_list(equivalents)
        }
    equivalents = set(_string_list(raw))
    expected_ids = _string_list(case.get("expected_chunk_ids") or [])
    return {expected_id: set(equivalents) for expected_id in expected_ids if equivalents}


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


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item) for item in value if str(item).strip()]


def _record_text(record: dict[str, Any]) -> str:
    return str(record.get("text_clean") or record.get("text") or "")


def _field(record: dict[str, Any], key: str) -> str:
    return str(record.get(key) or "").strip()


def _source_type_from_id(chunk_id: str) -> str:
    if chunk_id.startswith("ticket_answer_bank_"):
        return "ticket_answer_bank"
    if chunk_id.startswith("xlsx_"):
        return "xlsx"
    if chunk_id.startswith("docx_"):
        return "docx"
    return "unknown"


def _text_similarity(left: str, right: str) -> float:
    left_normalized = _normalize_text(left)
    right_normalized = _normalize_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    return SequenceMatcher(None, left_normalized, right_normalized).ratio()


def _token_jaccard(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(_normalize_text(text)))


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit strict citation failures for equivalent source chunks."
    )
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--metrics", required=True, type=Path)
    parser.add_argument("--kb-seed", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument("--enhanced-cases-output", type=Path, default=None)
    parser.add_argument("--rescored-output", type=Path, default=None)
    parser.add_argument("--min-text-similarity", type=float, default=0.92)
    parser.add_argument("--min-token-jaccard", type=float, default=0.72)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = audit_equivalent_chunks(
        cases_path=args.cases,
        metrics_path=args.metrics,
        kb_seed_path=args.kb_seed,
        min_text_similarity=args.min_text_similarity,
        min_token_jaccard=args.min_token_jaccard,
    )
    if args.output:
        _write_json(args.output, report)
    if args.markdown:
        write_markdown(report, args.markdown)

    enhanced_cases: list[dict[str, Any]] | None = None
    if args.enhanced_cases_output or args.rescored_output:
        enhanced_cases = build_cases_with_equivalents(_read_json_array(args.cases), report)
    if args.enhanced_cases_output and enhanced_cases is not None:
        _write_json(args.enhanced_cases_output, enhanced_cases)
    if args.rescored_output and enhanced_cases is not None:
        metrics = _read_json_object(args.metrics)
        _write_json(
            args.rescored_output,
            rescore_metrics_with_equivalents(
                enhanced_cases=enhanced_cases,
                metrics=metrics,
            ),
        )

    print(json.dumps({key: value for key, value in report.items() if key != "rows"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
