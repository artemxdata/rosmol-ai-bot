from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.rag.retriever import _keyword_haystack, _keyword_score, _keyword_tokens


def audit_expected_chunks(
    cases_path: Path,
    kb_seed_path: Path,
    *,
    max_rank: int = 10,
    min_score: float = 0.05,
) -> dict[str, Any]:
    cases = _read_json_array(cases_path)
    records = _read_json_array(kb_seed_path)
    chunks = {
        str(record.get("chunk_id")): record
        for record in records
        if record.get("chunk_id") and record.get("status") in (None, "", "published")
    }

    rows = []
    for case in cases:
        expected_ids = _string_list(
            case.get("expected_chunk_ids") or case.get("expected_chunks") or []
        )
        if not expected_ids:
            continue

        query = str(case.get("query") or "")
        query_tokens = _keyword_tokens(query)
        ranked = _rank_chunks(query, query_tokens, list(chunks.values()))
        rank_by_id = {chunk_id: index for index, (_score, chunk_id) in enumerate(ranked, start=1)}
        score_by_id = {chunk_id: score for score, chunk_id in ranked}

        for expected_id in expected_ids:
            row = _audit_expected_id(
                case_id=str(case.get("id") or ""),
                expected_id=expected_id,
                known_chunks=chunks,
                rank_by_id=rank_by_id,
                score_by_id=score_by_id,
                top_chunk_ids=[chunk_id for _score, chunk_id in ranked[:5]],
                max_rank=max_rank,
                min_score=min_score,
            )
            rows.append(row)

    reason_counts = Counter(row["reason"] for row in rows)
    passed = sum(row["ok"] for row in rows)
    return {
        "cases_path": str(cases_path),
        "kb_seed_path": str(kb_seed_path),
        "checked_expected_chunks": len(rows),
        "passed": passed,
        "pass_rate": _rate(passed, len(rows)),
        "reason_counts": dict(reason_counts),
        "rows": rows,
    }


def write_markdown(report: dict[str, Any], output_path: Path) -> None:
    lines = [
        "# Expected Chunk Label Audit",
        "",
        f"- Cases: `{report['cases_path']}`",
        f"- KB seed: `{report['kb_seed_path']}`",
        f"- Checked expected chunks: `{report['checked_expected_chunks']}`",
        f"- Passed: `{report['passed']}`",
        f"- Pass rate: `{_format_rate(report['pass_rate'])}`",
        "",
        "## Reason Counts",
        "",
    ]
    lines.extend(
        f"- `{count}` {reason}" for reason, count in report["reason_counts"].items()
    )
    failed_rows = [row for row in report["rows"] if not row["ok"]]
    if failed_rows:
        lines.extend(["", "## Failed Labels", ""])
        for row in failed_rows[:100]:
            top_ids = ", ".join(row["top_chunk_ids"])
            lines.append(
                f"- `{row['case_id']}` expected=`{row['expected_id']}` "
                f"rank=`{row['rank']}` score=`{row['score']}` "
                f"reason=`{row['reason']}` top=`{top_ids}`"
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def filter_cases_passing_audit(
    cases: list[dict[str, Any]],
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    audited_case_ids = {row["case_id"] for row in report["rows"]}
    failed_case_ids = {row["case_id"] for row in report["rows"] if not row["ok"]}
    return [
        case
        for case in cases
        if str(case.get("id") or "") in audited_case_ids
        and str(case.get("id") or "") not in failed_case_ids
    ]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _audit_expected_id(
    *,
    case_id: str,
    expected_id: str,
    known_chunks: dict[str, dict[str, Any]],
    rank_by_id: dict[str, int],
    score_by_id: dict[str, float],
    top_chunk_ids: list[str],
    max_rank: int,
    min_score: float,
) -> dict[str, Any]:
    if expected_id not in known_chunks:
        return _row(
            case_id=case_id,
            expected_id=expected_id,
            ok=False,
            reason="missing_expected_chunk_in_kb",
            rank=None,
            score=None,
            top_chunk_ids=top_chunk_ids,
        )

    rank = rank_by_id.get(expected_id)
    score = score_by_id.get(expected_id, 0.0)
    if rank is None or score <= 0:
        return _row(
            case_id=case_id,
            expected_id=expected_id,
            ok=False,
            reason="expected_chunk_has_no_query_overlap",
            rank=rank,
            score=score,
            top_chunk_ids=top_chunk_ids,
        )
    if score < min_score:
        return _row(
            case_id=case_id,
            expected_id=expected_id,
            ok=False,
            reason="expected_chunk_score_too_low",
            rank=rank,
            score=score,
            top_chunk_ids=top_chunk_ids,
        )
    if rank > max_rank:
        return _row(
            case_id=case_id,
            expected_id=expected_id,
            ok=False,
            reason="expected_chunk_rank_too_low",
            rank=rank,
            score=score,
            top_chunk_ids=top_chunk_ids,
        )
    return _row(
        case_id=case_id,
        expected_id=expected_id,
        ok=True,
        reason="ok",
        rank=rank,
        score=score,
        top_chunk_ids=top_chunk_ids,
    )


def _rank_chunks(
    query: str,
    query_tokens: set[str],
    chunks: list[dict[str, Any]],
) -> list[tuple[float, str]]:
    rows = []
    for chunk in chunks:
        chunk_id = str(chunk.get("chunk_id") or "")
        score = _keyword_score(query, query_tokens, _keyword_haystack(chunk), chunk)
        if score > 0 and chunk_id:
            rows.append((score, chunk_id))
    return sorted(rows, reverse=True)


def _row(
    *,
    case_id: str,
    expected_id: str,
    ok: bool,
    reason: str,
    rank: int | None,
    score: float | None,
    top_chunk_ids: list[str],
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "expected_id": expected_id,
        "ok": ok,
        "reason": reason,
        "rank": rank,
        "score": round(score, 6) if isinstance(score, float) else score,
        "top_chunk_ids": top_chunk_ids,
    }


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"file must contain a JSON array: {path}")
    if not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"JSON array must contain only objects: {path}")
    return payload


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _format_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit whether expected chunk labels match case queries."
    )
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--kb-seed", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument("--passed-cases-output", type=Path, default=None)
    parser.add_argument("--max-rank", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=0.05)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = audit_expected_chunks(
        args.cases,
        args.kb_seed,
        max_rank=args.max_rank,
        min_score=args.min_score,
    )
    if args.output:
        write_json(args.output, report)
    if args.markdown:
        write_markdown(report, args.markdown)
    if args.passed_cases_output:
        cases = _read_json_array(args.cases)
        write_json(args.passed_cases_output, filter_cases_passing_audit(cases, report))
    summary = {key: value for key, value in report.items() if key != "rows"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
