from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eval.gold_ticket import seal_gold_ticket
from scripts.build_rag_dataset_demo_cases import validate_output_policy
from src.security.private_dataset_registry import (
    HASH_FIELDS,
    build_retention_plan,
    complete_human_review,
    empty_registry,
    freeze_dataset,
    inventory_private_datasets,
    load_registry,
    register_dataset,
    save_registry,
    start_review,
    supersede_dataset,
    validate_registry,
)

NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
PAST_DELETE = "2026-08-03T08:00:00+00:00"


def _entry(
    dataset_id: str = "ticket-corpus",
    version: str = "v1",
    *,
    root: str | None = None,
    kind: str = "legacy_private",
    state: str = "draft",
    sources: list[str] | None = None,
    requires_parent_bytes: bool = False,
    supersedes: str | None = None,
    delete_after: str | None = None,
    retention_policy_ref: str = "unapproved",
    human_review_status: str = "not_required",
    evaluation_role: str = "none",
    independent_evaluation: bool = False,
    cases_total: int = 0,
) -> dict[str, object]:
    return {
        "dataset_id": dataset_id,
        "version": version,
        "relative_root": root or f"data/private/tickets/{dataset_id}-{version}",
        "kind": kind,
        "purpose": "product_quality",
        "privacy_class": (
            "deidentified_private" if kind == "gold_ticket" else "raw_restricted"
        ),
        "export_class": "private_only",
        "state": state,
        "created_at": "2026-08-04T08:00:00+00:00",
        "lifecycle_updated_at": "2026-08-04T08:00:00+00:00",
        "frozen_at": None,
        "delete_after": delete_after,
        "retention_policy_ref": retention_policy_ref,
        "owner_role": "quality_owner",
        "source_dataset_ids": list(sources or []),
        "requires_parent_bytes": requires_parent_bytes,
        "contains_raw_text": kind != "gold_ticket",
        "pii_possible": kind != "gold_ticket",
        "contains_operator_answers": False,
        "evaluation_role": evaluation_role,
        "independent_evaluation": independent_evaluation,
        "human_review_status": human_review_status,
        "cases_total": cases_total,
        "hashes": {field: None for field in HASH_FIELDS},
        "supersedes": supersedes,
        "superseded_by": None,
        "hold_reason": "none",
        "frozen_payload_sha256": None,
    }


def _gold150(
    version: str = "v1",
    *,
    dataset_id: str = "gold150",
    supersedes: str | None = None,
    delete_after: str | None = None,
    retention_policy_ref: str = "unapproved",
) -> dict[str, object]:
    entry = _entry(
        dataset_id,
        version,
        kind="gold_ticket",
        root=f"data/private/eval/{dataset_id}-{version}",
        human_review_status="pending",
        evaluation_role="calibration_sanity",
        independent_evaluation=False,
        cases_total=2,
        supersedes=supersedes,
        delete_after=delete_after,
        retention_policy_ref=retention_policy_ref,
    )
    return entry


def _register_and_freeze(
    registry: dict[str, object],
    entry: dict[str, object],
    *,
    private_root: Path,
) -> dict[str, object]:
    gold_path = _materialize_gold_artifacts(private_root, entry)
    registered = register_dataset(registry, entry, now=NOW)
    reviewing = start_review(
        registered,
        f"{entry['dataset_id']}@{entry['version']}",
        now=NOW,
    )
    completed = complete_human_review(
        reviewing,
        f"{entry['dataset_id']}@{entry['version']}",
        gold_artifact_path=gold_path,
        private_root=private_root,
        now=NOW,
    )
    return freeze_dataset(
        completed,
        f"{entry['dataset_id']}@{entry['version']}",
        private_root=private_root,
        now=NOW,
    )


def _materialize_gold_artifacts(
    private_root: Path,
    entry: dict[str, object],
) -> Path:
    relative_root = Path(*Path(str(entry["relative_root"])).parts[2:])
    dataset_root = private_root / relative_root
    dataset_root.mkdir(parents=True, exist_ok=False)
    artifact_manifest_sha256 = "1" * 64
    selected_records: list[dict[str, str]] = []
    gold_lines: list[str] = []
    for index in range(int(entry["cases_total"])):
        case_id = f"{index + 1:064x}"
        component_id = f"{1000 + index:064x}"
        selected_records.append(
            {
                "case_id_hash": case_id,
                "duplicate_component_id": component_id,
            }
        )
        ticket = seal_gold_ticket(
            {
                "schema_version": "gold-ticket.v1",
                "dataset_id": entry["dataset_id"],
                "ticket_id_hash": case_id,
                "duplicate_component_id": component_id,
                "split": "calibration",
                "measurement_unit": "full_ticket",
                "source_binding": {
                    "artifact_manifest_sha256": artifact_manifest_sha256,
                    "normalized_source_sha256": "2" * 64,
                    "source_record_fingerprint": f"{2000 + index:064x}",
                    "available_at": "2026-08-01T00:00:00Z",
                    "source_channel": "api",
                },
                "turns": [
                    {
                        "turn_id": "t001",
                        "source_turn_index": 0,
                        "role_candidate": "user",
                        "reviewed_role": "user",
                        "assistant_kind": None,
                        "text_deidentified": "Синтетический вопрос",
                        "include_in_replay": True,
                        "privacy_verdict": "approved",
                    }
                ],
                "evaluation_steps": [
                    {
                        "step_id": "s001",
                        "user_turn_ids": ["t001"],
                        "history_turn_ids": [],
                        "expected_action": "clarify",
                        "expected_escalation_reason": None,
                        "intents": ["forums.dates"],
                        "entities": [],
                        "requested_aspects": ["dates"],
                        "constraints": [],
                        "answerability": "none",
                        "missing_aspects": [],
                        "qrels": [],
                        "expected_claims": [],
                        "forbidden_profiles": [],
                    }
                ],
                "expected_ticket_outcome": "unresolved",
                "operator_evidence": {
                    "available": False,
                    "behavior_tags": [],
                    "used_as_factual_truth": False,
                },
                "review_provenance": {
                    "status": "human_reviewed",
                    "primary_reviewer_id": "reviewer-a",
                    "primary_reviewed_at": "2026-08-04T00:00:00Z",
                    "secondary_reviewer_id": None,
                    "secondary_reviewed_at": None,
                    "disagreement": False,
                    "adjudicator_id": None,
                    "adjudicated_at": None,
                },
                "privacy_provenance": {
                    "status": "approved",
                    "scanner": "synthetic-scanner-v1",
                    "reviewer_id": "privacy-a",
                    "reviewed_at": "2026-08-04T00:00:00Z",
                    "raw_text_exported": False,
                },
                "knowledge_snapshot": {
                    "canonical_seed_sha256": "3" * 64,
                    "published_yonote_chunks": 1,
                    "source_type": "yonote",
                },
            }
        )
        gold_lines.append(
            json.dumps(
                ticket.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    source_artifacts = {
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "cases_source_sha256": "7" * 64,
        "conversations_source_sha256": "8" * 64,
        "normalized_source_sha256": "2" * 64,
    }
    selection = {
        "schema_version": "gold-ticket-selection.v1",
        "dataset_id": entry["dataset_id"],
        "selected_total": entry["cases_total"],
        "source_artifacts": source_artifacts,
        "records": selected_records,
    }
    selection["selection_sha256"] = _canonical_sha256(selection)
    selection_path = dataset_root / f"{entry['dataset_id']}_selection.json"
    selection_path.write_text(
        json.dumps(selection, ensure_ascii=False), encoding="utf-8"
    )
    gold_path = dataset_root / f"{entry['dataset_id']}_gold.jsonl"
    gold_path.write_text("\n".join(gold_lines) + "\n", encoding="utf-8")
    case_ids = sorted(record["case_id_hash"] for record in selected_records)
    entry["hashes"] = {
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "source_snapshot_sha256": _canonical_sha256(source_artifacts),
        "kb_seed_sha256": "3" * 64,
        "selection_manifest_sha256": _sha256(selection_path),
        "review_manifest_sha256": _sha256(gold_path),
        "case_ids_sha256": hashlib.sha256(
            ("\n".join(case_ids) + "\n").encode("utf-8")
        ).hexdigest(),
        "duplicate_exclusion_sha256": "6" * 64,
    }
    return gold_path


def _canonical_sha256(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_registry_rejects_unknown_fields_and_unsafe_roots() -> None:
    entry = _entry()
    entry["relative_root"] = "C:/private/tickets"
    with pytest.raises(ValueError, match="data/private|relative_root"):
        register_dataset(empty_registry(now=NOW), entry, now=NOW)

    entry = _entry()
    entry["unexpected_free_text"] = "must not enter the registry"
    with pytest.raises(ValueError, match="invalid fields"):
        register_dataset(empty_registry(now=NOW), entry, now=NOW)


def test_raw_and_aggregate_classifications_are_fail_closed() -> None:
    raw = _entry(kind="raw_ticket_export")
    register_dataset(empty_registry(now=NOW), raw, now=NOW)

    unsafe_raw = deepcopy(raw)
    unsafe_raw["contains_raw_text"] = False
    with pytest.raises(ValueError, match="raw dataset classification"):
        register_dataset(empty_registry(now=NOW), unsafe_raw, now=NOW)

    operator_raw = _entry("operator-raw", kind="raw_operator_export")
    with pytest.raises(ValueError, match="declare operator answers"):
        register_dataset(empty_registry(now=NOW), operator_raw, now=NOW)
    operator_raw["contains_operator_answers"] = True
    register_dataset(empty_registry(now=NOW), operator_raw, now=NOW)

    aggregate = _entry("safe-counts", kind="safe_aggregate")
    aggregate.update(
        {
            "privacy_class": "aggregate",
            "export_class": "aggregate_allowlisted",
            "contains_raw_text": False,
            "pii_possible": False,
        }
    )
    register_dataset(empty_registry(now=NOW), aggregate, now=NOW)

    self_attested = _entry("derived-counts", kind="derived_analysis")
    self_attested.update(
        {
            "privacy_class": "aggregate",
            "export_class": "aggregate_allowlisted",
            "contains_raw_text": False,
            "pii_possible": False,
        }
    )
    with pytest.raises(ValueError, match="aggregate export classification"):
        register_dataset(empty_registry(now=NOW), self_attested, now=NOW)


def test_registry_rejects_duplicate_refs_roots_and_lineage_cycles() -> None:
    registry = register_dataset(empty_registry(now=NOW), _entry(), now=NOW)
    with pytest.raises(ValueError, match="already registered"):
        register_dataset(registry, _entry(), now=NOW)

    same_root = _entry("other-corpus", root="data/private/tickets/ticket-corpus-v1")
    with pytest.raises(ValueError, match="relative_root"):
        register_dataset(registry, same_root, now=NOW)

    case_alias = _entry(
        "case-alias", root="data/private/TICKETS/TICKET-CORPUS-V1"
    )
    with pytest.raises(ValueError, match="relative_root"):
        register_dataset(registry, case_alias, now=NOW)

    nested = _entry(
        "nested", root="data/private/tickets/ticket-corpus-v1/derived"
    )
    with pytest.raises(ValueError, match="ancestor/descendant"):
        register_dataset(registry, nested, now=NOW)

    first = _entry("first", sources=["second@v1"])
    second = _entry("second", sources=["first@v1"])
    payload = empty_registry(now=NOW)
    payload["datasets"] = [first, second]
    with pytest.raises(ValueError, match="cycle"):
        validate_registry(payload)


def test_review_and_freeze_are_monotonic_and_frozen_payload_is_immutable(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "data" / "private"
    private_root.mkdir(parents=True)
    entry = _gold150()
    gold_path = _materialize_gold_artifacts(private_root, entry)
    registry = register_dataset(empty_registry(now=NOW), entry, now=NOW)
    reviewing = start_review(registry, "gold150@v1", now=NOW)
    assert reviewing["datasets"][0]["state"] == "reviewing"
    with pytest.raises(ValueError, match="states"):
        start_review(reviewing, "gold150@v1", now=NOW)

    completed = complete_human_review(
        reviewing,
        "gold150@v1",
        gold_artifact_path=gold_path,
        private_root=private_root,
        now=NOW,
    )
    frozen = freeze_dataset(
        completed,
        "gold150@v1",
        private_root=private_root,
        now=NOW,
    )
    assert frozen["datasets"][0]["state"] == "frozen"
    assert frozen["datasets"][0]["frozen_payload_sha256"]
    with pytest.raises(ValueError, match="draft or reviewing"):
        freeze_dataset(
            frozen,
            "gold150@v1",
            private_root=private_root,
            now=NOW,
        )

    tampered = deepcopy(frozen)
    tampered["datasets"][0]["owner_role"] = "data_owner"
    with pytest.raises(ValueError, match="modified"):
        validate_registry(tampered)


def test_gold150_requires_human_review_and_exact_provenance_before_freeze(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "data" / "private"
    private_root.mkdir(parents=True)
    pending = _gold150()
    _materialize_gold_artifacts(private_root, pending)
    registry = register_dataset(empty_registry(now=NOW), pending, now=NOW)
    with pytest.raises(ValueError, match="human review"):
        freeze_dataset(
            registry,
            "gold150@v1",
            private_root=private_root,
            now=NOW,
        )

    complete_entry = _gold150("v2")
    gold_path = _materialize_gold_artifacts(private_root, complete_entry)
    registry = register_dataset(empty_registry(now=NOW), complete_entry, now=NOW)
    reviewing = start_review(registry, "gold150@v2", now=NOW)
    completed = complete_human_review(
        reviewing,
        "gold150@v2",
        gold_artifact_path=gold_path,
        private_root=private_root,
        now=NOW,
    )
    completed["datasets"][0]["hashes"]["review_manifest_sha256"] = None
    with pytest.raises(ValueError, match="sealed review artifact hash"):
        freeze_dataset(
            completed,
            "gold150@v2",
            private_root=private_root,
            now=NOW,
        )

    frozen = _register_and_freeze(
        empty_registry(now=NOW),
        _gold150("v3"),
        private_root=private_root,
    )
    assert frozen["datasets"][0]["evaluation_role"] == "calibration_sanity"
    assert frozen["datasets"][0]["independent_evaluation"] is False

    invalid = _gold150()
    invalid["independent_evaluation"] = True
    with pytest.raises(ValueError, match="holdout|calibration"):
        register_dataset(empty_registry(now=NOW), invalid, now=NOW)


def test_complete_review_rebinds_final_artifact_and_freeze_detects_tamper(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "data" / "private"
    private_root.mkdir(parents=True)
    entry = _gold150()
    gold_path = _materialize_gold_artifacts(private_root, entry)
    entry["hashes"]["review_manifest_sha256"] = "7" * 64
    registry = register_dataset(empty_registry(now=NOW), entry, now=NOW)
    reviewing = start_review(registry, "gold150@v1", now=NOW)
    completed = complete_human_review(
        reviewing,
        "gold150@v1",
        gold_artifact_path=gold_path,
        private_root=private_root,
        now=NOW,
    )
    assert completed["datasets"][0]["hashes"]["review_manifest_sha256"] == _sha256(
        gold_path
    )

    selection_path = gold_path.with_name("gold150_selection.json")
    selection_path.write_text(
        selection_path.read_text(encoding="utf-8") + " ", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="selection manifest differs"):
        freeze_dataset(
            completed,
            "gold150@v1",
            private_root=private_root,
            now=NOW,
        )


def test_complete_review_rejects_hardlinked_artifact_tree(tmp_path: Path) -> None:
    private_root = tmp_path / "data" / "private"
    private_root.mkdir(parents=True)
    entry = _gold150()
    gold_path = _materialize_gold_artifacts(private_root, entry)
    alias = gold_path.with_name("unexpected-alias.jsonl")
    try:
        alias.hardlink_to(gold_path)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")
    registry = register_dataset(empty_registry(now=NOW), entry, now=NOW)
    reviewing = start_review(registry, "gold150@v1", now=NOW)

    with pytest.raises(ValueError, match="hardlink"):
        complete_human_review(
            reviewing,
            "gold150@v1",
            gold_artifact_path=gold_path,
            private_root=private_root,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("knowledge", "knowledge snapshot mismatch"),
        ("normalized_source", "normalized source mismatch"),
    ),
)
def test_complete_review_binds_gold_to_exact_source_snapshots(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    private_root = tmp_path / "data" / "private"
    private_root.mkdir(parents=True)
    entry = _gold150()
    gold_path = _materialize_gold_artifacts(private_root, entry)
    rewritten: list[str] = []
    for line in gold_path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        payload.pop("record_sha256")
        if mutation == "knowledge":
            payload["knowledge_snapshot"]["canonical_seed_sha256"] = "9" * 64
        else:
            payload["source_binding"]["normalized_source_sha256"] = "9" * 64
        rewritten.append(
            json.dumps(
                seal_gold_ticket(payload).model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    gold_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    registry = register_dataset(empty_registry(now=NOW), entry, now=NOW)
    reviewing = start_review(registry, "gold150@v1", now=NOW)

    with pytest.raises(ValueError, match=message):
        complete_human_review(
            reviewing,
            "gold150@v1",
            gold_artifact_path=gold_path,
            private_root=private_root,
            now=NOW,
        )


def test_freeze_unsupported_dataset_kind_is_fail_closed() -> None:
    registry = register_dataset(empty_registry(now=NOW), _entry(), now=NOW)
    reviewing = start_review(registry, "ticket-corpus@v1", now=NOW)
    with pytest.raises(ValueError, match="artifact verification is unsupported"):
        freeze_dataset(reviewing, "ticket-corpus@v1", now=NOW)


def test_supersede_requires_frozen_declared_successor_and_is_reciprocal(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "data" / "private"
    private_root.mkdir(parents=True)
    registry = _register_and_freeze(
        empty_registry(now=NOW), _gold150("v1"), private_root=private_root
    )
    successor = _gold150("v2", supersedes="gold150@v1")
    successor_gold = _materialize_gold_artifacts(private_root, successor)
    registry = register_dataset(registry, successor, now=LATER)
    registry = start_review(registry, "gold150@v2", now=LATER)
    registry = complete_human_review(
        registry,
        "gold150@v2",
        gold_artifact_path=successor_gold,
        private_root=private_root,
        now=LATER,
    )
    with pytest.raises(ValueError, match="both current and successor"):
        supersede_dataset(registry, "gold150@v1", "gold150@v2", now=LATER)

    registry = freeze_dataset(
        registry, "gold150@v2", private_root=private_root, now=LATER
    )
    updated = supersede_dataset(registry, "gold150@v1", "gold150@v2", now=LATER)
    by_version = {entry["version"]: entry for entry in updated["datasets"]}
    assert by_version["v1"]["state"] == "superseded"
    assert by_version["v1"]["superseded_by"] == "gold150@v2"
    assert by_version["v2"]["supersedes"] == "gold150@v1"


def test_retention_plan_is_preview_only_and_blocks_active_children(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "data" / "private"
    private_root.mkdir(parents=True)
    parent = _gold150(
        dataset_id="ticket-corpus",
        delete_after=PAST_DELETE,
        retention_policy_ref="owner-approved-30d",
    )
    registry = _register_and_freeze(
        empty_registry(now=NOW), parent, private_root=private_root
    )
    successor = _gold150(
        version="v2",
        dataset_id="ticket-corpus",
        supersedes="ticket-corpus@v1",
    )
    registry = _register_and_freeze(
        registry, successor, private_root=private_root
    )
    registry = supersede_dataset(
        registry,
        "ticket-corpus@v1",
        "ticket-corpus@v2",
        now=NOW,
    )
    plan = build_retention_plan(registry, as_of=LATER)
    assert plan["deletion_performed"] is False
    assert plan["candidates"] == ["ticket-corpus@v1"]

    child = _entry(
        "active-child",
        sources=["ticket-corpus@v1"],
        requires_parent_bytes=True,
    )
    registry = register_dataset(registry, child, now=NOW)
    blocked = build_retention_plan(registry, as_of=LATER)
    parent_block = next(
        item for item in blocked["blocked"] if item["dataset_ref"] == "ticket-corpus@v1"
    )
    assert "active_children_require_parent_bytes" in parent_block["reasons"]
    assert parent_block["blocking_children"] == ["active-child@v1"]


def test_unapproved_retention_is_fail_closed(tmp_path: Path) -> None:
    private_root = tmp_path / "data" / "private"
    private_root.mkdir(parents=True)
    registry = _register_and_freeze(
        empty_registry(now=NOW),
        _gold150(dataset_id="ticket-corpus"),
        private_root=private_root,
    )
    plan = build_retention_plan(registry, as_of=LATER)
    row = next(item for item in plan["blocked"] if item["dataset_ref"] == "ticket-corpus@v1")
    assert "retention_unapproved" in row["reasons"]
    assert plan["deletion_performed"] is False


def test_registry_round_trip_is_private_atomic_and_rejects_hardlink(tmp_path: Path) -> None:
    private_root = tmp_path / "data" / "private"
    registry_path = private_root / "_registry" / "datasets.json"
    registry = register_dataset(empty_registry(now=NOW), _entry(), now=NOW)
    save_registry(registry_path, registry, private_root=private_root)
    assert load_registry(registry_path, private_root=private_root) == registry

    alias = private_root / "_registry" / "alias.json"
    try:
        alias.hardlink_to(registry_path)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")
    with pytest.raises(ValueError, match="hardlink"):
        load_registry(registry_path, private_root=private_root)


def test_inventory_reads_metadata_only_and_does_not_mutate(tmp_path: Path) -> None:
    private_root = tmp_path / "data" / "private"
    dataset = private_root / "tickets" / "legacy-one"
    dataset.mkdir(parents=True)
    canary = "PRIVATE-CONTENT-MUST-NOT-APPEAR"
    (dataset / "source.json").write_text(canary, encoding="utf-8")
    before = (dataset / "source.json").read_bytes()

    report = inventory_private_datasets(
        private_root,
        registry=empty_registry(now=NOW),
        areas=("tickets",),
    )

    serialized = json.dumps(report, ensure_ascii=False)
    assert canary not in serialized
    assert report["content_read"] is False
    assert report["mutation_performed"] is False
    assert report["counts"]["unregistered"] == 1
    assert (dataset / "source.json").read_bytes() == before


def test_default_inventory_includes_private_eval_area(tmp_path: Path) -> None:
    private_root = tmp_path / "data" / "private"
    dataset = private_root / "eval" / "gold-draft"
    dataset.mkdir(parents=True)
    (dataset / "selection.json").write_text("{}", encoding="utf-8")

    report = inventory_private_datasets(
        private_root,
        registry=empty_registry(now=NOW),
    )

    assert any(row["relative_root"] == "data/private/eval/gold-draft" for row in report["roots"])


def test_raw_ticket_demo_mode_cannot_write_to_reports_or_lookalike_private(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "data" / "private"
    source = private_root / "tickets" / "source.xlsx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"private-source")

    with pytest.raises(ValueError, match="under data/private"):
        validate_output_policy(
            source=source,
            output=tmp_path / "reports" / "cases.json",
            profile_output=tmp_path / "reports" / "profile.json",
            raw_ticket_candidates=True,
            private_root=private_root,
        )
    with pytest.raises(ValueError, match="under data/private"):
        validate_output_policy(
            source=source,
            output=tmp_path / "data" / "private-copy" / "cases.json",
            profile_output=private_root / "tickets" / "profile.json",
            raw_ticket_candidates=True,
            private_root=private_root,
        )


def test_raw_ticket_demo_mode_allows_only_distinct_private_outputs(tmp_path: Path) -> None:
    private_root = tmp_path / "data" / "private"
    source = private_root / "tickets" / "source.xlsx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"private-source")
    output = private_root / "tickets" / "derived" / "cases.json"
    profile = private_root / "tickets" / "derived" / "profile.json"

    validate_output_policy(
        source=source,
        output=output,
        profile_output=profile,
        raw_ticket_candidates=True,
        private_root=private_root,
    )
    with pytest.raises(ValueError, match="different files"):
        validate_output_policy(
            source=source,
            output=output,
            profile_output=output,
            raw_ticket_candidates=True,
            private_root=private_root,
        )
    with pytest.raises(ValueError, match="must not alias the source"):
        validate_output_policy(
            source=source,
            output=source,
            profile_output=profile,
            raw_ticket_candidates=True,
            private_root=private_root,
        )

    alias = private_root / "tickets" / "source-alias.xlsx"
    try:
        alias.hardlink_to(source)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")
    with pytest.raises(ValueError, match="hardlink"):
        validate_output_policy(
            source=alias,
            output=output,
            profile_output=profile,
            raw_ticket_candidates=True,
            private_root=private_root,
        )

    validate_output_policy(
        source=tmp_path / "missing-curated-source.xlsx",
        output=tmp_path / "reports" / "cases.json",
        profile_output=tmp_path / "reports" / "profile.json",
        raw_ticket_candidates=False,
        private_root=private_root,
    )
