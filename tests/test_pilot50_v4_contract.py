from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from eval.run_ask import _normalize_answer_contains_text, _normalize_case
from scripts import build_pilot50_v4_contract as builder
from scripts import pilot50

V3_MANIFEST = pilot50.PROJECT_ROOT / "eval/cases/pilot50_balanced_v3.json"
V4_MANIFEST = pilot50.PROJECT_ROOT / "eval/cases/pilot50_balanced_v4.json"
SEED = pilot50.PROJECT_ROOT / "data/knowledge_base_seed.json"

V3_RAW_SHA256 = "fef1caa227777e2c198bd6acdc77471fbf2551732c85e2334f8cad781025e875"
V3_CASES_SHA256 = "3c76d0de9a31cf3a36a38346d38fa75d5173ac2b452ddcbf60c551678580d112"
V4_MANIFEST_SHA256 = "bfd14ae638da0d65b2c07ff299f8f366a2d8fb8be772223a931e601691125ede"
V4_CASES_SHA256 = "c88a52225f6eec3b21a5837a94f12670f5a8ff1006818f559cb81e438d52fab8"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_v4_builder_exactly_reproduces_separate_frozen_contract() -> None:
    typical, atypical, manifest = builder.build_contract()

    assert builder._canonical_bytes(typical) == builder.TYPICAL_PATH.read_bytes()
    assert builder._canonical_bytes(atypical) == builder.ATYPICAL_PATH.read_bytes()
    assert builder._canonical_bytes(manifest) == builder.MANIFEST_PATH.read_bytes()
    assert hashlib.sha256(builder.MANIFEST_PATH.read_bytes()).hexdigest() == (
        V4_MANIFEST_SHA256
    )

    cases, receipt = pilot50.build_materialized_cases(V4_MANIFEST)
    assert receipt == {
        "status": "OK",
        "operation": "prepare",
        "dataset_id": "pilot50_balanced_v4",
        "cases_total": 50,
        "type_counts": {"typical": 25, "atypical": 25},
        "expected_behavior": "answer",
        "expected_escalated": False,
        "manifest_sha256": V4_MANIFEST_SHA256,
        "cases_sha256": V4_CASES_SHA256,
    }
    assert len(cases) == 50


def test_v4_has_50_qrels_15_critical_and_only_typed_semantic_groups() -> None:
    cases, _receipt = pilot50.build_materialized_cases(V4_MANIFEST)

    assert [case["user_id"] for case in cases] == [
        f"pilot50-v4-{ordinal:02d}" for ordinal in range(1, 51)
    ]
    assert sum(case["pilot50_group"] == "typical" for case in cases) == 25
    assert sum(case["pilot50_group"] == "atypical" for case in cases) == 25
    assert sum(bool(pilot50._qrel_ids_from_case(case)) for case in cases) == 50
    assert sum(pilot50._candidate_case_is_critical(case) for case in cases) == 15

    allowed_kinds = {"text_any", "date", "date_range", "time", "number"}
    for case in cases:
        assert "expected_answer_contains" not in case
        groups = case.get("expected_answer_fact_groups")
        assert isinstance(groups, list) and groups
        assert {group["kind"] for group in groups} <= allowed_kinds
        assert _normalize_case(case)["expected_answer_fact_groups"] == groups
        for group in groups:
            if group["kind"] != "text_any":
                continue
            assert any(
                len(re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", alternative)) >= 2
                for alternative in group["alternatives"]
            )


def test_v4_qrels_and_declared_equivalents_are_exact_published_yonote() -> None:
    cases, _receipt = pilot50.build_materialized_cases(V4_MANIFEST)
    seed_rows = _load(SEED)
    seed_by_id = {row["chunk_id"]: row for row in seed_rows}
    declared_pairs: list[tuple[str, str]] = []

    for case in cases:
        for qrel_id in pilot50._qrel_ids_from_case(case):
            source = seed_by_id[qrel_id]
            assert source["status"] == "published"
            assert source["source_type"] == "yonote"
            assert source["source"] == "yonote_api"
            assert source["version"] == "yonote-api-v1"
        for expected, equivalents in case.get("equivalent_chunk_ids", {}).items():
            for equivalent in equivalents:
                declared_pairs.append((expected, equivalent))
                assert seed_by_id[expected]["text_clean"] == seed_by_id[equivalent][
                    "text_clean"
                ]

    assert declared_pairs == [
        (builder.GRANT_APPLICATION_CHUNK, builder.GRANT_APPLICATION_DUPLICATE),
        (builder.GRANT_APPLICATION_CHUNK, builder.GRANT_APPLICATION_DUPLICATE),
    ]


def test_v4_repairs_nomination_and_temporal_contracts() -> None:
    cases, _receipt = pilot50.build_materialized_cases(V4_MANIFEST)
    seed_by_id = {row["chunk_id"]: row for row in _load(SEED)}

    nomination = cases[15]
    assert nomination["id"] == "pilot50_v4_grant_nomination_definition"
    assert "перечисл" not in nomination["query"].casefold()
    assert "сколько стандартных номинаций" in nomination["query"].casefold()
    nomination_evidence = _normalize_answer_contains_text(
        "\n".join(
            seed_by_id[qrel_id]["text_clean"]
            for qrel_id in pilot50._qrel_ids_from_case(nomination)
        )
    )
    assert "тематика проекта" in nomination_evidence
    assert re.search(r"\b18\b", nomination_evidence)

    temporal = [case for case in cases if case.get("temporal_as_of_date")]
    assert len(temporal) == 8
    assert {case["temporal_as_of_date"] for case in temporal} == {"2026-08-14"}
    assert Counter(case["expected_temporal_polarity"] for case in temporal) == {
        "closed": 6,
        "completed": 1,
        "in_progress": 1,
    }
    assert cases[48]["expected_answer_fact_groups"] == [
        {
            "kind": "date_range",
            "start": "2026-07-26",
            "end": "2026-07-30",
            "context_any": ["смена Правда", "Правда"],
            "context_position": "before",
        }
    ]
    assert cases[49]["expected_answer_fact_groups"] == [
        {
            "kind": "date_range",
            "start": "2026-08-08",
            "end": "2026-08-15",
            "context_any": ["первая смена"],
            "context_position": "before",
        }
    ]


def test_v4_does_not_mutate_sealed_v3_identity() -> None:
    assert hashlib.sha256(V3_MANIFEST.read_bytes()).hexdigest() == V3_RAW_SHA256
    _cases, receipt = pilot50.build_materialized_cases(V3_MANIFEST)
    assert receipt["cases_sha256"] == V3_CASES_SHA256
    assert pilot50.V3_CANDIDATE_CASES_SHA256 == V3_CASES_SHA256
    assert pilot50._candidate_contract_config(pilot50.V4_DATASET_ID) == {
        "contract_id": "pilot50-v4-candidate-v1",
        "cases_sha256": V4_CASES_SHA256,
        "cost_scope": "pilot50-v4-candidate",
        "quality_gate_schema_version": "pilot50-v4-quality-gate-v1",
        "expected_qrel_cases": 50,
        "expected_critical_cases": 15,
    }
