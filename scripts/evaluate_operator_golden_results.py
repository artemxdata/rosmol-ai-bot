from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CASES = Path("data/private/operator_qa/analysis/operator_golden_calibration.json")
DEFAULT_EVAL = Path("data/private/operator_qa/analysis/calibration_bot_eval.json")
DEFAULT_OUTPUT = Path("data/private/operator_qa/analysis/calibration_quality_analysis.json")

TOKEN_RE = re.compile(r"[a-zа-я0-9]{3,}", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)
STOPWORDS = {
    "без",
    "более",
    "будет",
    "быть",
    "ваш",
    "ваша",
    "ваше",
    "ваши",
    "всех",
    "для",
    "если",
    "ещё",
    "или",
    "как",
    "можно",
    "необходимо",
    "нужно",
    "пожалуйста",
    "после",
    "при",
    "также",
    "того",
    "чтобы",
    "этого",
}
RUSSIAN_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "ого",
    "ему",
    "ому",
    "ыми",
    "ими",
    "ать",
    "ять",
    "ить",
    "ете",
    "ите",
    "ом",
    "ем",
    "ах",
    "ях",
    "ый",
    "ий",
    "ая",
    "яя",
    "ое",
    "ее",
    "ую",
    "юю",
    "а",
    "я",
    "ы",
    "и",
    "у",
    "ю",
    "е",
)


def evaluate_operator_results(
    cases_path: Path = DEFAULT_CASES,
    eval_path: Path = DEFAULT_EVAL,
    output_path: Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    cases = _read_json_array(cases_path)
    evaluation = _read_json_object(eval_path)
    results_by_id = {
        str(item.get("id")): item
        for item in evaluation.get("results") or []
        if isinstance(item, dict) and item.get("id")
    }
    rows = [score_case(case, results_by_id.get(str(case.get("id")))) for case in cases]
    report = build_report(rows, cases_path=cases_path, eval_path=eval_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output_path, report)
    markdown_path = output_path.with_suffix(".md")
    markdown_path.write_text(build_markdown(report), encoding="utf-8")
    gaps_path = output_path.with_name("calibration_gap_candidates.json")
    _write_json(
        gaps_path,
        [row for row in rows if row["gap_candidate"]],
    )
    summary = {key: value for key, value in report.items() if key != "cases"}
    summary["output"] = str(output_path)
    summary["markdown"] = str(markdown_path)
    summary["gaps"] = str(gaps_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return report


def score_case(case: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    reference = str(case.get("reference_answer") or "")
    response = str((result or {}).get("response") or "")
    expected_behavior = str(case.get("expected_behavior") or "answer")
    observed_behavior = str((result or {}).get("observed_behavior") or "missing")
    was_escalated = bool((result or {}).get("was_escalated"))
    evaluated = bool(result)
    answered_without_operator = (
        evaluated
        and expected_behavior == "answer"
        and observed_behavior == "answer"
        and not was_escalated
    )
    token_precision, token_recall, token_f1 = token_overlap(reference, response)
    facts = [str(value) for value in case.get("reference_facts") or [] if str(value)]
    fact_coverage, matched_facts = fact_coverage_score(facts, response)
    cited_sources = _cited_sources(result)
    source_count = len(cited_sources)
    source_grounded = answered_without_operator and source_count > 0
    content_aligned = answer_alignment(
        answered_without_operator=answered_without_operator,
        token_f1=token_f1,
        fact_coverage=fact_coverage,
        facts_count=len(facts),
    )
    gap_candidate = evaluated and (
        not answered_without_operator or not source_grounded or not content_aligned
    )
    return {
        "id": case.get("id"),
        "query": case.get("query"),
        "reference_answer": reference,
        "bot_response": response,
        "category": case.get("category"),
        "topic": case.get("topic"),
        "forum_normalized": case.get("forum_normalized"),
        "difficulty": case.get("difficulty"),
        "expected_behavior": expected_behavior,
        "observed_behavior": observed_behavior,
        "evaluated": evaluated,
        "answered_without_operator": answered_without_operator,
        "was_escalated": was_escalated,
        "escalation_reason": (result or {}).get("escalation_reason"),
        "generator_model": (result or {}).get("generator_model"),
        "cited_sources": cited_sources,
        "source_grounded": source_grounded,
        "token_precision": round(token_precision, 6),
        "token_recall": round(token_recall, 6),
        "token_f1": round(token_f1, 6),
        "reference_facts": facts,
        "matched_facts": matched_facts,
        "fact_coverage": round(fact_coverage, 6),
        "content_aligned_heuristic": content_aligned,
        "latency_ms": (result or {}).get("trace_total_latency_ms")
        or (result or {}).get("latency_ms"),
        "gap_candidate": gap_candidate,
        "review_priority": review_priority(
            evaluated=evaluated,
            answered_without_operator=answered_without_operator,
            source_grounded=source_grounded,
            content_aligned=content_aligned,
        ),
    }


def _cited_sources(result: dict[str, Any] | None) -> list[str]:
    if not result:
        return []
    raw_sources = result.get("cited_source_ids") or result.get("cited_sources") or []
    if isinstance(raw_sources, str):
        raw_sources = [raw_sources]
    return [str(source) for source in raw_sources if str(source).strip()]


def token_overlap(reference: str, response: str) -> tuple[float, float, float]:
    reference_tokens = _tokens(reference)
    response_tokens = _tokens(response)
    if not reference_tokens or not response_tokens:
        return 0.0, 0.0, 0.0
    overlap = reference_tokens & response_tokens
    precision = len(overlap) / len(response_tokens)
    recall = len(overlap) / len(reference_tokens)
    if precision + recall == 0:
        return precision, recall, 0.0
    return precision, recall, 2 * precision * recall / (precision + recall)


def fact_coverage_score(facts: list[str], response: str) -> tuple[float, list[str]]:
    if not facts:
        return 1.0, []
    normalized_response = _normalize_fact_text(response)
    matched = [fact for fact in facts if _normalize_fact_text(fact) in normalized_response]
    return len(matched) / len(facts), matched


def answer_alignment(
    *,
    answered_without_operator: bool,
    token_f1: float,
    fact_coverage: float,
    facts_count: int,
) -> bool:
    if not answered_without_operator:
        return False
    if token_f1 >= 0.23:
        return True
    if facts_count and fact_coverage >= 0.60 and token_f1 >= 0.10:
        return True
    return False


def review_priority(
    *,
    evaluated: bool = True,
    answered_without_operator: bool,
    source_grounded: bool,
    content_aligned: bool,
) -> str:
    if not evaluated:
        return "P2_not_evaluated"
    if not answered_without_operator:
        return "P0_conversion"
    if not source_grounded:
        return "P1_no_source"
    if not content_aligned:
        return "P1_content_mismatch"
    return "P3_pass"


def build_report(
    rows: list[dict[str, Any]],
    *,
    cases_path: Path,
    eval_path: Path,
) -> dict[str, Any]:
    total = len(rows)
    evaluated_rows = [row for row in rows if row["evaluated"]]
    evaluated = len(evaluated_rows)
    answered = sum(row["answered_without_operator"] for row in rows)
    grounded = sum(row["source_grounded"] for row in rows)
    aligned = sum(row["content_aligned_heuristic"] for row in rows)
    latencies = [int(row["latency_ms"]) for row in rows if row.get("latency_ms") is not None]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "cases_path": str(cases_path),
        "eval_path": str(eval_path),
        "cases_total": total,
        "cases_evaluated": evaluated,
        "cases_not_evaluated": total - evaluated,
        "answered_without_operator": answered,
        "conversion_without_operator_lower_bound": _ratio(answered, total),
        "conversion_without_operator_evaluated": _ratio(answered, evaluated),
        "source_grounded": grounded,
        "source_grounded_rate_evaluated": _ratio(grounded, evaluated),
        "content_aligned_heuristic": aligned,
        "content_aligned_heuristic_rate_evaluated": _ratio(aligned, evaluated),
        "review_priority_counts": dict(Counter(row["review_priority"] for row in rows)),
        "category_metrics": grouped_metrics(rows, "category"),
        "topic_metrics": grouped_metrics(rows, "topic"),
        "forum_metrics": grouped_metrics(rows, "forum_normalized", limit=40),
        "latency_ms": number_summary(latencies),
        "cases": rows,
        "methodology_note": (
            "Content alignment is a lexical/fact heuristic for triage, not a final factual judge. "
            "Holdout cases are intentionally excluded from this calibration report."
        ),
    }


def grouped_metrics(
    rows: list[dict[str, Any]],
    field: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    ordered = sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0]))
    if limit is not None:
        ordered = ordered[:limit]
    return [
        _group_metric(field, name, items)
        for name, items in ordered
    ]


def _group_metric(
    field: str,
    name: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    evaluated = [item for item in items if item["evaluated"]]
    return {
        field: name,
        "cases": len(items),
        "evaluated": len(evaluated),
        "evaluation_coverage": _ratio(len(evaluated), len(items)),
        "conversion_without_operator_evaluated": _ratio(
            sum(item["answered_without_operator"] for item in evaluated),
            len(evaluated),
        ),
        "source_grounded_rate_evaluated": _ratio(
            sum(item["source_grounded"] for item in evaluated),
            len(evaluated),
        ),
        "content_aligned_heuristic_rate_evaluated": _ratio(
            sum(item["content_aligned_heuristic"] for item in evaluated),
            len(evaluated),
        ),
    }


def number_summary(values: list[int]) -> dict[str, int | float | None]:
    if not values:
        return {"avg": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "avg": round(sum(values) / len(values), 2),
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "max": max(values),
    }


def build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Operator Golden Calibration Report",
        "",
        f"- Cases: `{report['cases_total']}`",
        f"- Evaluated: `{report['cases_evaluated']}`",
        f"- Not evaluated: `{report['cases_not_evaluated']}`",
        "- Conversion without operator, evaluated: "
        f"`{report['conversion_without_operator_evaluated']:.1%}`",
        "- Conversion lower bound over all cases: "
        f"`{report['conversion_without_operator_lower_bound']:.1%}`",
        f"- Source grounded, evaluated: `{report['source_grounded_rate_evaluated']:.1%}`",
        "- Content aligned heuristic, evaluated: "
        f"`{report['content_aligned_heuristic_rate_evaluated']:.1%}`",
        f"- Latency p50: `{report['latency_ms']['p50']} ms`",
        f"- Latency p95: `{report['latency_ms']['p95']} ms`",
        "",
        "## Important",
        "",
        f"- {report['methodology_note']}",
        "- Operator replies are references; public Yonote/RAG remains the factual source of truth.",
        "- The sealed holdout set was not used in this run.",
        "",
        "## Review Priorities",
        "",
    ]
    for key, value in report["review_priority_counts"].items():
        lines.append(f"- `{value}` {key}")
    lines.extend(["", "## Lowest Conversion Topics", ""])
    topics = sorted(
        report["topic_metrics"],
        key=lambda item: (
            item["conversion_without_operator_evaluated"],
            -item["evaluated"],
        ),
    )
    for item in topics[:20]:
        lines.append(
            f"- `{item['cases']}` {item['topic']}: "
            f"evaluated={item['evaluated']}, "
            f"conversion={item['conversion_without_operator_evaluated']:.1%}, "
            f"grounded={item['source_grounded_rate_evaluated']:.1%}, "
            f"aligned={item['content_aligned_heuristic_rate_evaluated']:.1%}"
        )
    lines.extend(["", "## Top P0/P1 Cases", ""])
    failures = [case for case in report["cases"] if case["review_priority"] != "P3_pass"]
    for case in failures[:40]:
        lines.append(
            f"- `{case['id']}` {case['review_priority']} "
            f"{case.get('category')}/{case.get('topic')}: {str(case.get('query') or '')[:180]}"
        )
    return "\n".join(lines) + "\n"


def _tokens(text: str) -> set[str]:
    normalized = str(text or "").casefold().replace("ё", "е")
    return {
        _stem_token(token)
        for token in TOKEN_RE.findall(normalized)
        if token not in STOPWORDS
    }


def _stem_token(token: str) -> str:
    if not re.fullmatch(r"[а-я]+", token):
        return token
    for suffix in RUSSIAN_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def _normalize_fact_text(text: str) -> str:
    value = str(text or "").casefold().replace("ё", "е")
    value = URL_RE.sub(lambda match: match.group(0).rstrip(".,;:/"), value)
    return " ".join(value.split())


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _percentile(ordered: list[int], quantile: float) -> int:
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{path} must contain a JSON array of objects")
    return [dict(item) for item in value]


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare live bot results with the private operator golden calibration set."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--eval", type=Path, default=DEFAULT_EVAL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    evaluate_operator_results(args.cases, args.eval, args.output)


if __name__ == "__main__":
    main()
