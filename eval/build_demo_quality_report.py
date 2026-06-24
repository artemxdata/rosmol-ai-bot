from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_demo_quality_report(
    metrics_path: Path,
    output_path: Path,
    *,
    max_examples: int = 10,
) -> dict[str, Any]:
    metrics = _read_metrics(metrics_path)
    examples = _select_examples(metrics.get("results") or [], max_examples=max_examples)
    report = {
        "metrics_path": str(metrics_path),
        "output_path": str(output_path),
        "cases_total": metrics.get("cases_total"),
        "pass_rate": metrics.get("pass_rate"),
        "http_success_rate": metrics.get("http_success_rate"),
        "source_coverage_rate": _source_coverage_rate(metrics),
        "escalation_rate": metrics.get("escalation_rate"),
        "cache_hit_rate": metrics.get("cache_hit_rate"),
        "llm_estimated_cost_rub": metrics.get("llm_estimated_cost_rub"),
        "llm_budget_rub": metrics.get("llm_budget_rub"),
        "generator_model_counts": metrics.get("generator_model_counts") or {},
        "escalation_reason_counts": metrics.get("escalation_reason_counts") or {},
        "failure_reason_counts": metrics.get("failure_reason_counts") or {},
        "examples": examples,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_markdown(report), encoding="utf-8")
    return report


def _read_metrics(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"ask eval metrics must contain a JSON object: {path}")
    return payload


def _source_coverage_rate(metrics: dict[str, Any]) -> Any:
    return (
        metrics.get("expected_cited_or_equivalent_chunk_hit_rate")
        or metrics.get("expected_cited_chunk_hit_rate")
        or metrics.get("expected_or_equivalent_chunk_hit_rate")
        or metrics.get("expected_chunk_hit_rate")
    )


def _select_examples(results: list[dict[str, Any]], *, max_examples: int) -> list[dict[str, Any]]:
    if max_examples < 1:
        raise ValueError("max_examples must be greater than zero")
    ranked = sorted(results, key=_example_rank)
    return [_compact_example(item) for item in ranked[:max_examples]]


def _example_rank(item: dict[str, Any]) -> tuple[int, int, str]:
    return (
        0 if _is_demo_worthy(item) else 1,
        0 if item.get("passed") is True else 1,
        str(item.get("id") or ""),
    )


def _is_demo_worthy(item: dict[str, Any]) -> bool:
    tags = {str(tag).casefold() for tag in item.get("tags") or []}
    model = str(item.get("generator_model") or "")
    query = str(item.get("query") or "")
    return (
        "complex" in tags
        or "manual_complex" in tags
        or model not in {"", "source_chunk", "source_only", "unknown"}
        or len(query) >= 90
        or item.get("was_escalated") is True
    )


def _compact_example(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "passed": item.get("passed"),
        "query": item.get("query"),
        "response": item.get("response"),
        "generator_model": item.get("generator_model"),
        "was_escalated": item.get("was_escalated"),
        "escalation_reason": item.get("escalation_reason"),
        "cited_source_ids": item.get("cited_source_ids") or [],
        "cited_source_types": item.get("cited_source_types") or [],
        "failure_reasons": item.get("failure_reasons") or [],
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Demo Quality Report",
        "",
        f"- Ask eval: `{report['metrics_path']}`",
        f"- Cases: `{report.get('cases_total')}`",
        f"- Pass rate: `{_format_rate(report.get('pass_rate'))}`",
        f"- HTTP success rate: `{_format_rate(report.get('http_success_rate'))}`",
        f"- Source coverage rate: `{_format_rate(report.get('source_coverage_rate'))}`",
        f"- Escalation rate: `{_format_rate(report.get('escalation_rate'))}`",
        f"- Cache hit rate: `{_format_rate(report.get('cache_hit_rate'))}`",
        f"- LLM estimated cost, RUB: `{report.get('llm_estimated_cost_rub')}`",
        f"- LLM budget, RUB: `{report.get('llm_budget_rub')}`",
        "",
        "## Model Usage",
        "",
    ]
    lines.extend(_dict_lines(report.get("generator_model_counts") or {}, empty="- no models"))
    lines.extend(["", "## Escalations", ""])
    lines.extend(
        _dict_lines(report.get("escalation_reason_counts") or {}, empty="- no escalations")
    )
    lines.extend(["", "## Failure Reasons", ""])
    lines.extend(_dict_lines(report.get("failure_reason_counts") or {}, empty="- no failures"))
    lines.extend(["", "## Demo Examples", ""])
    for idx, item in enumerate(report.get("examples") or [], start=1):
        sources = ", ".join(item.get("cited_source_ids") or []) or "-"
        source_types = ", ".join(item.get("cited_source_types") or []) or "-"
        failures = ", ".join(item.get("failure_reasons") or []) or "-"
        lines.extend(
            [
                f"### {idx}. `{item.get('id')}`",
                "",
                f"- Passed: `{item.get('passed')}`",
                f"- Model: `{item.get('generator_model') or '-'}`",
                f"- Escalated: `{item.get('was_escalated')}`",
                f"- Escalation reason: `{item.get('escalation_reason') or '-'}`",
                f"- Sources: `{sources}`",
                f"- Source types: `{source_types}`",
                f"- Failures: `{failures}`",
                "",
                f"**Question:** {_clip(item.get('query'))}",
                "",
                f"**Answer:** {_clip(item.get('response'), limit=700)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _dict_lines(values: dict[str, Any], *, empty: str) -> list[str]:
    if not values:
        return [empty]
    return [f"- `{key}`: `{values[key]}`" for key in sorted(values)]


def _format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _clip(value: Any, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text or "-"
    return text[: limit - 1].rstrip() + "…"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/quality_suite/ask_eval.json")
    parser.add_argument("--output", default="reports/demo_quality.md")
    parser.add_argument("--max-examples", type=int, default=10)
    args = parser.parse_args()

    report = build_demo_quality_report(
        Path(args.metrics),
        Path(args.output),
        max_examples=args.max_examples,
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "examples"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
