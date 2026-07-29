from __future__ import annotations

import json
from pathlib import Path

from eval.run_ask import _normalize_case

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = (
    PROJECT_ROOT
    / "eval"
    / "cases"
    / "product_date_aspect_regression_v1.json"
)
KB_SEED_PATH = PROJECT_ROOT / "data" / "knowledge_base_seed.json"


def test_date_aspect_regression_is_grounded_and_off_aspect_guarded() -> None:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    kb_records = json.loads(KB_SEED_PATH.read_text(encoding="utf-8-sig"))
    kb_by_id = {
        str(record["chunk_id"]): record
        for record in kb_records
        if isinstance(record, dict) and record.get("chunk_id")
    }

    assert len(cases) == 2
    assert len({case["id"] for case in cases}) == 2
    assert {case["id"] for case in cases} == {
        "synthetic_mashuk_first_shift_dates_only",
        "synthetic_pravda_shift_dates_only",
    }
    for case in cases:
        assert case["privacy_class"] == "standard"
        assert case["label_status"] == "synthetic_curated"
        assert case["requires_human_review"] is False
        assert case["split"] == "calibration"
        assert case["dataset_kind"] == "synthetic_aspect_regression"
        assert case["evidence_scope"] == "directional_calibration_only"
        assert case["expected_answer_contains"]
        assert case["allowed_cited_source_types"] == ["yonote"]
        assert case["expected_cited_chunk_ids"] == case["expected_chunk_ids"]
        for chunk_id in case["expected_chunk_ids"]:
            record = kb_by_id[chunk_id]
            assert record.get("status") == "published"
            assert record.get("source_type") == "yonote"

        executable = _normalize_case(case)
        assert executable["expected_behavior"] == "answer"
        assert executable["expected_response_profile"] == "dates"
        assert executable["allowed_cited_source_types"] == ["yonote"]
        assert {
            "application",
            "selection_status",
            "travel",
        } <= set(executable["forbidden_response_profiles"])
