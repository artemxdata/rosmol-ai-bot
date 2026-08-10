from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from eval.run_ask import _normalize_case, score_case
from eval.social_ticket_benchmark import (
    BILLING_RECONCILIATION_SCHEMA_VERSION,
    DEIDENTIFICATION_CONTRACT_VERSION,
    EXPECTED_CHANNEL_COUNTS,
    EXPECTED_ELIGIBLE,
    EXPECTED_OPAQUE_SELECTED,
    EXPECTED_OPAQUE_TERMINAL,
    EXPECTED_ORDERED_SELECTION_SHA256,
    EXPECTED_OWNER_NO_CONTINUATION_IDS,
    EXPECTED_PHASE0_CASES_FILE_SHA256,
    EXPECTED_SOCIAL_ONLY,
    EXPECTED_SOCIAL_ONLY_CLOSED,
    EXPECTED_SOURCE_SHA256,
    EXPECTED_STRATUM_COUNTS,
    FIRST_CONTENT_CONTRACT_VERSION,
    MANIFEST_SCHEMA_VERSION,
    OWNER_JOIN_CONTRACT_VERSION,
    PHASE0_APPROVAL_ID,
    PHASE0_COST_CAP_RUB,
    PHASE0_PROVENANCE_PATHS,
    PHASE0_REPORT_CLASSIFICATION,
    PHASE0_SELECTION_SEED,
    PHASE0_STRATUM_QUOTAS,
    PRIVATE_DATA_ROOT,
    SELECTION_CONTRACT_VERSION,
    SOCIAL_CONTRACT_VERSION,
    OwnerIdList,
    _atomic_write_no_overwrite,
    _canonical_sha256,
    _deidentify_query,
    _joint_bypass_decision,
    _manifest_case,
    _validate_builder_git_provenance,
    build_cohort_v1,
    build_phase0_artifacts,
    build_safe_phase0_metrics,
    execution_order_key_v1,
    is_social_only_v1,
    load_owner_no_continuation_ids,
    load_source_jsonl,
    select_first_content_turn_v1,
    select_phase0_cases_v1,
    selection_key_v1,
    social_turn_kind_v1,
)
from scripts.analyze_ticket_dataset import private_id_hash
from src.graph.provenance import PROVENANCE_SCHEMA_VERSION
from src.kb.source_extractors import SpreadsheetRow

PRIVATE_SOURCE = PRIVATE_DATA_ROOT / "july_vk_max_tickets.jsonl"


def test_social_only_and_first_content_contract_keeps_opaque_turn() -> None:
    assert social_turn_kind_v1("/start") == "startup"
    assert social_turn_kind_v1("Начать") == "startup"
    assert social_turn_kind_v1("Привет!") == "greeting"
    assert social_turn_kind_v1("Спасибо") == "gratitude"
    assert is_social_only_v1(["/start", "Спасибо"])

    selected = select_first_content_turn_v1(
        ["Привет", "!", "Синтетический более поздний вопрос"]
    )

    assert selected.index == 1
    assert selected.mode == "opaque_nontext"
    assert selected.text == "!"


def test_selection_hash_has_stable_synthetic_vectors() -> None:
    ticket_hash = private_id_hash("ticket-001")

    assert ticket_hash == "3852e1bb1a3b7d24722160fc"
    assert selection_key_v1(ticket_hash, seed=PHASE0_SELECTION_SEED) == (
        "8f250abc85edf283da6b53e79454f95d1529014f49e2c675f287bb77b355e52d"
    )
    assert execution_order_key_v1(ticket_hash, seed=PHASE0_SELECTION_SEED) == (
        "6d5a847f3c3c7379dbad2d22aa34f06ff58b5dd6c0dd3be5ae61330423f41c00"
    )


def test_phase0_selection_rejects_unapproved_seed_and_quotas() -> None:
    cohort = build_cohort_v1(
        [_source_record("synthetic", ["Вопрос"], channel="vk", forum=None)],
        enforce_july_contract=False,
    )

    with pytest.raises(ValueError, match="approved seed"):
        select_phase0_cases_v1(cohort, seed="different")
    with pytest.raises(ValueError, match="11/11/4/4"):
        select_phase0_cases_v1(
            cohort,
            quotas={
                "vk/forum": 10,
                "vk/no_forum": 12,
                "max/forum": 4,
                "max/no_forum": 4,
            },
        )


def test_phase0_builder_rejects_unapproved_seed_before_touching_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="approved seed"):
        build_phase0_artifacts(
            source_path=tmp_path / "missing-source.jsonl",
            cases_output_path=tmp_path / "cases.json",
            manifest_output_path=tmp_path / "manifest.json",
            seed="different",
            telemetry_git_sha="a" * 40,
            private_root=tmp_path,
        )

    assert list(tmp_path.iterdir()) == []


def test_phase0_builder_provenance_requires_exact_clean_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> object:
        calls.append(args)
        if args[1] == "rev-parse":
            return type("Result", (), {"stdout": "a" * 40 + "\n"})()
        if args[1] == "status":
            return type("Result", (), {"stdout": ""})()
        return type(
            "Result",
            (),
            {"stdout": "\n".join(PHASE0_PROVENANCE_PATHS) + "\n"},
        )()

    from eval import social_ticket_benchmark as benchmark

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)

    _validate_builder_git_provenance("a" * 40)

    assert [call[1] for call in calls] == ["rev-parse", "status", "ls-tree"]


def test_phase0_builder_provenance_rejects_dirty_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval import social_ticket_benchmark as benchmark

    def fake_run(args: list[str], **_kwargs: object) -> object:
        stdout = "a" * 40 + "\n" if args[1] == "rev-parse" else " M src/x.py\n"
        return type("Result", (), {"stdout": stdout})()

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="differ from telemetry HEAD"):
        _validate_builder_git_provenance("a" * 40)


def test_local_query_deidentification_exports_only_rescanned_text() -> None:
    class SyntheticMasker:
        def mask(self, text: str) -> tuple[str, dict[str, list[str]]]:
            if "Иван Иванов" in text:
                return text.replace("Иван Иванов", "[ИМЯ]"), {
                    "name": ["Иван Иванов"]
                }
            return text, {}

    prepared = _deidentify_query(
        "Иван Иванов спрашивает про форум",
        masker=SyntheticMasker(),  # type: ignore[arg-type]
    )

    assert prepared["text"] == "[ИМЯ] спрашивает про форум"
    assert prepared["pii_types_detected"] == ["name"]
    assert prepared["sha256"] == hashlib.sha256(
        prepared["text"].encode("utf-8")
    ).hexdigest()


@pytest.mark.skipif(not PRIVATE_SOURCE.is_file(), reason="private July source is local-only")
def test_private_july_contract_and_fixed_selection_are_deterministic() -> None:
    source_sha = hashlib.sha256(PRIVATE_SOURCE.read_bytes()).hexdigest()
    records = load_source_jsonl(PRIVATE_SOURCE)
    cohort = build_cohort_v1(records)
    first = select_phase0_cases_v1(cohort)
    second = select_phase0_cases_v1(cohort)

    assert source_sha == EXPECTED_SOURCE_SHA256
    assert cohort.source_rows_total == 852
    assert dict(cohort.channel_counts) == EXPECTED_CHANNEL_COUNTS
    assert cohort.social_only_total == EXPECTED_SOCIAL_ONLY
    assert cohort.social_only_closed == EXPECTED_SOCIAL_ONLY_CLOSED
    assert len(cohort.cases) == EXPECTED_ELIGIBLE
    assert cohort.opaque_selected_total == EXPECTED_OPAQUE_SELECTED
    assert cohort.opaque_terminal_total == EXPECTED_OPAQUE_TERMINAL
    assert dict(cohort.selected_turn_index_counts) == {0: 425, 1: 300, 2: 8}
    assert dict(cohort.stratum_counts) == dict(sorted(EXPECTED_STRATUM_COUNTS.items()))
    assert len(first) == 30
    assert Counter(item["stratum"] for item in first) == PHASE0_STRATUM_QUOTAS
    assert [item["case"].case_id for item in first] == [
        item["case"].case_id for item in second
    ]
    assert _canonical_sha256([item["case"].case_id for item in first]) == (
        EXPECTED_ORDERED_SELECTION_SHA256
    )
    assert {
        item["stratum"]: (
            item["weight_numerator"],
            item["weight_denominator"],
            item["post_stratification_weight"],
        )
        for item in first
    } == {
        "vk/forum": (176, 11, 16.0),
        "vk/no_forum": (374, 11, 34.0),
        "max/forum": (69, 4, 17.25),
        "max/no_forum": (114, 4, 28.5),
    }


@pytest.mark.parametrize("suffix", [".json", ".csv"])
def test_owner_list_loaders_require_exact_unique_id_column_and_167_rows(
    tmp_path: Path,
    suffix: str,
) -> None:
    values = [f"synthetic-{index:03d}" for index in range(167)]
    path = tmp_path / f"owner{suffix}"
    if suffix == ".json":
        path.write_text(
            json.dumps({"unique_id": values}),
            encoding="utf-8",
        )
    else:
        path.write_text(
            "unique_id\n" + "\n".join(values) + "\n",
            encoding="utf-8",
        )

    owner = load_owner_no_continuation_ids(path)

    assert len(owner.ids) == EXPECTED_OWNER_NO_CONTINUATION_IDS
    assert owner.source_format == suffix.removeprefix(".")


def test_owner_xlsx_loader_uses_exact_unique_id_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "owner.xlsx"
    path.write_bytes(b"synthetic-xlsx-placeholder")
    rows = [SpreadsheetRow("Owner", 1, ("unique_id",))]
    rows.extend(
        SpreadsheetRow("Owner", index + 2, (f"synthetic-{index:03d}",))
        for index in range(167)
    )
    monkeypatch.setattr(
        "eval.social_ticket_benchmark.read_xlsx_sheets",
        lambda _: {"Owner": rows},
    )

    owner = load_owner_no_continuation_ids(path)

    assert len(owner.ids) == 167
    assert owner.source_format == "xlsx"


def test_owner_join_is_exact_and_marks_external_population_incomplete() -> None:
    records = [
        _source_record("social-match", ["Привет"], channel="vk", forum=None),
        _source_record("eligible-match", ["Вопрос"], channel="vk", forum=None),
        _source_record("eligible-miss", ["Другой вопрос"], channel="max", forum="Форум"),
    ]
    owner_values = {"social-match", "eligible-match"}
    owner_values.update(f"external-{index:03d}" for index in range(165))
    owner = OwnerIdList(
        ids=frozenset(owner_values),
        file_sha256="a" * 64,
        source_format="json",
    )

    cohort = build_cohort_v1(
        records,
        owner_ids=owner,
        enforce_july_contract=False,
    )
    joined = {case.ticket_id: case.source_no_continuation for case in cohort.cases}

    assert joined == {"eligible-match": True, "eligible-miss": False}
    assert cohort.owner_join["matched_source_total"] == 2
    assert cohort.owner_join["unmatched_source_total"] == 165
    assert cohort.owner_join["matched_social_only_total"] == 1
    assert cohort.owner_join["matched_eligible_total"] == 1
    assert cohort.owner_join["completeness"] == "partial_external_population_join"


def test_private_writer_never_overwrites_existing_or_concurrent_target(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing.json"
    existing.write_bytes(b"owner-data")

    with pytest.raises(FileExistsError, match="must be absent"):
        _atomic_write_no_overwrite(existing, b"replacement", private_mode=True)
    assert existing.read_bytes() == b"owner-data"

    concurrent = tmp_path / "concurrent.json"

    def publish(payload: bytes) -> str:
        try:
            _atomic_write_no_overwrite(concurrent, payload, private_mode=True)
            return "created"
        except FileExistsError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, (b"first", b"second")))

    assert Counter(outcomes) == {"created": 1, "rejected": 1}
    assert concurrent.read_bytes() in {b"first", b"second"}
    if os.name != "nt":
        assert concurrent.stat().st_mode & 0o777 == 0o600


def test_source_observed_diagnostic_requires_opt_in_and_stays_unscored() -> None:
    raw = {
        "id": "social-p0-" + "a" * 24,
        "query": "Синтетический диагностический запрос",
        "privacy_class": "private_ticket_derived",
        "split": "calibration",
        "label_status": "source_observed_diagnostic",
        "requires_human_review": False,
        "channel": "vk",
        "tags": ["benchmark:social_only_v1", "split:calibration"],
    }

    with pytest.raises(ValueError, match="explicit runner opt-in"):
        _normalize_case(raw)

    normalized = _normalize_case(
        raw,
        allow_source_observed_diagnostic=True,
    )

    assert normalized["label_status"] == "source_observed_diagnostic"
    assert normalized["expected_behavior"] is None
    assert normalized["expected_chunk_ids"] == []
    assert normalized["expected_cited_chunk_ids"] == []

    with pytest.raises(ValueError, match="cannot contain expected"):
        _normalize_case(
            {**raw, "expected_behavior": "answer"},
            allow_source_observed_diagnostic=True,
        )
    with pytest.raises(ValueError, match="cannot contain expected"):
        _normalize_case(
            {**raw, "qrels": [{"chunk_id": "source"}]},
            allow_source_observed_diagnostic=True,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("acceptable_chunk_ids", {"source": ["equivalent"]}),
        ("expected_message_masked_contains", ["expected"]),
        ("forbidden_message_masked_contains", ["forbidden"]),
        ("forbidden_profiles", ["dates"]),
    ],
)
def test_source_observed_diagnostic_rejects_all_expectation_aliases(
    field: str,
    value: object,
) -> None:
    raw = {
        "id": "social-p0-" + "a" * 24,
        "query": "Синтетический диагностический запрос",
        "privacy_class": "private_ticket_derived",
        "split": "calibration",
        "label_status": "source_observed_diagnostic",
        "requires_human_review": False,
        "channel": "vk",
        "tags": ["benchmark:social_only_v1", "split:calibration"],
        field: value,
    }

    with pytest.raises(ValueError, match="cannot contain expected"):
        _normalize_case(raw, allow_source_observed_diagnostic=True)


def test_pipeline_lineage_uses_first_attempt_and_exports_joint_bypass_fields() -> None:
    trace = {
        "trace_events": [
            {"node": "analyze", "metadata": {"mode": "fallback"}},
            {
                "node": "retrieve",
                "metadata": {
                    "metadata_lookup_succeeded": True,
                    "question_provenance": [
                        {
                            "schema_version": PROVENANCE_SCHEMA_VERSION,
                            "question_id": "q1",
                            "retrieved_chunk_ids": ["source"],
                            "attempts": [
                                {
                                    "retrieval_method": "hybrid",
                                    "metadata_lookup_attempted": False,
                                    "metadata_lookup_succeeded": False,
                                },
                                {
                                    "retrieval_method": "metadata",
                                    "metadata_lookup_attempted": True,
                                    "metadata_lookup_succeeded": True,
                                },
                            ],
                        }
                    ],
                },
            },
            {
                "node": "rerank",
                "metadata": {
                    "reranker_invoked": False,
                    "synthetic_high_score_applied": True,
                    "score_origin": "synthetic",
                    "question_provenance": [],
                },
            },
            {
                "node": "generate_selection",
                "metadata": {
                    "generator_path": "source_chunk",
                    "source_chunk_applied": True,
                },
            },
        ]
    }

    result = score_case(
        {"id": "synthetic", "query": "safe"},
        {"http_status": 200, "response": "safe"},
        trace,
    )

    assert result["analyzer_execution_mode"] == "fallback"
    assert result["metadata_primary_attempted"] is False
    assert result["metadata_primary_succeeded"] is False
    assert result["reranker_invoked"] is False
    assert result["reranker_synthetic_high_score_applied"] is True
    assert result["generator_path"] == "source_chunk"
    assert result["source_chunk_applied"] is True


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        (0.60, "confirmed"),
        (0.30, "partially_confirmed"),
        (0.299999, "refuted_stop"),
    ],
)
def test_joint_bypass_decision_boundaries(rate: float, expected: str) -> None:
    assert _joint_bypass_decision(rate, valid=True) == expected
    assert _joint_bypass_decision(rate, valid=False) == "invalid"


@pytest.mark.parametrize(
    ("missing_field", "invalid_reason", "joint_scored"),
    [
        ("http_success", "http_success", 30),
        ("trace_found", "trace_found", 30),
        ("cache_hit", "cache_hit", 30),
        ("observed_behavior", "observed_behavior", 30),
        ("was_escalated", "was_escalated", 30),
        ("llm_estimated_cost_rub", "llm_estimated_cost_rub", 30),
        ("analyzer_execution_mode", "analyzer_execution_mode", 29),
        ("metadata_lookup_attempted", "metadata_lookup_attempted", 30),
        ("metadata_primary_succeeded", "metadata_primary_succeeded", 29),
        (
            "reranker_synthetic_high_score_applied",
            "reranker_synthetic_high_score_applied",
            29,
        ),
        ("source_chunk_applied", "source_chunk_applied", 29),
        ("hybrid_candidates_present", "hybrid_candidates_present", 30),
        ("reranker_invoked", "reranker_invoked", 30),
        ("reranker_score_origin", "reranker_score_origin", 30),
        ("generator_path", "generator_path", 30),
    ],
)
def test_phase0_gate_fails_closed_on_missing_required_evidence(
    missing_field: str,
    invalid_reason: str,
    joint_scored: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    report["results"][0].pop(missing_field)

    gate = build_safe_phase0_metrics(manifest, report)["phase0_gate"]

    assert gate["valid"] is False
    assert gate["decision"] == "invalid"
    assert invalid_reason in gate["invalid_reasons"]
    assert gate["joint_bypass"]["scored"] == joint_scored


def test_phase0_product_and_cost_metrics_fail_closed_on_missing_trace_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    report["results"][0].pop("was_escalated")
    report["results"][1].pop("llm_estimated_cost_rub")

    metrics = build_safe_phase0_metrics(manifest, report)

    assert metrics["phase0_gate"]["valid"] is False
    assert metrics["sample"]["answer_no_operator"]["scored"] == 29
    assert metrics["sample"]["containment"]["scored"] == 29
    assert metrics["diagnostics"]["llm_estimated_cost_rub"] is None


def test_phase0_gate_requires_exact_report_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    report["report_classification"].pop("calibration_only")

    gate = build_safe_phase0_metrics(manifest, report)["phase0_gate"]

    assert gate["valid"] is False
    assert gate["decision"] == "invalid"
    assert "report_classification" in gate["invalid_reasons"]


def test_safe_report_drops_extra_private_classification_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    sentinel = "PRIVATE_CLASSIFICATION_SENTINEL"
    report["report_classification"]["private_label"] = sentinel

    metrics = build_safe_phase0_metrics(manifest, report)
    serialized = json.dumps(metrics, ensure_ascii=False)

    assert metrics["phase0_gate"]["valid"] is False
    assert "report_classification" in metrics["phase0_gate"]["invalid_reasons"]
    assert set(metrics["report_classification"]) == set(PHASE0_REPORT_CLASSIFICATION)
    assert sentinel not in serialized


def test_manifest_rejects_extra_private_owner_join_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    manifest["owner_join"]["private_label"] = "PRIVATE_OWNER_SENTINEL"
    _rehash_manifest_core(manifest)

    with pytest.raises(ValueError, match="exact safe schema"):
        build_safe_phase0_metrics(manifest, report)


def test_safe_owner_join_suppresses_counts_below_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)

    owner_join = build_safe_phase0_metrics(manifest, report)["owner_join"]

    assert all(value is None for value in owner_join["counts"].values())
    assert owner_join["completeness"] is None
    assert owner_join["completeness_suppressed"] is True
    assert set(owner_join["suppressed_count_fields"]) == set(
        owner_join["counts"]
    )
    assert owner_join["complementary_suppression"] is True


def test_public_top_level_binary_metrics_apply_complementary_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    for result in report["results"]:
        result["was_escalated"] = False
    report["results"][0]["was_escalated"] = True

    metrics = build_safe_phase0_metrics(manifest, report)
    escalation = metrics["sample"]["escalation"]

    assert escalation["scored"] == 30
    assert escalation["true"] is None
    assert escalation["false"] is None
    assert escalation["unweighted_rate"] is None
    assert escalation["post_stratified_rate"] is None
    assert escalation["suppression"]["applied"] is True


def test_public_outcomes_hide_nested_delta_below_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    for result in report["results"][24:29]:
        result["observed_behavior"] = "clarify"
    report["results"][29]["observed_behavior"] = "scope_note"

    sample = build_safe_phase0_metrics(manifest, report)["sample"]

    assert sample["containment"]["true"] == 25
    assert sample["containment"]["false"] == 5
    assert sample["answer_no_operator"]["true"] is None
    assert sample["answer_no_operator"]["false"] is None
    assert sample["clarification"]["true"] is None
    assert sample["escalation"]["true"] is None
    assert sample["behavior_counts"]["cells"] == {}
    assert sample["cross_metric_suppression"]["applied"] is True


def test_public_outcomes_withhold_secondary_metrics_for_coarse_partition_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    behaviors = (
        ["answer"] * 5
        + ["scope_note"] * 5
        + ["clarify"] * 5
        + ["escalate"] * 5
        + ["clarify"]
        + ["escalate"] * 9
    )
    for index, (result, behavior) in enumerate(
        zip(report["results"], behaviors, strict=True)
    ):
        result["observed_behavior"] = behavior
        result["was_escalated"] = index >= 20

    metrics = build_safe_phase0_metrics(manifest, report)
    sample = metrics["sample"]

    assert metrics["phase0_gate"]["valid"] is True
    assert sample["containment"]["true"] == 10
    assert sample["containment"]["false"] == 20
    assert sample["behavior_counts"]["cells"] == {}
    assert sample["answer_no_operator"]["true"] is None
    assert sample["clarification"]["true"] is None
    assert sample["escalation"]["true"] is None
    assert sample["cross_metric_suppression"]["policy"] == (
        "phase0_public_primary_outcome_only_v1"
    )


def test_phase0_rejects_same_membership_in_a_different_execution_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    report["results"][0], report["results"][1] = (
        report["results"][1],
        report["results"][0],
    )

    with pytest.raises(ValueError, match="case order"):
        build_safe_phase0_metrics(manifest, report)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("source", "source cohort"),
        ("weight", "weighting"),
        ("approval", "approval binding"),
    ],
)
def test_phase0_rejects_self_consistent_but_unapproved_manifest_changes(
    mutation: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    if mutation == "source":
        manifest["source"]["rows_total"] = 851
    elif mutation == "weight":
        manifest["cases"][0]["post_stratification_weight"] = 1.0
    else:
        manifest["approval"]["hard_cap_rub"] = 199.0
    _rehash_manifest_core(manifest)

    with pytest.raises(ValueError, match=message):
        build_safe_phase0_metrics(manifest, report)


@pytest.mark.parametrize(
    ("mutation", "invalid_reason"),
    [
        ("runtime", "runtime_identity"),
        ("approval", "cost_control"),
        ("reservation_sha", "cost_reservation"),
        ("cap", "cost_control"),
        ("billing_missing", "provider_billing_reconciliation"),
        ("billing_discrepancy", "provider_billing_reconciliation"),
        ("billing_window", "provider_billing_reconciliation"),
    ],
)
def test_phase0_gate_rejects_unbound_runtime_cost_or_billing(
    mutation: str,
    invalid_reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    if mutation == "runtime":
        report["runtime_identity"]["postflight_release_git_sha"] = "b" * 40
    elif mutation == "approval":
        report["cost_control"]["high_cost_approval_id"] = "OTHER-APPROVAL"
    elif mutation == "reservation_sha":
        report["cost_control"]["reservation"]["manifest_sha256"] = "c" * 64
    elif mutation == "cap":
        report["llm_budget_rub"] = 199.0
    elif mutation == "billing_missing":
        report.pop("provider_billing_reconciliation")
    elif mutation == "billing_discrepancy":
        report["provider_billing_reconciliation"]["provider_billed_rub"] = 1.0
    else:
        report["provider_billing_reconciliation"]["window_ended_at"] = (
            "2026-08-05T00:00:30+00:00"
        )

    gate = build_safe_phase0_metrics(manifest, report)["phase0_gate"]

    assert gate["valid"] is False
    assert gate["decision"] == "invalid"
    assert invalid_reason in gate["invalid_reasons"]


def test_phase0_billing_accepts_exact_ten_percent_discrepancy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    report["results"][0]["llm_estimated_cost_rub"] = 9.0
    report["llm_estimated_cost_rub"] = 9.0
    billing = report["provider_billing_reconciliation"]
    billing["provider_billed_rub"] = 10.0
    billing["runner_estimated_rub"] = 9.0

    metrics = build_safe_phase0_metrics(manifest, report)

    assert metrics["phase0_gate"]["valid"] is True
    assert metrics["provider_billing_reconciliation"]["relative_discrepancy"] == (
        pytest.approx(0.10)
    )


def test_safe_metrics_keeps_controlled_generator_model_labels_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    for result in report["results"][:5]:
        result["generator_model"] = "not_run"
    for result in report["results"][5:10]:
        result["generator_model"] = "source_only"

    counts = build_safe_phase0_metrics(manifest, report)["diagnostics"][
        "generator_model_counts"
    ]

    assert counts["cells"] == {
        "not_run": 5,
        "source_chunk": 20,
        "source_only": 5,
    }
    assert counts["suppressed"]["applied"] is False


def test_safe_metrics_suppresses_every_rare_categorical_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    rare_values = {
        "rare_behavior_private",
        "rare_generator_model_private",
        "rare_escalation_reason_private",
        "rare_generator_path_private",
        "rare_score_origin_private",
    }
    first_result = report["results"][0]
    first_result.update(
        {
            "observed_behavior": "rare_behavior_private",
            "generator_model": "rare_generator_model_private",
            "escalation_reason": "rare_escalation_reason_private",
            "generator_path": "rare_generator_path_private",
            "reranker_score_origin": "rare_score_origin_private",
        }
    )

    metrics = build_safe_phase0_metrics(manifest, report)
    serialized = json.dumps(metrics, ensure_ascii=False)

    categorical_maps = [
        metrics["sample"]["stratum_counts"],
        metrics["sample"]["behavior_counts"],
        metrics["diagnostics"]["generator_model_counts"],
        metrics["diagnostics"]["escalation_reason_counts"],
        metrics["diagnostics"]["generator_path_counts"],
        metrics["diagnostics"]["reranker_score_origin_counts"],
    ]
    for categorical_map in categorical_maps:
        assert all(count >= 5 for count in categorical_map["cells"].values())
    assert metrics["sample"]["stratum_counts"]["cells"] == {}
    assert metrics["sample"]["stratum_counts"]["suppressed"]["cells"] == 4
    assert (
        metrics["sample"]["stratum_counts"]["suppressed"]["complementary"]
        is True
    )

    owner_matrix = metrics["owner_join"]["sample_behavior_matrix"]
    assert owner_matrix["suppressed"]["cells"] >= 1
    assert owner_matrix["cells"] == {}
    assert all(
        count >= 5
        for row in owner_matrix["cells"].values()
        for count in row.values()
    )
    assert not any(value in serialized for value in rare_values)
    assert metrics["sample"]["behavior_counts"]["cells"] == {}
    assert metrics["slices"]["forum_presence"]["groups"] == {}
    assert metrics["sample"]["answer_no_operator"]["scored"] == 30
    assert metrics["sample"]["source_no_continuation"]["scored"] == 30
    assert metrics["phase0_gate"]["joint_bypass"]["scored"] == 30


def test_public_categorical_suppression_hides_complement_for_26_plus_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    for case in manifest["cases"][:4]:
        case["source_category"] = "форумы"
    for case in manifest["cases"][4:]:
        case["source_category"] = "гранты"
    _rehash_manifest_core(manifest)

    category = build_safe_phase0_metrics(manifest, report)["slices"]["category"]

    assert category["groups"] == {}
    assert category["suppressed"] == {
        "minimum_group_size": 5,
        "groups": 2,
        "complementary": True,
        "applied": True,
        "reason": None,
    }
    assert "cases" not in category["suppressed"]


def test_public_categorical_suppression_hides_entire_24_5_1_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    for result in report["results"][24:29]:
        result["observed_behavior"] = "clarify"
    report["results"][29]["observed_behavior"] = "escalate"

    behavior = build_safe_phase0_metrics(manifest, report)["sample"][
        "behavior_counts"
    ]

    assert behavior["cells"] == {}
    assert behavior["suppressed"] == {
        "minimum_cell_size": 5,
        "cells": None,
        "complementary": True,
        "applied": True,
        "reason": "cross_metric_protection",
    }


def test_public_metrics_redact_repeated_free_text_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    sentinel = "PRIVATE QUERY OR EVIDENCE SENTINEL"
    for result in report["results"][:5]:
        result["generator_model"] = sentinel
        result["escalation_reason"] = sentinel
        result["generator_path"] = sentinel
        result["reranker_score_origin"] = sentinel
    for case in manifest["cases"][:5]:
        case["source_category"] = sentinel
        case["source_forum"] = sentinel
    _rehash_manifest_core(manifest)

    serialized = json.dumps(
        build_safe_phase0_metrics(manifest, report),
        ensure_ascii=False,
    )

    assert sentinel not in serialized
    assert "other_or_redacted" in serialized


def test_phase0_summary_rejects_manifest_changed_after_live_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    manifest["cases"][0]["source_category"] = "гранты"
    _rehash_manifest_core(manifest)

    gate = build_safe_phase0_metrics(manifest, report)["phase0_gate"]

    assert gate["valid"] is False
    assert "phase0_run_binding" in gate["invalid_reasons"]


def test_safe_slice_suppresses_primary_and_complementary_small_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    for case in manifest["cases"][:5]:
        case["source_category"] = "форумы"
    for case in manifest["cases"][5:]:
        case["source_category"] = "гранты"
    _rehash_manifest_core(manifest)
    for result in report["results"][1:5]:
        result["observed_behavior"] = "clarify"

    group = build_safe_phase0_metrics(manifest, report)["slices"]["category"][
        "groups"
    ]["forums"]

    assert group["cases"] == 5
    assert group["answer_no_operator"] == {
        "scored": 5,
        "true": None,
        "false": None,
        "unweighted_rate": None,
        "post_stratified_rate": None,
        "suppression": {
            "applied": True,
            "minimum_outcome_cell_size": 5,
            "primary_cell": "true",
            "complementary_cell": "false",
            "policy": "public_slice_outcomes_withheld",
        },
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "binding_false",
        "trace_run_id_mismatch",
        "trace_case_id_mismatch",
        "trace_not_found",
        "query_error",
        "cardinality_missing",
        "cardinality_total",
        "cardinality_missing_case",
        "cardinality_duplicate_case",
        "cardinality_unknown_case",
        "cardinality_pair_mismatch",
        "cardinality_query_error",
    ],
)
def test_phase0_gate_rejects_unbound_or_non_exact_trace_evidence(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, report = _synthetic_phase0_inputs(monkeypatch)
    first_result = report["results"][0]
    first_id = first_result["id"]
    if mutation == "binding_false":
        first_result["trace_binding_match"] = False
    elif mutation == "trace_run_id_mismatch":
        first_result["trace_eval_run_id"] = "another-run"
    elif mutation == "trace_case_id_mismatch":
        first_result["trace_eval_case_id"] = "another-case"
    elif mutation == "trace_not_found":
        first_result["trace_found"] = False
    elif mutation == "query_error":
        first_result["error"] = "database query failed"
    elif mutation == "cardinality_missing":
        report.pop("trace_cardinality")
    elif mutation == "cardinality_total":
        report["trace_cardinality"]["traces_total"] = 29
    elif mutation == "cardinality_missing_case":
        report["trace_cardinality"]["missing_case_ids"] = [first_id]
    elif mutation == "cardinality_duplicate_case":
        report["trace_cardinality"]["duplicate_case_ids"] = [first_id]
    elif mutation == "cardinality_unknown_case":
        report["trace_cardinality"]["unknown_case_ids"] = ["unknown-case"]
    elif mutation == "cardinality_pair_mismatch":
        report["trace_cardinality"]["request_case_pairs_match"] = False
    else:
        report["trace_cardinality_error"] = "database query failed"

    gate = build_safe_phase0_metrics(manifest, report)["phase0_gate"]

    assert gate["valid"] is False
    assert gate["decision"] == "invalid"
    if mutation == "query_error":
        assert "query_error" in gate["invalid_reasons"]
    elif mutation.startswith("cardinality_"):
        assert "trace_cardinality" in gate["invalid_reasons"]
    else:
        assert "trace_binding" in gate["invalid_reasons"]


@pytest.mark.skipif(not PRIVATE_SOURCE.is_file(), reason="private July source is local-only")
def test_safe_phase0_join_aggregates_joint_bypass_without_case_payloads() -> None:
    cohort = build_cohort_v1(load_source_jsonl(PRIVATE_SOURCE))
    selected = select_phase0_cases_v1(cohort)
    manifest_cases = [
        _manifest_case(item, runner_case_sha256="a" * 64) for item in selected
    ]
    core = _approved_manifest_core(
        manifest_cases,
        ordered_selection_sha256=EXPECTED_ORDERED_SELECTION_SHA256,
        owner_join=dict(cohort.owner_join),
    )
    cases_file_sha = EXPECTED_PHASE0_CASES_FILE_SHA256
    manifest = {
        **core,
        "integrity": {
            "cases_file_sha256": cases_file_sha,
            "ordered_selection_sha256": _canonical_sha256(
                [case["id"] for case in manifest_cases]
            ),
            "manifest_core_sha256": _canonical_sha256(core),
        },
    }
    results = [
        {
            "id": case["id"],
            "http_success": True,
            "trace_found": True,
            "cache_hit": False,
            "observed_behavior": "answer",
            "was_escalated": False,
            "generator_model": "source_chunk",
            "source_chunk_applied": True,
            "generator_path": "source_chunk",
            "analyzer_execution_mode": "deterministic",
            "metadata_lookup_attempted": True,
            "metadata_primary_succeeded": True,
            "hybrid_candidates_present": False,
            "reranker_invoked": False,
            "reranker_score_origin": "synthetic",
            "reranker_synthetic_high_score_applied": True,
            "latency_ms": 100,
            "trace_total_latency_ms": 90,
            "llm_estimated_cost_rub": 0.0,
        }
        for case in manifest_cases
    ]
    report = _approved_phase0_report(
        results,
        cases_file_sha=cases_file_sha,
        manifest=manifest,
    )

    metrics = build_safe_phase0_metrics(manifest, report)
    serialized = json.dumps(metrics, ensure_ascii=False)

    assert metrics["cases_total"] == 30
    assert metrics["diagnostics"]["joint_bypass_rate"] is None
    assert metrics["diagnostics"]["joint_bypass"]["post_stratified_rate"] is None
    assert metrics["diagnostics"]["joint_bypass"]["suppression"]["applied"] is True
    assert metrics["diagnostics"]["metadata_primary_success_rate"] is None
    assert metrics["phase0_gate"]["valid"] is True
    assert metrics["phase0_gate"]["decision"] == "confirmed"
    assert metrics["sample"]["source_no_continuation"] is None
    assert all(case["id"] not in serialized for case in manifest_cases)


def _synthetic_phase0_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, object], dict[str, object]]:
    weights = {
        "vk/forum": 16.0,
        "vk/no_forum": 34.0,
        "max/forum": 17.25,
        "max/no_forum": 28.5,
    }
    cases: list[dict[str, object]] = []
    for stratum, quota in PHASE0_STRATUM_QUOTAS.items():
        channel, forum_presence = stratum.split("/", maxsplit=1)
        for rank in range(1, quota + 1):
            case_number = len(cases)
            ticket_hash = hashlib.sha256(
                f"synthetic-{case_number}".encode()
            ).hexdigest()[:24]
            query_sha = hashlib.sha256(
                f"query-{case_number}".encode()
            ).hexdigest()
            cases.append(
                {
                    "id": f"social-p0-{ticket_hash}",
                    "source_ticket_id_hash": ticket_hash,
                    "query_sha256": query_sha,
                    "deidentified_query_sha256": query_sha,
                    "runner_case_sha256": hashlib.sha256(
                        f"runner-{case_number}".encode()
                    ).hexdigest(),
                    "pii_types_detected": [],
                    "selected_turn_index": 0,
                    "selection_mode": "normalized_text",
                    "stratum": stratum,
                    "selection_rank_within_stratum": rank,
                    "selection_key": "c" * 64,
                    "execution_order_key": "d" * 64,
                    "execution_order": case_number + 1,
                    "post_stratification_weight": weights[stratum],
                    "weight_numerator": EXPECTED_STRATUM_COUNTS[stratum],
                    "weight_denominator": quota,
                    "source_closed_without_operator": False,
                    "source_was_escalated": True,
                    "source_no_continuation": case_number < 6,
                    "source_channel": channel,
                    "source_forum_presence": forum_presence,
                    "source_category": "synthetic",
                    "source_forum": None,
                    "source_user_turns_count": 1,
                    "source_dialogue_length_bucket": "1",
                }
            )
    ordered_selection_sha256 = _canonical_sha256([case["id"] for case in cases])
    monkeypatch.setattr(
        "eval.social_ticket_benchmark.EXPECTED_ORDERED_SELECTION_SHA256",
        ordered_selection_sha256,
    )
    core = _approved_manifest_core(
        cases,
        ordered_selection_sha256=ordered_selection_sha256,
        owner_join={
            "contract": OWNER_JOIN_CONTRACT_VERSION,
            "status": "joined",
            "completeness": "complete_for_current_source",
            "list_sha256": "9" * 64,
            "declared_ids_total": 167,
            "matched_source_total": 167,
            "unmatched_source_total": 0,
            "matched_eligible_total": 160,
            "matched_social_only_total": 7,
            "source_format": "json",
        },
    )
    cases_file_sha = EXPECTED_PHASE0_CASES_FILE_SHA256
    manifest: dict[str, object] = {
        **core,
        "integrity": {
            "cases_file_sha256": cases_file_sha,
            "ordered_selection_sha256": _canonical_sha256(
                [case["id"] for case in cases]
            ),
            "manifest_core_sha256": _canonical_sha256(core),
        },
    }
    results = [
        {
            "id": case["id"],
            "http_success": True,
            "trace_found": True,
            "cache_hit": False,
            "observed_behavior": "answer",
            "was_escalated": False,
            "generator_model": "source_chunk",
            "source_chunk_applied": True,
            "generator_path": "source_chunk",
            "analyzer_execution_mode": "deterministic",
            "metadata_lookup_attempted": True,
            "metadata_primary_succeeded": True,
            "hybrid_candidates_present": False,
            "reranker_invoked": False,
            "reranker_score_origin": "synthetic",
            "reranker_synthetic_high_score_applied": True,
            "latency_ms": 100,
            "trace_total_latency_ms": 90,
            "llm_estimated_cost_rub": 0.0,
        }
        for case in cases
    ]
    report = _approved_phase0_report(
        results,
        cases_file_sha=cases_file_sha,
        manifest=manifest,
    )
    return manifest, report


def _approved_manifest_core(
    cases: list[dict[str, object]],
    *,
    ordered_selection_sha256: str,
    owner_join: dict[str, object],
) -> dict[str, object]:
    telemetry_git_sha = "a" * 40
    safe_owner_join = owner_join or {
        "contract": OWNER_JOIN_CONTRACT_VERSION,
        "status": "not_provided",
        "completeness": "unavailable",
        "list_sha256": None,
        "declared_ids_total": None,
        "matched_source_total": None,
        "unmatched_source_total": None,
        "matched_eligible_total": None,
        "matched_social_only_total": None,
        "source_format": None,
    }
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "contracts": {
            "social_only": SOCIAL_CONTRACT_VERSION,
            "first_content": FIRST_CONTENT_CONTRACT_VERSION,
            "owner_no_continuation": OWNER_JOIN_CONTRACT_VERSION,
            "selection": SELECTION_CONTRACT_VERSION,
            "deidentification": DEIDENTIFICATION_CONTRACT_VERSION,
        },
        "source": {
            "file_sha256": EXPECTED_SOURCE_SHA256,
            "rows_total": 852,
            "channel_counts": dict(EXPECTED_CHANNEL_COUNTS),
            "period": "2026-07",
            "tool_sha256": "e" * 64,
            "social_classifier_dependency_sha256": "f" * 64,
        },
        "telemetry": {"git_sha": telemetry_git_sha},
        "approval": {
            "id": PHASE0_APPROVAL_ID,
            "hard_cap_rub": PHASE0_COST_CAP_RUB,
            "case_count": 30,
            "telemetry_git_sha": telemetry_git_sha,
            "ordered_selection_sha256": ordered_selection_sha256,
            "cache_bypass_required": True,
            "selective_reruns_forbidden": True,
            "provider_billing_reconciliation_required": True,
        },
        "deidentification": {
            "contract": DEIDENTIFICATION_CONTRACT_VERSION,
            "performed_locally": True,
            "single_turn_only": True,
            "raw_query_exported": False,
            "scanned_cases": 30,
            "changed_cases": 0,
            "pii_type_counts": {},
        },
        "population": {
            "social_only_total": EXPECTED_SOCIAL_ONLY,
            "social_only_closed_without_operator": EXPECTED_SOCIAL_ONLY_CLOSED,
            "eligible_total": EXPECTED_ELIGIBLE,
            "opaque_nontext_selected_total": EXPECTED_OPAQUE_SELECTED,
            "opaque_nontext_terminal_total": EXPECTED_OPAQUE_TERMINAL,
            "selected_turn_index_counts": {"0": 425, "1": 300, "2": 8},
            "stratum_counts": dict(sorted(EXPECTED_STRATUM_COUNTS.items())),
        },
        "owner_join": safe_owner_join,
        "selection": {
            "seed": PHASE0_SELECTION_SEED,
            "sample_size": 30,
            "quotas": dict(PHASE0_STRATUM_QUOTAS),
            "hash_algorithm": "sha256-domain-separated-v1",
            "global_order": "sha256-domain-separated-v1",
            "post_stratification": {
                stratum: {
                    "population": EXPECTED_STRATUM_COUNTS[stratum],
                    "sample": PHASE0_STRATUM_QUOTAS[stratum],
                    "weight_numerator": EXPECTED_STRATUM_COUNTS[stratum],
                    "weight_denominator": PHASE0_STRATUM_QUOTAS[stratum],
                    "weight": (
                        EXPECTED_STRATUM_COUNTS[stratum]
                        / PHASE0_STRATUM_QUOTAS[stratum]
                    ),
                }
                for stratum in PHASE0_STRATUM_QUOTAS
            },
        },
        "cases": cases,
    }


def _approved_phase0_report(
    results: list[dict[str, object]],
    *,
    cases_file_sha: str,
    manifest: dict[str, object],
) -> dict[str, object]:
    telemetry_git_sha = "a" * 40
    eval_run_id = "ask-eval-phase0-synthetic"
    for result in results:
        result.setdefault("error", None)
        result.setdefault("trace_eval_run_id", eval_run_id)
        result.setdefault("trace_eval_case_id", result["id"])
        result.setdefault("trace_binding_match", True)
        result.setdefault("trace_error", None)
        result.setdefault("llm_accounting_present", True)
    case_counts = {str(result["id"]): 1 for result in results}
    return {
        "eval_run_id": eval_run_id,
        "run_started_at": "2026-08-05T00:01:00+00:00",
        "run_completed_at": "2026-08-05T00:02:00+00:00",
        "cases_file_sha256": cases_file_sha,
        "phase0_run": {
            "status": "completed",
            "completed": True,
            "expected_cases_total": 30,
            "executed_cases_total": 30,
            "cases_file_sha256": cases_file_sha,
            "manifest_file_sha256": "c" * 64,
            "manifest_binding_sha256": _canonical_sha256(manifest),
            "ordered_selection_sha256": (
                manifest["integrity"]["ordered_selection_sha256"]
            ),
            "runtime_git_sha": telemetry_git_sha,
            "approval_id": PHASE0_APPROVAL_ID,
            "cost_scope": "phase0-social-30",
            "integrity_failures": [],
            "selective_reruns_forbidden": True,
        },
        "report_classification": dict(PHASE0_REPORT_CLASSIFICATION),
        "runtime_identity": {
            "required": True,
            "status": "verified",
            "expected_runtime_git_sha": telemetry_git_sha,
            "preflight_release_git_sha": telemetry_git_sha,
            "postflight_release_git_sha": telemetry_git_sha,
            "verified_release_git_sha": telemetry_git_sha,
            "matched_expected_runtime": True,
        },
        "cost_control": {
            "strict_live": True,
            "high_cost_approval_id": PHASE0_APPROVAL_ID,
            "pricing_complete": True,
            "reservation": {
                "valid": True,
                "run_id": eval_run_id,
                "scope": "phase0-social-30",
                "runtime_git_sha": telemetry_git_sha,
                "manifest_sha256": cases_file_sha,
                "cases_file_sha256": cases_file_sha,
                "manifest_matches_cases_file": True,
                "case_count": 30,
                "approved_cap_rub": PHASE0_COST_CAP_RUB,
                "approval_required": True,
                "high_cost_approval_id": PHASE0_APPROVAL_ID,
            },
        },
        "llm_budget_rub": PHASE0_COST_CAP_RUB,
        "llm_budget_exceeded": False,
        "llm_estimated_cost_rub": 0.0,
        "provider_billing_reconciliation": {
            "schema_version": BILLING_RECONCILIATION_SCHEMA_VERSION,
            "approval_id": PHASE0_APPROVAL_ID,
            "eval_run_id": eval_run_id,
            "runtime_git_sha": telemetry_git_sha,
            "cases_file_sha256": cases_file_sha,
            "attribution_scope": "dedicated_eval_credential",
            "provider_currency": "RUB",
            "provider_reference": "provider-eval-001",
            "window_started_at": "2026-08-05T00:00:00+00:00",
            "window_ended_at": "2026-08-05T00:03:00+00:00",
            "provider_billed_rub": 0.0,
            "runner_estimated_rub": 0.0,
            "hard_cap_rub": PHASE0_COST_CAP_RUB,
            "verified_at": "2026-08-05T00:04:00+00:00",
        },
        "trace_cardinality": {
            "eval_run_id": eval_run_id,
            "expected_cases_total": 30,
            "traces_total": 30,
            "case_counts": case_counts,
            "missing_case_ids": [],
            "duplicate_case_ids": [],
            "unknown_case_ids": [],
            "expected_request_ids_total": 30,
            "distinct_request_ids_total": 30,
            "invalid_expected_request_ids_total": 0,
            "invalid_observed_request_ids_total": 0,
            "duplicate_request_ids_total": 0,
            "missing_request_case_pairs_total": 0,
            "unexpected_request_case_pairs_total": 0,
            "request_case_pairs_match": True,
            "cache_hit_true_total": 0,
            "cache_hit_false_total": 30,
            "cache_hit_unknown_total": 0,
        },
        "results": results,
    }


def _rehash_manifest_core(manifest: dict[str, object]) -> None:
    core = {key: value for key, value in manifest.items() if key != "integrity"}
    manifest["integrity"]["manifest_core_sha256"] = _canonical_sha256(core)


def _source_record(
    ticket_id: str,
    user_turns: list[str],
    *,
    channel: str,
    forum: str | None,
) -> dict[str, object]:
    return {
        "ticket_id": ticket_id,
        "user_turns": user_turns,
        "bot_turns": [],
        "channel": channel,
        "category": None,
        "forum": forum,
        "topic": None,
        "created_at": "2026-07-01T00:00:00+03:00",
        "closed_at": "2026-07-01T00:01:00+03:00",
        "closed_without_operator": False,
        "was_escalated": True,
        "counted_in_conversion": True,
        "is_substantive": True,
    }
