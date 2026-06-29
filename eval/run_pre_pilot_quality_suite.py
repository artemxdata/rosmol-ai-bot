from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import asyncpg
import httpx

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.pre_pilot_cases import (
    ASK_SECTION_FILES,
    DEFAULT_CASES_DIR,
    FOLLOWUP_FILE,
    build_pre_pilot_case_sets,
)
from eval.run_ask import (
    _auth_headers,
    _fetch_trace,
    _llm_cost_rub_total,
    _normalize_case,
    _trace_dsn_candidates,
    score_case,
)
from eval.run_ask import (
    run_eval as run_ask_eval,
)

DEFAULT_OUTPUT_DIR = Path("reports/pre_pilot_quality_suite")
DEFAULT_SECTIONS = ("forums", "safety", "off_topic", "pii", "followup")


async def run_pre_pilot_quality_suite(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cases_dir: Path = DEFAULT_CASES_DIR,
    kb_seed_path: Path = Path("data/knowledge_base_seed.json"),
    target: str = "http://localhost:8001/ask",
    sections: tuple[str, ...] = DEFAULT_SECTIONS,
    rebuild_cases: bool = False,
    concurrency: int = 1,
    request_timeout: float = 180.0,
    trace_lookup: bool = True,
    trace_dsn: str | None = None,
    bypass_cache: bool = True,
    max_llm_cost_rub: float | None = 200.0,
    allow_unbounded_llm_cost: bool = False,
) -> dict[str, Any]:
    _validate_sections(sections)
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(cases_dir.mkdir, parents=True, exist_ok=True)
    if rebuild_cases or not _all_case_files_exist(cases_dir):
        await asyncio.to_thread(
            build_pre_pilot_case_sets,
            kb_seed_path=kb_seed_path,
            output_dir=cases_dir,
        )

    section_reports: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    stopped_by_budget = False
    for section in sections:
        if section == "followup":
            if _budget_exhausted(max_llm_cost_rub, total_cost):
                stopped_by_budget = True
                break
            report = await run_followup_eval(
                cases_path=cases_dir / FOLLOWUP_FILE,
                output_path=output_dir / "followup_eval.json",
                markdown_path=output_dir / "followup_eval.md",
                target=target,
                request_timeout=request_timeout,
                trace_lookup=trace_lookup,
                trace_dsn=trace_dsn,
                bypass_cache=bypass_cache,
                max_llm_cost_rub=_remaining_budget(max_llm_cost_rub, total_cost),
            )
        else:
            if _budget_exhausted(max_llm_cost_rub, total_cost):
                stopped_by_budget = True
                break
            report = await run_ask_eval(
                cases_path=cases_dir / ASK_SECTION_FILES[section],
                output_path=output_dir / f"{section}_ask_eval.json",
                target=target,
                concurrency=concurrency,
                request_timeout=request_timeout,
                trace_lookup=trace_lookup,
                trace_dsn=trace_dsn,
                kb_seed_path=kb_seed_path,
                markdown_path=output_dir / f"{section}_ask_eval.md",
                bypass_cache=bypass_cache,
                max_llm_cost_rub=_remaining_budget(max_llm_cost_rub, total_cost),
                require_budget_for_large_runs=not allow_unbounded_llm_cost,
            )
        section_reports[section] = report
        total_cost += _section_cost(report)
        if (
            max_llm_cost_rub is not None
            and total_cost > max_llm_cost_rub
            and not allow_unbounded_llm_cost
        ):
            stopped_by_budget = True
            break

    summary = _build_summary(
        output_dir=output_dir,
        cases_dir=cases_dir,
        target=target,
        sections=sections,
        section_reports=section_reports,
        max_llm_cost_rub=max_llm_cost_rub,
        stopped_by_budget=stopped_by_budget,
    )
    summary_path = output_dir / "summary.json"
    summary_md_path = output_dir / "summary.md"
    await asyncio.to_thread(
        summary_path.write_text,
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    await asyncio.to_thread(_write_summary_markdown, summary_md_path, summary)
    return summary


async def run_followup_eval(
    *,
    cases_path: Path,
    output_path: Path,
    markdown_path: Path | None = None,
    target: str = "http://localhost:8001/ask",
    request_timeout: float = 180.0,
    trace_lookup: bool = True,
    trace_dsn: str | None = None,
    bypass_cache: bool = True,
    max_llm_cost_rub: float | None = None,
) -> dict[str, Any]:
    conversations = _load_followup_cases(cases_path)
    trace_pool = await _open_trace_pool(trace_dsn) if trace_lookup else None
    headers = _auth_headers("API_AUTH_TOKEN")
    if bypass_cache:
        headers["X-Bypass-Cache"] = "1"

    results: list[dict[str, Any]] = []
    budget_stopped = False
    async with httpx.AsyncClient(timeout=request_timeout) as client:
        for conversation_index, conversation in enumerate(conversations, start=1):
            user_id = f"pre-pilot-followup-{conversation_index}"
            turns = conversation.get("turns") or []
            for raw_turn in turns:
                case = _normalize_case({**raw_turn, "user_id": user_id})
                result = await _run_followup_turn(
                    client=client,
                    target=target,
                    headers=headers,
                    case=case,
                    trace_pool=trace_pool,
                    conversation_id=str(conversation.get("id") or conversation_index),
                )
                results.append(result)
                if max_llm_cost_rub is not None and _llm_cost_rub_total(results) > max_llm_cost_rub:
                    budget_stopped = True
                    break
            if budget_stopped:
                break

    if trace_pool:
        await trace_pool.close()

    metrics = _summarize_followup_results(
        results,
        cases_path=cases_path,
        target=target,
        conversations_total=len(conversations),
        budget_stopped=budget_stopped,
        max_llm_cost_rub=max_llm_cost_rub,
    )
    await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        output_path.write_text,
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if markdown_path:
        await asyncio.to_thread(_write_followup_markdown, markdown_path, metrics)
    return metrics


async def _run_followup_turn(
    *,
    client: httpx.AsyncClient,
    target: str,
    headers: dict[str, str],
    case: dict[str, Any],
    trace_pool: asyncpg.Pool | None,
    conversation_id: str,
) -> dict[str, Any]:
    started_at = perf_counter()
    request_id = ""
    try:
        response = await client.post(
            target,
            headers=headers,
            json={
                "user_id": case["user_id"],
                "channel": case["channel"],
                "text": case["query"],
            },
        )
        payload = response.json() if response.content else {}
        request_id = str(payload.get("request_id") or "")
        response_text = str(payload.get("response") or response.text)
        http_result = {
            "http_status": response.status_code,
            "request_id": request_id,
            "response": response_text,
            "latency_ms": int((perf_counter() - started_at) * 1000),
            "error": None if response.is_success else response.text[:500],
        }
    except Exception as exc:
        http_result = {
            "http_status": None,
            "request_id": request_id,
            "response": "",
            "latency_ms": int((perf_counter() - started_at) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }

    trace = await _fetch_trace(trace_pool, request_id) if trace_pool and request_id else None
    scored = score_case(case, http_result, trace)
    scored["conversation_id"] = conversation_id
    return scored


async def _open_trace_pool(trace_dsn: str | None) -> asyncpg.Pool | None:
    errors: list[str] = []
    for candidate in _trace_dsn_candidates(trace_dsn):
        try:
            return await asyncpg.create_pool(candidate, min_size=1, max_size=1)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    if os.getenv("PRE_PILOT_TRACE_REQUIRED", "").strip() == "1":
        raise RuntimeError("; ".join(errors))
    return None


def _load_followup_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Follow-up cases must contain a JSON array: {path}")
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("turns"), list):
            raise ValueError("Each follow-up case must be an object with a turns array")
    return payload


def _summarize_followup_results(
    results: list[dict[str, Any]],
    *,
    cases_path: Path,
    target: str,
    conversations_total: int,
    budget_stopped: bool,
    max_llm_cost_rub: float | None,
) -> dict[str, Any]:
    conversations: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        conversations.setdefault(str(result.get("conversation_id")), []).append(result)
    conversation_passed = [
        all(turn.get("passed") is True for turn in turns) for turns in conversations.values()
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "target": target,
        "cases_path": str(cases_path),
        "conversations_total": conversations_total,
        "conversations_executed": len(conversations),
        "conversation_pass_rate": _rate(conversation_passed),
        "turns_total": len(results),
        "turn_pass_rate": _rate([item.get("passed") is True for item in results]),
        "http_success_rate": _rate([item.get("http_success") is True for item in results]),
        "trace_coverage_rate": _rate([item.get("trace_found") is True for item in results]),
        "expected_or_equivalent_chunk_hit_rate": _rate(
            [
                item.get("expected_or_equivalent_chunk_hit") is True
                for item in results
                if item.get("expected_chunk_ids")
            ]
        ),
        "llm_estimated_cost_rub": round(_llm_cost_rub_total(results), 6),
        "llm_budget_rub": max_llm_cost_rub,
        "llm_budget_stopped": budget_stopped,
        "generator_model_counts": dict(
            Counter(str(item.get("generator_model") or "unknown") for item in results)
        ),
        "failure_reason_counts": dict(
            Counter(reason for item in results for reason in item.get("failure_reasons") or [])
        ),
        "results": results,
    }


def _build_summary(
    *,
    output_dir: Path,
    cases_dir: Path,
    target: str,
    sections: tuple[str, ...],
    section_reports: dict[str, dict[str, Any]],
    max_llm_cost_rub: float | None,
    stopped_by_budget: bool,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "target": target,
        "cases_dir": str(cases_dir),
        "output_dir": str(output_dir),
        "requested_sections": list(sections),
        "completed_sections": list(section_reports),
        "passed": all(_section_passed(report) for report in section_reports.values())
        and not stopped_by_budget,
        "max_llm_cost_rub": max_llm_cost_rub,
        "llm_estimated_cost_rub": round(sum(_section_cost(r) for r in section_reports.values()), 6),
        "llm_budget_stopped": stopped_by_budget,
        "sections": {
            name: _compact_section_report(name, report) for name, report in section_reports.items()
        },
    }


def _compact_section_report(name: str, report: dict[str, Any]) -> dict[str, Any]:
    if name == "followup":
        return {
            "turns_total": report.get("turns_total"),
            "turn_pass_rate": report.get("turn_pass_rate"),
            "conversation_pass_rate": report.get("conversation_pass_rate"),
            "http_success_rate": report.get("http_success_rate"),
            "trace_coverage_rate": report.get("trace_coverage_rate"),
            "expected_or_equivalent_chunk_hit_rate": report.get(
                "expected_or_equivalent_chunk_hit_rate"
            ),
            "llm_estimated_cost_rub": report.get("llm_estimated_cost_rub"),
            "generator_model_counts": report.get("generator_model_counts"),
            "failure_reason_counts": report.get("failure_reason_counts"),
        }
    return {
        "cases_total": report.get("cases_total"),
        "pass_rate": report.get("pass_rate"),
        "http_success_rate": report.get("http_success_rate"),
        "behavior_match_rate": report.get("behavior_match_rate"),
        "trace_coverage_rate": report.get("trace_coverage_rate"),
        "expected_or_equivalent_chunk_hit_rate": report.get(
            "expected_or_equivalent_chunk_hit_rate"
        ),
        "llm_estimated_cost_rub": report.get("llm_estimated_cost_rub"),
        "generator_model_counts": report.get("generator_model_counts"),
        "failure_reason_counts": report.get("failure_reason_counts"),
    }


def _section_passed(report: dict[str, Any]) -> bool:
    if "pass_rate" in report:
        return float(report.get("pass_rate") or 0.0) >= 0.9
    return float(report.get("turn_pass_rate") or 0.0) >= 0.9


def _section_cost(report: dict[str, Any]) -> float:
    try:
        return float(report.get("llm_estimated_cost_rub") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _budget_exhausted(max_llm_cost_rub: float | None, spent: float) -> bool:
    return max_llm_cost_rub is not None and spent >= max_llm_cost_rub


def _remaining_budget(max_llm_cost_rub: float | None, spent: float) -> float | None:
    if max_llm_cost_rub is None:
        return None
    return max(0.0, max_llm_cost_rub - spent)


def _validate_sections(sections: tuple[str, ...]) -> None:
    valid = set(DEFAULT_SECTIONS)
    invalid = [section for section in sections if section not in valid]
    if invalid:
        raise ValueError(f"Unknown pre-pilot sections: {', '.join(invalid)}")


def _all_case_files_exist(cases_dir: Path) -> bool:
    return all((cases_dir / filename).exists() for filename in ASK_SECTION_FILES.values()) and (
        cases_dir / FOLLOWUP_FILE
    ).exists()


def _rate(values: list[bool]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value) / len(values)


def _write_followup_markdown(path: Path, metrics: dict[str, Any]) -> None:
    lines = [
        "# Follow-up Eval",
        "",
        f"- Turns: `{metrics.get('turns_total')}`",
        f"- Turn pass rate: `{_format_rate(metrics.get('turn_pass_rate'))}`",
        f"- Conversation pass rate: `{_format_rate(metrics.get('conversation_pass_rate'))}`",
        f"- Trace coverage: `{_format_rate(metrics.get('trace_coverage_rate'))}`",
        f"- Cost, RUB: `{metrics.get('llm_estimated_cost_rub')}`",
        "",
        "## Failures",
        "",
    ]
    failures = metrics.get("failure_reason_counts") or {}
    if failures:
        lines.extend(f"- `{key}`: `{value}`" for key, value in sorted(failures.items()))
    else:
        lines.append("- no failures")
    lines.extend(["", "## Turns", ""])
    for item in metrics.get("results") or []:
        lines.extend(
            [
                f"### `{item.get('id')}`",
                "",
                f"- Passed: `{item.get('passed')}`",
                f"- Conversation: `{item.get('conversation_id')}`",
                f"- Model: `{item.get('generator_model') or '-'}`",
                f"- Sources: `{', '.join(item.get('cited_source_ids') or []) or '-'}`",
                f"- Failures: `{', '.join(item.get('failure_reasons') or []) or '-'}`",
                "",
                f"**Question:** {item.get('query') or '-'}",
                "",
                f"**Answer:** {_clip(item.get('response'))}",
                "",
            ]
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_summary_markdown(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Pre-pilot Quality Suite",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- Target: `{summary.get('target')}`",
        f"- Passed: `{summary.get('passed')}`",
        f"- Cost, RUB: `{summary.get('llm_estimated_cost_rub')}`",
        f"- Budget, RUB: `{summary.get('max_llm_cost_rub')}`",
        f"- Budget stopped: `{summary.get('llm_budget_stopped')}`",
        "",
        "| Section | Cases/Turns | Pass | Trace | Sources | Cost RUB |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, report in summary.get("sections", {}).items():
        cases = report.get("cases_total") or report.get("turns_total")
        pass_rate = (
            report.get("pass_rate") if "pass_rate" in report else report.get("turn_pass_rate")
        )
        source_rate = report.get("expected_or_equivalent_chunk_hit_rate")
        lines.append(
            f"| {name} | {cases} | {_format_rate(pass_rate)} | "
            f"{_format_rate(report.get('trace_coverage_rate'))} | "
            f"{_format_rate(source_rate)} | {report.get('llm_estimated_cost_rub')} |"
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _clip(value: Any, *, limit: int = 700) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text or "-"
    return text[: limit - 1].rstrip() + "…"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--cases-dir", default=str(DEFAULT_CASES_DIR))
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
    parser.add_argument("--target", default="http://localhost:8001/ask")
    parser.add_argument("--sections", default=",".join(DEFAULT_SECTIONS))
    parser.add_argument("--rebuild-cases", action="store_true")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--request-timeout", type=float, default=180.0)
    parser.add_argument("--no-db-traces", action="store_true")
    parser.add_argument("--trace-dsn", default="")
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--max-llm-cost-rub", type=float, default=200.0)
    parser.add_argument("--allow-unbounded-llm-cost", action="store_true")
    args = parser.parse_args()

    summary = asyncio.run(
        run_pre_pilot_quality_suite(
            output_dir=Path(args.output_dir),
            cases_dir=Path(args.cases_dir),
            kb_seed_path=Path(args.kb_seed),
            target=args.target,
            sections=tuple(
                section.strip() for section in args.sections.split(",") if section.strip()
            ),
            rebuild_cases=args.rebuild_cases,
            concurrency=args.concurrency,
            request_timeout=args.request_timeout,
            trace_lookup=not args.no_db_traces,
            trace_dsn=args.trace_dsn or None,
            bypass_cache=not args.use_cache,
            max_llm_cost_rub=(
                None if args.allow_unbounded_llm_cost else args.max_llm_cost_rub
            ),
            allow_unbounded_llm_cost=args.allow_unbounded_llm_cost,
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
