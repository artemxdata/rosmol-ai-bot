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
) -> dict:
    records = json.loads(kb_seed_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("KB seed must be a JSON array")

    cases = build_seed_ask_cases(
        records,
        max_cases=max_cases,
        per_category_limit=per_category_limit,
        per_forum_limit=per_forum_limit,
    )
    write_cases(output_path, cases)
    summary = summarize_cases(cases)
    summary["output"] = str(output_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
    parser.add_argument("--output", default="reports/ask_eval_set.generated.json")
    parser.add_argument("--max-cases", type=int, default=100)
    parser.add_argument("--per-category-limit", type=int, default=None)
    parser.add_argument("--per-forum-limit", type=int, default=3)
    args = parser.parse_args()

    summary = build_eval_set(
        kb_seed_path=Path(args.kb_seed),
        output_path=Path(args.output),
        max_cases=args.max_cases,
        per_category_limit=args.per_category_limit,
        per_forum_limit=args.per_forum_limit,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
