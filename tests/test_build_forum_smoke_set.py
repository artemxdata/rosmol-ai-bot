from __future__ import annotations

import json
from pathlib import Path

from eval.build_forum_smoke_set import build_forum_smoke_set


def test_build_forum_smoke_set_covers_each_forum(tmp_path: Path) -> None:
    kb_seed = tmp_path / "kb.json"
    output = tmp_path / "forum_cases.json"
    kb_seed.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "mashuk_docs",
                    "status": "published",
                    "category": "форумы",
                    "forum_normalized": "Машук",
                    "intent_name": "Документы",
                    "intent_examples": ["какие документы нужны"],
                },
                {
                    "chunk_id": "mashuk_old",
                    "status": "archived",
                    "category": "форумы",
                    "forum_normalized": "Машук",
                    "intent_name": "Старый ответ",
                },
                {
                    "chunk_id": "utro_transfer",
                    "status": "published",
                    "category": "форумы",
                    "forum_normalized": "Утро",
                    "intent_name": "Трансфер",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = build_forum_smoke_set(kb_seed, output, per_forum=1)

    cases = json.loads(output.read_text(encoding="utf-8"))
    assert summary["cases_total"] == 2
    assert summary["forums_total"] == 2
    assert [case["expected_chunk_ids"][0] for case in cases] == [
        "mashuk_docs",
        "utro_transfer",
    ]
    assert cases[0]["query"] == "Машук какие документы нужны"
    assert cases[0]["expected_escalated"] is False
