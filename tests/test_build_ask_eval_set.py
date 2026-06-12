from __future__ import annotations

import json
from pathlib import Path

from eval.build_ask_eval_set import build_eval_set


def test_build_eval_set_writes_balanced_cases(tmp_path: Path) -> None:
    kb_seed = tmp_path / "kb.json"
    output = tmp_path / "ask_cases.json"
    kb_seed.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "forum_1",
                    "status": "published",
                    "category": "форумы",
                    "forum_normalized": "Машук",
                    "intent_examples": ["как подать заявку"],
                    "text_clean": "Ответ",
                },
                {
                    "chunk_id": "grant_1",
                    "status": "published",
                    "category": "гранты",
                    "intent_examples": ["как получить грант"],
                    "text_clean": "Ответ",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = build_eval_set(
        kb_seed_path=kb_seed,
        output_path=output,
        max_cases=10,
        per_category_limit=None,
        per_forum_limit=3,
    )

    cases = json.loads(output.read_text(encoding="utf-8"))
    assert summary["cases_total"] == 2
    assert {case["expected_chunk_ids"][0] for case in cases} == {"forum_1", "grant_1"}
