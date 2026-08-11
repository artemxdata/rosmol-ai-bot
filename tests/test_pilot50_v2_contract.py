from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from eval.run_ask import _normalize_answer_contains_text
from scripts import pilot50

V1_MANIFEST = pilot50.PROJECT_ROOT / "eval/cases/pilot50_balanced_v1.json"
V2_MANIFEST = pilot50.PROJECT_ROOT / "eval/cases/pilot50_balanced_v2.json"
V2_SOURCE = pilot50.PROJECT_ROOT / "eval/cases/pilot50_atypical_yonote_v2.json"
SEED = pilot50.PROJECT_ROOT / "data/knowledge_base_seed.json"

V1_MANIFEST_SHA256 = "d6f38ee2a7c95c6b558c55d0a6e5f67bd3fa92fac6b954bb0c2b23a88d322ca7"
V1_CASES_SHA256 = "65da11ebc790b37e0b8e5dff2601f6cc2cd3956d17652f7d74ab95eb1c21c6ed"
V2_MANIFEST_SHA256 = "6995b96b4658f53e40a0bb982145465cbc347d9df041fc4dd66a9d15687b822b"
V2_CASES_SHA256 = "b027e469e062682b6dc341b2dd4c87440edffb1955c2111f38e6c44a92a3a14d"
V2_SOURCE_CANONICAL_SHA256 = (
    "5a13dbeb95442295dc6d3367a7240fa2b27072a5060e4649e3a84d9cac44dff0"
)
V2_NEW_IDS = [
    "pilot50_v2_fgais_registration_navigation",
    "pilot50_v2_fgais_lost_email_statuses",
    "pilot50_v2_ladoga_deadline_food_travel",
    "pilot50_v2_patriot_deadline_participants",
    "pilot50_v2_territory_overview_shifts",
    "pilot50_v2_grant_directions_application",
    "pilot50_v2_grant_review_timelines",
    "pilot50_v2_dobro_registration_volunteering",
    "pilot50_v2_mashuk_results_program",
    "pilot50_v2_mashuk_shift_dates",
    "pilot50_v2_territory_forum_pravda_dates",
]


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_v2_replaces_exactly_the_invalid_v1_block_and_keeps_order() -> None:
    v1_cases, v1_receipt = pilot50.build_materialized_cases(V1_MANIFEST)
    v2_cases, v2_receipt = pilot50.build_materialized_cases(V2_MANIFEST)
    v1_ids = [case["id"] for case in v1_cases]
    v2_ids = [case["id"] for case in v2_cases]

    assert v1_receipt["manifest_sha256"] == V1_MANIFEST_SHA256
    assert v1_receipt["cases_sha256"] == V1_CASES_SHA256
    assert v2_receipt == {
        "status": "OK",
        "operation": "prepare",
        "dataset_id": "pilot50_balanced_v2",
        "cases_total": 50,
        "type_counts": {"typical": 25, "atypical": 25},
        "expected_behavior": "answer",
        "expected_escalated": False,
        "manifest_sha256": V2_MANIFEST_SHA256,
        "cases_sha256": V2_CASES_SHA256,
    }
    assert v2_ids[:25] == v1_ids[:25]
    assert v2_ids[25:36] == V2_NEW_IDS
    assert v2_ids[36:] == v1_ids[36:]
    assert set(v2_ids) - set(v1_ids) == set(V2_NEW_IDS)
    assert len(set(v1_ids) - set(v2_ids)) == 11


def test_v2_materialization_has_versioned_identity_and_no_private_markers() -> None:
    cases, _receipt = pilot50.build_materialized_cases(V2_MANIFEST)
    normalized_queries = [" ".join(case["query"].casefold().split()) for case in cases]
    user_ids = [case["user_id"] for case in cases]

    assert len(cases) == 50
    assert len({case["id"] for case in cases}) == 50
    assert len(set(normalized_queries)) == 50
    assert len(set(user_ids)) == 50
    assert user_ids == [f"pilot50-v2-{ordinal:02d}" for ordinal in range(1, 51)]
    assert sum(case["pilot50_group"] == "typical" for case in cases) == 25
    assert sum(case["pilot50_group"] == "atypical" for case in cases) == 25
    for case in cases:
        group = case["pilot50_group"]
        assert case["privacy_class"] == "standard"
        assert case["expected_behavior"] == "answer"
        assert case["expected_escalated"] is False
        assert pilot50._query_has_pii(case["query"]) is False
        assert str(case.get("split") or "").casefold() != "holdout"
        assert "pilot50:v2" in case["tags"]
        assert "pilot50:v1" not in case["tags"]
        assert f"type:{group}" in case["tags"]
        assert not any("holdout" in tag.casefold() for tag in case["tags"])
        assert not (pilot50.FORBIDDEN_CASE_FIELDS & set(case))


def test_v2_qrels_are_only_published_yonote_in_the_frozen_seed() -> None:
    cases, _receipt = pilot50.build_materialized_cases(V2_MANIFEST)
    seed_rows = _load_json(SEED)
    assert isinstance(seed_rows, list)
    seed_by_id = {row["chunk_id"]: row for row in seed_rows}
    qrel_slots = 0

    for case in cases:
        qrel_ids = pilot50._qrel_ids_from_case(case)
        qrel_slots += len(case.get("expected_chunk_ids") or [])
        qrel_slots += len(case.get("expected_cited_chunk_ids") or [])
        for qrel_id in qrel_ids:
            source = seed_by_id[qrel_id]
            assert qrel_id.startswith("yonote_api_")
            assert source["status"] == "published"
            assert source["source_type"] == "yonote"

    new_cases = cases[25:36]
    assert all(len(case["expected_chunk_ids"]) >= 2 for case in new_cases)
    assert all(
        case["expected_cited_chunk_ids"] == case["expected_chunk_ids"]
        for case in new_cases
    )
    assert all(case["allowed_cited_source_types"] == ["yonote"] for case in new_cases)
    for case in new_cases:
        evidence = _normalize_answer_contains_text("\n".join(
            str(seed_by_id[qrel_id]["text_clean"])
            for qrel_id in case["expected_chunk_ids"]
        ))
        for expected_text in case["expected_answer_contains"]:
            assert _normalize_answer_contains_text(expected_text) in evidence
    assert sum(len(case["expected_chunk_ids"]) for case in new_cases) == 23
    assert qrel_slots == 100


@pytest.mark.parametrize(
    "qrel_id",
    [
        "xlsx_category_r0001_vernut_denezhnye_sredstva",
        "yonote_api_gkby3eml8d_s0032_socseti_vk_tg",
        "yonote_api_missing_pilot50_v2_qrel",
    ],
)
def test_v2_qrel_audit_fails_closed_for_legacy_archived_or_missing(
    qrel_id: str,
) -> None:
    with pytest.raises(
        pilot50.Pilot50Error,
        match="not a published Yonote source",
    ):
        pilot50._validate_published_yonote_qrels(
            [
                {
                    "expected_chunk_ids": [qrel_id],
                    "expected_cited_chunk_ids": [qrel_id],
                }
            ]
        )


def test_v2_manifest_and_source_are_frozen_by_canonical_hash() -> None:
    manifest = _load_json(V2_MANIFEST)
    source = _load_json(V2_SOURCE)
    assert isinstance(manifest, dict)
    assert isinstance(source, list)

    assert hashlib.sha256(V2_MANIFEST.read_bytes()).hexdigest() == V2_MANIFEST_SHA256
    assert (
        hashlib.sha256(pilot50._canonical_json_bytes(manifest)).hexdigest()
        == pilot50.V2_EXPECTED_MANIFEST_CANONICAL_SHA256
    )
    assert (
        hashlib.sha256(pilot50._canonical_json_bytes(source)).hexdigest()
        == V2_SOURCE_CANONICAL_SHA256
    )
    v2_source = next(
        item
        for item in manifest["sources"]
        if item["path"] == "eval/cases/pilot50_atypical_yonote_v2.json"
    )
    assert v2_source["sha256"] == V2_SOURCE_CANONICAL_SHA256
    assert v2_source["case_ids"] == V2_NEW_IDS
