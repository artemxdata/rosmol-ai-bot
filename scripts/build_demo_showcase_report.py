from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.graph.nodes.respond import normalize_final_response


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a readable demo-quality Markdown report.")
    parser.add_argument("--cases", default="reports/rag_dataset_demo_100_cases.json")
    parser.add_argument("--results", default="reports/rag_dataset_demo_100_results.json")
    parser.add_argument("--profile", default="reports/rag_dataset_demo_profile.json")
    parser.add_argument("--output", default="reports/rag_dataset_demo_100_showcase.md")
    args = parser.parse_args()

    cases = _read_json(Path(args.cases))
    results = _read_json(Path(args.results))
    profile = _read_json(Path(args.profile))
    case_by_id = {case["id"]: case for case in cases}

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        build_markdown(case_by_id=case_by_id, results=results, profile=profile),
        encoding="utf-8",
    )
    print(f"showcase={output}")


def build_markdown(
    *,
    case_by_id: dict[str, dict[str, Any]],
    results: dict[str, Any],
    profile: dict[str, Any],
) -> str:
    result_items = results.get("results") or []
    typical_items = [item for item in result_items if _group(item) == "typical"]
    atypical_items = [item for item in result_items if _group(item) == "atypical"]

    lines = [
        "# Демонстрация качества AI-бота Росмолодёжи",
        "",
        "## Что проверяли",
        "",
        (
            "Набор состоит из 100 обезличенных representative prompts: "
            "50 типовых и 50 нетиповых запросов. RAG_Dataset.xlsx использован "
            "для профиля реальных обращений и распределения типовой/нетиповой нагрузки; "
            "сырые тексты тикетов, ФИО, контакты и история сообщений в отчёт не экспортируются."
        ),
        "",
        "## Профиль источника",
        "",
        f"- Строк в приватном RAG_Dataset: `{profile.get('dataset_rows')}`",
        f"- Метки в RAG_Dataset: `{_json_inline(profile.get('dataset_label_counts'))}`",
        f"- Выбрано для демо: `{_json_inline(profile.get('selected_counts'))}`",
        f"- Темы в демо-наборе: `{_json_inline(profile.get('selected_topic_counts'))}`",
        "",
        "## Итог прогона",
        "",
        (
            f"- Pass rate: `{_rate(results.get('pass_rate'))}` "
            f"({results.get('cases_passed')}/{results.get('cases_total')})"
        ),
        f"- HTTP success: `{_rate(results.get('http_success_rate'))}`",
        f"- Trace coverage: `{_rate(results.get('trace_coverage_rate'))}`",
        f"- Expected chunk hit: `{_rate(results.get('expected_or_equivalent_chunk_hit_rate'))}`",
        (
            "- Expected cited source hit: "
            f"`{_rate(results.get('expected_cited_or_equivalent_chunk_hit_rate'))}`"
        ),
        f"- Escalation rate: `{_rate(results.get('escalation_rate'))}`",
        f"- Source chunk rate: `{_rate(results.get('source_chunk_rate'))}`",
        f"- LLM cost estimate: `{results.get('llm_estimated_cost_rub'):.4f} ₽`",
        f"- LLM total tokens: `{results.get('llm_total_tokens')}`",
        f"- Latency avg/p95/max: `{_latency(results.get('latency_ms'))}`",
        f"- Generator models: `{_json_inline(results.get('generator_model_counts'))}`",
        f"- Escalation reasons: `{_json_inline(results.get('escalation_reason_counts'))}`",
        "",
        "## По группам",
        "",
        "| Группа | Кейсов | Passed | Escalated | Avg latency |",
        "|---|---:|---:|---:|---:|",
        _group_row("Типовые", typical_items),
        _group_row("Нетиповые", atypical_items),
        "",
        "## 10 примеров ответов",
        "",
    ]

    examples = [*typical_items[:5], *atypical_items[:5]]
    for index, item in enumerate(examples, 1):
        case = case_by_id.get(item["id"], {})
        lines.extend(
            [
                (
                    f"### {index}. {_label(_group(item))}: "
                    f"{_md(case.get('query') or item.get('query'))}"
                ),
                "",
                f"- Поведение: `{item.get('observed_behavior')}`",
                f"- Модель/режим: `{item.get('generator_model')}`",
                f"- Источники: `{', '.join(item.get('cited_source_ids') or []) or 'нет'}`",
                f"- Ответ: {_md(_display_response(item))}",
                "",
            ]
        )

    lines.extend(
        [
            "## Все 100 запросов",
            "",
            "| # | Группа | Запрос | Поведение | Модель/режим | Ответ | Источники |",
            "|---:|---|---|---|---|---|---|",
        ]
    )
    for index, item in enumerate(result_items, 1):
        case = case_by_id.get(item["id"], {})
        row_template = (
            "| {index} | {group} | {query} | `{behavior}` | `{model}` | "
            "{response} | {sources} |"
        )
        lines.append(
            row_template.format(
                index=index,
                group=_label(_group(item)),
                query=_md(_clip(case.get("query") or item.get("query") or "", 120)),
                behavior=item.get("observed_behavior"),
                model=item.get("generator_model"),
                response=_md(_clip(_display_response(item), 180)),
                sources=_md(_clip(", ".join(item.get("cited_source_ids") or []), 160)),
            )
        )

    lines.extend(
        [
            "",
            "## Вывод",
            "",
            (
                "На этом наборе бот стабильно отвечает только из найденных источников: "
                "неподходящие или недостаточные источники переводятся в "
                "controlled escalation/clarification, "
                "а off-topic не отправляется в модель как свободная болталка."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _display_response(item: dict[str, Any]) -> str:
    return normalize_final_response(str(item.get("response") or ""))


def _group(item: dict[str, Any]) -> str:
    identity = f"{item.get('id') or ''} {' '.join(item.get('tags') or [])}"
    if "atypical" in identity:
        return "atypical"
    return "typical"


def _group_row(label: str, items: list[dict[str, Any]]) -> str:
    if not items:
        return f"| {label} | 0 | 0 | 0 | - |"
    passed = sum(1 for item in items if item.get("passed") is True)
    escalated = sum(1 for item in items if item.get("was_escalated") is True)
    avg_latency = sum(float(item.get("latency_ms") or 0) for item in items) / len(items)
    return f"| {label} | {len(items)} | {passed} | {escalated} | {avg_latency:.0f} ms |"


def _rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _latency(value: Any) -> str:
    if not isinstance(value, dict):
        return "n/a"
    return f"{value.get('avg')} / {value.get('p95')} / {value.get('max')} ms"


def _json_inline(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _md(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _label(group: str) -> str:
    return "нетиповой" if group == "atypical" else "типовой"


if __name__ == "__main__":
    main()
