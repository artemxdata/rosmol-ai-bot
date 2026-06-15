from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GateConfig:
    min_recall_at_5: float = 0.85
    min_ask_pass_rate: float = 0.9
    min_expected_chunk_hit_rate: float = 0.85
    min_http_success_rate: float = 0.99
    min_trace_coverage_rate: float = 0.95
    max_low_confidence_hit_rate: float = 0.1
    max_latency_p95_ms: int | None = None
    max_llm_cost_rub: float | None = None
    min_forum_pass_rate: float = 1.0
    min_forum_expected_chunk_hit_rate: float = 1.0
    max_problem_forums: int = 0
    min_forums_total: int | None = None
    min_generation_pass_rate: float = 0.9
    min_generation_source_context_rate: float = 0.9
    max_generation_hallucination_rate: float = 0.0


def build_quality_gate_report(
    *,
    retrieval_metrics: dict[str, Any] | None,
    ask_metrics: dict[str, Any] | None,
    threshold_suggestions: dict[str, Any] | None = None,
    forum_metrics: dict[str, Any] | None = None,
    generation_metrics: dict[str, Any] | None = None,
    config: GateConfig | None = None,
) -> dict[str, Any]:
    config = config or GateConfig()
    checks: list[dict[str, Any]] = []

    if retrieval_metrics is None:
        checks.append(_check("retrieval_metrics_present", "fail", None, "report is required"))
    else:
        checks.append(
            _min_check(
                "retrieval_recall_at_5",
                retrieval_metrics.get("recall_at_5") or retrieval_metrics.get("recall_at_k"),
                config.min_recall_at_5,
            )
        )
        checks.append(
            _min_check(
                "retrieval_scored_cases",
                retrieval_metrics.get("cases_scored"),
                1,
                integer=True,
            )
        )

    if ask_metrics is None:
        checks.append(_check("ask_metrics_present", "fail", None, "report is required"))
    else:
        checks.extend(
            [
                _min_check(
                    "ask_pass_rate",
                    ask_metrics.get("pass_rate"),
                    config.min_ask_pass_rate,
                ),
                _min_check(
                    "ask_expected_chunk_hit_rate",
                    ask_metrics.get("expected_chunk_hit_rate"),
                    config.min_expected_chunk_hit_rate,
                ),
                _min_check(
                    "ask_http_success_rate",
                    ask_metrics.get("http_success_rate"),
                    config.min_http_success_rate,
                ),
                _min_check(
                    "ask_trace_coverage_rate",
                    ask_metrics.get("trace_coverage_rate"),
                    config.min_trace_coverage_rate,
                ),
                _max_check(
                    "ask_low_confidence_expected_chunk_hit_rate",
                    ask_metrics.get("low_confidence_expected_chunk_hit_rate"),
                    config.max_low_confidence_hit_rate,
                ),
            ]
        )
        if config.max_latency_p95_ms is not None:
            checks.append(
                _max_check(
                    "ask_latency_p95_ms",
                    (ask_metrics.get("latency_ms") or {}).get("p95"),
                    config.max_latency_p95_ms,
                    integer=True,
                )
            )
        if config.max_llm_cost_rub is not None:
            checks.append(
                _max_check(
                    "ask_llm_estimated_cost_rub",
                    ask_metrics.get("llm_estimated_cost_rub"),
                    config.max_llm_cost_rub,
                )
            )

    if generation_metrics is not None:
        checks.extend(
            [
                _min_check(
                    "generation_pass_rate",
                    generation_metrics.get("pass_rate"),
                    config.min_generation_pass_rate,
                ),
                _min_check(
                    "generation_source_context_rate",
                    generation_metrics.get("source_context_rate"),
                    config.min_generation_source_context_rate,
                ),
                _max_check(
                    "generation_verifier_hallucination_rate",
                    generation_metrics.get("verifier_hallucination_rate"),
                    config.max_generation_hallucination_rate,
                ),
            ]
        )

    if forum_metrics is not None:
        checks.extend(
            [
                _min_check(
                    "forum_smoke_pass_rate",
                    forum_metrics.get("pass_rate"),
                    config.min_forum_pass_rate,
                ),
                _min_check(
                    "forum_smoke_expected_chunk_hit_rate",
                    forum_metrics.get("expected_chunk_hit_rate"),
                    config.min_forum_expected_chunk_hit_rate,
                ),
                _max_check(
                    "forum_smoke_problem_forums",
                    len(forum_metrics.get("problem_forums") or []),
                    config.max_problem_forums,
                    integer=True,
                ),
            ]
        )
        if config.min_forums_total is not None:
            checks.append(
                _min_check(
                    "forum_smoke_forums_total",
                    forum_metrics.get("forums_total"),
                    config.min_forums_total,
                    integer=True,
                )
            )

    if threshold_suggestions is None:
        checks.append(
            _check(
                "threshold_suggestions_present",
                "warn",
                None,
                "run eval/suggest_rag_thresholds.py before final calibration",
            )
        )
    else:
        current_low = _float_or_none(threshold_suggestions.get("current_low_threshold"))
        recommended_low = _float_or_none(
            threshold_suggestions.get("recommended_low_threshold")
        )
        needs_lower_low_threshold = (
            current_low is not None
            and recommended_low is not None
            and recommended_low < current_low
        )
        if needs_lower_low_threshold:
            checks.append(
                _check(
                    "rag_low_threshold_calibration",
                    "warn",
                    recommended_low,
                    f"recommended low threshold is below current {current_low}",
                )
            )
        else:
            checks.append(
                _check(
                    "rag_low_threshold_calibration",
                    "pass",
                    recommended_low,
                    "low threshold is compatible with this eval",
                )
            )

    failed = [item for item in checks if item["status"] == "fail"]
    warnings = [item for item in checks if item["status"] == "warn"]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": not failed,
        "failed_checks": len(failed),
        "warning_checks": len(warnings),
        "checks": checks,
        "inputs": {
            "retrieval_cases": (retrieval_metrics or {}).get("cases_total"),
            "ask_cases": (ask_metrics or {}).get("cases_total"),
            "generation_cases": (generation_metrics or {}).get("cases_total"),
            "forum_cases": (forum_metrics or {}).get("cases_total"),
            "forum_count": (forum_metrics or {}).get("forums_total"),
            "generated_smoke_cases": {
                "retrieval": (retrieval_metrics or {}).get("generated_smoke_cases"),
                "ask": (ask_metrics or {}).get("generated_smoke_cases"),
            },
        },
    }


def _min_check(
    name: str,
    actual: Any,
    expected: float | int,
    *,
    integer: bool = False,
) -> dict[str, Any]:
    value = _int_or_none(actual) if integer else _float_or_none(actual)
    if value is None:
        return _check(name, "fail", actual, f">= {expected}")
    return _check(name, "pass" if value >= expected else "fail", value, f">= {expected}")


def _max_check(
    name: str,
    actual: Any,
    expected: float | int,
    *,
    integer: bool = False,
) -> dict[str, Any]:
    value = _int_or_none(actual) if integer else _float_or_none(actual)
    if value is None:
        return _check(name, "fail", actual, f"<= {expected}")
    return _check(name, "pass" if value <= expected else "fail", value, f"<= {expected}")


def _check(name: str, status: str, actual: Any, expected: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "actual": actual,
        "expected": expected,
    }


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Quality Gate Report",
        "",
        f"- Passed: `{report['passed']}`",
        f"- Failed checks: `{report['failed_checks']}`",
        f"- Warning checks: `{report['warning_checks']}`",
        "",
        "| Check | Status | Actual | Expected |",
        "|---|---|---:|---|",
    ]
    for check in report["checks"]:
        lines.append(
            f"| `{check['name']}` | `{check['status']}` | "
            f"`{check['actual']}` | `{check['expected']}` |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"metrics file must contain a JSON object: {path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval-metrics", default="reports/retrieval_eval.json")
    parser.add_argument("--ask-metrics", default="reports/ask_eval.json")
    parser.add_argument("--generation-metrics", default="")
    parser.add_argument("--threshold-suggestions", default="")
    parser.add_argument("--forum-summary", default="")
    parser.add_argument("--output", default="reports/quality_gate.json")
    parser.add_argument("--markdown", default="")
    parser.add_argument("--min-recall-at-5", type=float, default=0.85)
    parser.add_argument("--min-ask-pass-rate", type=float, default=0.9)
    parser.add_argument("--min-expected-chunk-hit-rate", type=float, default=0.85)
    parser.add_argument("--min-http-success-rate", type=float, default=0.99)
    parser.add_argument("--min-trace-coverage-rate", type=float, default=0.95)
    parser.add_argument("--max-low-confidence-hit-rate", type=float, default=0.1)
    parser.add_argument("--max-latency-p95-ms", type=int, default=None)
    parser.add_argument("--max-llm-cost-rub", type=float, default=None)
    parser.add_argument("--min-forum-pass-rate", type=float, default=1.0)
    parser.add_argument("--min-forum-expected-chunk-hit-rate", type=float, default=1.0)
    parser.add_argument("--max-problem-forums", type=int, default=0)
    parser.add_argument("--min-forums-total", type=int, default=None)
    parser.add_argument("--min-generation-pass-rate", type=float, default=0.9)
    parser.add_argument("--min-generation-source-context-rate", type=float, default=0.9)
    parser.add_argument("--max-generation-hallucination-rate", type=float, default=0.0)
    parser.add_argument("--no-fail", action="store_true")
    args = parser.parse_args()

    config = GateConfig(
        min_recall_at_5=args.min_recall_at_5,
        min_ask_pass_rate=args.min_ask_pass_rate,
        min_expected_chunk_hit_rate=args.min_expected_chunk_hit_rate,
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
    threshold_path = Path(args.threshold_suggestions) if args.threshold_suggestions else None
    forum_path = Path(args.forum_summary) if args.forum_summary else None
    generation_path = Path(args.generation_metrics) if args.generation_metrics else None
    report = build_quality_gate_report(
        retrieval_metrics=_read_json_if_exists(Path(args.retrieval_metrics)),
        ask_metrics=_read_json_if_exists(Path(args.ask_metrics)),
        threshold_suggestions=_read_json_if_exists(threshold_path) if threshold_path else None,
        forum_metrics=_read_json_if_exists(forum_path) if forum_path else None,
        generation_metrics=(
            _read_json_if_exists(generation_path) if generation_path else None
        ),
        config=config,
    )
    output_path = Path(args.output)
    write_report(output_path, report)
    markdown_path = Path(args.markdown) if args.markdown else output_path.with_suffix(".md")
    write_markdown(markdown_path, report)
    summary = {key: value for key, value in report.items() if key != "checks"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not report["passed"] and not args.no_fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
