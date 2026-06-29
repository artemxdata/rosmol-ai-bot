from __future__ import annotations

import json
from pathlib import Path

from eval.pre_pilot_cases import build_pre_pilot_case_sets


def test_build_pre_pilot_case_sets_writes_separate_sections(tmp_path: Path) -> None:
    paths = build_pre_pilot_case_sets(
        kb_seed_path=Path("data/knowledge_base_seed.json"),
        output_dir=tmp_path,
    )

    assert set(paths) == {"forums", "safety", "off_topic", "pii", "followup", "all_ask"}
    for path in paths.values():
        assert path.exists()

    forums = json.loads(paths["forums"].read_text(encoding="utf-8"))
    safety = json.loads(paths["safety"].read_text(encoding="utf-8"))
    off_topic = json.loads(paths["off_topic"].read_text(encoding="utf-8"))
    pii = json.loads(paths["pii"].read_text(encoding="utf-8"))
    followup = json.loads(paths["followup"].read_text(encoding="utf-8"))
    all_ask = json.loads(paths["all_ask"].read_text(encoding="utf-8"))

    assert len(forums) >= 10
    assert len(safety) >= 8
    assert len(off_topic) >= 8
    assert len(pii) >= 4
    assert len(followup) >= 3
    assert len(all_ask) == len(forums) + len(safety) + len(off_topic) + len(pii)
    assert all(case["expected_behavior"] == "escalate" for case in safety)
    assert all(case["expected_behavior"] == "scope_note" for case in off_topic)
    assert any(case["forbidden_message_masked_contains"] for case in pii)
    assert all("turns" in case for case in followup)

    bctp_case = next(case for case in forums if case["id"] == "forum_bctp_family_transfer_food")
    equivalent_ids = {
        item
        for values in bctp_case["equivalent_chunk_ids"].values()
        for item in values
    }
    assert "xlsx_category_r0627_transfer_do_mesta_provedeniya_meropriyatiya" in equivalent_ids
