from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))


def summarize_forum_ask(
    ask_metrics_path: Path,
    kb_seed_path: Path,
    output_path: Path,
    markdown_path: Path | None = None,
) -> dict[str, Any]:
    metrics = json.loads(ask_metrics_path.read_text(encoding="utf-8"))
    kb_records = json.loads(kb_seed_path.read_text(encoding="utf-8"))
    if not isinstance(metrics, dict):
        raise ValueError("ask metrics must be a JSON object")
    if not isinstance(kb_records, list):
        raise ValueError("KB seed must be a JSON array")

    chunk_forums = {
        str(record.get("chunk_id")): str(record.get("forum_normalized") or "")
        for record in kb_records
        if record.get("chunk_id")
    }
    rows = _forum_rows(metrics.get("results") or [], chunk_forums)
    summary = {
        "source": str(ask_metrics_path),
        "cases_total": int(metrics.get("cases_total") or 0),
        "forums_total": len(rows),
        "pass_rate": metrics.get("pass_rate"),
        "expected_chunk_hit_rate": metrics.get("expected_chunk_hit_rate"),
        "http_success_rate": metrics.get("http_success_rate"),
        "escalation_rate": metrics.get("escalation_rate"),
        "llm_estimated_cost_rub": metrics.get("llm_estimated_cost_rub"),
        "latency_ms": metrics.get("latency_ms") or {},
        "trace_total_latency_ms": metrics.get("trace_total_latency_ms") or {},
        "problem_forums": [
            row
            for row in rows
            if row["pass_rate"] < 1.0 or row["foreign_observed_forums"]
        ],
        "forums": rows,
    }
    _write_json(output_path, summary)
    if markdown_path:
        _write_markdown(markdown_path, summary)
    return summary


def _forum_rows(
    results: list[dict[str, Any]],
    chunk_forums: dict[str, str],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        grouped[_case_forum(item)].append(item)

    rows: list[dict[str, Any]] = []
    for forum in sorted(grouped):
        items = grouped[forum]
        foreign_forums: Counter[str] = Counter()
        for item in items:
            for chunk_id in item.get("observed_chunk_ids") or []:
                chunk_forum = chunk_forums.get(str(chunk_id), "")
                if chunk_forum and forum != "unknown" and chunk_forum != forum:
                    foreign_forums[chunk_forum] += 1

        rows.append(
            {
                "forum": forum,
                "cases": len(items),
                "passed": sum(1 for item in items if item.get("passed") is True),
                "pass_rate": _rate(items, "passed"),
                "expected_chunk_hit_rate": _rate(items, "expected_chunk_hit"),
                "http_success_rate": _rate(items, "http_success"),
                "escalated": sum(1 for item in items if item.get("was_escalated") is True),
                "avg_latency_ms": _average(_numbers(items, "latency_ms")),
                "avg_trace_latency_ms": _average(_numbers(items, "trace_total_latency_ms")),
                "avg_reranker_score": _average(_numbers(items, "max_reranker_score")),
                "foreign_observed_forums": dict(foreign_forums.most_common()),
                "failed_case_ids": [
                    str(item.get("id")) for item in items if item.get("passed") is not True
                ],
            }
        )
    return rows


def _case_forum(item: dict[str, Any]) -> str:
    for tag in item.get("tags") or []:
        tag = str(tag)
        if tag.startswith("forum:"):
            return tag.removeprefix("forum:")
    return "unknown"


def _rate(items: list[dict[str, Any]], key: str) -> float:
    scored = [item for item in items if item.get(key) is not None]
    if not scored:
        return 0.0
    return round(sum(1 for item in scored if item.get(key) is True) / len(scored), 6)


def _numbers(items: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for item in items:
        value = item.get(key)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric):
            values.append(numeric)
    return values


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_markdown(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Forum Ask Quality Report",
        "",
        f"- Cases: `{summary['cases_total']}`",
        f"- Forums: `{summary['forums_total']}`",
        f"- Pass rate: `{_format_rate(summary.get('pass_rate'))}`",
        f"- Expected chunk hit rate: `{_format_rate(summary.get('expected_chunk_hit_rate'))}`",
        f"- HTTP success rate: `{_format_rate(summary.get('http_success_rate'))}`",
        f"- Escalation rate: `{_format_rate(summary.get('escalation_rate'))}`",
        f"- LLM estimated cost, RUB: `{summary.get('llm_estimated_cost_rub')}`",
        "",
        (
            "| Forum | Cases | Pass | Chunk hit | Esc | Avg trace ms | "
            "Avg score | Foreign forums | Failed cases |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in summary["forums"]:
        foreign = ", ".join(
            f"{forum}:{count}" for forum, count in row["foreign_observed_forums"].items()
        )
        lines.append(
            "| "
            f"{row['forum']} | "
            f"{row['cases']} | "
            f"{_format_rate(row['pass_rate'])} | "
            f"{_format_rate(row['expected_chunk_hit_rate'])} | "
            f"{row['escalated']} | "
            f"{row['avg_trace_latency_ms']} | "
            f"{row['avg_reranker_score']} | "
            f"{foreign or '-'} | "
            f"{', '.join(row['failed_case_ids']) or '-'} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ask-metrics", default="reports/forum_ask_eval.json")
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
    parser.add_argument("--output", default="reports/forum_ask_summary.json")
    parser.add_argument("--markdown", default="")
    args = parser.parse_args()

    markdown_path = Path(args.markdown) if args.markdown else Path(args.output).with_suffix(".md")
    summary = summarize_forum_ask(
        ask_metrics_path=Path(args.ask_metrics),
        kb_seed_path=Path(args.kb_seed),
        output_path=Path(args.output),
        markdown_path=markdown_path,
    )
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "forums"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
