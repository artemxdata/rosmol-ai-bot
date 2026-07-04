from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize private ticket conversion eval.")
    parser.add_argument(
        "--input",
        default="data/private/tickets/eval_2026_full/conversion_eval_2026.json",
    )
    parser.add_argument(
        "--markdown",
        default="data/private/tickets/eval_2026_full/conversion_eval_2026_summary.md",
    )
    args = parser.parse_args()

    metrics = json.loads(Path(args.input).read_text(encoding="utf-8"))
    results = metrics.get("results") or []
    summary = build_summary(results)
    markdown = render_markdown(summary, metrics)

    markdown_path = Path(args.markdown)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown, encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"markdown={markdown_path}")


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        groups[_type_from_tags(item.get("tags") or [])].append(item)

    return {
        "overall": summarize_group(results),
        "groups": {name: summarize_group(items) for name, items in sorted(groups.items())},
    }


def summarize_group(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(items)
    if total == 0:
        return {
            "cases": 0,
            "http_success_rate": None,
            "controlled_success_rate": None,
            "auto_answer_rate": None,
            "non_operator_rate": None,
            "controlled_escalation_rate": None,
            "latency_ms_avg": None,
            "latency_ms_p95": None,
            "llm_cost_rub": 0.0,
        }

    http_success = [item for item in items if item.get("http_success") is True]
    passed = [item for item in items if item.get("passed") is True]
    auto_answer = [
        item
        for item in items
        if item.get("observed_behavior") == "answer" and item.get("was_escalated") is not True
    ]
    non_operator = [item for item in items if item.get("was_escalated") is not True]
    controlled_escalation = [
        item for item in items if item.get("observed_behavior") == "escalate"
    ]
    latencies = [
        int(item["trace_total_latency_ms"])
        for item in items
        if isinstance(item.get("trace_total_latency_ms"), int)
    ]
    costs = [_float_or_zero(item.get("llm_estimated_cost_rub")) for item in items]

    return {
        "cases": total,
        "http_success_rate": rate(len(http_success), total),
        "controlled_success_rate": rate(len(passed), total),
        "auto_answer_rate": rate(len(auto_answer), total),
        "non_operator_rate": rate(len(non_operator), total),
        "controlled_escalation_rate": rate(len(controlled_escalation), total),
        "latency_ms_avg": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "latency_ms_p95": percentile(latencies, 95),
        "llm_cost_rub": round(sum(costs), 6),
        "observed_behavior_counts": dict(
            Counter(str(item.get("observed_behavior") or "unknown") for item in items)
        ),
        "escalation_reason_counts": dict(
            Counter(str(item.get("escalation_reason") or "none") for item in items)
        ),
        "generator_model_counts": dict(
            Counter(str(item.get("generator_model") or "none") for item in items)
        ),
    }


def render_markdown(summary: dict[str, Any], metrics: dict[str, Any]) -> str:
    lines = [
        "# 2026 Ticket Conversion Eval",
        "",
        "Приватный отчёт по деперсонализированным тикетам 2026 года.",
        "Сырые тикеты и тексты кейсов не предназначены для Git.",
        "",
        f"- Cases: `{summary['overall']['cases']}`",
        f"- Target: `{metrics.get('target')}`",
        f"- LLM cost: `{summary['overall']['llm_cost_rub']}` RUB",
        "",
        (
            "| Group | Cases | HTTP | Controlled success | Auto answer | No operator | "
            "Escalation | Avg latency | P95 latency |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for group_name, group in [("overall", summary["overall"])] + list(
        summary["groups"].items()
    ):
        lines.append(
            "| "
            + " | ".join(
                [
                    group_name,
                    str(group["cases"]),
                    fmt_rate(group["http_success_rate"]),
                    fmt_rate(group["controlled_success_rate"]),
                    fmt_rate(group["auto_answer_rate"]),
                    fmt_rate(group["non_operator_rate"]),
                    fmt_rate(group["controlled_escalation_rate"]),
                    str(group["latency_ms_avg"]),
                    str(group["latency_ms_p95"]),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Details", ""])
    for group_name, group in [("overall", summary["overall"])] + list(
        summary["groups"].items()
    ):
        lines.extend(
            [
                f"### {group_name}",
                "",
                f"- observed_behavior_counts: `{group.get('observed_behavior_counts')}`",
                f"- escalation_reason_counts: `{group.get('escalation_reason_counts')}`",
                f"- generator_model_counts: `{group.get('generator_model_counts')}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _type_from_tags(tags: list[str]) -> str:
    for tag in tags:
        if tag.startswith("type:"):
            return tag.split(":", 1)[1] or "unknown"
    return "unknown"


def rate(value: int, total: int) -> float:
    return round(value / total, 6) if total else 0.0


def percentile(values: list[int], percentile_value: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile_value / 100)))
    return ordered[index]


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def fmt_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


if __name__ == "__main__":
    main()
