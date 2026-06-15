from __future__ import annotations

# ruff: noqa: E402,I001

import argparse
import asyncio
import json
import math
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.models import Chunk
from src.rag.errors import MLDependencyError
from src.rag.reranker import Reranker

DEFAULT_PAIRS = Path("data/private/tickets/analysis/reranker_calibration_pairs.jsonl")
DEFAULT_OUTPUT = Path("data/private/tickets/eval/reranker_calibration_report.json")

THRESHOLD_CANDIDATES = [
    0.0,
    0.005,
    0.01,
    0.015,
    0.02,
    0.025,
    0.03,
    0.05,
    0.075,
    0.1,
    0.15,
    0.2,
    0.3,
    0.4,
    0.5,
    0.7,
]


class PairScorer(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]:
        pass


class RerankerPairScorer:
    def __init__(self) -> None:
        self.reranker = Reranker()

    def score(self, query: str, texts: list[str]) -> list[float]:
        chunks = [
            Chunk(chunk_id=f"candidate_{index}", text=text, metadata={})
            for index, text in enumerate(texts)
        ]
        ranked = self.reranker.rerank(query, chunks, top_k=len(chunks))
        by_id = {chunk.chunk_id: chunk.reranker_score for chunk in ranked}
        return [float(by_id[f"candidate_{index}"]) for index in range(len(texts))]


async def calibrate_pairs(
    pairs_path: Path,
    output_path: Path,
    *,
    limit: int | None = None,
    target_positive_retention: float = 0.9,
    scorer: PairScorer | None = None,
) -> dict[str, Any]:
    pairs = read_jsonl(pairs_path)
    if limit is not None:
        pairs = pairs[:limit]
    scorer = scorer or RerankerPairScorer()

    scored_pairs: list[dict[str, Any]] = []
    for pair in pairs:
        scored_pairs.append(await score_pair(pair, scorer))

    report = analyze_scored_pairs(
        scored_pairs,
        target_positive_retention=target_positive_retention,
        source_path=pairs_path,
    )
    if not is_stdio_path(output_path):
        write_json(output_path, report)
        write_markdown(output_path.with_suffix(".md"), report)
    return report


async def score_pair(pair: dict[str, Any], scorer: PairScorer) -> dict[str, Any]:
    query = str(pair.get("query") or "")
    positive_text = str(pair.get("positive_text") or "")
    negatives = [str(item) for item in pair.get("hard_negative_texts") or []]
    texts = [positive_text, *negatives]
    scores = await asyncio.to_thread(scorer.score, query, texts)
    positive_score = float(scores[0]) if scores else None
    negative_scores = [float(score) for score in scores[1:]]
    return {
        "query": query,
        "forum_normalized": pair.get("forum_normalized"),
        "category": pair.get("category"),
        "topic": pair.get("topic"),
        "source_ticket_ids": pair.get("source_ticket_ids") or [],
        "positive_score": positive_score,
        "negative_scores": negative_scores,
        "positive_rank": positive_rank(positive_score, negative_scores),
        "margin": score_margin(positive_score, negative_scores),
    }


def analyze_scored_pairs(
    scored_pairs: list[dict[str, Any]],
    *,
    target_positive_retention: float,
    source_path: Path | None = None,
) -> dict[str, Any]:
    positive_scores = [
        float(item["positive_score"])
        for item in scored_pairs
        if item.get("positive_score") is not None
    ]
    negative_scores = [
        float(score)
        for item in scored_pairs
        for score in item.get("negative_scores") or []
        if score is not None
    ]
    positive_at_1 = sum(1 for item in scored_pairs if item.get("positive_rank") == 1)
    positive_ranks = [
        int(item["positive_rank"])
        for item in scored_pairs
        if item.get("positive_rank") is not None
    ]
    margins = [
        float(item["margin"])
        for item in scored_pairs
        if item.get("margin") is not None and math.isfinite(float(item["margin"]))
    ]
    negative_beats_positive = sum(1 for rank in positive_ranks if rank > 1)
    rows = [
        threshold_row(threshold, positive_scores, negative_scores)
        for threshold in THRESHOLD_CANDIDATES
    ]
    recommended = recommend_threshold(rows, target_positive_retention)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_path": str(source_path) if source_path else None,
        "pairs_total": len(scored_pairs),
        "positive_scores": score_summary(positive_scores),
        "negative_scores": score_summary(negative_scores),
        "positive_at_1_rate": rate(positive_at_1, len(scored_pairs)),
        "positive_rank_histogram": rank_histogram(positive_ranks),
        "negative_beats_positive_rate": rate(negative_beats_positive, len(scored_pairs)),
        "margin_summary": score_summary(margins),
        "target_positive_retention": target_positive_retention,
        "recommended_threshold": recommended,
        "threshold_candidates": rows,
        "worst_positive_cases": worst_positive_cases(scored_pairs),
    }
    report["quality_warnings"] = calibration_quality_warnings(report)
    return report


def threshold_row(
    threshold: float,
    positive_scores: list[float],
    negative_scores: list[float],
) -> dict[str, Any]:
    retained_positive = sum(score >= threshold for score in positive_scores)
    rejected_negative = sum(score < threshold for score in negative_scores)
    precision_denominator = retained_positive + sum(score >= threshold for score in negative_scores)
    return {
        "threshold": threshold,
        "positive_retention_rate": rate(retained_positive, len(positive_scores)),
        "negative_rejection_rate": rate(rejected_negative, len(negative_scores)),
        "precision_if_answered": rate(retained_positive, precision_denominator),
    }


def recommend_threshold(
    rows: list[dict[str, Any]],
    target_positive_retention: float,
) -> float | None:
    eligible = [
        row
        for row in rows
        if row["positive_retention_rate"] is not None
        and row["positive_retention_rate"] >= target_positive_retention
    ]
    if not eligible:
        return None
    eligible.sort(
        key=lambda row: (
            row["negative_rejection_rate"] or 0.0,
            row["precision_if_answered"] or 0.0,
            row["threshold"],
        ),
        reverse=True,
    )
    return float(eligible[0]["threshold"])


def positive_rank(positive_score: float | None, negative_scores: list[float]) -> int | None:
    if positive_score is None:
        return None
    better_or_equal = sum(1 for score in negative_scores if score > positive_score)
    return better_or_equal + 1


def score_margin(positive_score: float | None, negative_scores: list[float]) -> float | None:
    if positive_score is None:
        return None
    best_negative = max(negative_scores) if negative_scores else 0.0
    return round(positive_score - best_negative, 6)


def score_summary(values: list[float]) -> dict[str, float | None]:
    numeric = sorted(value for value in values if math.isfinite(value))
    if not numeric:
        return {"min": None, "p50": None, "p90": None, "p95": None, "max": None}
    return {
        "min": round(numeric[0], 6),
        "p50": percentile(numeric, 0.50),
        "p90": percentile(numeric, 0.90),
        "p95": percentile(numeric, 0.95),
        "max": round(numeric[-1], 6),
    }


def rank_histogram(ranks: list[int]) -> dict[str, int]:
    counts = Counter(ranks)
    return {str(rank): counts[rank] for rank in sorted(counts)}


def calibration_quality_warnings(report: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    positive_at_1 = report.get("positive_at_1_rate")
    if positive_at_1 is not None and float(positive_at_1) < 0.6:
        warnings.append("low_positive_at_1_rate_review_pairs_or_reranker")
    negative_beats = report.get("negative_beats_positive_rate")
    if negative_beats is not None and float(negative_beats) > 0.3:
        warnings.append("many_hard_negatives_beat_positive")

    recommended = report.get("recommended_threshold")
    threshold_rows = report.get("threshold_candidates") or []
    selected_row = next(
        (row for row in threshold_rows if row.get("threshold") == recommended),
        None,
    )
    if selected_row and (selected_row.get("precision_if_answered") or 0.0) < 0.5:
        warnings.append("recommended_threshold_has_low_precision")
    if recommended is None:
        warnings.append("no_threshold_meets_target_positive_retention")
    return warnings


def worst_positive_cases(
    scored_pairs: list[dict[str, Any]],
    limit: int = 20,
) -> list[dict[str, Any]]:
    ordered = sorted(
        scored_pairs,
        key=lambda item: (
            item.get("positive_score") is None,
            float(item.get("positive_score") or 0.0),
            float(item.get("margin") or 0.0),
        ),
    )
    return [
        {
            "query": item.get("query"),
            "positive_score": item.get("positive_score"),
            "max_negative_score": max(item.get("negative_scores") or [0.0]),
            "positive_rank": item.get("positive_rank"),
            "margin": item.get("margin"),
            "category": item.get("category"),
            "topic": item.get("topic"),
            "forum_normalized": item.get("forum_normalized"),
            "source_ticket_ids": item.get("source_ticket_ids"),
        }
        for item in ordered[:limit]
    ]


def percentile(sorted_values: list[float], quantile: float) -> float | None:
    if not sorted_values:
        return None
    index = round((len(sorted_values) - 1) * quantile)
    return round(sorted_values[max(0, min(len(sorted_values) - 1, index))], 6)


def rate(count: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(count / total, 6)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if is_stdio_path(path):
        return read_jsonl_lines(sys.stdin)
    with path.open("r", encoding="utf-8") as file:
        return read_jsonl_lines(file)


def read_jsonl_lines(lines: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        value = json.loads(stripped)
        if not isinstance(value, dict):
            raise ValueError(f"JSONL line {line_number} must be an object")
        records.append(value)
    return records


def is_stdio_path(path: Path) -> bool:
    return str(path) == "-"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Reranker Calibration Report",
        "",
        f"- Pairs: `{report['pairs_total']}`",
        f"- Positive@1 rate: `{format_rate(report['positive_at_1_rate'])}`",
        f"- Negative beats positive rate: `{format_rate(report['negative_beats_positive_rate'])}`",
        f"- Recommended threshold: `{report['recommended_threshold']}`",
        f"- Target positive retention: `{format_rate(report['target_positive_retention'])}`",
        f"- Quality warnings: `{', '.join(report.get('quality_warnings') or []) or 'none'}`",
        "",
        "## Score Summary",
        "",
        "| Group | min | p50 | p90 | p95 | max |",
        "|---|---:|---:|---:|---:|---:|",
        summary_row("Positive", report["positive_scores"]),
        summary_row("Negative", report["negative_scores"]),
        summary_row("Margin", report["margin_summary"]),
        "",
        "## Positive Rank Histogram",
        "",
        "| Rank | Count |",
        "|---:|---:|",
        *[
            f"| {rank} | {count} |"
            for rank, count in report.get("positive_rank_histogram", {}).items()
        ],
        "",
        "## Threshold Candidates",
        "",
        "| Threshold | Positive retention | Negative rejection | Precision if answered |",
        "|---:|---:|---:|---:|",
    ]
    for row in report["threshold_candidates"]:
        lines.append(
            "| "
            f"{row['threshold']} | "
            f"{format_rate(row['positive_retention_rate'])} | "
            f"{format_rate(row['negative_rejection_rate'])} | "
            f"{format_rate(row['precision_if_answered'])} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def summary_row(label: str, summary: dict[str, Any]) -> str:
    return (
        f"| {label} | {summary['min']} | {summary['p50']} | {summary['p90']} | "
        f"{summary['p95']} | {summary['max']} |"
    )


def format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calibrate bge-reranker threshold on private ticket query/chunk pairs."
    )
    parser.add_argument(
        "--pairs",
        type=Path,
        default=DEFAULT_PAIRS,
        help="JSONL path or '-' for stdin.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="JSON report path or '-' to skip file writes and print aggregate stdout only.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--target-positive-retention", type=float, default=0.9)
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    try:
        report = asyncio.run(
            calibrate_pairs(
                pairs_path=args.pairs,
                output_path=args.output,
                limit=args.limit,
                target_positive_retention=args.target_positive_retention,
            )
        )
    except MLDependencyError as exc:
        raise SystemExit(
            "ML dependency is missing. Run this script in the ML Docker runtime or install "
            f"project ML extras locally. Details: {exc}"
        ) from exc

    summary = {key: value for key, value in report.items() if key != "worst_positive_cases"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
