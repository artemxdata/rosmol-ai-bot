from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

SOURCE_RE = re.compile(r"\[src:([^\]]+)\]")
DEFAULT_INPUT = Path("reports/ask_eval.json")
DEFAULT_OUTPUT = Path("reports/generation_eval.json")


def run_generation_eval(
    cases_path: Path = DEFAULT_INPUT,
    output_path: Path = DEFAULT_OUTPUT,
    markdown_path: Path | None = None,
) -> dict[str, Any]:
    cases = load_generation_cases(cases_path)
    results = [score_generation_case(case) for case in cases]
    report = summarize_generation_results(results, cases_path)
    write_json(output_path, report)
    if markdown_path:
        write_markdown(markdown_path, report)
    return report


def load_generation_cases(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"generation eval input not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        payload = payload["results"]
    if not isinstance(payload, list):
        raise ValueError("generation eval input must be a JSON array or object with results[]")
    cases: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"generation eval case {index} must be an object")
        cases.append(item)
    return cases


def score_generation_case(raw: dict[str, Any]) -> dict[str, Any]:
    case_id = case_identifier(raw)
    response = response_text(raw)
    source_ids = source_context_ids(raw)
    cited_ids = cited_source_ids(raw, response)
    expected_chunk_ids = string_list(raw.get("expected_chunk_ids") or raw.get("expected_chunks"))
    expected_contains = string_list(
        raw.get("expected_answer_contains") or raw.get("answer_contains")
    )
    forbidden_phrases = string_list(raw.get("forbidden_phrases"))
    was_escalated = optional_bool(raw.get("was_escalated"))
    expected_escalated = optional_bool(raw.get("expected_escalated"))
    hallucination_flag = verifier_hallucination(raw)
    unknown_markers = sorted(cited_ids - source_ids) if source_ids else sorted(cited_ids)
    missing_expected_text = [
        phrase for phrase in expected_contains if phrase.casefold() not in response.casefold()
    ]
    forbidden_hits = [
        phrase for phrase in forbidden_phrases if phrase.casefold() in response.casefold()
    ]
    expected_chunk_hit = expected_chunk_hit_check(expected_chunk_ids, cited_ids, source_ids)
    escalation_match = (
        None if expected_escalated is None else bool(was_escalated) == expected_escalated
    )
    source_context_present = bool(cited_ids or source_ids)
    source_required_missing = was_escalated is not True and not source_context_present
    response_required_missing = was_escalated is not True and not response.strip()
    http_success = raw.get("http_success")
    checks = {
        "http_success": True if http_success is None else bool(http_success),
        "response_present": not response_required_missing,
        "source_context_present": not source_required_missing,
        "unknown_source_markers": not unknown_markers,
        "expected_chunk_hit": expected_chunk_hit,
        "expected_text_present": not missing_expected_text,
        "forbidden_text_absent": not forbidden_hits,
        "verifier_passed": hallucination_flag is not True,
        "escalation_match": escalation_match,
    }
    passed = all(value is not False for value in checks.values())
    return {
        "id": case_id,
        "tags": string_list(raw.get("tags")),
        "passed": passed,
        "was_escalated": was_escalated,
        "expected_escalated": expected_escalated,
        "source_context_present": source_context_present,
        "cited_source_ids": sorted(cited_ids),
        "source_context_ids": sorted(source_ids),
        "expected_chunk_ids": expected_chunk_ids,
        "expected_chunk_hit": expected_chunk_hit,
        "unknown_source_markers": unknown_markers,
        "missing_expected_text_count": len(missing_expected_text),
        "forbidden_text_hit_count": len(forbidden_hits),
        "verifier_hallucination": hallucination_flag,
        "checks": checks,
    }


def summarize_generation_results(
    results: list[dict[str, Any]],
    cases_path: Path,
) -> dict[str, Any]:
    total = len(results)
    expected_chunk_cases = [item for item in results if item["expected_chunk_ids"]]
    source_context_required = [
        item for item in results if item.get("was_escalated") is not True
    ]
    escalation_labeled = [
        item for item in results if item.get("expected_escalated") is not None
    ]
    checks = Counter()
    for item in results:
        for name, value in item["checks"].items():
            if value is False:
                checks[name] += 1
    return {
        "source": str(cases_path),
        "cases_total": total,
        "pass_rate": rate(sum(1 for item in results if item["passed"]), total),
        "source_context_rate": rate(
            sum(1 for item in source_context_required if item["source_context_present"]),
            len(source_context_required),
        ),
        "expected_chunk_hit_rate": rate(
            sum(1 for item in expected_chunk_cases if item["expected_chunk_hit"] is True),
            len(expected_chunk_cases),
        ),
        "escalation_match_rate": rate(
            sum(
                1
                for item in escalation_labeled
                if item["checks"].get("escalation_match") is True
            ),
            len(escalation_labeled),
        ),
        "verifier_hallucination_rate": rate(
            sum(1 for item in results if item["verifier_hallucination"] is True),
            total,
        ),
        "failed_check_counts": dict(sorted(checks.items())),
        "results": results,
    }


def case_identifier(raw: dict[str, Any]) -> str:
    value = raw.get("id") or raw.get("case_id") or raw.get("request_id")
    if value:
        return str(value)
    return "generation-case"


def response_text(raw: dict[str, Any]) -> str:
    value = (
        raw.get("response")
        or raw.get("generated_response")
        or raw.get("final_response")
        or raw.get("answer")
        or ""
    )
    return str(value)


def source_context_ids(raw: dict[str, Any]) -> set[str]:
    ids = set(string_list(raw.get("cited_sources") or raw.get("cited_source_ids")))
    for field in ("sources", "source_chunks", "reranked_chunks", "retrieved_chunks"):
        for item in raw.get(field) or []:
            if isinstance(item, dict):
                value = item.get("chunk_id") or item.get("id")
            else:
                value = item
            if value:
                ids.add(str(value))
    return ids


def cited_source_ids(raw: dict[str, Any], response: str) -> set[str]:
    ids = set(string_list(raw.get("cited_sources") or raw.get("cited_source_ids")))
    ids.update(SOURCE_RE.findall(response))
    return ids


def expected_chunk_hit_check(
    expected_chunk_ids: list[str],
    cited_ids: set[str],
    source_ids: set[str],
) -> bool | None:
    if not expected_chunk_ids:
        return None
    observed = cited_ids or source_ids
    return bool(set(expected_chunk_ids) & observed)


def verifier_hallucination(raw: dict[str, Any]) -> bool | None:
    verifier = raw.get("verifier_result")
    if isinstance(verifier, str):
        try:
            verifier = json.loads(verifier)
        except json.JSONDecodeError:
            return None
    if isinstance(verifier, dict) and "has_hallucination" in verifier:
        return bool(verifier["has_hallucination"])
    if "verifier_hallucination" in raw:
        return optional_bool(raw.get("verifier_hallucination"))
    return None


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return bool(value)


def rate(count: int, total: int) -> float | None:
    if total == 0:
        return None
    return round(count / total, 6)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Generation Eval Report",
        "",
        f"- Cases: `{report['cases_total']}`",
        f"- Pass rate: `{format_rate(report['pass_rate'])}`",
        f"- Source context rate: `{format_rate(report['source_context_rate'])}`",
        f"- Expected chunk hit rate: `{format_rate(report['expected_chunk_hit_rate'])}`",
        f"- Escalation match rate: `{format_rate(report['escalation_match_rate'])}`",
        f"- Verifier hallucination rate: `{format_rate(report['verifier_hallucination_rate'])}`",
        "",
        "## Failed Checks",
        "",
        "| Check | Count |",
        "|---|---:|",
    ]
    failed = report.get("failed_check_counts") or {}
    if failed:
        lines.extend(f"| {name} | {count} |" for name, count in failed.items())
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Failed Cases",
            "",
            "| Case | Failed checks |",
            "|---|---|",
        ]
    )
    failed_cases = [item for item in report["results"] if not item["passed"]]
    if failed_cases:
        for item in failed_cases[:50]:
            failed_checks = [
                name for name, value in item["checks"].items() if value is False
            ]
            lines.append(f"| {item['id']} | {', '.join(failed_checks)} |")
    else:
        lines.append("| none | none |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate grounded generation results without sending data to an LLM."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markdown", type=Path, default=Path("reports/generation_eval.md"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run_generation_eval(
        cases_path=args.cases,
        output_path=args.output,
        markdown_path=args.markdown,
    )
    summary = {key: value for key, value in report.items() if key != "results"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
