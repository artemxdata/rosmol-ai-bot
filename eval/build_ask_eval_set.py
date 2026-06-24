from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.ask_cases import build_seed_ask_cases, summarize_cases, write_cases


def build_eval_set(
    kb_seed_path: Path,
    output_path: Path,
    max_cases: int,
    per_category_limit: int | None,
    per_forum_limit: int,
    source_type_limits: dict[str, int] | None = None,
    require_cited_chunks: bool = False,
) -> dict:
    records = json.loads(kb_seed_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("KB seed must be a JSON array")

    cases = build_seed_ask_cases(
        records,
        max_cases=max_cases,
        per_category_limit=per_category_limit,
        per_forum_limit=per_forum_limit,
        source_type_limits=source_type_limits,
        require_cited_chunks=require_cited_chunks,
    )
    write_cases(output_path, cases)
    summary = summarize_cases(cases)
    summary["output"] = str(output_path)
    return summary


def parse_source_type_limits(value: str) -> dict[str, int] | None:
    value = value.strip()
    if not value:
        return None
    limits: dict[str, int] = {}
    for item in value.split(","):
        key, separator, raw_limit = item.partition("=")
        source_type = key.strip()
        if not separator or not source_type:
            raise ValueError(
                "source type limits must use comma-separated source_type=count pairs"
            )
        try:
            limit = int(raw_limit.strip())
        except ValueError as exc:
            raise ValueError(f"invalid source type limit for {source_type}") from exc
        if limit < 0:
            raise ValueError(f"source type limit must be non-negative: {source_type}")
        limits[source_type] = limit
    return limits


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
    parser.add_argument("--output", default="reports/ask_eval_set.generated.json")
    parser.add_argument("--max-cases", type=int, default=100)
    parser.add_argument("--per-category-limit", type=int, default=None)
    parser.add_argument("--per-forum-limit", type=int, default=3)
    parser.add_argument(
        "--source-type-limits",
        default="",
        help=(
            "Comma-separated source_type=count quotas, for example "
            "ticket_answer_bank=100,xlsx=45,docx=15."
        ),
    )
    parser.add_argument(
        "--require-cited-chunks",
        action="store_true",
        help="Require every generated case to cite its expected chunk in ask eval scoring.",
    )
    args = parser.parse_args()

    summary = build_eval_set(
        kb_seed_path=Path(args.kb_seed),
        output_path=Path(args.output),
        max_cases=args.max_cases,
        per_category_limit=args.per_category_limit,
        per_forum_limit=args.per_forum_limit,
        source_type_limits=parse_source_type_limits(args.source_type_limits),
        require_cited_chunks=args.require_cited_chunks,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
