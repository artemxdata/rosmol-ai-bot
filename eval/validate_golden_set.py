from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VALID_DIFFICULTIES = {"simple", "medium", "complex"}


@dataclass(frozen=True)
class GoldenValidationConfig:
    min_cases: int = 50
    require_expected_chunks: bool = True
    require_known_chunks: bool = True
    warn_without_reference_answer: bool = True


def validate_golden_set(
    cases: list[dict[str, Any]],
    *,
    kb_records: list[dict[str, Any]] | None = None,
    config: GoldenValidationConfig | None = None,
) -> dict[str, Any]:
    config = config or GoldenValidationConfig()
    known_chunk_ids = {
        str(record.get("chunk_id"))
        for record in kb_records or []
        if record.get("chunk_id")
    }
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    ids: set[str] = set()
    category_counts: Counter[str] = Counter()
    forum_counts: Counter[str] = Counter()
    difficulty_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()

    if len(cases) < config.min_cases:
        errors.append(
            _issue(
                "min_cases",
                f"golden set has {len(cases)} cases, expected at least {config.min_cases}",
            )
        )

    for index, raw_case in enumerate(cases, start=1):
        case_id = _clean(raw_case.get("id") or raw_case.get("case_id"))
        query = _clean(raw_case.get("query") or raw_case.get("question") or raw_case.get("text"))
        expected_chunks = _expected_chunks(raw_case)
        category = _clean(raw_case.get("category") or raw_case.get("expected_category"))
        forum = _clean(raw_case.get("forum_normalized") or raw_case.get("expected_forum"))
        difficulty = _clean(raw_case.get("difficulty"))
        source = _clean(raw_case.get("source"))

        if not case_id:
            errors.append(_case_issue(index, "", "missing_id", "case id is required"))
        elif case_id in ids:
            errors.append(_case_issue(index, case_id, "duplicate_id", "case id must be unique"))
        else:
            ids.add(case_id)

        if not query:
            errors.append(
                _case_issue(
                    index,
                    case_id,
                    "missing_query",
                    "query/question/text is required",
                )
            )

        if config.require_expected_chunks and not expected_chunks:
            errors.append(
                _case_issue(
                    index,
                    case_id,
                    "missing_expected_chunks",
                    "expected_chunk_ids/expected_chunks is required",
                )
            )

        if config.require_known_chunks and known_chunk_ids and expected_chunks:
            unknown = [chunk_id for chunk_id in expected_chunks if chunk_id not in known_chunk_ids]
            if unknown:
                errors.append(
                    _case_issue(
                        index,
                        case_id,
                        "unknown_expected_chunks",
                        f"unknown chunk ids: {', '.join(unknown)}",
                    )
                )

        if difficulty and difficulty not in VALID_DIFFICULTIES:
            warnings.append(
                _case_issue(
                    index,
                    case_id,
                    "unknown_difficulty",
                    f"difficulty should be one of {sorted(VALID_DIFFICULTIES)}",
                )
            )

        if config.warn_without_reference_answer and not _clean(raw_case.get("reference_answer")):
            warnings.append(
                _case_issue(
                    index,
                    case_id,
                    "missing_reference_answer",
                    "reference_answer is recommended for generation quality review",
                )
            )

        category_counts[category or "unknown"] += 1
        forum_counts[forum or "unknown"] += 1
        difficulty_counts[difficulty or "unknown"] += 1
        source_counts[source or "unknown"] += 1

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "valid": not errors,
        "cases_total": len(cases),
        "cases_with_expected_chunks": sum(1 for case in cases if _expected_chunks(case)),
        "unique_ids": len(ids),
        "errors_total": len(errors),
        "warnings_total": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "category_counts": dict(category_counts),
        "forum_counts_top": dict(forum_counts.most_common(25)),
        "difficulty_counts": dict(difficulty_counts),
        "source_counts": dict(source_counts),
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Golden Set Validation",
        "",
        f"- Valid: `{report['valid']}`",
        f"- Cases: `{report['cases_total']}`",
        f"- Cases with expected chunks: `{report['cases_with_expected_chunks']}`",
        f"- Unique IDs: `{report['unique_ids']}`",
        f"- Errors: `{report['errors_total']}`",
        f"- Warnings: `{report['warnings_total']}`",
        "",
        "## Distribution",
        "",
        f"- Categories: `{json.dumps(report['category_counts'], ensure_ascii=False)}`",
        f"- Difficulties: `{json.dumps(report['difficulty_counts'], ensure_ascii=False)}`",
        f"- Sources: `{json.dumps(report['source_counts'], ensure_ascii=False)}`",
    ]
    if report["errors"]:
        lines.extend(["", "## Errors", ""])
        lines.extend(_issue_lines(report["errors"]))
    if report["warnings"]:
        lines.extend(["", "## Warnings", ""])
        lines.extend(_issue_lines(report["warnings"][:50]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _issue_lines(issues: list[dict[str, Any]]) -> list[str]:
    return [
        f"- `{issue.get('code')}` case=`{issue.get('case_id')}` index=`{issue.get('index')}`: "
        f"{issue.get('message')}"
        for issue in issues
    ]


def _issue(code: str, message: str) -> dict[str, Any]:
    return {"index": None, "case_id": None, "code": code, "message": message}


def _case_issue(index: int, case_id: str, code: str, message: str) -> dict[str, Any]:
    return {"index": index, "case_id": case_id or None, "code": code, "message": message}


def _expected_chunks(raw_case: dict[str, Any]) -> list[str]:
    value = (
        raw_case.get("expected_chunk_ids")
        or raw_case.get("expected_chunks")
        or raw_case.get("relevant_chunk_ids")
        or []
    )
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in value if str(item).strip()]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"file must contain a JSON array: {path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", default="data/golden_set.json")
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
    parser.add_argument("--output", default="reports/golden_set_validation.json")
    parser.add_argument("--markdown", default="")
    parser.add_argument("--min-cases", type=int, default=50)
    parser.add_argument("--allow-unknown-chunks", action="store_true")
    parser.add_argument("--allow-missing-expected-chunks", action="store_true")
    parser.add_argument("--allow-missing-reference-answer", action="store_true")
    parser.add_argument("--no-fail", action="store_true")
    args = parser.parse_args()

    report = validate_golden_set(
        _read_json_array(Path(args.golden)),
        kb_records=_read_json_array(Path(args.kb_seed)),
        config=GoldenValidationConfig(
            min_cases=args.min_cases,
            require_expected_chunks=not args.allow_missing_expected_chunks,
            require_known_chunks=not args.allow_unknown_chunks,
            warn_without_reference_answer=not args.allow_missing_reference_answer,
        ),
    )
    output_path = Path(args.output)
    write_report(output_path, report)
    markdown_path = Path(args.markdown) if args.markdown else output_path.with_suffix(".md")
    write_markdown(markdown_path, report)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key not in {"errors", "warnings"}},
            ensure_ascii=False,
            indent=2,
        )
    )
    if not report["valid"] and not args.no_fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
