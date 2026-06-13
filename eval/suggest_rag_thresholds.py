from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_CANDIDATES = (
    0.0,
    0.005,
    0.01,
    0.015,
    0.02,
    0.025,
    0.03,
    0.05,
    0.1,
    0.2,
    0.3,
    0.4,
    0.5,
    0.7,
)


def analyze_thresholds(
    metrics: dict[str, Any],
    *,
    current_low: float = 0.4,
    current_high: float = 0.7,
    target_hit_retention: float = 0.9,
    candidates: list[float] | None = None,
) -> dict[str, Any]:
    results = metrics.get("results") or []
    scored = [
        item
        for item in results
        if item.get("expected_chunk_ids") and _score(item) is not None
    ]
    hits = [item for item in scored if item.get("expected_chunk_hit") is True]
    misses = [item for item in scored if item.get("expected_chunk_hit") is False]
    low_confidence_hits = [
        item for item in hits if item.get("escalation_reason") == "low_confidence"
    ]

    candidate_values = _candidate_values(candidates, current_low, current_high)
    rows = [
        _threshold_row(
            threshold,
            scored=scored,
            hits=hits,
            misses=misses,
        )
        for threshold in candidate_values
    ]
    recommended_low = _recommend_low_threshold(
        rows,
        target_hit_retention=target_hit_retention,
        current_low=current_low,
    )

    return {
        "cases_total": len(results),
        "scored_cases": len(scored),
        "expected_chunk_hits": len(hits),
        "expected_chunk_misses": len(misses),
        "low_confidence_expected_chunk_hits": len(low_confidence_hits),
        "current_low_threshold": current_low,
        "current_high_threshold": current_high,
        "target_hit_retention": target_hit_retention,
        "recommended_low_threshold": recommended_low,
        "recommended_high_threshold": current_high,
        "recommendation_note": _recommendation_note(
            current_low=current_low,
            recommended_low=recommended_low,
            current_high=current_high,
        ),
        "hit_score": _score_summary([_score(item) for item in hits]),
        "miss_score": _score_summary([_score(item) for item in misses]),
        "threshold_candidates": rows,
    }


def _threshold_row(
    threshold: float,
    *,
    scored: list[dict[str, Any]],
    hits: list[dict[str, Any]],
    misses: list[dict[str, Any]],
) -> dict[str, Any]:
    retained_hits = sum(1 for item in hits if _score(item) >= threshold)
    rejected_misses = sum(1 for item in misses if _score(item) < threshold)
    escalated_total = sum(1 for item in scored if _score(item) < threshold)
    return {
        "threshold": threshold,
        "hit_retention_rate": _rate(retained_hits, len(hits)),
        "miss_rejection_rate": _rate(rejected_misses, len(misses)),
        "would_escalate_cases": escalated_total,
    }


def _recommend_low_threshold(
    rows: list[dict[str, Any]],
    *,
    target_hit_retention: float,
    current_low: float,
) -> float:
    eligible = [
        row
        for row in rows
        if row["hit_retention_rate"] is not None
        and row["hit_retention_rate"] >= target_hit_retention
    ]
    if not eligible:
        return current_low
    return max(float(row["threshold"]) for row in eligible)


def _recommendation_note(
    *,
    current_low: float,
    recommended_low: float,
    current_high: float,
) -> str:
    if recommended_low < current_low:
        return (
            "Current low threshold rejects too many expected chunk hits in this eval. "
            "Review the markdown table and calibrate on a larger golden set before changing .env."
        )
    if recommended_low > current_low:
        return (
            "This eval allows a stricter low threshold, but increase it only after checking miss "
            f"rejection and source quality. Keep high threshold conservative at {current_high}."
        )
    return (
        "Current low threshold is compatible with the requested hit retention on this eval. "
        f"Keep high threshold conservative at {current_high} unless source_chunk cases are proven."
    )


def _candidate_values(
    candidates: list[float] | None,
    current_low: float,
    current_high: float,
) -> list[float]:
    values = set(candidates or DEFAULT_CANDIDATES)
    values.update({current_low, current_high})
    return sorted(round(float(value), 6) for value in values if 0 <= float(value) <= 1)


def _score(item: dict[str, Any]) -> float | None:
    value = item.get("max_reranker_score")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _score_summary(values: list[float | None]) -> dict[str, float | None]:
    numeric = sorted(value for value in values if value is not None)
    if not numeric:
        return {"min": None, "p50": None, "p90": None, "max": None}
    return {
        "min": round(numeric[0], 6),
        "p50": round(_percentile(numeric, 0.5), 6),
        "p90": round(_percentile(numeric, 0.9), 6),
        "max": round(numeric[-1], 6),
    }


def _percentile(values: list[float], quantile: float) -> float:
    if len(values) == 1:
        return values[0]
    index = round((len(values) - 1) * quantile)
    return values[max(0, min(len(values) - 1, index))]


def _rate(count: int, total: int) -> float | None:
    if total == 0:
        return None
    return round(count / total, 6)


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# RAG Threshold Suggestions",
        "",
        f"- Cases: `{report['cases_total']}`",
        f"- Scored cases: `{report['scored_cases']}`",
        f"- Expected chunk hits: `{report['expected_chunk_hits']}`",
        f"- Expected chunk misses: `{report['expected_chunk_misses']}`",
        "- Low-confidence expected chunk hits: "
        f"`{report['low_confidence_expected_chunk_hits']}`",
        f"- Current low threshold: `{report['current_low_threshold']}`",
        f"- Recommended low threshold: `{report['recommended_low_threshold']}`",
        f"- Current high threshold: `{report['current_high_threshold']}`",
        f"- Recommended high threshold: `{report['recommended_high_threshold']}`",
        f"- Note: {report['recommendation_note']}",
        "",
        "## Score Summary",
        "",
        "| Group | min | p50 | p90 | max |",
        "|---|---:|---:|---:|---:|",
        _score_summary_row("Hits", report["hit_score"]),
        _score_summary_row("Misses", report["miss_score"]),
        "",
        "## Low Threshold Candidates",
        "",
        "| Threshold | Hit retention | Miss rejection | Would escalate |",
        "|---:|---:|---:|---:|",
    ]
    for row in report["threshold_candidates"]:
        lines.append(
            "| "
            f"{row['threshold']} | "
            f"{_format_rate(row['hit_retention_rate'])} | "
            f"{_format_rate(row['miss_rejection_rate'])} | "
            f"{row['would_escalate_cases']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _score_summary_row(label: str, summary: dict[str, Any]) -> str:
    return (
        f"| {label} | {summary['min']} | {summary['p50']} | "
        f"{summary['p90']} | {summary['max']} |"
    )


def _format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/ask_eval.json")
    parser.add_argument("--output", default="reports/rag_threshold_suggestions.json")
    parser.add_argument("--markdown", default="")
    parser.add_argument("--current-low", type=float, default=0.4)
    parser.add_argument("--current-high", type=float, default=0.7)
    parser.add_argument("--target-hit-retention", type=float, default=0.9)
    args = parser.parse_args()

    metrics_path = Path(args.metrics)
    if not metrics_path.exists():
        raise SystemExit(f"ask eval metrics file not found: {metrics_path}")
    report = analyze_thresholds(
        _read_json(metrics_path),
        current_low=args.current_low,
        current_high=args.current_high,
        target_hit_retention=args.target_hit_retention,
    )
    output_path = Path(args.output)
    write_report(output_path, report)
    markdown_path = Path(args.markdown) if args.markdown else output_path.with_suffix(".md")
    write_markdown(markdown_path, report)
    summary = {key: value for key, value in report.items() if key != "threshold_candidates"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
