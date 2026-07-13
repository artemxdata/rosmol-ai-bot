from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT = Path("reports/june_2026_operator_quality_comparison.json")


def compare_audits(
    baseline_path: Path,
    post_fix_path: Path,
    output_path: Path = DEFAULT_OUTPUT,
    context_scenario_path: Path | None = None,
    dialog_scenario_paths: list[Path] | None = None,
) -> dict[str, Any]:
    baseline = _read_json_object(baseline_path)
    post_fix = _read_json_object(post_fix_path)
    _validate_coverage(baseline, post_fix)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "baseline_path": str(baseline_path),
        "post_fix_path": str(post_fix_path),
        "coverage": post_fix["coverage"],
        "overall": _quality_comparison(baseline["quality"], post_fix["quality"]),
        "golden_eligible": _quality_comparison(
            baseline["golden_eligible_quality"],
            post_fix["golden_eligible_quality"],
        ),
        "context_limited": _quality_comparison(
            baseline["context_limited_quality"],
            post_fix["context_limited_quality"],
        ),
        "category_comparison": _category_comparison(baseline, post_fix),
        "latency_ms": {
            "baseline": baseline["latency_ms"],
            "post_fix": post_fix["latency_ms"],
            "p50_delta": _delta(
                baseline["latency_ms"].get("p50"),
                post_fix["latency_ms"].get("p50"),
            ),
            "p95_delta": _delta(
                baseline["latency_ms"].get("p95"),
                post_fix["latency_ms"].get("p95"),
            ),
        },
        "cost_rub": {
            "baseline": baseline["cost"]["llm_estimated_cost_rub"],
            "post_fix": post_fix["cost"]["llm_estimated_cost_rub"],
        },
        "methodology_notes": [
            "Конверсия — прямой содержательный первый ответ без оператора.",
            "Уточнение считается удержанием без оператора, но не считается закрытым тикетом.",
            "Golden-eligible — автоматически очищенное подмножество, а не ручная "
            "экспертная разметка всех строк.",
            "Ответ оператора не считается автоматически истинным: часть операторских "
            "данных получена из ФГАИС, соцсетей, второй линии и внутреннего чата.",
            "Пустые вопросы и attachment-only строки не запускаются как текстовый RAG-тест.",
        ],
    }
    if context_scenario_path is not None:
        report["explicit_channel_context_scenario"] = _scenario_summary(
            context_scenario_path
        )
    if dialog_scenario_paths:
        report["multi_turn_scenarios"] = [
            _dialog_scenario_summary(path) for path in dialog_scenario_paths
        ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output_path.with_suffix(".md").write_text(
        build_markdown(report),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def _scenario_summary(path: Path) -> dict[str, Any]:
    source = _read_json_object(path)
    cases = int(source.get("cases_total") or 0)
    behavior_counts = dict(source.get("observed_behavior_counts") or {})
    direct_answers = int(behavior_counts.get("answer") or 0)
    escalated = int(behavior_counts.get("escalate") or 0)
    return {
        "label": "MAX / День молодёжи с явным контекстом кампании из HDE",
        "status": "сценарный потенциал, не текущая общая конверсия",
        "cases": cases,
        "direct_answers": direct_answers,
        "direct_conversion": round(direct_answers / cases, 6) if cases else 0.0,
        "escalated": escalated,
        "escalation_rate": round(escalated / cases, 6) if cases else 0.0,
        "http_success_rate": float(source.get("http_success_rate") or 0.0),
        "trace_coverage_rate": float(source.get("trace_coverage_rate") or 0.0),
        "latency_ms": dict(source.get("latency_ms") or {}),
        "llm_estimated_cost_rub": float(
            source.get("llm_estimated_cost_rub") or 0.0
        ),
    }


def _dialog_scenario_summary(path: Path) -> dict[str, Any]:
    source = _read_json_object(path)
    return {
        "label": path.stem,
        "path": str(path),
        "conversations_total": int(source.get("conversations_total") or 0),
        "conversations_executed": int(source.get("conversations_executed") or 0),
        "conversation_pass_rate": float(source.get("conversation_pass_rate") or 0.0),
        "turns_total": int(source.get("turns_total") or 0),
        "turn_pass_rate": float(source.get("turn_pass_rate") or 0.0),
        "http_success_rate": float(source.get("http_success_rate") or 0.0),
        "trace_coverage_rate": float(source.get("trace_coverage_rate") or 0.0),
        "llm_estimated_cost_rub": float(
            source.get("llm_estimated_cost_rub") or 0.0
        ),
    }


def _quality_comparison(
    baseline: dict[str, Any],
    post_fix: dict[str, Any],
) -> dict[str, Any]:
    keys = (
        "direct_conversion",
        "containment_rate",
        "escalation_rate",
        "source_grounded_direct_answer_rate",
        "strict_grounded_answer_rate",
    )
    return {
        "cases": post_fix["cases"],
        "baseline": baseline,
        "post_fix": post_fix,
        "delta": {
            key: round(float(post_fix[key]) - float(baseline[key]), 6)
            for key in keys
        },
    }


def _category_comparison(
    baseline: dict[str, Any],
    post_fix: dict[str, Any],
) -> list[dict[str, Any]]:
    baseline_by_category = {
        str(item["category"]): item for item in baseline["category_metrics"]
    }
    rows: list[dict[str, Any]] = []
    for item in post_fix["category_metrics"]:
        category = str(item["category"])
        before = baseline_by_category.get(category, {})
        rows.append(
            {
                "category": category,
                "cases": item["cases"],
                "direct_conversion_baseline": before.get("direct_conversion", 0.0),
                "direct_conversion_post_fix": item["direct_conversion"],
                "direct_conversion_delta": round(
                    float(item["direct_conversion"])
                    - float(before.get("direct_conversion", 0.0)),
                    6,
                ),
                "containment_rate_baseline": before.get("containment_rate", 0.0),
                "containment_rate_post_fix": item["containment_rate"],
                "containment_rate_delta": round(
                    float(item["containment_rate"])
                    - float(before.get("containment_rate", 0.0)),
                    6,
                ),
            }
        )
    return rows


def _validate_coverage(baseline: dict[str, Any], post_fix: dict[str, Any]) -> None:
    for field in ("source_rows_total", "runnable_rows", "empty_queries_excluded"):
        before = baseline.get("coverage", {}).get(field)
        after = post_fix.get("coverage", {}).get(field)
        if before != after:
            raise ValueError(f"Audit coverage mismatch for {field}: {before} != {after}")
    if post_fix.get("coverage", {}).get("missing_results"):
        raise ValueError("Post-fix audit is incomplete")


def build_markdown(report: dict[str, Any]) -> str:
    coverage = report["coverage"]
    overall = report["overall"]
    golden = report["golden_eligible"]
    context_limited = report["context_limited"]
    lines = [
        "# Качество бота на реальных обращениях за июнь 2026",
        "",
        "## Данные",
        "",
        f"- Всего пар вопрос/ответ: **{coverage['source_rows_total']}**.",
        f"- Запущено текстовых кейсов: **{coverage['runnable_rows']}**.",
        f"- Пустых/нетекстовых строк учтено отдельно: **{coverage['empty_queries_excluded']}**.",
        "- Массовый прогон выполнен локально через `/ask`; HDE не использовался.",
        "",
        "## До и после",
        "",
        "| Метрика | Baseline | После исправлений | Дельта |",
        "|---|---:|---:|---:|",
        _metric_row(overall, "direct_conversion", "Прямое закрытие без оператора"),
        _metric_row(overall, "containment_rate", "Удержание без оператора"),
        _metric_row(overall, "escalation_rate", "Передача оператору"),
        _metric_row(
            overall,
            "source_grounded_direct_answer_rate",
            "Прямой ответ с источником",
        ),
        "",
        "## Golden-eligible подмножество",
        "",
        "| Метрика | Baseline | После исправлений | Дельта |",
        "|---|---:|---:|---:|",
        _metric_row(golden, "direct_conversion", "Прямое закрытие"),
        _metric_row(golden, "containment_rate", "Удержание без оператора"),
        _metric_row(
            golden,
            "source_grounded_direct_answer_rate",
            "Прямой ответ с источником",
        ),
        "",
        "## Обращения с недостаточным исходным контекстом",
        "",
        f"- Кейсов: **{context_limited['cases']}**.",
        "- Это короткие или зависимые от предыдущей переписки реплики. Их нельзя "
        "надёжно закрыть первым ответом без названия события или иной сущности.",
        "- Для них отдельно измеряется многошаговое разрешение после уточнения.",
        "",
        "## Категории",
        "",
        "| Категория | Кейсов | Baseline | После | Дельта |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in report["category_comparison"]:
        lines.append(
            f"| {row['category']} | {row['cases']} | "
            f"{row['direct_conversion_baseline']:.1%} | "
            f"{row['direct_conversion_post_fix']:.1%} | "
            f"{row['direct_conversion_delta']:+.1%} |"
        )
    latency = report["latency_ms"]
    scenario = report.get("explicit_channel_context_scenario")
    dialog_scenarios = report.get("multi_turn_scenarios") or []
    lines.extend(
        [
            "",
            "## Производительность",
            "",
            f"- p50: {latency['baseline'].get('p50')} → {latency['post_fix'].get('p50')} мс.",
            f"- p95: {latency['baseline'].get('p95')} → {latency['post_fix'].get('p95')} мс.",
            "",
            "## Ограничения",
            "",
            *[f"- {note}" for note in report["methodology_notes"]],
        ]
    )
    if scenario:
        lines.extend(
            [
                "",
                "## Сценарий с контекстом канала HDE",
                "",
                f"- Набор: **{scenario['label']}**.",
                f"- Кейсов: **{scenario['cases']}**.",
                f"- Прямых ответов без оператора: **{scenario['direct_answers']} "
                f"({scenario['direct_conversion']:.1%})**.",
                f"- Передано оператору: **{scenario['escalated']} "
                f"({scenario['escalation_rate']:.1%})**.",
                f"- p50: **{scenario['latency_ms'].get('p50')} мс**; "
                f"p95: **{scenario['latency_ms'].get('p95')} мс**.",
                "- Это сценарный потенциал после настройки выделенного HDE-правила, "
                "а не текущая общая конверсия всех каналов.",
            ]
        )
    if dialog_scenarios:
        lines.extend(
            [
                "",
                "## Многошаговые диалоги",
                "",
                "| Набор | Диалогов | Успешные диалоги | Реплик | Успешные реплики |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for item in dialog_scenarios:
            lines.append(
                f"| {item['label']} | {item['conversations_total']} | "
                f"{item['conversation_pass_rate']:.1%} | {item['turns_total']} | "
                f"{item['turn_pass_rate']:.1%} |"
            )
        lines.extend(
            [
                "",
                "- Эти показатели оценивают решение после уточнений и не заменяют "
                "метрику прямого закрытия первым ответом.",
            ]
        )
    return "\n".join(lines) + "\n"


def _metric_row(comparison: dict[str, Any], key: str, label: str) -> str:
    return (
        f"| {label} | {comparison['baseline'][key]:.1%} | "
        f"{comparison['post_fix'][key]:.1%} | {comparison['delta'][key]:+.1%} |"
    )


def _delta(before: Any, after: Any) -> int | None:
    if before is None or after is None:
        return None
    return int(after) - int(before)


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return dict(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare complete baseline and post-fix operator quality audits."
    )
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--post-fix", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--context-scenario", type=Path)
    parser.add_argument("--dialog-scenario", type=Path, action="append")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    compare_audits(
        args.baseline,
        args.post_fix,
        args.output,
        context_scenario_path=args.context_scenario,
        dialog_scenario_paths=args.dialog_scenario,
    )


if __name__ == "__main__":
    main()
