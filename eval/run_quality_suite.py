from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.build_forum_smoke_set import build_forum_smoke_set
from eval.check_quality_gate import (
    GateConfig,
    build_quality_gate_report,
)
from eval.check_quality_gate import (
    write_markdown as write_gate_markdown,
)
from eval.check_quality_gate import (
    write_report as write_gate_report,
)
from eval.run_ask import run_eval as run_ask_eval
from eval.run_generation import run_generation_eval
from eval.run_retrieval import run_eval as run_retrieval_eval
from eval.suggest_rag_thresholds import (
    analyze_thresholds,
)
from eval.suggest_rag_thresholds import (
    write_markdown as write_threshold_markdown,
)
from eval.suggest_rag_thresholds import (
    write_report as write_threshold_report,
)
from eval.summarize_forum_ask import summarize_forum_ask


async def run_quality_suite(
    *,
    output_dir: Path = Path("reports/quality_suite"),
    golden_path: Path = Path("data/golden_set.json"),
    ask_cases_path: Path = Path("data/ask_eval_set.json"),
    kb_seed_path: Path = Path("data/knowledge_base_seed.json"),
    target: str = "http://localhost:8001/ask",
    retrieval_backend: str = "qdrant",
    top_k: int = 5,
    auto_smoke_cases: bool = False,
    max_smoke_cases: int = 50,
    ask_concurrency: int = 1,
    ask_timeout: float = 120.0,
    trace_lookup: bool = True,
    trace_dsn: str | None = None,
    current_low: float = 0.4,
    current_high: float = 0.7,
    target_hit_retention: float = 0.9,
    forum_smoke: bool = False,
    forum_smoke_per_forum: int = 1,
    bypass_cache: bool = True,
    max_llm_cost_rub: float | None = None,
    ask_max_cases: int | None = None,
    allow_unbounded_llm_cost: bool = False,
    gate_config: GateConfig | None = None,
) -> dict[str, Any]:
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
    paths = _suite_paths(output_dir)
    run_prefix = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")

    retrieval_metrics = await run_retrieval_eval(
        golden_path,
        paths["retrieval_json"],
        top_k,
        backend=retrieval_backend,
        kb_seed_path=kb_seed_path,
        auto_smoke_cases=auto_smoke_cases,
        max_smoke_cases=max_smoke_cases,
        markdown_path=paths["retrieval_md"],
    )
    ask_metrics = await run_ask_eval(
        cases_path=ask_cases_path,
        output_path=paths["ask_json"],
        target=target,
        concurrency=ask_concurrency,
        request_timeout=ask_timeout,
        trace_lookup=trace_lookup,
        trace_dsn=trace_dsn,
        kb_seed_path=kb_seed_path,
        auto_smoke_cases=auto_smoke_cases,
        max_smoke_cases=max_smoke_cases,
        markdown_path=paths["ask_md"],
        bypass_cache=bypass_cache,
        generated_user_prefix=f"ask-eval-{run_prefix}",
        max_cases=ask_max_cases,
        max_llm_cost_rub=max_llm_cost_rub,
        require_budget_for_large_runs=not allow_unbounded_llm_cost,
    )
    threshold_report = analyze_thresholds(
        ask_metrics,
        current_low=current_low,
        current_high=current_high,
        target_hit_retention=target_hit_retention,
    )
    write_threshold_report(paths["threshold_json"], threshold_report)
    write_threshold_markdown(paths["threshold_md"], threshold_report)
    generation_metrics = await asyncio.to_thread(
        run_generation_eval,
        paths["ask_json"],
        paths["generation_json"],
        paths["generation_md"],
    )

    forum_summary: dict[str, Any] | None = None
    if forum_smoke:
        await asyncio.to_thread(
            build_forum_smoke_set,
            kb_seed_path,
            paths["forum_cases_json"],
            per_forum=forum_smoke_per_forum,
            user_prefix=f"forum-smoke-{run_prefix}",
        )
        await run_ask_eval(
            cases_path=paths["forum_cases_json"],
            output_path=paths["forum_ask_json"],
            target=target,
            concurrency=ask_concurrency,
            request_timeout=ask_timeout,
            trace_lookup=trace_lookup,
            trace_dsn=trace_dsn,
            kb_seed_path=kb_seed_path,
            markdown_path=paths["forum_ask_md"],
            bypass_cache=bypass_cache,
            max_llm_cost_rub=max_llm_cost_rub,
            require_budget_for_large_runs=not allow_unbounded_llm_cost,
        )
        forum_summary = await asyncio.to_thread(
            summarize_forum_ask,
            paths["forum_ask_json"],
            kb_seed_path,
            paths["forum_summary_json"],
            paths["forum_summary_md"],
        )

    gate_report = build_quality_gate_report(
        retrieval_metrics=retrieval_metrics,
        ask_metrics=ask_metrics,
        threshold_suggestions=threshold_report,
        forum_metrics=forum_summary,
        generation_metrics=generation_metrics,
        config=gate_config,
    )
    write_gate_report(paths["gate_json"], gate_report)
    write_gate_markdown(paths["gate_md"], gate_report)

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": gate_report["passed"],
        "retrieval": _summary_subset(
            retrieval_metrics,
            (
                "backend",
                "cases_total",
                "cases_scored",
                "recall_at_5",
                "recall_at_10",
                "recall_at_k",
                "mrr",
                "avg_expected_rank",
            ),
        ),
        "ask": _summary_subset(
            ask_metrics,
            (
                "cases_total",
                "pass_rate",
                "expected_chunk_hit_rate",
                "expected_or_equivalent_chunk_hit_rate",
                "behavior_match_rate",
                "low_confidence_expected_chunk_hit_rate",
                "llm_estimated_cost_rub",
                "llm_budget_rub",
                "llm_budget_exceeded",
            ),
        ),
        "generation": _summary_subset(
            generation_metrics,
            (
                "cases_total",
                "pass_rate",
                "source_context_rate",
                "expected_chunk_hit_rate",
                "verifier_hallucination_rate",
            ),
        ),
        "quality_gate": {
            "failed_checks": gate_report["failed_checks"],
            "warning_checks": gate_report["warning_checks"],
        },
        "forum_smoke": (
            _summary_subset(
                forum_summary or {},
                (
                    "cases_total",
                    "forums_total",
                    "pass_rate",
                    "expected_chunk_hit_rate",
                    "escalation_rate",
                ),
            )
            if forum_smoke
            else None
        ),
        "paths": {key: str(value) for key, value in paths.items()},
    }
    await asyncio.to_thread(
        paths["summary_json"].write_text,
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    await asyncio.to_thread(_write_summary_markdown, paths["summary_md"], summary)
    return summary


def _suite_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "retrieval_json": output_dir / "retrieval_eval.json",
        "retrieval_md": output_dir / "retrieval_eval.md",
        "ask_json": output_dir / "ask_eval.json",
        "ask_md": output_dir / "ask_eval.md",
        "generation_json": output_dir / "generation_eval.json",
        "generation_md": output_dir / "generation_eval.md",
        "threshold_json": output_dir / "rag_threshold_suggestions.json",
        "threshold_md": output_dir / "rag_threshold_suggestions.md",
        "forum_cases_json": output_dir / "forum_smoke_set.json",
        "forum_ask_json": output_dir / "forum_ask_eval.json",
        "forum_ask_md": output_dir / "forum_ask_eval.md",
        "forum_summary_json": output_dir / "forum_ask_summary.json",
        "forum_summary_md": output_dir / "forum_ask_summary.md",
        "gate_json": output_dir / "quality_gate.json",
        "gate_md": output_dir / "quality_gate.md",
        "summary_json": output_dir / "summary.json",
        "summary_md": output_dir / "summary.md",
    }


def _summary_subset(payload: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: payload.get(key) for key in keys}


def _write_summary_markdown(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Quality Suite Summary",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- Passed: `{summary.get('passed')}`",
        f"- Failed checks: `{summary['quality_gate'].get('failed_checks')}`",
        f"- Warning checks: `{summary['quality_gate'].get('warning_checks')}`",
        "",
        "| Area | Cases | Main metric | Secondary metric |",
        "|---|---:|---:|---:|",
        (
            f"| Retrieval | {summary['retrieval'].get('cases_total')} | "
            f"recall@5 `{_format_rate(summary['retrieval'].get('recall_at_5'))}` | "
            f"recall@10 `{_format_rate(summary['retrieval'].get('recall_at_10'))}` |"
        ),
        (
            f"| Ask | {summary['ask'].get('cases_total')} | "
            f"pass `{_format_rate(summary['ask'].get('pass_rate'))}` | "
            "chunk hit "
            f"`{_format_rate(_ask_chunk_hit_rate(summary))}` |"
        ),
        (
            f"| Generation | {summary['generation'].get('cases_total')} | "
            f"pass `{_format_rate(summary['generation'].get('pass_rate'))}` | "
            f"grounded `{_format_rate(summary['generation'].get('source_context_rate'))}` |"
        ),
    ]
    forum = summary.get("forum_smoke")
    if forum:
        lines.append(
            f"| Forum smoke | {forum.get('cases_total')} | "
            f"pass `{_format_rate(forum.get('pass_rate'))}` | "
            f"forums `{forum.get('forums_total')}` |"
        )

    lines.extend(["", "## Report Files", ""])
    for key, value in summary.get("paths", {}).items():
        lines.append(f"- `{key}`: `{value}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _ask_chunk_hit_rate(summary: dict[str, Any]) -> Any:
    return _preferred_rate(
        summary["ask"],
        "expected_or_equivalent_chunk_hit_rate",
        "expected_chunk_hit_rate",
    )


def _preferred_rate(payload: dict[str, Any], preferred: str, fallback: str) -> Any:
    value = payload.get(preferred)
    return payload.get(fallback) if value is None else value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="reports/quality_suite")
    parser.add_argument("--golden", default="data/golden_set.json")
    parser.add_argument("--ask-cases", default="data/ask_eval_set.json")
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
    parser.add_argument("--target", default="http://localhost:8001/ask")
    parser.add_argument("--retrieval-backend", choices=["qdrant", "lexical"], default="qdrant")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--auto-smoke-cases", action="store_true")
    parser.add_argument("--max-smoke-cases", type=int, default=50)
    parser.add_argument("--ask-concurrency", type=int, default=1)
    parser.add_argument("--ask-timeout", type=float, default=120.0)
    parser.add_argument("--ask-max-cases", type=int, default=None)
    parser.add_argument("--no-db-traces", action="store_true")
    parser.add_argument("--trace-dsn", default="")
    parser.add_argument("--current-low", type=float, default=0.4)
    parser.add_argument("--current-high", type=float, default=0.7)
    parser.add_argument("--target-hit-retention", type=float, default=0.9)
    parser.add_argument("--forum-smoke", action="store_true")
    parser.add_argument("--forum-smoke-per-forum", type=int, default=1)
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--min-recall-at-5", type=float, default=0.85)
    parser.add_argument("--min-ask-pass-rate", type=float, default=0.9)
    parser.add_argument("--min-expected-chunk-hit-rate", type=float, default=0.85)
    parser.add_argument("--min-behavior-match-rate", type=float, default=0.95)
    parser.add_argument("--min-http-success-rate", type=float, default=0.99)
    parser.add_argument("--min-trace-coverage-rate", type=float, default=0.95)
    parser.add_argument("--max-low-confidence-hit-rate", type=float, default=0.1)
    parser.add_argument("--max-latency-p95-ms", type=int, default=None)
    parser.add_argument("--max-llm-cost-rub", type=float, default=None)
    parser.add_argument("--allow-unbounded-llm-cost", action="store_true")
    parser.add_argument("--min-forum-pass-rate", type=float, default=1.0)
    parser.add_argument("--min-forum-expected-chunk-hit-rate", type=float, default=1.0)
    parser.add_argument("--max-problem-forums", type=int, default=0)
    parser.add_argument("--min-forums-total", type=int, default=None)
    parser.add_argument("--min-generation-pass-rate", type=float, default=0.9)
    parser.add_argument("--min-generation-source-context-rate", type=float, default=0.9)
    parser.add_argument("--max-generation-hallucination-rate", type=float, default=0.0)
    parser.add_argument("--no-fail", action="store_true")
    args = parser.parse_args()

    gate_config = GateConfig(
        min_recall_at_5=args.min_recall_at_5,
        min_ask_pass_rate=args.min_ask_pass_rate,
        min_expected_chunk_hit_rate=args.min_expected_chunk_hit_rate,
        min_behavior_match_rate=args.min_behavior_match_rate,
        min_http_success_rate=args.min_http_success_rate,
        min_trace_coverage_rate=args.min_trace_coverage_rate,
        max_low_confidence_hit_rate=args.max_low_confidence_hit_rate,
        max_latency_p95_ms=args.max_latency_p95_ms,
        max_llm_cost_rub=args.max_llm_cost_rub,
        min_forum_pass_rate=args.min_forum_pass_rate,
        min_forum_expected_chunk_hit_rate=args.min_forum_expected_chunk_hit_rate,
        max_problem_forums=args.max_problem_forums,
        min_forums_total=args.min_forums_total,
        min_generation_pass_rate=args.min_generation_pass_rate,
        min_generation_source_context_rate=args.min_generation_source_context_rate,
        max_generation_hallucination_rate=args.max_generation_hallucination_rate,
    )
    summary = asyncio.run(
        run_quality_suite(
            output_dir=Path(args.output_dir),
            golden_path=Path(args.golden),
            ask_cases_path=Path(args.ask_cases),
            kb_seed_path=Path(args.kb_seed),
            target=args.target,
            retrieval_backend=args.retrieval_backend,
            top_k=args.top_k,
            auto_smoke_cases=args.auto_smoke_cases,
            max_smoke_cases=args.max_smoke_cases,
            ask_concurrency=args.ask_concurrency,
            ask_timeout=args.ask_timeout,
            ask_max_cases=args.ask_max_cases,
            trace_lookup=not args.no_db_traces,
            trace_dsn=args.trace_dsn or None,
            current_low=args.current_low,
            current_high=args.current_high,
            target_hit_retention=args.target_hit_retention,
            forum_smoke=args.forum_smoke,
            forum_smoke_per_forum=args.forum_smoke_per_forum,
            bypass_cache=not args.use_cache,
            max_llm_cost_rub=args.max_llm_cost_rub,
            allow_unbounded_llm_cost=args.allow_unbounded_llm_cost,
            gate_config=gate_config,
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["passed"] and not args.no_fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
