from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts import build_gold_ticket_dataset as builder
from scripts.build_gold_ticket_dataset import (
    GoldSampleCandidate,
    GoldSamplingConfig,
    build_gold_ticket_dataset,
    select_gold_ticket_candidates,
)
from src.security.private_dataset_registry import (
    HASH_FIELDS,
    REGISTRY_SCHEMA,
    freeze_dataset,
    validate_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "eval" / "datasets" / "gold150_sanity_v1.json"


def _candidate(index: int) -> GoldSampleCandidate:
    profiles = ("dates", "application", "program", "food")
    routes = ("answer", "clarify", "escalate")
    profile = profiles[index % len(profiles)]
    route = routes[index % len(routes)]
    return GoldSampleCandidate(
        ticket_id_hash=f"{index + 1:064x}",
        duplicate_component_id=f"{10_000 + index:064x}",
        source_case_fingerprint=f"{20_000 + index:064x}",
        source_schema_version="product-eval-case.v2",
        available_at="2026-08-01T00:00:00+00:00",
        source_channel="api",
        intent_hint="forums.dates",
        entity_class_hint="forums:named",
        profile_hint=profile,
        route_hint=route,
        escalation_reason_hint="missing_coverage" if route == "escalate" else "",
        time_sensitive=True,
        multiturn_status="multi_turn",
        role_reconstruction_status="partial",
        risk_flags=(
            "critical_profile",
            "multi_turn",
            "operator_route",
            "role_review_required",
            "time_sensitive",
        ),
    )


def _load_default_config() -> GoldSamplingConfig:
    return GoldSamplingConfig.model_validate(
        json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    )


def test_default_sampler_is_exact_deterministic_and_component_disjoint() -> None:
    config = _load_default_config()
    candidates = [_candidate(index) for index in range(180)]

    selected = select_gold_ticket_candidates(
        candidates,
        config=config,
        source_manifest_sha256="a" * 64,
    )
    reversed_selection = select_gold_ticket_candidates(
        reversed(candidates),
        config=config,
        source_manifest_sha256="a" * 64,
    )

    signature = [
        (
            item.candidate.ticket_id_hash,
            item.candidate.duplicate_component_id,
            item.segment,
            item.stratum,
        )
        for item in selected
    ]
    assert signature == [
        (
            item.candidate.ticket_id_hash,
            item.candidate.duplicate_component_id,
            item.segment,
            item.stratum,
        )
        for item in reversed_selection
    ]
    assert len(selected) == 150
    assert Counter(item.segment for item in selected) == {
        "traffic": 100,
        "risk": 50,
    }
    assert Counter(
        item.stratum for item in selected if item.segment == "risk"
    ) == config.risk_slot_quotas
    assert len(
        {item.candidate.duplicate_component_id for item in selected}
    ) == 150


def test_sampler_fails_closed_when_a_risk_quota_is_infeasible() -> None:
    config = _small_config()
    candidates = [
        replace(
            _candidate(index),
            risk_flags=(
                "critical_profile",
                "multi_turn",
                "operator_route",
                "time_sensitive",
            ),
        )
        for index in range(20)
    ]

    with pytest.raises(ValueError, match="risk quota is infeasible"):
        select_gold_ticket_candidates(
            candidates,
            config=config,
            source_manifest_sha256="b" * 64,
        )


def test_builder_keeps_text_private_and_emits_registry_compatible_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _private_fixture(tmp_path, monkeypatch, population=24)

    first = build_gold_ticket_dataset(
        **fixture["arguments"],
        output_dir=fixture["private_root"] / "eval" / "gold-test-v1",
    )

    assert capsys.readouterr().out == ""
    assert first["selected_total"] == 10
    assert first["traffic_selected"] == 5
    assert first["risk_selected"] == 5
    output = fixture["private_root"] / "eval" / "gold-test-v1"
    selection_path = output / "gold_test_v1_selection.json"
    review_path = output / "gold_test_v1_review.jsonl"
    registry_path = output / "gold_test_v1_registry.json"
    selection_text = selection_path.read_text(encoding="utf-8")
    review_text = review_path.read_text(encoding="utf-8")
    registry_text = registry_path.read_text(encoding="utf-8")

    assert "SYNTHETIC_PRIVATE_QUERY_" not in selection_text
    assert "SYNTHETIC_PRIVATE_TURN_" not in selection_text
    assert "SYNTHETIC_PRIVATE_QUERY_" not in registry_text
    assert "SYNTHETIC_PRIVATE_TURN_" not in registry_text
    assert "SYNTHETIC_PRIVATE_TURN_" in review_text

    selection = json.loads(selection_text)
    assert selection["weak_labels_are_sampling_hints_only"] is True
    assert selection["operator_answers_used_as_facts"] is False
    assert len(selection["records"]) == 10
    assert all(
        record["weak_label_hints"]["sampling_only"] is True
        for record in selection["records"]
    )
    review_records = [json.loads(line) for line in review_text.splitlines()]
    assert all(
        record["weak_label_hints"]["must_not_be_exported_as_gold"] is True
        and record["operator_evidence"]["used_as_factual_truth"] is False
        and record["human_annotation"]["status"] == "pending"
        for record in review_records
    )

    entry = json.loads(registry_text)
    assert entry["kind"] == "gold_ticket"
    assert entry["evaluation_role"] == "calibration_sanity"
    assert entry["independent_evaluation"] is False
    assert entry["human_review_status"] == "pending"
    assert entry["state"] == "draft"
    assert set(entry["hashes"]) == set(HASH_FIELDS)
    assert all(entry["hashes"].values())
    seed_payload = json.loads(fixture["arguments"]["kb_seed_path"].read_text(encoding="utf-8"))
    assert entry["hashes"]["kb_seed_sha256"] == _canonical_sha256(seed_payload)
    registry = validate_registry(
        {
            "schema": REGISTRY_SCHEMA,
            "updated_at": entry["created_at"],
            "datasets": [entry],
        }
    )
    with pytest.raises(ValueError, match="human review"):
        freeze_dataset(registry, "gold_test_v1@v1")

    second_output = fixture["private_root"] / "eval" / "gold-test-v2"
    second = build_gold_ticket_dataset(
        **fixture["arguments"],
        output_dir=second_output,
        exclusion_paths=[selection_path],
    )
    second_selection = json.loads(
        (second_output / "gold_test_v1_selection.json").read_text(encoding="utf-8")
    )
    first_components = {
        record["duplicate_component_id"] for record in selection["records"]
    }
    second_components = {
        record["duplicate_component_id"]
        for record in second_selection["records"]
    }
    assert second["selected_total"] == 10
    assert first_components.isdisjoint(second_components)


def test_builder_supports_independent_full_ticket_holdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _private_fixture(tmp_path, monkeypatch, population=24)
    arguments = fixture["arguments"]
    cases_path = arguments["cases_path"]
    conversations_path = arguments["conversations_path"]
    config_path = arguments["config_path"]

    cases = [
        {**json.loads(line), "split": "holdout"}
        for line in cases_path.read_text(encoding="utf-8").splitlines()
    ]
    conversations = [
        {**item, "split": "holdout"}
        for item in json.loads(conversations_path.read_text(encoding="utf-8"))
    ]
    cases_path.write_text(_jsonl(cases), encoding="utf-8")
    conversations_path.write_text(
        json.dumps(conversations, ensure_ascii=False),
        encoding="utf-8",
    )
    _refresh_artifact_manifest(arguments["artifact_manifest_path"])
    config = _small_config().model_dump(mode="json")
    config.update(
        {
            "schema_version": "gold-ticket-sampling.v1",
            "dataset_id": "blind_test_v1",
            "purpose": "independent_holdout",
            "source_split": "holdout",
            "stable_rank_namespace": "synthetic-blind-test-v1",
        }
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = build_gold_ticket_dataset(
        **arguments,
        output_dir=fixture["private_root"] / "eval" / "blind-test-v1",
    )

    assert result["selected_total"] == 10
    entry = json.loads(
        (
            fixture["private_root"]
            / "eval"
            / "blind-test-v1"
            / "blind_test_v1_registry.json"
        ).read_text(encoding="utf-8")
    )
    assert entry["evaluation_role"] == "holdout"
    assert entry["independent_evaluation"] is True


def test_sampling_purpose_must_match_source_split() -> None:
    payload = _small_config().model_dump(mode="json")
    payload["source_split"] = "holdout"

    with pytest.raises(ValueError, match="purpose and source split"):
        GoldSamplingConfig.model_validate(payload)


def test_sampling_allows_zero_quota_for_absent_risk_flag() -> None:
    payload = _small_config().model_dump(mode="json")
    payload["risk_slot_quotas"].update(
        {
            "critical_profile": payload["risk_slot_quotas"]["critical_profile"] + 1,
            "role_review_required": 0,
        }
    )

    config = GoldSamplingConfig.model_validate(payload)

    assert config.risk_slot_quotas["role_review_required"] == 0


def test_builder_rejects_paths_outside_private_and_manifest_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _private_fixture(tmp_path, monkeypatch, population=12)
    arguments = fixture["arguments"]

    with pytest.raises(ValueError, match="data/private"):
        build_gold_ticket_dataset(
            **{**arguments, "cases_path": fixture["project"] / "outside.jsonl"},
            output_dir=fixture["private_root"] / "eval" / "outside",
        )

    cases_path = arguments["cases_path"]
    cases_path.write_text(
        cases_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="differs from artifact manifest"):
        build_gold_ticket_dataset(
            **arguments,
            output_dir=fixture["private_root"] / "eval" / "drift",
        )


def test_builder_publishes_atomically_and_never_overwrites_existing_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _private_fixture(tmp_path, monkeypatch, population=12)
    destination = fixture["private_root"] / "eval" / "atomic-failure"

    def fail_registry(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("synthetic registry failure")

    monkeypatch.setattr(builder, "_build_registry_payload", fail_registry)
    with pytest.raises(RuntimeError, match="synthetic registry failure"):
        build_gold_ticket_dataset(
            **fixture["arguments"],
            output_dir=destination,
        )
    assert not destination.exists()
    assert not list(destination.parent.glob(f".{destination.name}.*.staging"))

    monkeypatch.undo()
    fixture = _private_fixture(tmp_path / "second", monkeypatch, population=12)
    destination = fixture["private_root"] / "eval" / "existing-v1"
    destination.mkdir(parents=True)
    sentinel = destination / "owner-file.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe overwrite is disabled"):
        build_gold_ticket_dataset(
            **fixture["arguments"],
            output_dir=destination,
            overwrite=True,
        )
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_builder_coarsens_free_form_hints_out_of_metadata_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _private_fixture(tmp_path, monkeypatch, population=12)
    cases_path = fixture["arguments"]["cases_path"]
    records = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines()]
    canaries = {
        "category": "person@example.invalid",
        "topic": "+7-900-000-00-00",
        "entity": "PRIVATE_ENTITY_CANARY",
        "expected_escalation_reason": "PRIVATE_REASON_CANARY",
    }
    for record in records:
        record.update(canaries)
    cases_path.write_text(_jsonl(records), encoding="utf-8")
    _refresh_artifact_manifest(fixture["arguments"]["artifact_manifest_path"])
    destination = fixture["private_root"] / "eval" / "coarsened-v1"

    build_gold_ticket_dataset(
        **fixture["arguments"],
        output_dir=destination,
    )

    metadata = (
        (destination / "gold_test_v1_selection.json").read_text(encoding="utf-8")
        + (destination / "gold_test_v1_registry.json").read_text(encoding="utf-8")
    )
    for canary in canaries.values():
        assert canary not in metadata
    selection = json.loads(
        (destination / "gold_test_v1_selection.json").read_text(encoding="utf-8")
    )
    hints = selection["records"][0]["weak_label_hints"]
    assert hints["intent_hint"] == "profile:dates"
    assert hints["entity_class_hint"] == "named"
    assert hints["escalation_reason_hint"] == "present"


def test_builder_rejects_unrecognized_profile_instead_of_copying_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _private_fixture(tmp_path, monkeypatch, population=12)
    cases_path = fixture["arguments"]["cases_path"]
    records = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines()]
    records[0]["expected_response_profile"] = "person@example.invalid"
    cases_path.write_text(_jsonl(records), encoding="utf-8")
    _refresh_artifact_manifest(fixture["arguments"]["artifact_manifest_path"])

    with pytest.raises(ValueError, match="governed enum"):
        build_gold_ticket_dataset(
            **fixture["arguments"],
            output_dir=fixture["private_root"] / "eval" / "invalid-profile",
        )


def test_builder_accepts_governed_legacy_source_schema_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _private_fixture(tmp_path, monkeypatch, population=12)
    cases_path = fixture["arguments"]["cases_path"]
    records = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record["schema_version"] = "1.0.0"
    cases_path.write_text(_jsonl(records), encoding="utf-8")
    _refresh_artifact_manifest(fixture["arguments"]["artifact_manifest_path"])

    result = build_gold_ticket_dataset(
        **fixture["arguments"],
        output_dir=fixture["private_root"] / "eval" / "legacy-schema-v1",
    )

    assert result["selected_total"] == 10


def _small_config() -> GoldSamplingConfig:
    return GoldSamplingConfig.model_validate(
        {
            "schema_version": "gold150-sampling.v1",
            "dataset_id": "gold_test_v1",
            "dataset_version": "v1",
            "registry_created_at": "2026-08-04T00:00:00+00:00",
            "purpose": "sanity_calibration",
            "source_split": "calibration",
            "measurement_unit": "full_ticket",
            "target_total": 10,
            "traffic_target": 5,
            "risk_target": 5,
            "traffic_strata": [
                "expected_response_profile",
                "expected_route",
            ],
            "risk_slot_quotas": {
                "multi_turn": 1,
                "time_sensitive": 1,
                "critical_profile": 1,
                "operator_route": 1,
                "role_review_required": 1,
            },
            "critical_profiles": ["dates"],
            "stable_rank_namespace": "synthetic-gold-test-v1",
            "operator_answers_used_as_facts": False,
            "weak_labels_are_sampling_hints_only": True,
            "review_policy": {
                "primary_human_review_required": True,
                "dual_review_all_critical": True,
                "deterministic_secondary_audit_fraction": 0.25,
                "adjudication_required_on_disagreement": True,
            },
        }
    )


def _private_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    population: int,
) -> dict[str, Any]:
    project = tmp_path / "workspace"
    versioned_root = project / "data"
    private_root = versioned_root / "private"
    source = private_root / "source"
    config_root = project / "eval" / "datasets"
    source.mkdir(parents=True)
    config_root.mkdir(parents=True)
    monkeypatch.setattr(builder, "PRIVATE_DATA_ROOT", private_root.resolve())
    monkeypatch.setattr(builder, "VERSIONED_DATA_ROOT", versioned_root.resolve())
    monkeypatch.setattr(builder, "DATASET_CONFIG_ROOT", config_root.resolve())

    cases = []
    conversations = []
    normalized = []
    for index in range(population):
        ticket_id = f"{index + 1:064x}"
        component_id = f"{50_000 + index:064x}"
        cases.append(
            {
                "schema_version": "product-eval-case.v2",
                "split": "calibration",
                "ticket_id_hash": ticket_id,
                "duplicate_component_id": component_id,
                "operator_answer_included": False,
                "operator_answer_used_as_fact": False,
                "query": f"SYNTHETIC_PRIVATE_QUERY_{index}",
                "category": "forums",
                "topic": "dates",
                "entity": "synthetic_event",
                "expected_response_profile": "dates",
                "expected_route": "escalate",
                "expected_escalation_reason": "missing_coverage",
                "forbidden_response_profiles": [],
                "role_reconstruction_status": "partial",
                "multiturn_status": "multi_turn",
                "time_sensitive": True,
                "channel": "api",
                "available_at": "2026-08-01T00:00:00+00:00",
            }
        )
        conversations.append(
            {
                "split": "calibration",
                "ticket_id_hash": ticket_id,
                "duplicate_component_id": component_id,
                "operator_answer_included": False,
                "operator_answer_used_as_fact": False,
                "turns_count": 2,
            }
        )
        normalized.append(
            {
                "ticket_hash": ticket_id,
                "review_required_turns_count": 1,
                "dialogue_turns": [
                    {
                        "turn_index": 0,
                        "role": "user",
                        "assistant_kind": None,
                        "role_confidence": "high",
                        "role_reason": "synthetic",
                        "text_masked": f"SYNTHETIC_PRIVATE_TURN_{index}_USER",
                    },
                    {
                        "turn_index": 1,
                        "role": "assistant",
                        "assistant_kind": "operator",
                        "role_confidence": "medium",
                        "role_reason": "synthetic",
                        "text_masked": f"SYNTHETIC_PRIVATE_TURN_{index}_OPERATOR",
                    },
                ],
            }
        )

    cases_path = source / "cases.jsonl"
    conversations_path = source / "conversations.json"
    normalized_path = source / "normalized.jsonl"
    cases_path.write_text(_jsonl(cases), encoding="utf-8")
    conversations_path.write_text(
        json.dumps(conversations, ensure_ascii=False),
        encoding="utf-8",
    )
    normalized_path.write_text(_jsonl(normalized), encoding="utf-8")
    manifest_path = source / "artifact_manifest.json"
    artifacts = []
    for path in (cases_path, conversations_path, normalized_path):
        artifacts.append(
            {
                "path": path.relative_to(source).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    manifest_path.write_text(
        json.dumps({"complete": True, "artifacts": artifacts}),
        encoding="utf-8",
    )

    config_path = config_root / "gold_test_v1.json"
    config_path.write_text(
        json.dumps(_small_config().model_dump(mode="json")),
        encoding="utf-8",
    )
    seed_path = versioned_root / "knowledge_base_seed.json"
    seed_path.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "yonote-synthetic",
                    "source_type": "yonote",
                    "status": "published",
                    "content": "synthetic source",
                }
            ]
        ),
        encoding="utf-8",
    )
    return {
        "project": project,
        "private_root": private_root,
        "arguments": {
            "cases_path": cases_path,
            "conversations_path": conversations_path,
            "normalized_tickets_path": normalized_path,
            "artifact_manifest_path": manifest_path,
            "config_path": config_path,
            "kb_seed_path": seed_path,
        },
    }


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _refresh_artifact_manifest(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for artifact in payload["artifacts"]:
        artifact_path = path.parent / artifact["path"]
        artifact["size_bytes"] = artifact_path.stat().st_size
        artifact["sha256"] = _sha256(artifact_path)
    path.write_text(json.dumps(payload), encoding="utf-8")
