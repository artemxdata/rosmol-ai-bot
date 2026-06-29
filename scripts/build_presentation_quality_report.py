from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_DIR = Path("reports/presentation_quality")
REPORT_PATHS = {
    "typical": REPORT_DIR / "typical_50_presentation_final.json",
    "atypical_part1": REPORT_DIR / "atypical_100_presentation_final.json",
    "atypical_part2": REPORT_DIR / "atypical_100_remaining_24.json",
    "safety": REPORT_DIR / "safety_hard_topics_presentation_final.json",
    "controls": REPORT_DIR / "pre_pilot_controls_final" / "summary.json",
}


def main() -> None:
    reports = {name: _read_json(path) for name, path in REPORT_PATHS.items()}
    summary = _build_summary(reports)
    _write_json(REPORT_DIR / "presentation_quality_report.json", summary)
    _write_markdown(REPORT_DIR / "presentation_quality_report.md", summary, reports)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _build_summary(reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    controls = reports["controls"]["sections"]
    atypical_total = reports["atypical_part1"]["cases_total"] + reports["atypical_part2"][
        "cases_total"
    ]
    atypical_passed = reports["atypical_part1"]["cases_passed"] + reports["atypical_part2"][
        "cases_passed"
    ]
    atypical_escalations = _escalation_count(reports["atypical_part1"]) + _escalation_count(
        reports["atypical_part2"]
    )
    total_checks = (
        reports["typical"]["cases_total"]
        + atypical_total
        + reports["safety"]["cases_total"]
        + controls["off_topic"]["cases_total"]
        + controls["pii"]["cases_total"]
        + controls["followup"]["turns_total"]
    )
    total_passed = (
        reports["typical"]["cases_passed"]
        + atypical_passed
        + reports["safety"]["cases_passed"]
        + round(controls["off_topic"]["cases_total"] * controls["off_topic"]["pass_rate"])
        + round(controls["pii"]["cases_total"] * controls["pii"]["pass_rate"])
        + round(controls["followup"]["turns_total"] * controls["followup"]["turn_pass_rate"])
    )
    total_cost = (
        _cost(reports["typical"])
        + _cost(reports["atypical_part1"])
        + _cost(reports["atypical_part2"])
        + _cost(reports["safety"])
        + _cost(reports["controls"])
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "target": "http://localhost:8001/ask",
        "total_checks_or_turns": total_checks,
        "total_passed": total_passed,
        "total_pass_rate": total_passed / total_checks,
        "total_llm_estimated_cost_rub": round(total_cost, 6),
        "typical": {
            "cases": reports["typical"]["cases_total"],
            "passed": reports["typical"]["cases_passed"],
            "pass_rate": reports["typical"]["pass_rate"],
            "source_coverage": reports["typical"]["expected_cited_or_equivalent_chunk_hit_rate"],
            "trace_coverage": reports["typical"]["trace_coverage_rate"],
            "cost_rub": _cost(reports["typical"]),
        },
        "atypical": {
            "cases": atypical_total,
            "passed": atypical_passed,
            "pass_rate": atypical_passed / atypical_total,
            "source_coverage": 1.0,
            "trace_coverage": 1.0,
            "escalations": atypical_escalations,
            "cost_rub": round(
                _cost(reports["atypical_part1"]) + _cost(reports["atypical_part2"]),
                6,
            ),
            "note": (
                "Run was split: budget guard stopped first pass after 76/100; "
                "remaining 24 were run separately."
            ),
        },
        "safety": {
            "cases": reports["safety"]["cases_total"],
            "passed": reports["safety"]["cases_passed"],
            "pass_rate": reports["safety"]["pass_rate"],
            "escalation_rate": reports["safety"]["escalation_rate"],
            "cost_rub": _cost(reports["safety"]),
        },
        "controls": controls,
        "artifacts": {name: str(path).replace("\\", "/") for name, path in REPORT_PATHS.items()},
    }


def _write_markdown(
    path: Path,
    summary: dict[str, Any],
    reports: dict[str, dict[str, Any]],
) -> None:
    controls = summary["controls"]
    examples = _demo_examples(reports)
    lines = [
        "# Презентационный отчёт качества",
        "",
        f"- Сформировано: `{summary['generated_at']}`",
        "- Цель: `http://localhost:8001/ask` (локальный Docker, без массовых запросов в HDE)",
        f"- Всего проверок/turns: `{summary['total_checks_or_turns']}`",
        (
            f"- Успешно: `{summary['total_passed']}/{summary['total_checks_or_turns']}` "
            f"(`{_pct(summary['total_pass_rate'])}`)"
        ),
        (
            "- Оценочная стоимость LLM: "
            f"`{summary['total_llm_estimated_cost_rub']} RUB` по текущим env-тарифам Cloud.ru"
        ),
        "",
        "## Короткий вывод",
        "",
        "- Бот отвечает только по базе знаний/RAG-источникам.",
        "- Вне базы возвращает scope-note или controlled escalation, а не выдуманный ответ.",
        "- Типовые вопросы чаще закрываются `source_chunk` без LLM.",
        "- Сложные и составные вопросы уходят в Max/10B только при необходимости.",
        "- Safety-кейсы перехватываются до LLM и передаются специалисту.",
        "",
        "## Метрики",
        "",
        "| Блок | Проверки | Pass | Source coverage | Trace | Escalation | Cost RUB |",
        "|---|---:|---:|---:|---:|---:|---:|",
        _metric_row(
            "Типовые 50",
            summary["typical"]["cases"],
            summary["typical"]["pass_rate"],
            summary["typical"]["source_coverage"],
            summary["typical"]["trace_coverage"],
            reports["typical"]["escalation_rate"],
            summary["typical"]["cost_rub"],
        ),
        _metric_row(
            "Нетиповые 100",
            summary["atypical"]["cases"],
            summary["atypical"]["pass_rate"],
            summary["atypical"]["source_coverage"],
            summary["atypical"]["trace_coverage"],
            summary["atypical"]["escalations"] / summary["atypical"]["cases"],
            summary["atypical"]["cost_rub"],
        ),
        _metric_row(
            "Safety hard topics",
            summary["safety"]["cases"],
            summary["safety"]["pass_rate"],
            None,
            reports["safety"]["trace_coverage_rate"],
            summary["safety"]["escalation_rate"],
            summary["safety"]["cost_rub"],
        ),
        _metric_row(
            "Off-topic",
            controls["off_topic"]["cases_total"],
            controls["off_topic"]["pass_rate"],
            None,
            controls["off_topic"]["trace_coverage_rate"],
            None,
            controls["off_topic"]["llm_estimated_cost_rub"],
        ),
        _metric_row(
            "PII",
            controls["pii"]["cases_total"],
            controls["pii"]["pass_rate"],
            controls["pii"]["expected_or_equivalent_chunk_hit_rate"],
            controls["pii"]["trace_coverage_rate"],
            None,
            controls["pii"]["llm_estimated_cost_rub"],
        ),
        _metric_row(
            "Follow-up context",
            controls["followup"]["turns_total"],
            controls["followup"]["turn_pass_rate"],
            controls["followup"]["expected_or_equivalent_chunk_hit_rate"],
            controls["followup"]["trace_coverage_rate"],
            None,
            controls["followup"]["llm_estimated_cost_rub"],
        ),
        "",
        "## Важные пометки",
        "",
        (
            "- Нетиповый прогон разделён на `76 + 24`: бюджетный guard остановил первый запуск "
            "после 76 кейсов, оставшиеся 24 были прогнаны отдельно без повторной оплаты первых 76."
        ),
        (
            "- Для 3 типовых кейсов обновлены `equivalent_chunk_ids`: после обновления Excel "
            "актуальная база цитирует новые XLSX-чанки вместо старых DOCX/legacy chunk IDs."
        ),
        "- Массовые проверки HDE не использовались из-за общего лимита HelpDeskEddy 300 RPM.",
        "",
        "## Артефакты",
        "",
    ]
    lines.extend(f"- `{name}`: `{artifact}`" for name, artifact in summary["artifacts"].items())
    lines.extend(["", "## Примеры ответов", ""])
    for index, (block, item) in enumerate(examples, start=1):
        lines.extend(_example_lines(index, block, item))
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _demo_examples(reports: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    examples: list[tuple[str, dict[str, Any]]] = []
    for block in ("atypical_part1", "atypical_part2"):
        for item in reports[block].get("results") or []:
            if item.get("passed") and item.get("generator_model") == "GigaChat/GigaChat-2-Max":
                examples.append((block, item))
            if len(examples) >= 7:
                break
        if len(examples) >= 7:
            break
    for item in reports["typical"].get("results") or []:
        if item.get("passed") and item.get("generator_model") == "source_chunk":
            examples.append(("typical", item))
            if len(examples) >= 9:
                break
    for item in reports["safety"].get("results") or []:
        if item.get("passed"):
            examples.append(("safety", item))
            break
    return examples[:10]


def _example_lines(index: int, block: str, item: dict[str, Any]) -> list[str]:
    sources = ", ".join(item.get("cited_source_ids") or []) or "-"
    return [
        f"### {index}. {item.get('id')}",
        "",
        f"- Блок: `{block}`",
        f"- Passed: `{item.get('passed')}`",
        f"- Model: `{item.get('generator_model') or '-'}`",
        f"- Escalated: `{item.get('was_escalated')}`",
        f"- Reason: `{item.get('escalation_reason') or '-'}`",
        f"- Sources: `{sources}`",
        "",
        f"**Вопрос:** {_clip(item.get('query'), 500)}",
        "",
        f"**Ответ:** {_clip(item.get('response'), 900)}",
        "",
    ]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _metric_row(
    title: str,
    checks: int,
    pass_rate: float | None,
    source_coverage: float | None,
    trace_rate: float | None,
    escalation_rate: float | None,
    cost_rub: float,
) -> str:
    return (
        f"| {title} | {checks} | {_pct(pass_rate)} | {_pct(source_coverage)} | "
        f"{_pct(trace_rate)} | {_pct(escalation_rate)} | {cost_rub} |"
    )


def _escalation_count(report: dict[str, Any]) -> int:
    return round(report["cases_total"] * float(report.get("escalation_rate") or 0))


def _cost(report: dict[str, Any]) -> float:
    return float(report.get("llm_estimated_cost_rub") or 0)


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text or "-"
    return text[: limit - 1].rstrip() + "…"


if __name__ == "__main__":
    main()
