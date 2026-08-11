from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from eval import run_ask
from eval.run_ask import _normalize_answer_contains_text
from scripts import pilot50

V2_MANIFEST = pilot50.PROJECT_ROOT / "eval/cases/pilot50_balanced_v2.json"
V3_MANIFEST = pilot50.PROJECT_ROOT / "eval/cases/pilot50_balanced_v3.json"
V3_SOURCE = pilot50.PROJECT_ROOT / "eval/cases/pilot50_critical_yonote_v3.json"
SEED = pilot50.PROJECT_ROOT / "data/knowledge_base_seed.json"

V3_MANIFEST_SHA256 = "fef1caa227777e2c198bd6acdc77471fbf2551732c85e2334f8cad781025e875"
V3_CASES_SHA256 = "3c76d0de9a31cf3a36a38346d38fa75d5173ac2b452ddcbf60c551678580d112"
V3_SOURCE_CANONICAL_SHA256 = (
    "ee97ca78d090bd188f005e45e7aa084327156a9a29d122a3aa0ede383f864a4c"
)
V3_ENRICHED_IDS = [
    "adv_insult_actionable",
    "adv_answer_rostov_application",
    "adv_answer_rostov_slang",
    "adv_answer_grant_application",
    "adv_answer_fgais_registration",
    "adv_answer_forum_list",
    "adv_answer_shum_entry",
    "adv_answer_tavrida_entry",
    "adv_answer_profanity_with_request",
]
V3_REPLACEMENTS = [
    (
        "pilot50_v3_inactive_fgais_button",
        "Какого хрена кнопка “Подать заявку” во ФГАИС неактивна?",
        "yonote_api_u7b5sscrri_s0012_registraciya_na_municipalnoe_meropriyatie",
        ["недостающие данные", "даты регистрации"],
    ),
    (
        "pilot50_v3_youth_day_ticket_after_max_registration",
        "Блин, как получить билет на День молодёжи после регистрации через МАХ?",
        "yonote_api_nwr3m74g03_s0003_sposob_1_cherez_chat_bot_v_mah_https_max_ru_youthday_bot",
        ["код билета", "диалоге", "почту"],
    ),
    (
        "pilot50_v3_ladoga_application_deadline",
        "Блин, куда подать заявку на “Ладогу” и до какого срока?",
        "yonote_api_irwwd4t2v8_s0006_forum",
        ["30 июня 2026", "Росмолодёжь.Форумы"],
    ),
]
V3_HARDENED_DATE_IDS = [
    "synthetic_pravda_shift_dates_only",
    "synthetic_mashuk_first_shift_dates_only",
]


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _published_text_supports_anchor(anchor: str, evidence: str) -> bool:
    normalized_anchor = _normalize_answer_contains_text(anchor)
    if normalized_anchor in evidence:
        return True
    date_match = re.fullmatch(
        r"(?P<day>\d{1,2}) (?P<month>января|февраля|марта|апреля|мая|июня|"
        r"июля|августа|сентября|октября|ноября|декабря)",
        normalized_anchor,
    )
    if date_match is None:
        return False
    day = int(date_match.group("day"))
    month = date_match.group("month")
    for range_match in re.finditer(
        rf"(?P<start>\d{{1,2}})\s*-\s*(?P<end>\d{{1,2}}) {month}",
        evidence,
    ):
        if int(range_match.group("start")) <= day <= int(range_match.group("end")):
            return True
    return False


def test_v3_replaces_only_ordinals_46_through_48() -> None:
    v2_cases, v2_receipt = pilot50.build_materialized_cases(V2_MANIFEST)
    v3_cases, v3_receipt = pilot50.build_materialized_cases(V3_MANIFEST)

    assert v2_receipt["cases_sha256"] == pilot50.CANDIDATE_CASES_SHA256
    assert v3_receipt == {
        "status": "OK",
        "operation": "prepare",
        "dataset_id": "pilot50_balanced_v3",
        "cases_total": 50,
        "type_counts": {"typical": 25, "atypical": 25},
        "expected_behavior": "answer",
        "expected_escalated": False,
        "manifest_sha256": V3_MANIFEST_SHA256,
        "cases_sha256": V3_CASES_SHA256,
    }

    preserved_indexes = [*range(45), 48, 49]
    assert [v3_cases[index]["id"] for index in preserved_indexes] == [
        v2_cases[index]["id"] for index in preserved_indexes
    ]
    assert [v3_cases[index]["query"] for index in preserved_indexes] == [
        v2_cases[index]["query"] for index in preserved_indexes
    ]
    assert [v3_cases[index]["pilot50_group"] for index in range(50)] == [
        v2_cases[index]["pilot50_group"] for index in range(50)
    ]
    assert [
        (case["id"], case["query"])
        for case in v3_cases[45:48]
    ] == [(case_id, query) for case_id, query, _qrel, _anchors in V3_REPLACEMENTS]


def test_v3_has_versioned_identity_and_exact_quality_coverage() -> None:
    cases, _receipt = pilot50.build_materialized_cases(V3_MANIFEST)

    assert [case["user_id"] for case in cases] == [
        f"pilot50-v3-{ordinal:02d}" for ordinal in range(1, 51)
    ]
    assert sum(case["pilot50_group"] == "typical" for case in cases) == 25
    assert sum(case["pilot50_group"] == "atypical" for case in cases) == 25
    assert sum(
        bool(case.get("expected_chunk_ids") or case.get("expected_cited_chunk_ids"))
        for case in cases
    ) == 50
    assert sum(pilot50._candidate_case_is_critical(case) for case in cases) == 15
    for case in cases:
        assert case["privacy_class"] == "standard"
        assert case["expected_behavior"] == "answer"
        assert case["expected_escalated"] is False
        assert "pilot50:v3" in case["tags"]
        assert f"type:{case['pilot50_group']}" in case["tags"]


def test_v3_qrels_and_content_anchors_bind_published_yonote() -> None:
    cases, _receipt = pilot50.build_materialized_cases(V3_MANIFEST)
    seed_rows = _load_json(SEED)
    assert isinstance(seed_rows, list)
    seed_by_id = {row["chunk_id"]: row for row in seed_rows}

    qrel_cases = [
        case
        for case in cases
        if case.get("expected_chunk_ids") or case.get("expected_cited_chunk_ids")
    ]
    assert len(qrel_cases) == 50
    for case in qrel_cases:
        qrel_ids = pilot50._qrel_ids_from_case(case)
        anchors = case.get("expected_answer_contains")
        assert qrel_ids
        assert isinstance(anchors, list) and anchors
        evidence = _normalize_answer_contains_text(
            "\n".join(str(seed_by_id[qrel_id]["text_clean"]) for qrel_id in qrel_ids)
        )
        for qrel_id in qrel_ids:
            source = seed_by_id[qrel_id]
            assert qrel_id.startswith("yonote_api_")
            assert source["status"] == "published"
            assert source["source_type"] == "yonote"
        for anchor in anchors:
            assert _published_text_supports_anchor(anchor, evidence)

    for case, (_case_id, _query, qrel_id, anchors) in zip(
        cases[45:48],
        V3_REPLACEMENTS,
        strict=True,
    ):
        assert case["expected_chunk_ids"] == [qrel_id]
        assert case["expected_cited_chunk_ids"] == [qrel_id]
        assert case["expected_answer_contains"] == anchors
        assert {"adversarial", "critical"}.issubset(case["tags"])

    critical_cases = [case for case in cases if pilot50._candidate_case_is_critical(case)]
    assert len(critical_cases) == 15
    assert all(pilot50._qrel_ids_from_case(case) for case in critical_cases)
    assert all(case.get("expected_answer_contains") for case in critical_cases)


def test_v3_manifest_source_and_cases_are_frozen_by_exact_hashes() -> None:
    manifest = _load_json(V3_MANIFEST)
    source = _load_json(V3_SOURCE)
    assert isinstance(manifest, dict)
    assert isinstance(source, list)

    assert hashlib.sha256(V3_MANIFEST.read_bytes()).hexdigest() == V3_MANIFEST_SHA256
    assert (
        hashlib.sha256(pilot50._canonical_json_bytes(manifest)).hexdigest()
        == pilot50.V3_EXPECTED_MANIFEST_CANONICAL_SHA256
    )
    assert (
        hashlib.sha256(pilot50._canonical_json_bytes(source)).hexdigest()
        == V3_SOURCE_CANONICAL_SHA256
    )
    source_contract = next(
        item
        for item in manifest["sources"]
        if item["path"] == "eval/cases/pilot50_critical_yonote_v3.json"
    )
    assert source_contract["sha256"] == V3_SOURCE_CANONICAL_SHA256
    assert all(
        item["path"] != "eval/cases/pre_pilot_adversarial.json"
        for item in manifest["sources"]
    )
    assert all(
        item["path"] != "eval/cases/product_date_aspect_regression_v1.json"
        for item in manifest["sources"]
    )
    assert source_contract["case_ids"] == [
        *V3_ENRICHED_IDS,
        *(item[0] for item in V3_REPLACEMENTS),
        *V3_HARDENED_DATE_IDS,
    ]


def test_v3_date_guards_use_full_date_anchors_not_bare_id_numbers() -> None:
    cases, _receipt = pilot50.build_materialized_cases(V3_MANIFEST)

    assert [case["id"] for case in cases[48:50]] == V3_HARDENED_DATE_IDS
    assert cases[48]["expected_answer_contains"] == ["26 июля", "30 июля"]
    assert cases[49]["expected_answer_contains"] == ["8 августа", "15 августа"]
    assert all(
        not anchor.isdecimal()
        for case in cases[48:50]
        for anchor in case["expected_answer_contains"]
    )


def test_v3_candidate_contract_is_explicit_and_v2_remains_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_sha = "c" * 40
    monkeypatch.setenv("RELEASE_GIT_SHA", runtime_sha)
    cases, _receipt = pilot50.build_materialized_cases(V3_MANIFEST)
    kwargs = {
        "cases": cases,
        "cases_file_sha256": V3_CASES_SHA256,
        "target": run_ask.PILOT50_CANDIDATE_TARGET,
        "concurrency": 1,
        "trace_lookup": True,
        "bypass_cache": True,
        "max_llm_cost_rub": run_ask.PILOT50_CANDIDATE_COST_CAP_RUB,
        "max_cases": None,
        "auto_smoke_cases": False,
        "generated_user_prefix": None,
        "private_contract_run": False,
        "source_diagnostic_cases": False,
        "phase0_contract": None,
        "strict_live": True,
        "high_cost_approval_id": run_ask._pilot50_v3_expected_approval_id(
            runtime_sha
        ),
        "expected_runtime_git_sha": runtime_sha,
        "require_budget_for_large_runs": True,
        "require_complete_traces": True,
        "rolling_24h_comparison_waiver_id": (
            run_ask._pilot50_v3_expected_waiver_id(runtime_sha)
        ),
    }

    contract = run_ask._validated_pilot50_candidate_contract(
        run_ask.PILOT50_V3_CANDIDATE_CONTRACT_ID,
        **kwargs,
    )
    assert contract is not None
    assert contract["contract_id"] == "pilot50-v3-candidate-v1"
    assert contract["cases_file_sha256"] == V3_CASES_SHA256
    assert contract["cost_scope"] == "pilot50-v3-candidate"
    assert pilot50._safe_result_evidence_contract("pilot50_balanced_v2")[
        "pricing_contract_id"
    ] == "pilot50-v2-candidate-v1"
    assert pilot50._safe_result_evidence_contract("pilot50_balanced_v3")[
        "pricing_contract_id"
    ] == "pilot50-v3-candidate-v1"

    with pytest.raises(pilot50.Pilot50Error, match="exact candidate contract"):
        pilot50._evidence_contract(
            pilot50.V3_DATASET_ID,
            candidate_contract=pilot50.CANDIDATE_CONTRACT_ID,
        )


def test_v3_quality_gate_uses_the_versioned_50_qrel_contract() -> None:
    gate = pilot50._build_candidate_quality_gate(
        dataset_id=pilot50.V3_DATASET_ID,
        typical_closed=17,
        atypical_closed=13,
        output_contract_escalations=6,
        source_binding_failures=0,
        applicable_qrel_cases=50,
        critical_case_failures=0,
        applicable_critical_cases=15,
    )

    assert gate["schema_version"] == "pilot50-v3-quality-gate-v1"
    assert gate["status"] == "GO"
    assert gate["criteria"]["source_binding_failures"] == {
        "actual": 0,
        "maximum": 0,
        "passed": True,
        "applicable_qrel_cases": 50,
        "total_cases": 50,
    }
    with pytest.raises(pilot50.Pilot50Error, match="qrel coverage"):
        pilot50._build_candidate_quality_gate(
            dataset_id=pilot50.V3_DATASET_ID,
            typical_closed=17,
            atypical_closed=13,
            output_contract_escalations=6,
            source_binding_failures=0,
            applicable_qrel_cases=38,
            critical_case_failures=0,
            applicable_critical_cases=15,
        )
