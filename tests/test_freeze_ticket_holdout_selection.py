from __future__ import annotations

import csv
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import scripts.build_ticket_product_review as review_builder
import scripts.freeze_ticket_holdout_selection as freezer
from scripts.build_ticket_product_review import source_case_fingerprint
from scripts.freeze_ticket_holdout_selection import freeze_holdout_selection

RUNTIME_SHA = "a" * 40


@pytest.fixture(autouse=True)
def _patch_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "project"
    private_root = project_root / "data" / "private"
    private_root.mkdir(parents=True)
    monkeypatch.setattr(freezer, "PROJECT_ROOT", project_root.resolve())
    monkeypatch.setattr(freezer, "PRIVATE_DATA_ROOT", private_root.resolve())
    monkeypatch.setattr(
        review_builder,
        "PRIVATE_DATA_ROOT",
        private_root.resolve(),
    )


def _private_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "project" / "data" / "private" / "holdout"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _case(
    label: str,
    *,
    split: str = "holdout",
    profile: str = "dates",
    route: str = "answer",
    multiturn_status: str = "single_turn",
    cluster: str | None = None,
    component: str | None = None,
) -> dict[str, object]:
    digest = hashlib.sha256(label.encode()).hexdigest()[:24]
    return {
        "schema_version": "2.0.0",
        "ticket_id_hash": digest,
        "query": f"ПРИВАТНЫЙ ЗАПРОС {label}",
        "duplicate_cluster_id": cluster or digest,
        "duplicate_component_id": component or digest,
        "category": "forums",
        "topic": "question",
        "entity": None,
        "channel": "api",
        "expected_response_profile": profile,
        "expected_route": route,
        "multiturn_status": multiturn_status,
        "time_sensitive": False,
        "difficulty": "simple",
        "role_reconstruction_status": "complete",
        "available_at": "2026-05-17T12:00:00+00:00",
        "split": split,
        "operator_answer_included": False,
        "operator_answer_used_as_fact": False,
    }


def _write_jsonl(path: Path, cases: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
        encoding="utf-8",
    )


def _write_selection(path: Path, cases: list[dict[str, object]]) -> None:
    rows = [
        {
            "case_id_hash": str(case["ticket_id_hash"]),
            "duplicate_cluster_id": str(case["duplicate_cluster_id"]),
            "source_schema_version": str(case["schema_version"]),
            "source_case_fingerprint": source_case_fingerprint(case),
            "aspect": str(case["expected_response_profile"]),
            "expected_route": str(case["expected_route"]),
            "multiturn_status": str(case["multiturn_status"]),
            "time_bucket": "2026-05",
        }
        for case in cases
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _kb_seed(tmp_path: Path) -> Path:
    path = tmp_path / "project" / "data" / "knowledge_base_seed.json"
    path.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "yonote_chunk",
                    "status": "published",
                    "source_type": "yonote",
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def _artifact_manifest(directory: Path, paths: list[Path]) -> Path:
    artifacts = [
        {
            "path": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in paths
    ]
    manifest = directory / "artifact_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "complete": True,
                "artifact_count": len(artifacts),
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _write_review_workbook(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as workbook:
        workbook.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>',
        )
        workbook.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>',
        )


def _evidence(
    tmp_path: Path,
    *,
    directory: Path,
    artifacts: list[Path],
    stress_total: int = 20,
) -> dict[str, Path]:
    stress = tmp_path / "project" / "eval" / "stress.json"
    stress.parent.mkdir(parents=True, exist_ok=True)
    stress.write_text(
        json.dumps([{"id": f"stress-{index}"} for index in range(stress_total)]),
        encoding="utf-8",
    )
    review_workbook = directory / "review.xlsx"
    _write_review_workbook(review_workbook)
    return {
        "stress_cases_path": stress,
        "artifact_manifest_path": _artifact_manifest(directory, artifacts),
        "review_workbook_path": review_workbook,
    }


def _freeze(
    tmp_path: Path,
    *,
    selected: list[dict[str, object]],
    source_cases: list[dict[str, object]] | None = None,
    comparison: list[dict[str, object]] | None = None,
    route_counts: dict[str, int] | None = None,
    pre_run_exclusions: dict[str, str] | None = None,
) -> tuple[dict[str, object], Path]:
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    comparison_path = directory / "calibration.jsonl"
    validation_path = directory / "validation.jsonl"
    output = directory / "freeze.json"
    _write_jsonl(source, source_cases or selected)
    _write_selection(selection, selected)
    _write_jsonl(
        comparison_path,
        comparison or [_case("calibration", split="calibration")],
    )
    _write_jsonl(
        validation_path,
        [_case("validation", split="validation")],
    )
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, comparison_path, validation_path],
    )
    result = freeze_holdout_selection(
        source_path=source,
        selection_path=selection,
        comparison_paths=[comparison_path, validation_path],
        output_path=output,
        runtime_git_sha=RUNTIME_SHA,
        kb_seed_path=_kb_seed(tmp_path),
        expected_total=len(selected),
        expected_route_counts=route_counts,
        pre_run_exclusions=pre_run_exclusions,
        **evidence,
    )
    return result, output


def test_freeze_seals_only_private_identifiers_and_aggregate_counts(
    tmp_path: Path,
) -> None:
    selected = [
        _case("answer", profile="dates"),
        _case(
            "escalate",
            profile="technical",
            route="escalate",
        ),
    ]

    result, output = _freeze(
        tmp_path,
        selected=selected,
        route_counts={"answer": 1, "escalate": 1},
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    serialized = output.read_text(encoding="utf-8")

    assert result["cases_total"] == 2
    assert result["execution_allowed"] is False
    assert result["cross_split_overlap"] == {
        "case_ids": 0,
        "duplicate_clusters": 0,
        "duplicate_components": 0,
    }
    assert payload["selection_status"] == "sealed_pending_human_review"
    assert payload["route_counts"] == {"answer": 1, "escalate": 1}
    assert payload["multiturn_status_counts"] == {"single_turn": 2}
    assert payload["selection"]["reproduction"]["input_cases"] == 2
    assert payload["selection"]["reproduction"]["eligible_cases"] == 2
    assert payload["selection"]["reproduction"]["selected_cases"] == 2
    assert payload["stress_suite"]["included_in_conversion"] is False
    assert payload["freeze_contract_sha256"] == freezer._freeze_contract_sha256(payload)
    assert "ПРИВАТНЫЙ ЗАПРОС" not in serialized
    assert "raw_operator_answer" not in serialized

    payload["cases_total"] = 3
    with pytest.raises(ValueError, match="self-hash mismatch"):
        freezer._verify_freeze_contract(payload)


def test_freeze_binds_pre_run_exclusion_and_replacement_selection(
    tmp_path: Path,
) -> None:
    source_cases = [
        _case(
            f"documents-escalate-{index}",
            profile="documents",
            route="escalate",
        )
        for index in range(3)
    ]
    ordered = sorted(
        source_cases,
        key=lambda case: str(case["ticket_id_hash"]),
    )
    excluded_case_id = str(ordered[0]["ticket_id_hash"])

    result, output = _freeze(
        tmp_path,
        selected=ordered[1:],
        source_cases=source_cases,
        route_counts={"escalate": 2},
        pre_run_exclusions={excluded_case_id: "not_user_turn"},
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert result["pre_run_exclusions"] == {
        "count": 1,
        "cases": [
            {
                "case_id_hash": excluded_case_id,
                "reason": "not_user_turn",
            }
        ],
    }
    assert payload["selection"]["reproduction"]["excluded_cases"] == 1
    assert (
        payload["selection"]["reproduction"][
            "excluded_case_ids_sha256"
        ]
        == hashlib.sha256(
            f"{excluded_case_id}\n".encode()
        ).hexdigest()
    )


def test_freeze_rejects_cross_split_duplicate_component(
    tmp_path: Path,
) -> None:
    selected = [_case("holdout", component="f" * 24)]
    comparison = [
        _case(
            "calibration",
            split="calibration",
            component="f" * 24,
        )
    ]

    with pytest.raises(ValueError, match="overlaps"):
        _freeze(
            tmp_path,
            selected=selected,
            comparison=comparison,
        )


def test_freeze_rejects_cherry_picked_replacement_with_same_labels(
    tmp_path: Path,
) -> None:
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    canonical_summary = directory / "canonical-summary.csv"
    canonical_manifest = directory / "canonical-selection.csv"
    selection = directory / "selection.csv"
    calibration = directory / "calibration.jsonl"
    validation = directory / "validation.jsonl"
    output = directory / "freeze.json"
    candidates = [_case("candidate-a"), _case("candidate-b")]
    _write_jsonl(source, candidates)
    review_builder.build_review_exports(
        source,
        canonical_summary,
        canonical_manifest,
        top_n=20,
        min_per_stratum=0,
        total=1,
        split="holdout",
        selection_mode="profile_route_frequency",
        multiturn_status="single_turn",
    )
    canonical_ids = {
        row["case_id_hash"]
        for row in csv.DictReader(
            canonical_manifest.read_text(encoding="utf-8-sig").splitlines()
        )
    }
    replacement = next(
        case
        for case in candidates
        if str(case["ticket_id_hash"]) not in canonical_ids
    )
    _write_selection(selection, [replacement])
    _write_jsonl(calibration, [_case("calibration", split="calibration")])
    _write_jsonl(validation, [_case("validation", split="validation")])
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, calibration, validation],
    )

    with pytest.raises(ValueError, match="deterministic selector"):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[calibration, validation],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
            **evidence,
        )


def test_freeze_rejects_stale_source_fingerprint(tmp_path: Path) -> None:
    selected = [_case("holdout")]
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    comparison = directory / "calibration.jsonl"
    validation = directory / "validation.jsonl"
    output = directory / "freeze.json"
    _write_jsonl(source, selected)
    _write_selection(selection, selected)
    rows = list(csv.DictReader(selection.read_text(encoding="utf-8").splitlines()))
    rows[0]["source_case_fingerprint"] = "0" * 64
    with selection.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    _write_jsonl(comparison, [_case("calibration", split="calibration")])
    _write_jsonl(validation, [_case("validation", split="validation")])
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, comparison, validation],
    )

    with pytest.raises(ValueError, match="stale source fingerprint"):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[comparison, validation],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
            **evidence,
        )


def test_freeze_rejects_multi_turn_case_in_first_turn_baseline(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="only single_turn"):
        _freeze(
            tmp_path,
            selected=[_case("multi", multiturn_status="multi_turn")],
        )


def test_freeze_rejects_stale_multiturn_status(tmp_path: Path) -> None:
    selected = [_case("holdout")]
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    comparison = directory / "calibration.jsonl"
    validation = directory / "validation.jsonl"
    output = directory / "freeze.json"
    _write_jsonl(source, selected)
    _write_selection(selection, selected)
    rows = list(csv.DictReader(selection.read_text(encoding="utf-8").splitlines()))
    rows[0]["multiturn_status"] = "multi_turn"
    with selection.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    _write_jsonl(comparison, [_case("calibration", split="calibration")])
    _write_jsonl(validation, [_case("validation", split="validation")])
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, comparison, validation],
    )

    with pytest.raises(ValueError, match="stale multiturn status"):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[comparison, validation],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
            **evidence,
        )


def test_freeze_rejects_unexpected_route_distribution(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="route counts"):
        _freeze(
            tmp_path,
            selected=[_case("answer")],
            route_counts={"escalate": 1},
        )


def test_freeze_rejects_output_outside_private_root(tmp_path: Path) -> None:
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    calibration = directory / "calibration.jsonl"
    validation = directory / "validation.jsonl"
    selected = [_case("holdout")]
    _write_jsonl(source, selected)
    _write_selection(selection, selected)
    _write_jsonl(calibration, [_case("calibration", split="calibration")])
    _write_jsonl(validation, [_case("validation", split="validation")])

    with pytest.raises(ValueError, match="under data/private"):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[calibration, validation],
            output_path=tmp_path / "public.json",
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
        )


@pytest.mark.parametrize("comparison_total", [0, 1, 3])
def test_freeze_requires_exactly_two_comparison_inputs(
    tmp_path: Path,
    comparison_total: int,
) -> None:
    directory = _private_dir(tmp_path)
    output = directory / "freeze.json"

    with pytest.raises(ValueError, match="exactly two comparison inputs"):
        freeze_holdout_selection(
            source_path=directory / "holdout.jsonl",
            selection_path=directory / "selection.csv",
            comparison_paths=[
                directory / f"comparison-{index}.jsonl" for index in range(comparison_total)
            ],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
        )

    assert not output.exists()


def test_freeze_requires_one_calibration_and_one_validation_input(
    tmp_path: Path,
) -> None:
    selected = [_case("holdout")]
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    calibration_one = directory / "calibration-one.jsonl"
    calibration_two = directory / "calibration-two.jsonl"
    output = directory / "freeze.json"
    _write_jsonl(source, selected)
    _write_selection(selection, selected)
    _write_jsonl(
        calibration_one,
        [_case("calibration-one", split="calibration")],
    )
    _write_jsonl(
        calibration_two,
        [_case("calibration-two", split="calibration")],
    )
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, calibration_one, calibration_two],
    )

    with pytest.raises(
        ValueError,
        match="exact calibration and validation comparisons",
    ):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[calibration_one, calibration_two],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
            **evidence,
        )

    assert not output.exists()


def test_freeze_rejects_empty_comparison_input(tmp_path: Path) -> None:
    selected = [_case("holdout")]
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    calibration = directory / "calibration.jsonl"
    validation = directory / "validation.jsonl"
    output = directory / "freeze.json"
    _write_jsonl(source, selected)
    _write_selection(selection, selected)
    calibration.write_text("", encoding="utf-8")
    _write_jsonl(validation, [_case("validation", split="validation")])
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, calibration, validation],
    )

    with pytest.raises(ValueError, match="Source cases are empty"):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[calibration, validation],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
            **evidence,
        )

    assert not output.exists()


def test_freeze_rejects_overlap_between_comparison_splits(
    tmp_path: Path,
) -> None:
    selected = [_case("holdout")]
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    calibration = directory / "calibration.jsonl"
    validation = directory / "validation.jsonl"
    output = directory / "freeze.json"
    shared_component = "f" * 24
    _write_jsonl(source, selected)
    _write_selection(selection, selected)
    _write_jsonl(
        calibration,
        [
            _case(
                "calibration",
                split="calibration",
                component=shared_component,
            )
        ],
    )
    _write_jsonl(
        validation,
        [
            _case(
                "validation",
                split="validation",
                component=shared_component,
            )
        ],
    )
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, calibration, validation],
    )

    with pytest.raises(ValueError, match="comparisons overlap"):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[calibration, validation],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
            **evidence,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    ("missing_argument", "expected_error"),
    [
        ("artifact_manifest_path", "requires an artifact manifest"),
        ("stress_cases_path", "requires a separate stress suite"),
        ("review_workbook_path", "requires a private review workbook"),
    ],
)
def test_freeze_requires_all_independence_evidence(
    tmp_path: Path,
    missing_argument: str,
    expected_error: str,
) -> None:
    directory = _private_dir(tmp_path)
    kwargs: dict[str, object] = {
        "source_path": directory / "holdout.jsonl",
        "selection_path": directory / "selection.csv",
        "comparison_paths": [
            directory / "calibration.jsonl",
            directory / "validation.jsonl",
        ],
        "output_path": directory / "freeze.json",
        "runtime_git_sha": RUNTIME_SHA,
        "kb_seed_path": _kb_seed(tmp_path),
        "stress_cases_path": tmp_path / "project" / "eval" / "stress.json",
        "artifact_manifest_path": directory / "artifact_manifest.json",
        "review_workbook_path": directory / "review.xlsx",
        "expected_total": 1,
    }
    kwargs[missing_argument] = None

    with pytest.raises(ValueError, match=expected_error):
        freeze_holdout_selection(**kwargs)  # type: ignore[arg-type]

    assert not Path(kwargs["output_path"]).exists()


@pytest.mark.parametrize(
    ("manifest_change", "expected_error"),
    [
        ("incomplete", "not complete"),
        ("missing_source", "does not bind"),
        ("stale_hash", "stale hash"),
        ("stale_size", "stale size"),
    ],
)
def test_freeze_rejects_invalid_source_artifact_manifest(
    tmp_path: Path,
    manifest_change: str,
    expected_error: str,
) -> None:
    selected = [_case("holdout")]
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    calibration = directory / "calibration.jsonl"
    validation = directory / "validation.jsonl"
    output = directory / "freeze.json"
    _write_jsonl(source, selected)
    _write_selection(selection, selected)
    _write_jsonl(calibration, [_case("calibration", split="calibration")])
    _write_jsonl(validation, [_case("validation", split="validation")])
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, calibration, validation],
    )
    manifest_path = evidence["artifact_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest_change == "incomplete":
        manifest["complete"] = False
    elif manifest_change == "missing_source":
        manifest["artifacts"] = [
            item for item in manifest["artifacts"] if item["path"] != source.name
        ]
        manifest["artifact_count"] = len(manifest["artifacts"])
    elif manifest_change == "stale_hash":
        manifest["artifacts"][0]["sha256"] = "0" * 64
    else:
        manifest["artifacts"][0]["size_bytes"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=expected_error):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[calibration, validation],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
            **evidence,
        )

    assert not output.exists()


def test_freeze_validates_every_complete_manifest_artifact(
    tmp_path: Path,
) -> None:
    selected = [_case("holdout")]
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    calibration = directory / "calibration.jsonl"
    validation = directory / "validation.jsonl"
    extra = directory / "extra.txt"
    output = directory / "freeze.json"
    _write_jsonl(source, selected)
    _write_selection(selection, selected)
    _write_jsonl(calibration, [_case("calibration", split="calibration")])
    _write_jsonl(validation, [_case("validation", split="validation")])
    extra.write_text("bound source artifact", encoding="utf-8")
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, calibration, validation, extra],
    )
    extra.write_text("changed after manifest", encoding="utf-8")

    with pytest.raises(ValueError, match="stale hash for extra.txt"):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[calibration, validation],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
            **evidence,
        )

    assert not output.exists()


@pytest.mark.parametrize("stress_total", [19, 21])
def test_freeze_requires_exactly_twenty_stress_cases(
    tmp_path: Path,
    stress_total: int,
) -> None:
    selected = [_case("holdout")]
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    calibration = directory / "calibration.jsonl"
    validation = directory / "validation.jsonl"
    output = directory / "freeze.json"
    _write_jsonl(source, selected)
    _write_selection(selection, selected)
    _write_jsonl(calibration, [_case("calibration", split="calibration")])
    _write_jsonl(validation, [_case("validation", split="validation")])
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, calibration, validation],
        stress_total=stress_total,
    )

    with pytest.raises(ValueError, match="exactly 20 cases"):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[calibration, validation],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
            **evidence,
        )

    assert not output.exists()


def test_freeze_requires_valid_private_review_workbook(
    tmp_path: Path,
) -> None:
    selected = [_case("holdout")]
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    calibration = directory / "calibration.jsonl"
    validation = directory / "validation.jsonl"
    output = directory / "freeze.json"
    _write_jsonl(source, selected)
    _write_selection(selection, selected)
    _write_jsonl(calibration, [_case("calibration", split="calibration")])
    _write_jsonl(validation, [_case("validation", split="validation")])
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, calibration, validation],
    )
    invalid_workbook = directory / "invalid.xlsx"
    invalid_workbook.write_bytes(b"not an xlsx")
    evidence["review_workbook_path"] = invalid_workbook

    with pytest.raises(ValueError, match="not a valid XLSX"):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[calibration, validation],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
            **evidence,
        )

    assert not output.exists()


def test_freeze_rejects_review_workbook_outside_private_root(
    tmp_path: Path,
) -> None:
    selected = [_case("holdout")]
    directory = _private_dir(tmp_path)
    source = directory / "holdout.jsonl"
    selection = directory / "selection.csv"
    calibration = directory / "calibration.jsonl"
    validation = directory / "validation.jsonl"
    output = directory / "freeze.json"
    _write_jsonl(source, selected)
    _write_selection(selection, selected)
    _write_jsonl(calibration, [_case("calibration", split="calibration")])
    _write_jsonl(validation, [_case("validation", split="validation")])
    evidence = _evidence(
        tmp_path,
        directory=directory,
        artifacts=[source, calibration, validation],
    )
    public_workbook = tmp_path / "project" / "review.xlsx"
    _write_review_workbook(public_workbook)
    evidence["review_workbook_path"] = public_workbook

    with pytest.raises(ValueError, match="under data/private"):
        freeze_holdout_selection(
            source_path=source,
            selection_path=selection,
            comparison_paths=[calibration, validation],
            output_path=output,
            runtime_git_sha=RUNTIME_SHA,
            kb_seed_path=_kb_seed(tmp_path),
            expected_total=1,
            **evidence,
        )

    assert not output.exists()
