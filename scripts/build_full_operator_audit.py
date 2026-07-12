from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_operator_golden_results import fact_coverage_score

DEFAULT_CASES = Path(
    "data/private/operator_qa/analysis/operator_full_june_eval_runnable.json"
)
DEFAULT_EMPTY_CASES = Path(
    "data/private/operator_qa/analysis/operator_full_june_empty_queries.json"
)
DEFAULT_OUTPUT = Path(
    "data/private/operator_qa/analysis/operator_full_june_quality_audit.json"
)
SELF_SERVICE_BEHAVIORS = {"answer", "clarify", "scope_note"}


def build_full_operator_audit(
    cases_path: Path,
    eval_paths: list[Path],
    output_path: Path,
    *,
    empty_cases_path: Path | None = None,
) -> dict[str, Any]:
    cases = _read_json_array(cases_path)
    empty_cases = (
        _read_json_array(empty_cases_path)
        if empty_cases_path and empty_cases_path.exists()
        else []
    )
    results, duplicate_results = merge_results(eval_paths)
    rows = [score_audit_row(case, results.get(str(case.get("id") or ""))) for case in cases]
    report = build_report(
        rows,
        cases_path=cases_path,
        eval_paths=eval_paths,
        empty_cases_count=len(empty_cases),
        duplicate_results=duplicate_results,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output_path.with_suffix(".md").write_text(
        build_markdown(report),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "cases"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return report


def merge_results(eval_paths: list[Path]) -> tuple[dict[str, dict[str, Any]], int]:
    merged: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for path in eval_paths:
        payload = _read_json_object(path)
        for item in payload.get("results") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            case_id = str(item["id"])
            if case_id in merged:
                duplicates += 1
            merged[case_id] = dict(item)
    return merged, duplicates


def score_audit_row(
    case: dict[str, Any],
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    result = result or {}
    evaluated = bool(result)
    observed = str(result.get("observed_behavior") or "missing")
    escalated = bool(result.get("was_escalated")) or observed == "escalate"
    direct_answer = evaluated and observed == "answer" and not escalated
    contained = evaluated and observed in SELF_SERVICE_BEHAVIORS and not escalated
    cited_sources = _list_value(
        result.get("cited_source_ids") or result.get("cited_sources") or []
    )
    facts = _list_value(case.get("reference_facts") or [])
    fact_coverage, matched_facts = fact_coverage_score(
        facts,
        str(result.get("response") or ""),
    )
    exclusion_reasons = _list_value(case.get("golden_exclusion_reasons") or [])
    context_limited = is_context_limited(case)
    strict_grounded_answer = (
        bool(case.get("golden_eligible")) and direct_answer and bool(cited_sources)
    )
    return {
        "id": str(case.get("id") or ""),
        "query": case.get("query"),
        "department": case.get("department"),
        "category": case.get("category"),
        "topic": case.get("topic"),
        "forum_normalized": case.get("forum_normalized"),
        "expected_behavior": str(case.get("expected_behavior") or "answer"),
        "observed_behavior": observed,
        "evaluated": evaluated,
        "direct_answer": direct_answer,
        "contained_without_operator": contained,
        "was_escalated": escalated,
        "escalation_reason": result.get("escalation_reason"),
        "generator_model": result.get("generator_model"),
        "cited_sources": cited_sources,
        "source_grounded": direct_answer and bool(cited_sources),
        "golden_eligible": bool(case.get("golden_eligible")),
        "golden_exclusion_reasons": exclusion_reasons,
        "context_limited": context_limited,
        "temporal_review_required": bool(case.get("temporal_review_required")),
        "strict_grounded_answer": strict_grounded_answer,
        "reference_facts_count": len(facts),
        "matched_reference_facts": matched_facts,
        "reference_fact_coverage": round(fact_coverage, 6),
        "latency_ms": result.get("trace_total_latency_ms") or result.get("latency_ms"),
        "llm_total_tokens": int(result.get("llm_total_tokens") or 0),
        "llm_estimated_cost_rub": float(result.get("llm_estimated_cost_rub") or 0.0),
    }


def is_context_limited(case: dict[str, Any]) -> bool:
    reasons = set(_list_value(case.get("golden_exclusion_reasons") or []))
    if "followup_without_context" in reasons:
        return True
    forum = str(case.get("forum_normalized") or "").strip()
    candidate_chunks = _list_value(case.get("candidate_chunk_ids") or [])
    weak_reasons = {
        "low_signal_question",
        "answer_shaped_or_weak_question",
        "unsupported_category",
    }
    return (
        not forum
        and not candidate_chunks
        and bool(reasons & weak_reasons)
    )


def build_report(
    rows: list[dict[str, Any]],
    *,
    cases_path: Path,
    eval_paths: list[Path],
    empty_cases_count: int,
    duplicate_results: int,
) -> dict[str, Any]:
    evaluated = [row for row in rows if row["evaluated"]]
    golden = [row for row in evaluated if row["golden_eligible"]]
    context_limited = [row for row in evaluated if row["context_limited"]]
    temporal = [row for row in evaluated if row["temporal_review_required"]]
    fact_scored = [
        row
        for row in golden
        if row["direct_answer"] and row["reference_facts_count"] > 0
    ]
    reliable_gaps = [
        row
        for row in golden
        if not row["direct_answer"] or not row["source_grounded"]
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "methodology": {
            "cases_path": str(cases_path),
            "eval_paths": [str(path) for path in eval_paths],
            "direct_conversion_definition": (
                "Первый ответ является содержательным answer без передачи оператору."
            ),
            "containment_definition": (
                "Ответ, корректное уточнение или scope-note без передачи оператору."
            ),
            "strict_quality_definition": (
                "Golden-eligible кейс получил прямой ответ с cited source."
            ),
            "operator_reference_warning": (
                "Ответ оператора не считается автоматически истинным: часть строк теряет "
                "контекст тикета, содержит временные сведения или данные из внутренних "
                "каналов, которых нет в источниках бота."
            ),
        },
        "coverage": {
            "source_rows_total": len(rows) + empty_cases_count,
            "runnable_rows": len(rows),
            "empty_queries_excluded": empty_cases_count,
            "evaluated_rows": len(evaluated),
            "missing_results": len(rows) - len(evaluated),
            "duplicate_eval_results": duplicate_results,
            "evaluation_coverage": _ratio(len(evaluated), len(rows)),
        },
        "quality": _quality_metrics(evaluated),
        "golden_eligible_quality": _quality_metrics(golden),
        "context_limited_quality": _quality_metrics(context_limited),
        "temporal_quality": _quality_metrics(temporal),
        "reference_fact_quality": {
            "cases": len(fact_scored),
            "average_coverage": _average(
                [row["reference_fact_coverage"] for row in fact_scored]
            ),
            "coverage_at_least_60_percent": sum(
                row["reference_fact_coverage"] >= 0.6 for row in fact_scored
            ),
            "coverage_at_least_60_percent_rate": _ratio(
                sum(row["reference_fact_coverage"] >= 0.6 for row in fact_scored),
                len(fact_scored),
            ),
        },
        "latency_ms": _number_summary(
            [int(row["latency_ms"]) for row in evaluated if row.get("latency_ms") is not None]
        ),
        "cost": {
            "llm_total_tokens": sum(row["llm_total_tokens"] for row in evaluated),
            "llm_estimated_cost_rub": round(
                sum(row["llm_estimated_cost_rub"] for row in evaluated),
                6,
            ),
        },
        "behavior_matrix": _behavior_matrix(evaluated),
        "category_metrics": _group_metrics(evaluated, "category"),
        "department_metrics": _group_metrics(evaluated, "department"),
        "context_limited_by_department": _group_metrics(
            context_limited,
            "department",
        ),
        "top_escalation_reasons": _counter(
            row.get("escalation_reason") or "none" for row in evaluated if row["was_escalated"]
        ),
        "reliable_gap_clusters": _gap_clusters(reliable_gaps),
        "reliable_gap_examples": [
            {
                "id": row["id"],
                "query": row["query"],
                "category": row["category"],
                "topic": row["topic"],
                "forum_normalized": row["forum_normalized"],
                "observed_behavior": row["observed_behavior"],
                "escalation_reason": row["escalation_reason"],
            }
            for row in reliable_gaps[:80]
        ],
        "cases": rows,
    }


def _quality_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    direct = sum(row["direct_answer"] for row in rows)
    contained = sum(row["contained_without_operator"] for row in rows)
    escalated = sum(row["was_escalated"] for row in rows)
    grounded = sum(row["source_grounded"] for row in rows)
    strict = sum(row["strict_grounded_answer"] for row in rows)
    return {
        "cases": total,
        "direct_answers": direct,
        "direct_conversion": _ratio(direct, total),
        "contained_without_operator": contained,
        "containment_rate": _ratio(contained, total),
        "escalated": escalated,
        "escalation_rate": _ratio(escalated, total),
        "source_grounded_direct_answers": grounded,
        "source_grounded_direct_answer_rate": _ratio(grounded, total),
        "strict_grounded_answers": strict,
        "strict_grounded_answer_rate": _ratio(strict, total),
    }


def _behavior_matrix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(
        (row["expected_behavior"], row["observed_behavior"])
        for row in rows
    )
    return [
        {"expected": expected, "observed": observed, "cases": count}
        for (expected, observed), count in counts.most_common()
    ]


def _group_metrics(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    return [
        {field: name, **_quality_metrics(items)}
        for name, items in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    ]


def _gap_clusters(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(
        (
            str(row.get("category") or "unknown"),
            str(row.get("topic") or "unknown"),
            str(row.get("forum_normalized") or "unknown"),
            str(row.get("escalation_reason") or row.get("observed_behavior") or "unknown"),
        )
        for row in rows
    )
    return [
        {
            "category": category,
            "topic": topic,
            "forum_normalized": forum,
            "reason": reason,
            "cases": count,
        }
        for (category, topic, forum, reason), count in counts.most_common(40)
    ]


def build_markdown(report: dict[str, Any]) -> str:
    coverage = report["coverage"]
    quality = report["quality"]
    golden = report["golden_eligible_quality"]
    context = report["context_limited_quality"]
    latency = report["latency_ms"]
    lines = [
        "# Полный аудит июньских обращений операторов",
        "",
        "## Покрытие",
        "",
        f"- Исходных пар вопрос/ответ: **{coverage['source_rows_total']}**.",
        f"- Запускаемых вопросов: **{coverage['runnable_rows']}**.",
        f"- Пустых вопросов исключено: **{coverage['empty_queries_excluded']}**.",
        f"- Получено результатов: **{coverage['evaluated_rows']}** "
        f"({coverage['evaluation_coverage']:.1%}).",
        "",
        "## Основные метрики",
        "",
        f"- Прямые автоответы: **{quality['direct_answers']} / {quality['cases']} "
        f"({quality['direct_conversion']:.1%})**.",
        f"- Удержание без оператора, включая корректные уточнения: "
        f"**{quality['contained_without_operator']} / {quality['cases']} "
        f"({quality['containment_rate']:.1%})**.",
        f"- Передачи оператору: **{quality['escalated']} / {quality['cases']} "
        f"({quality['escalation_rate']:.1%})**.",
        f"- Прямые ответы с источником: **{quality['source_grounded_direct_answers']} "
        f"({quality['source_grounded_direct_answer_rate']:.1%})**.",
        "",
        "## Проверяемое golden-подмножество",
        "",
        f"- Кейсов: **{golden['cases']}**.",
        f"- Прямые автоответы: **{golden['direct_answers']} "
        f"({golden['direct_conversion']:.1%})**.",
        f"- Прямые ответы с источником: **{golden['source_grounded_direct_answers']} "
        f"({golden['source_grounded_direct_answer_rate']:.1%})**.",
        "",
        "## Ограничения данных",
        "",
        f"- Контекстно неполных строк: **{context['cases']}**. На них корректное "
        "уточнение не следует считать ошибкой RAG.",
        "- Ответ оператора не является автоматически golden truth: операторы используют "
        "Yonote, ФГАИС, соцсети, вторую линию и внутренний чат.",
        "- Временные сведения требуют отдельной проверки актуальности.",
        "",
        "## Производительность и стоимость",
        "",
        f"- p50: **{latency.get('p50', 0)} мс**, p95: **{latency.get('p95', 0)} мс**, "
        f"максимум: **{latency.get('max', 0)} мс**.",
        f"- Оценочная стоимость LLM: **{report['cost']['llm_estimated_cost_rub']:.2f} RUB**.",
        "",
        "## Надёжные кластеры пробелов",
        "",
        "| Категория | Тема | Форум | Причина | Кейсов |",
        "|---|---|---|---|---:|",
    ]
    for item in report["reliable_gap_clusters"][:25]:
        lines.append(
            f"| {item['category']} | {item['topic']} | {item['forum_normalized']} | "
            f"{item['reason']} | {item['cases']} |"
        )
    return "\n".join(lines) + "\n"


def _number_summary(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"min": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "max": ordered[-1],
    }


def _percentile(values: list[int], percentile: float) -> int:
    index = min(len(values) - 1, max(0, round((len(values) - 1) * percentile)))
    return values[index]


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _counter(values: Any) -> list[dict[str, Any]]:
    return [
        {"value": value, "cases": count}
        for value, count in Counter(values).most_common(30)
    ]


def _list_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{path} must contain a JSON array of objects")
    return [dict(item) for item in value]


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return dict(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an honest full-June operator Q/A quality audit."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--eval", type=Path, action="append", required=True)
    parser.add_argument("--empty-cases", type=Path, default=DEFAULT_EMPTY_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build_full_operator_audit(
        args.cases,
        args.eval,
        args.output,
        empty_cases_path=args.empty_cases,
    )


if __name__ == "__main__":
    main()
