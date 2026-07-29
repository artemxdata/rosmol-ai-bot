from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import scripts.build_reviewed_ticket_ask_cases as exporter
from scripts.build_reviewed_ticket_ask_cases import (
    KB_SEED_HASH_CANONICALIZATION,
    build_reviewed_ticket_ask_cases,
    holdout_review_payload_sha256,
    seal_holdout_review_payload_hashes,
)
from scripts.build_ticket_product_review import source_case_fingerprint

DEFAULT_CHUNK_ID = "yonote_chunk_a"
RUNTIME_SHA = "4c6262455d1338c6e0f26b8900a5f66e64a97489"


def _canonical_json_sha256(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()

MANIFEST_FIELDS = (
    "case_id_hash",
    "intent",
    "aspect",
    "entity_class",
    "expected_route",
    "expected_escalation_reason",
    "time_sensitive",
    "role_reconstruction_status",
    "role_verdict",
    "multiturn_status",
    "label_verdict",
    "reviewer",
    "reviewed_at",
    "source_schema_version",
    "source_case_fingerprint",
    "approved_kb_seed_sha256",
    "corrected_intent",
    "corrected_aspect",
    "corrected_entity_class",
    "corrected_route",
    "corrected_escalation_reason",
    "deidentified_query",
    "privacy_verdict",
    "date_privacy_verdict",
    "review_workbook_sha256",
    "review_source_sha256",
    "review_selection_sha256",
    "review_freeze_contract_sha256",
    "review_payload_sha256",
    "answerable_from_snapshot",
    "approved_chunk_ids",
    "forbidden_profiles",
    "include_in_calibration",
    "include_in_validation",
    "include_in_holdout",
)


@pytest.fixture(autouse=True)
def _patch_data_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    versioned_root = (tmp_path / "data").resolve()
    monkeypatch.setattr(exporter, "VERSIONED_DATA_ROOT", versioned_root)
    monkeypatch.setattr(
        exporter,
        "PRIVATE_DATA_ROOT",
        (versioned_root / "private").resolve(),
    )
    monkeypatch.setattr(exporter, "_EXPECTED_HOLDOUT_CASES_TOTAL", 1)

    def load_test_workbook_rows(**kwargs: object) -> tuple[
        list[str],
        list[dict[str, str]],
    ]:
        directory = Path(str(kwargs["workbook_path"])).resolve().parent
        candidates = (
            directory / "holdout_review_manifest.csv",
            directory / "top20_review_manifest.csv",
            directory / "selection_manifest.csv",
        )
        manifest = next(
            (path for path in candidates if path.is_file()),
            None,
        )
        if manifest is None:
            raise AssertionError("Test review manifest is missing")
        with manifest.open("r", encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            return (
                list(reader.fieldnames or []),
                [dict(row) for row in reader],
            )

    monkeypatch.setattr(
        exporter,
        "load_holdout_review_workbook_rows",
        load_test_workbook_rows,
    )


def _private_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data" / "private" / "ticket-review"
    directory.mkdir(parents=True)
    return directory


def _source_case(
    case_id_hash: str,
    *,
    query: str = "Когда проходит форум?",
    route: str = "answer",
    split: str = "calibration",
    label_status: str = "weak_unreviewed",
    operator_answer_included: bool = False,
    operator_answer_used_as_fact: bool = False,
) -> dict[str, object]:
    return {
        "ticket_id_hash": case_id_hash,
        "query": query,
        "schema_version": "1.0.0",
        "category": "forums",
        "topic": "dates",
        "entity": "named-forum",
        "expected_response_profile": "dates",
        "channel": "Вконтакте",
        "expected_route": route,
        "expected_escalation_reason": (
            "personal_status" if route == "escalate" else None
        ),
        "answerable_from_snapshot": route == "answer",
        "forbidden_response_profiles": [],
        "role_reconstruction_status": "complete",
        "multiturn_status": "single_turn",
        "split": split,
        "label_status": label_status,
        "operator_answer_included": operator_answer_included,
        "operator_answer_used_as_fact": operator_answer_used_as_fact,
        "raw_operator_answer": "Этот текст нельзя экспортировать",
    }


def _manifest_row(
    case_id_hash: str,
    *,
    verdict: str = "approved",
    include: str = "true",
    route: str = "answer",
    role_status: str = "complete",
    role_verdict: str = "confirmed_user_turn",
    corrected_intent: str = "",
    corrected_aspect: str = "",
    corrected_entity: str = "",
    corrected_route: str = "",
    corrected_escalation_reason: str = "",
    deidentified_query: str = "",
    privacy_verdict: str = "",
    date_privacy_verdict: str = "not_present",
    review_payload_sha256: str = "",
    include_in_validation: str = "",
    include_in_holdout: str = "",
    approved_chunk_ids: str | None = None,
    forbidden_profiles: str = "",
    reviewer: str = "reviewer_01",
    reviewed_at: str = "2026-07-29T12:00:00+07:00",
    source_schema_version: str = "",
    fingerprint: str = "",
    approved_kb_seed_sha256: str = "",
) -> dict[str, str]:
    if approved_chunk_ids is None:
        approved_chunk_ids = DEFAULT_CHUNK_ID if route == "answer" else ""
    return {
        "case_id_hash": case_id_hash,
        "intent": "форумы.сроки",
        "aspect": "dates",
        "entity_class": "forum:named",
        "expected_route": route,
        "expected_escalation_reason": (
            "personal_status" if route == "escalate" else ""
        ),
        "time_sensitive": "true",
        "role_reconstruction_status": role_status,
        "role_verdict": role_verdict,
        "multiturn_status": "single_turn",
        "label_verdict": verdict,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "source_schema_version": source_schema_version,
        "source_case_fingerprint": fingerprint,
        "approved_kb_seed_sha256": approved_kb_seed_sha256,
        "corrected_intent": corrected_intent,
        "corrected_aspect": corrected_aspect,
        "corrected_entity_class": corrected_entity,
        "corrected_route": corrected_route,
        "corrected_escalation_reason": corrected_escalation_reason,
        "deidentified_query": deidentified_query,
        "privacy_verdict": privacy_verdict,
        "date_privacy_verdict": date_privacy_verdict,
        "review_workbook_sha256": "",
        "review_source_sha256": "",
        "review_selection_sha256": "",
        "review_freeze_contract_sha256": "",
        "review_payload_sha256": review_payload_sha256,
        "answerable_from_snapshot": "true" if route == "answer" else "false",
        "approved_chunk_ids": approved_chunk_ids,
        "forbidden_profiles": forbidden_profiles,
        "include_in_calibration": include,
        "include_in_validation": include_in_validation,
        "include_in_holdout": include_in_holdout,
    }


def _write_jsonl(path: Path, cases: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n"
            for case in cases
        ),
        encoding="utf-8",
    )


def _write_manifest(
    path: Path,
    rows: list[dict[str, str]],
    source_cases: list[dict[str, object]],
    seed_path: Path,
) -> None:
    seed_sha256 = _canonical_json_sha256(seed_path)
    sources_by_id = {
        str(case["ticket_id_hash"]): case
        for case in source_cases
    }
    bound_rows: list[dict[str, str]] = []
    for original in rows:
        row = dict(original)
        source = sources_by_id.get(row["case_id_hash"])
        if source is not None:
            row["source_schema_version"] = (
                row.get("source_schema_version")
                or str(source["schema_version"])
            )
            row["source_case_fingerprint"] = (
                row.get("source_case_fingerprint")
                or source_case_fingerprint(source)
            )
        row["approved_kb_seed_sha256"] = (
            row.get("approved_kb_seed_sha256")
            or seed_sha256
        )
        bound_rows.append(row)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(bound_rows)


def _write_kb_seed(
    tmp_path: Path,
    manifest_rows: list[dict[str, str]],
    *,
    seed_rows: list[dict[str, object]] | None = None,
) -> Path:
    path = tmp_path / "data" / "knowledge_base_seed.json"
    if seed_rows is None:
        chunk_ids = sorted(
            {
                chunk_id.strip()
                for row in manifest_rows
                for chunk_id in str(row.get("approved_chunk_ids") or "").split("|")
                if chunk_id.strip()
            }
        )
        if not chunk_ids:
            chunk_ids = ["yonote_unused_chunk"]
        seed_rows = [
            {
                "chunk_id": chunk_id,
                "status": "published",
                "source_type": "yonote",
            }
            for chunk_id in chunk_ids
        ]
    path.write_text(
        json.dumps(seed_rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _write_freeze(
    path: Path,
    *,
    source_path: Path,
    source_cases: list[dict[str, object]],
    selection_path: Path,
    manifest_rows: list[dict[str, str]],
    seed_path: Path,
    template_workbook_path: Path,
) -> Path:
    case_ids = sorted(str(row["case_id_hash"]) for row in manifest_rows)
    selected_ids_sha256 = hashlib.sha256(
        ("\n".join(case_ids) + "\n").encode("utf-8")
    ).hexdigest()
    profile_counts = Counter(
        str(row.get("corrected_aspect") or row["aspect"])
        for row in manifest_rows
    )
    route_counts = Counter(
        str(row.get("corrected_route") or row["expected_route"])
        for row in manifest_rows
    )
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "baseline_id": "independent_holdout_80_v1",
        "created_at": "2026-07-29T05:00:00Z",
        "runtime_git_sha": RUNTIME_SHA,
        "selection_status": "sealed_pending_human_review",
        "execution_allowed": False,
        "measurement_scope": (
            "independent_directional_single_turn_first_response_holdout; "
            "not ticket conversion and not a final 50-60% conversion claim"
        ),
        "cases_total": len(case_ids),
        "unique_case_ids": len(case_ids),
        "multiturn_status_counts": {
            "single_turn": len(case_ids),
        },
        "profile_counts": dict(sorted(profile_counts.items())),
        "route_counts": dict(sorted(route_counts.items())),
        "comparison_splits": ["calibration", "validation"],
        "cross_split_overlap": {
            "case_ids": 0,
            "duplicate_clusters": 0,
            "duplicate_components": 0,
        },
        "source": {
            "path": str(source_path.resolve()),
            "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "cases_total": len(source_cases),
        },
        "selection": {
            "path": str(selection_path.resolve()),
            "sha256": hashlib.sha256(selection_path.read_bytes()).hexdigest(),
            "selected_case_ids_sha256": selected_ids_sha256,
            "case_id_hashes": case_ids,
        },
        "knowledge_snapshot": {
            "canonical_sha256": _canonical_json_sha256(seed_path),
        },
        "pre_run_review_workbook": {
            "path": str(template_workbook_path.resolve()),
            "sha256": hashlib.sha256(
                template_workbook_path.read_bytes()
            ).hexdigest(),
        },
    }
    _rewrite_freeze(path, payload)
    return path


def _write_review_workbook(path: Path, *, marker: str) -> Path:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", "<Types/>")
        workbook.writestr(
            "xl/workbook.xml",
            f"<workbook marker={json.dumps(marker)}/>",
        )
    return path


def _review_receipt(
    freeze_path: Path,
    *,
    filled_workbook_path: Path,
) -> dict[str, str]:
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    return {
        "review_workbook_sha256": hashlib.sha256(
            filled_workbook_path.read_bytes()
        ).hexdigest(),
        "review_source_sha256": str(freeze["source"]["sha256"]),
        "review_selection_sha256": str(freeze["selection"]["sha256"]),
        "review_freeze_contract_sha256": str(
            freeze["freeze_contract_sha256"]
        ),
    }


def _write_reviewed_holdout_manifest(
    path: Path,
    rows: list[dict[str, str]],
    source_cases: list[dict[str, object]],
    seed_path: Path,
    freeze_path: Path,
    filled_workbook_path: Path,
    *,
    review_mode: str = exporter.HUMAN_REVIEW_MODE,
) -> None:
    receipt = _review_receipt(
        freeze_path,
        filled_workbook_path=filled_workbook_path,
    )
    seed_sha256 = _canonical_json_sha256(seed_path)
    sources_by_id = {
        str(source["ticket_id_hash"]): source
        for source in source_cases
    }
    reviewed_rows = []
    for original in rows:
        row = {**original, **receipt}
        source = sources_by_id[row["case_id_hash"]]
        row["source_schema_version"] = (
            row.get("source_schema_version")
            or str(source["schema_version"])
        )
        row["source_case_fingerprint"] = (
            row.get("source_case_fingerprint")
            or source_case_fingerprint(source)
        )
        row["approved_kb_seed_sha256"] = (
            row.get("approved_kb_seed_sha256")
            or seed_sha256
        )
        row["review_payload_sha256"] = holdout_review_payload_sha256(
            row,
            review_mode=review_mode,
        )
        reviewed_rows.append(row)
    _write_manifest(path, reviewed_rows, source_cases, seed_path)


def _write_imported_holdout_manifest(
    path: Path,
    rows: list[dict[str, str]],
    source_cases: list[dict[str, object]],
    seed_path: Path,
    freeze_path: Path,
    filled_workbook_path: Path,
) -> None:
    receipt = _review_receipt(
        freeze_path,
        filled_workbook_path=filled_workbook_path,
    )
    imported_rows = [
        {
            **row,
            **receipt,
            "review_payload_sha256": "",
        }
        for row in rows
    ]
    _write_manifest(path, imported_rows, source_cases, seed_path)


def _rewrite_freeze(path: Path, payload: dict[str, object]) -> None:
    payload.pop("freeze_contract_sha256", None)
    payload.pop("freeze_contract_hash_scope", None)
    contract_payload = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["freeze_contract_sha256"] = hashlib.sha256(
        contract_payload
    ).hexdigest()
    payload["freeze_contract_hash_scope"] = (
        "canonical_json_without_freeze_contract_sha256_fields"
    )
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _build(
    tmp_path: Path,
    source_cases: list[dict[str, object]],
    manifest_rows: list[dict[str, str]],
    *,
    output_name: str = "reviewed.json",
    seed_rows: list[dict[str, object]] | None = None,
    split: str = "calibration",
    review_mode: str = exporter.HUMAN_REVIEW_MODE,
    with_freeze: bool = True,
    with_review_workbook: bool = True,
) -> tuple[dict[str, object], Path]:
    directory = _private_dir(tmp_path)
    source = directory / "product_calibration_cases.jsonl"
    manifest = directory / "top20_review_manifest.csv"
    output = directory / output_name
    _write_jsonl(source, source_cases)
    seed = _write_kb_seed(tmp_path, manifest_rows, seed_rows=seed_rows)
    review_workbook: Path | None = None
    freeze = (
        directory / "holdout_freeze.json"
        if split == "holdout" and with_freeze
        else None
    )
    if freeze is not None:
        selection = directory / "holdout_selection.csv"
        template_workbook = _write_review_workbook(
            directory / "independent_holdout_80_review_template_v1.xlsx",
            marker="template",
        )
        review_workbook = _write_review_workbook(
            directory / "independent_holdout_80_review_v1.xlsx",
            marker="filled",
        )
        _write_manifest(selection, manifest_rows, source_cases, seed)
        _write_freeze(
            freeze,
            source_path=source,
            source_cases=source_cases,
            selection_path=selection,
            manifest_rows=manifest_rows,
            seed_path=seed,
            template_workbook_path=template_workbook,
        )
        _write_reviewed_holdout_manifest(
            manifest,
            manifest_rows,
            source_cases,
            seed,
            freeze,
            review_workbook,
            review_mode=review_mode,
        )
    else:
        _write_manifest(manifest, manifest_rows, source_cases, seed)
    stats = build_reviewed_ticket_ask_cases(
        source,
        manifest,
        output,
        seed,
        split=split,
        freeze_path=freeze,
        review_workbook_path=(
            review_workbook if with_review_workbook else None
        ),
        review_mode=review_mode,
    )
    return stats, output


def _prepare_holdout_evidence(
    directory: Path,
    *,
    source_path: Path,
    source_cases: list[dict[str, object]],
    manifest_path: Path,
    manifest_rows: list[dict[str, str]],
    seed_path: Path,
    frozen_rows: list[dict[str, str]] | None = None,
    seal_review: bool = True,
) -> tuple[Path, Path]:
    selection_rows = frozen_rows or manifest_rows
    selection_path = directory / "frozen_selection.csv"
    template_workbook_path = _write_review_workbook(
        directory / "independent_holdout_80_review_template_v1.xlsx",
        marker="template",
    )
    workbook_path = _write_review_workbook(
        directory / "independent_holdout_80_review_v1.xlsx",
        marker="filled",
    )
    _write_manifest(
        selection_path,
        selection_rows,
        source_cases,
        seed_path,
    )
    freeze_path = _write_freeze(
        directory / "holdout_freeze.json",
        source_path=source_path,
        source_cases=source_cases,
        selection_path=selection_path,
        manifest_rows=selection_rows,
        seed_path=seed_path,
        template_workbook_path=template_workbook_path,
    )
    if seal_review:
        _write_reviewed_holdout_manifest(
            manifest_path,
            manifest_rows,
            source_cases,
            seed_path,
            freeze_path,
            workbook_path,
        )
    else:
        _write_imported_holdout_manifest(
            manifest_path,
            manifest_rows,
            source_cases,
            seed_path,
            freeze_path,
            workbook_path,
        )
    return freeze_path, workbook_path


def _patch_workbook_import(
    monkeypatch: pytest.MonkeyPatch,
    manifest_path: Path,
) -> None:
    with manifest_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = list(reader.fieldnames or [])
        workbook_rows = [dict(row) for row in reader]
    monkeypatch.setattr(
        exporter,
        "load_holdout_review_workbook_rows",
        lambda **kwargs: (
            list(fieldnames),
            [dict(row) for row in workbook_rows],
        ),
    )


def test_export_uses_private_query_and_reviewed_corrections(tmp_path: Path) -> None:
    first_hash = "bbbbbbbbbbbbbbbb"
    second_hash = "aaaaaaaaaaaaaaaa"
    source_cases = [
        _source_case(first_hash, route="escalate", query="Мой статус заявки?"),
        _source_case(second_hash, query="Когда проходит Машук?"),
    ]
    first_row = _manifest_row(
        first_hash,
        route="escalate",
        corrected_intent="платформа_фгаис.статус",
        corrected_aspect="selection_status",
        corrected_entity="platform:event-scoped",
        forbidden_profiles="travel|dates",
    )
    second_row = _manifest_row(
        second_hash,
        corrected_aspect="dates",
        approved_chunk_ids="yonote_chunk_b|yonote_chunk_a",
        forbidden_profiles="travel",
    )
    first_row["query"] = "Подменённый CSV query"
    second_row["query"] = "Ещё один CSV query"

    stats, output = _build(tmp_path, source_cases, [first_row, second_row])
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert [item["case_id_hash"] for item in payload] == [second_hash, first_hash]
    assert payload[0]["query"] == "Когда проходит Машук?"
    assert payload[0]["channel"] == "api"
    assert payload[0]["privacy_class"] == "private_ticket_derived"
    assert payload[0]["label_status"] == "human_reviewed"
    assert payload[0]["requires_human_review"] is False
    assert payload[0]["role_verdict"] == "confirmed_user_turn"
    assert payload[0]["expected_response_profile"] == "dates"
    assert payload[0]["expected_behavior"] == "answer"
    assert payload[0]["expected_escalated"] is False
    assert payload[0]["expected_chunk_ids"] == ["yonote_chunk_a", "yonote_chunk_b"]
    assert payload[0]["expected_cited_chunk_ids"] == [
        "yonote_chunk_a",
        "yonote_chunk_b",
    ]
    assert payload[0]["allowed_cited_source_types"] == ["yonote"]
    assert payload[0]["forbidden_response_profiles"] == ["travel"]
    assert payload[1]["query"] == "Мой статус заявки?"
    assert payload[1]["intent"] == "платформа_фгаис.статус"
    assert payload[1]["entity_class"] == "platform:event-scoped"
    assert payload[1]["expected_response_profile"] == "selection_status"
    assert payload[1]["expected_behavior"] == "escalate"
    assert payload[1]["expected_escalated"] is True
    assert payload[1]["expected_escalation_reason"] == "personal_status"
    manifest = output.with_name("top20_review_manifest.csv")
    seed = tmp_path / "data" / "knowledge_base_seed.json"
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    seed_sha256 = _canonical_json_sha256(seed)
    assert payload[0]["review_provenance"] == {
        "reviewer": "reviewer_01",
        "reviewed_at": "2026-07-29T05:00:00Z",
        "manifest_sha256": manifest_sha256,
    }
    assert payload[0]["knowledge_provenance"] == {
        "seed_sha256": seed_sha256,
        "approved_seed_sha256": seed_sha256,
        "hash_canonicalization": KB_SEED_HASH_CANONICALIZATION,
        "status": "published",
        "source_type": "yonote",
    }
    assert payload[0]["source_provenance"] == {
        "schema_version": "1.0.0",
        "case_fingerprint": source_case_fingerprint(source_cases[1]),
        "fingerprint_algorithm": "sha256",
    }
    assert "raw_operator_answer" not in output.read_text(encoding="utf-8")
    assert "Подменённый CSV query" not in output.read_text(encoding="utf-8")
    assert stats["exported_cases"] == 2
    assert stats["behavior_counts"] == {"answer": 1, "escalate": 1}
    assert stats["corrected_intent_cases"] == 1
    assert stats["corrected_aspect_cases"] == 2
    assert stats["corrected_entity_cases"] == 1
    assert stats["provenance"]["review_manifest_sha256"] == manifest_sha256
    assert stats["provenance"]["kb_seed_sha256"] == seed_sha256
    assert stats["provenance"]["source_schema_versions"] == ["1.0.0"]


def test_corrected_escalation_reason_overrides_without_corrected_route(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(
        case_id_hash,
        route="escalate",
        query="Как узнать персональный статус?",
    )
    row = _manifest_row(
        case_id_hash,
        route="escalate",
        corrected_escalation_reason="missing_confirmed_data",
    )

    _, output = _build(tmp_path, [source], [row])
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload[0]["expected_behavior"] == "escalate"
    assert (
        payload[0]["expected_escalation_reason"]
        == "missing_confirmed_data"
    )


@pytest.mark.parametrize("corrected_route", ["answer", "clarify"])
def test_corrected_non_escalation_route_clears_original_reason(
    tmp_path: Path,
    corrected_route: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(
        case_id_hash,
        route="escalate",
        query="Как узнать персональный статус?",
    )
    row = _manifest_row(
        case_id_hash,
        route="escalate",
        corrected_route=corrected_route,
        corrected_escalation_reason="",
        approved_chunk_ids=(
            DEFAULT_CHUNK_ID if corrected_route == "answer" else ""
        ),
    )
    if corrected_route == "answer":
        row["answerable_from_snapshot"] = "true"

    _, output = _build(tmp_path, [source], [row])
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload[0]["expected_behavior"] == corrected_route
    assert payload[0]["expected_escalation_reason"] is None


def test_export_holdout_uses_only_human_deidentified_query(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    private_query = "Иван Иванов, мой телефон +7 999 000-00-00"
    deidentified_query = "Не получается отправить заявку на форум."
    source = _source_case(
        case_id_hash,
        query=private_query,
        split="holdout",
    )
    manifest = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        deidentified_query=deidentified_query,
        privacy_verdict="approved",
    )

    stats, output = _build(
        tmp_path,
        [source],
        [manifest],
        split="holdout",
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    serialized = output.read_text(encoding="utf-8")

    assert stats["exported_cases"] == 1
    assert stats["provenance"]["split"] == "holdout"
    assert payload[0]["query"] == deidentified_query
    assert payload[0]["split"] == "holdout"
    assert payload[0]["user_id"] == f"reviewed-holdout-{case_id_hash}"
    assert payload[0]["review_provenance"]["privacy_verdict"] == "approved"
    contract = payload[0]["holdout_contract"]
    assert stats["output_file_sha256"] == hashlib.sha256(
        output.read_bytes()
    ).hexdigest()
    assert stats["cases_payload_sha256"] == contract[
        "cases_payload_sha256"
    ]
    assert contract == {
        "schema_version": "1.1.0",
        "baseline_id": "independent_holdout_80_v1",
        "runtime_git_sha": RUNTIME_SHA,
        "review_mode": "human_reviewed",
        "product_verdict_eligible": True,
        "freeze_contract_sha256": contract["freeze_contract_sha256"],
        "review_manifest_sha256": stats["provenance"][
            "review_manifest_sha256"
        ],
        "selection_manifest_sha256": hashlib.sha256(
            output.with_name("holdout_selection.csv").read_bytes()
        ).hexdigest(),
        "review_workbook_sha256": hashlib.sha256(
            output.with_name(
                "independent_holdout_80_review_v1.xlsx"
            ).read_bytes()
        ).hexdigest(),
        "source_cases_sha256": hashlib.sha256(
            output.with_name("product_calibration_cases.jsonl").read_bytes()
        ).hexdigest(),
        "knowledge_base_seed_sha256": _canonical_json_sha256(
            tmp_path / "data" / "knowledge_base_seed.json"
        ),
        "cases_payload_sha256": exporter.holdout_cases_payload_sha256(
            [
                {
                    key: value
                    for key, value in item.items()
                    if key != "holdout_contract"
                }
                for item in payload
            ]
        ),
        "selected_case_ids_sha256": hashlib.sha256(
            f"{case_id_hash}\n".encode()
        ).hexdigest(),
        "cases_total": 1,
        "execution_allowed": True,
    }
    assert payload[0]["allowed_cited_source_types"] == ["yonote"]
    freeze_payload = json.loads(
        output.with_name("holdout_freeze.json").read_text(encoding="utf-8")
    )
    assert (
        Path(freeze_payload["pre_run_review_workbook"]["path"]).name
        == "independent_holdout_80_review_template_v1.xlsx"
    )
    assert (
        freeze_payload["pre_run_review_workbook"]["sha256"]
        != contract["review_workbook_sha256"]
    )
    changed_payload = json.loads(json.dumps(payload, ensure_ascii=False))
    changed_payload[0]["query"] = "Изменённое содержимое кейса."
    assert (
        exporter.holdout_cases_payload_sha256(
            [
                {
                    key: value
                    for key, value in item.items()
                    if key != "holdout_contract"
                }
                for item in changed_payload
            ]
        )
        != contract["cases_payload_sha256"]
    )
    assert private_query not in serialized
    assert "Иван Иванов" not in serialized
    assert "+7 999 000-00-00" not in serialized


def test_export_holdout_preserves_model_assisted_prerun_provenance(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(
        case_id_hash,
        query="private source query",
        split="holdout",
    )
    manifest = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        deidentified_query="Deidentified forum date question.",
        privacy_verdict="approved",
        reviewer="codex_model_assisted",
    )

    stats, output = _build(
        tmp_path,
        [source],
        [manifest],
        split="holdout",
        review_mode=exporter.MODEL_ASSISTED_PRERUN_MODE,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    case = payload[0]
    contract = case["holdout_contract"]

    assert case["label_status"] == exporter.MODEL_ASSISTED_PRERUN_MODE
    assert case["requires_human_review"] is True
    assert (
        f"review_mode:{exporter.MODEL_ASSISTED_PRERUN_MODE}"
        in case["tags"]
    )
    assert "product_verdict:provisional" in case["tags"]
    assert case["review_provenance"]["review_mode"] == (
        exporter.MODEL_ASSISTED_PRERUN_MODE
    )
    assert case["review_provenance"]["human_reviewed"] is False
    assert (
        case["review_provenance"]["product_verdict_eligible"] is False
    )
    assert contract["schema_version"] == "1.1.0"
    assert contract["review_mode"] == exporter.MODEL_ASSISTED_PRERUN_MODE
    assert contract["product_verdict_eligible"] is False
    assert stats["provenance"]["review_mode"] == (
        exporter.MODEL_ASSISTED_PRERUN_MODE
    )
    assert stats["provenance"]["human_reviewed"] is False
    assert stats["provenance"]["product_verdict_eligible"] is False


def test_model_assisted_prerun_is_rejected_for_non_holdout(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash)
    manifest = _manifest_row(case_id_hash)

    with pytest.raises(ValueError, match="only valid for the sealed holdout"):
        _build(
            tmp_path,
            [source],
            [manifest],
            review_mode=exporter.MODEL_ASSISTED_PRERUN_MODE,
        )


@pytest.mark.parametrize(
    ("review_mode", "reviewer", "message"),
    [
        (
            exporter.HUMAN_REVIEW_MODE,
            "codex-r1",
            "model reviewer pseudonym",
        ),
        (
            exporter.MODEL_ASSISTED_PRERUN_MODE,
            "reviewer_01",
            "without a model reviewer pseudonym",
        ),
    ],
)
def test_export_rejects_reviewer_provenance_mismatch(
    tmp_path: Path,
    review_mode: str,
    reviewer: str,
    message: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash, split="holdout")
    manifest = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        deidentified_query="Deidentified forum date question.",
        privacy_verdict="approved",
        reviewer=reviewer,
    )

    with pytest.raises(ValueError, match=message):
        _build(
            tmp_path,
            [source],
            [manifest],
            split="holdout",
            review_mode=review_mode,
        )


def test_review_payload_hash_binds_review_mode() -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Deidentified forum date question.",
    )

    assert holdout_review_payload_sha256(
        row,
        review_mode=exporter.HUMAN_REVIEW_MODE,
    ) != holdout_review_payload_sha256(
        row,
        review_mode=exporter.MODEL_ASSISTED_PRERUN_MODE,
    )


@pytest.mark.parametrize(
    ("privacy_verdict", "deidentified_query", "message"),
    [
        ("", "Обезличенный вопрос", "privacy_verdict=approved"),
        ("rejected", "Обезличенный вопрос", "privacy_verdict=approved"),
        ("approved", "", "invalid deidentified_query"),
    ],
)
def test_export_holdout_fails_closed_on_incomplete_privacy_review(
    tmp_path: Path,
    privacy_verdict: str,
    deidentified_query: str,
    message: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash, split="holdout")
    manifest = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict=privacy_verdict,
        deidentified_query=deidentified_query,
    )

    with pytest.raises(ValueError, match=message):
        _build(
            tmp_path,
            [source],
            [manifest],
            split="holdout",
        )


def test_export_holdout_requires_freeze(tmp_path: Path) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash, split="holdout")
    manifest = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )

    with pytest.raises(ValueError, match="requires --freeze"):
        _build(
            tmp_path,
            [source],
            [manifest],
            split="holdout",
            with_freeze=False,
        )


def test_export_holdout_requires_review_workbook(tmp_path: Path) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Когда пройдёт форум?",
    )

    with pytest.raises(ValueError, match="requires --review-workbook"):
        _build(
            tmp_path,
            [source],
            [row],
            split="holdout",
            with_review_workbook=False,
        )


def test_export_holdout_rejects_filled_workbook_changed_after_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Когда пройдёт форум?",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, filled_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
        seal_review=False,
    )
    _patch_workbook_import(monkeypatch, manifest_path)
    seal_holdout_review_payload_hashes(
        manifest_path,
        source_path,
        freeze_path,
        filled_workbook,
    )
    _write_review_workbook(
        filled_workbook,
        marker="changed-after-seal",
    )

    with pytest.raises(ValueError, match="stale review_workbook_sha256"):
        build_reviewed_ticket_ask_cases(
            source_path,
            manifest_path,
            output_path,
            seed_path,
            split="holdout",
            freeze_path=freeze_path,
            review_workbook_path=filled_workbook,
        )


def test_export_holdout_rejects_frozen_template_mutation(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Когда пройдёт форум?",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, filled_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
    )
    template_workbook = (
        directory / "independent_holdout_80_review_template_v1.xlsx"
    )
    _write_review_workbook(template_workbook, marker="changed-template")

    with pytest.raises(
        ValueError,
        match="stale pre_run_review_workbook_template file evidence",
    ):
        build_reviewed_ticket_ask_cases(
            source_path,
            manifest_path,
            output_path,
            seed_path,
            split="holdout",
            freeze_path=freeze_path,
            review_workbook_path=filled_workbook,
        )


def test_export_holdout_rejects_template_as_filled_workbook(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Когда пройдёт форум?",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, _ = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
    )
    template_workbook = (
        directory / "independent_holdout_80_review_template_v1.xlsx"
    )

    with pytest.raises(ValueError, match="must be distinct from the frozen template"):
        build_reviewed_ticket_ask_cases(
            source_path,
            manifest_path,
            output_path,
            seed_path,
            split="holdout",
            freeze_path=freeze_path,
            review_workbook_path=template_workbook,
        )


def test_export_holdout_requires_frozen_case_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exporter, "_EXPECTED_HOLDOUT_CASES_TOTAL", 2)
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash, split="holdout")
    manifest = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )

    with pytest.raises(ValueError, match="exactly 2 cases"):
        _build(
            tmp_path,
            [source],
            [manifest],
            split="holdout",
        )


def test_export_holdout_rejects_freeze_self_hash_tampering(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
    )
    freeze_payload = json.loads(freeze_path.read_text(encoding="utf-8"))
    freeze_payload["runtime_git_sha"] = "0" * 40
    freeze_path.write_text(
        json.dumps(freeze_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="self-hash mismatch"):
        build_reviewed_ticket_ask_cases(
            source_path,
            manifest_path,
            output_path,
            seed_path,
            split="holdout",
            freeze_path=freeze_path,
            review_workbook_path=review_workbook,
        )


def test_export_holdout_rejects_self_hashed_multiturn_freeze(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
    )
    freeze_payload = json.loads(freeze_path.read_text(encoding="utf-8"))
    freeze_payload["multiturn_status_counts"] = {"multi_turn": 1}
    _rewrite_freeze(freeze_path, freeze_payload)

    with pytest.raises(ValueError, match="only single_turn"):
        build_reviewed_ticket_ask_cases(
            source_path,
            manifest_path,
            output_path,
            seed_path,
            split="holdout",
            freeze_path=freeze_path,
            review_workbook_path=review_workbook,
        )


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("runtime_git_sha", "invalid runtime_git_sha"),
        ("source_sha256", "source hash differs"),
        ("kb_sha256", "KB seed hash differs"),
        ("selected_ids_sha256", "selected case ID digest mismatch"),
    ],
)
def test_export_holdout_validates_frozen_evidence(
    tmp_path: Path,
    corruption: str,
    message: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
    )
    freeze_payload = json.loads(freeze_path.read_text(encoding="utf-8"))
    if corruption == "runtime_git_sha":
        freeze_payload["runtime_git_sha"] = "not-a-full-sha"
    elif corruption == "source_sha256":
        freeze_payload["source"]["sha256"] = "0" * 64
    elif corruption == "kb_sha256":
        freeze_payload["knowledge_snapshot"]["canonical_sha256"] = "0" * 64
    else:
        freeze_payload["selection"]["selected_case_ids_sha256"] = "0" * 64
    _rewrite_freeze(freeze_path, freeze_payload)

    with pytest.raises(ValueError, match=message):
        build_reviewed_ticket_ask_cases(
            source_path,
            manifest_path,
            output_path,
            seed_path,
            split="holdout",
            freeze_path=freeze_path,
            review_workbook_path=review_workbook,
        )


def test_export_holdout_rejects_manifest_subset_of_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exporter, "_EXPECTED_HOLDOUT_CASES_TOTAL", 2)
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source_cases = [
        _source_case("aaaaaaaaaaaaaaaa", split="holdout"),
        _source_case("bbbbbbbbbbbbbbbb", split="holdout"),
    ]
    rows = [
        _manifest_row(
            str(source["ticket_id_hash"]),
            include="false",
            include_in_holdout="true",
            privacy_verdict="approved",
            deidentified_query=f"Вопрос о заявке номер {index}.",
        )
        for index, source in enumerate(source_cases, start=1)
    ]
    _write_jsonl(source_path, source_cases)
    seed_path = _write_kb_seed(tmp_path, rows)
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=source_cases,
        manifest_path=manifest_path,
        manifest_rows=rows[:1],
        frozen_rows=rows,
        seed_path=seed_path,
    )

    with pytest.raises(ValueError, match="exact frozen case set"):
        build_reviewed_ticket_ask_cases(
            source_path,
            manifest_path,
            output_path,
            seed_path,
            split="holdout",
            freeze_path=freeze_path,
            review_workbook_path=review_workbook,
        )


@pytest.mark.parametrize(
    ("label_verdict", "include_in_holdout"),
    [("rejected", "false"), ("approved", "false")],
)
def test_export_holdout_forbids_shrinking_frozen_denominator(
    tmp_path: Path,
    label_verdict: str,
    include_in_holdout: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash, split="holdout")
    manifest = _manifest_row(
        case_id_hash,
        verdict=label_verdict,
        include="false",
        include_in_holdout=include_in_holdout,
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )

    with pytest.raises(
        ValueError,
        match="Every frozen holdout case must be approved and included",
    ):
        _build(
            tmp_path,
            [source],
            [manifest],
            split="holdout",
        )


def test_export_holdout_rejects_review_payload_mutation(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
    )
    with manifest_path.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as file:
        manifest_rows = list(csv.DictReader(file))
    manifest_rows[0]["deidentified_query"] = "Изменённый вопрос о заявке."
    with manifest_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(file, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(manifest_rows)

    with pytest.raises(ValueError, match="stale review_payload_sha256"):
        build_reviewed_ticket_ask_cases(
            source_path,
            manifest_path,
            output_path,
            seed_path,
            split="holdout",
            freeze_path=freeze_path,
            review_workbook_path=review_workbook,
        )


@pytest.mark.parametrize(
    "receipt_field",
    [
        "review_workbook_sha256",
        "review_source_sha256",
        "review_selection_sha256",
        "review_freeze_contract_sha256",
    ],
)
def test_export_holdout_rejects_review_receipt_tampering(
    tmp_path: Path,
    receipt_field: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
    )
    with manifest_path.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    rows[0][receipt_field] = "0" * 64
    rows[0]["review_payload_sha256"] = holdout_review_payload_sha256(rows[0])
    with manifest_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(ValueError, match=f"stale {receipt_field}"):
        build_reviewed_ticket_ask_cases(
            source_path,
            manifest_path,
            output_path,
            seed_path,
            split="holdout",
            freeze_path=freeze_path,
            review_workbook_path=review_workbook,
        )


def test_export_holdout_rejects_frozen_selection_field_tampering(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Когда пройдёт форум?",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
    )
    with manifest_path.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    rows[0]["intent"] = "форумы.подменённый_intent"
    rows[0]["review_payload_sha256"] = holdout_review_payload_sha256(rows[0])
    with manifest_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(
        ValueError,
        match="changed frozen selection fields: intent",
    ):
        build_reviewed_ticket_ask_cases(
            source_path,
            manifest_path,
            output_path,
            seed_path,
            split="holdout",
            freeze_path=freeze_path,
            review_workbook_path=review_workbook,
        )


def test_export_holdout_allows_corrected_margins_and_reports_delta(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source = _source_case(case_id_hash, split="holdout")
    frozen_row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Когда пройдёт форум?",
    )
    corrected_row = dict(frozen_row)
    corrected_row.update(
        {
            "corrected_aspect": "generic",
            "corrected_route": "escalate",
            "corrected_escalation_reason": "missing_confirmed_data",
            "answerable_from_snapshot": "false",
            "approved_chunk_ids": "",
        }
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [frozen_row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[corrected_row],
        frozen_rows=[frozen_row],
        seed_path=seed_path,
    )

    stats = build_reviewed_ticket_ask_cases(
        source_path,
        manifest_path,
        output_path,
        seed_path,
        split="holdout",
        freeze_path=freeze_path,
        review_workbook_path=review_workbook,
    )
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert payload[0]["expected_response_profile"] == "generic"
    assert payload[0]["expected_behavior"] == "escalate"
    assert stats["provenance"]["holdout_label_margins"] == {
        "pre_review": {
            "profile_counts": {"dates": 1},
            "route_counts": {"answer": 1},
        },
        "reviewed": {
            "profile_counts": {"generic": 1},
            "route_counts": {"escalate": 1},
        },
    }


@pytest.mark.parametrize(
    "deidentified_query",
    [
        "Напишите на user@example.test по вопросу заявки.",
        "Мой телефон +7 999 000-00-00.",
        "Напиши пользователю @private_user.",
        "Профиль: https://vk.com/id123456.",
        "UUID 550e8400-e29b-41d4-a716-446655440000.",
        "Заявка № ABCD123456.",
        "Дата рождения 01.02.2000.",
        "Адрес: улица Ленина 10.",
        "номер 123456789",
        "г. Москва, Тверская 10",
        "день рождения 02.03.2008",
        "мне 17 лет, 02.03.2008",
        "Мой день рождения 2 марта 2008 года",
        "Мой адрес: Москва, улица Тверская",
        "Живу по адресу: Томск, проспект Ленина",
    ],
)
def test_export_holdout_rejects_residual_pii(
    tmp_path: Path,
    deidentified_query: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash, split="holdout")
    manifest = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query=deidentified_query,
    )

    with pytest.raises(ValueError, match="still contains PII"):
        _build(
            tmp_path,
            [source],
            [manifest],
            split="holdout",
        )


def test_export_holdout_allows_event_date_after_human_review(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash, split="holdout")
    manifest = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        date_privacy_verdict="event_date_only",
        deidentified_query="Форум пройдёт 08.08.2026?",
    )

    stats, output = _build(
        tmp_path,
        [source],
        [manifest],
        split="holdout",
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert stats["exported_cases"] == 1
    assert payload[0]["query"] == "Форум пройдёт 08.08.2026?"


@pytest.mark.parametrize(
    ("query", "date_privacy_verdict", "message"),
    [
        (
            "Форум пройдёт 08.08.2026?",
            "not_present",
            "date_privacy_verdict=event_date_only",
        ),
        (
            "Когда пройдёт форум?",
            "event_date_only",
            "date_privacy_verdict=not_present",
        ),
    ],
)
def test_export_holdout_requires_exact_date_privacy_verdict(
    tmp_path: Path,
    query: str,
    date_privacy_verdict: str,
    message: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        date_privacy_verdict=date_privacy_verdict,
        deidentified_query=query,
    )

    with pytest.raises(ValueError, match=message):
        _build(tmp_path, [source], [row], split="holdout")


def test_export_holdout_allows_source_above_review_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data" / "private" / "roles_v1"
    review = root / "independent_holdout_80_v1"
    review.mkdir(parents=True)
    source_path = root / "product_holdout_cases.jsonl"
    manifest_path = review / "selection_manifest.csv"
    output_path = review / "reviewed.json"
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        review,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
    )

    stats = build_reviewed_ticket_ask_cases(
        source_path,
        manifest_path,
        output_path,
        seed_path,
        split="holdout",
        freeze_path=freeze_path,
        review_workbook_path=review_workbook,
    )

    assert stats["exported_cases"] == 1


def test_export_holdout_fails_when_pii_scan_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableMasker:
        def mask(self, _text: str) -> tuple[str, dict[str, list[str]]]:
            raise exporter.PIIMaskingUnavailable("pii_ner_unavailable")

    monkeypatch.setattr(exporter, "PIIMasker", UnavailableMasker)
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source = _source_case(case_id_hash, split="holdout")
    manifest = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )

    with pytest.raises(ValueError, match="could not be PII-scanned"):
        _build(
            tmp_path,
            [source],
            [manifest],
            split="holdout",
        )


@pytest.mark.parametrize(
    "review_mode",
    [
        exporter.HUMAN_REVIEW_MODE,
        exporter.MODEL_ASSISTED_PRERUN_MODE,
    ],
)
def test_seal_review_payload_hashes_is_explicit_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    review_mode: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
        reviewer=(
            "codex-r1"
            if review_mode == exporter.MODEL_ASSISTED_PRERUN_MODE
            else "reviewer_01"
        ),
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
        seal_review=False,
    )
    _patch_workbook_import(monkeypatch, manifest_path)

    stats = seal_holdout_review_payload_hashes(
        manifest_path,
        source_path,
        freeze_path,
        review_workbook,
        review_mode=review_mode,
    )
    with manifest_path.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as file:
        sealed_row = next(csv.DictReader(file))

    assert stats["sealed_rows"] == 1
    assert stats["reviewed_profile_counts"] == {"dates": 1}
    assert stats["reviewed_route_counts"] == {"answer": 1}
    assert sealed_row["review_workbook_sha256"] == hashlib.sha256(
        review_workbook.read_bytes()
    ).hexdigest()
    assert sealed_row["review_payload_sha256"] == (
        holdout_review_payload_sha256(
            sealed_row,
            review_mode=review_mode,
        )
    )
    assert stats["review_mode"] == review_mode
    assert stats["human_reviewed"] is (
        review_mode == exporter.HUMAN_REVIEW_MODE
    )
    assert stats["product_verdict_eligible"] is stats["human_reviewed"]
    first_bytes = manifest_path.read_bytes()
    repeated_stats = seal_holdout_review_payload_hashes(
        manifest_path,
        source_path,
        freeze_path,
        review_workbook,
        review_mode=review_mode,
    )
    assert manifest_path.read_bytes() == first_bytes
    assert repeated_stats["review_manifest_sha256"] == stats[
        "review_manifest_sha256"
    ]


@pytest.mark.parametrize(
    ("deidentified_query", "date_privacy_verdict"),
    [
        ("Мой день рождения 2 марта 2008 года", "event_date_only"),
        ("Мой адрес: Москва, улица Тверская", "not_present"),
        ("Живу по адресу: Томск, проспект Ленина", "not_present"),
    ],
)
def test_seal_rejects_contextual_dob_and_personal_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deidentified_query: str,
    date_privacy_verdict: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        date_privacy_verdict=date_privacy_verdict,
        deidentified_query=deidentified_query,
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
        seal_review=False,
    )
    _patch_workbook_import(monkeypatch, manifest_path)

    with pytest.raises(ValueError, match="still contains PII"):
        seal_holdout_review_payload_hashes(
            manifest_path,
            source_path,
            freeze_path,
            review_workbook,
        )


@pytest.mark.parametrize(
    ("field", "mutated_value"),
    [
        ("corrected_intent", "technical"),
        ("corrected_aspect", "technical"),
        ("corrected_entity_class", "platform"),
        ("corrected_route", "clarify"),
        ("corrected_escalation_reason", "missing_confirmed_data"),
        ("deidentified_query", "Подменённый обезличенный запрос."),
        ("role_verdict", "rejected_not_user_turn"),
        ("label_verdict", "rejected"),
        ("answerable_from_snapshot", "false"),
        ("approved_chunk_ids", "yonote_other_chunk"),
        ("forbidden_profiles", "travel"),
        ("include_in_holdout", "false"),
        ("reviewer", "reviewer_02"),
        ("privacy_verdict", "rejected"),
        ("date_privacy_verdict", "event_date_only"),
        ("approved_kb_seed_sha256", "4" * 64),
        ("review_workbook_sha256", "0" * 64),
        ("review_source_sha256", "1" * 64),
        ("review_selection_sha256", "2" * 64),
        ("review_freeze_contract_sha256", "3" * 64),
    ],
)
def test_seal_rejects_manifest_mutation_after_workbook_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    mutated_value: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
        seal_review=False,
    )
    _patch_workbook_import(monkeypatch, manifest_path)
    with manifest_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(item) for item in reader]
    rows[0][field] = mutated_value
    with manifest_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(ValueError, match="differs from the filled workbook"):
        seal_holdout_review_payload_hashes(
            manifest_path,
            source_path,
            freeze_path,
            review_workbook,
        )


def test_export_rejects_forged_self_hashed_manifest_not_in_workbook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source_path = directory / "product_holdout_cases.jsonl"
    manifest_path = directory / "holdout_review_manifest.csv"
    output_path = directory / "reviewed.json"
    source = _source_case(case_id_hash, split="holdout")
    row = _manifest_row(
        case_id_hash,
        include="false",
        include_in_holdout="true",
        privacy_verdict="approved",
        deidentified_query="Не получается отправить заявку на форум.",
    )
    _write_jsonl(source_path, [source])
    seed_path = _write_kb_seed(tmp_path, [row])
    freeze_path, review_workbook = _prepare_holdout_evidence(
        directory,
        source_path=source_path,
        source_cases=[source],
        manifest_path=manifest_path,
        manifest_rows=[row],
        seed_path=seed_path,
    )
    _patch_workbook_import(monkeypatch, manifest_path)
    with manifest_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(item) for item in reader]
    rows[0]["deidentified_query"] = "Подменённый, но заново захешированный запрос."
    rows[0]["review_payload_sha256"] = holdout_review_payload_sha256(rows[0])
    with manifest_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(ValueError, match="differs from the filled workbook"):
        build_reviewed_ticket_ask_cases(
            source_path,
            manifest_path,
            output_path,
            seed_path,
            split="holdout",
            freeze_path=freeze_path,
            review_workbook_path=review_workbook,
        )


def test_export_requires_both_manifest_approval_flags(tmp_path: Path) -> None:
    included = "aaaaaaaaaaaaaaaa"
    excluded_by_verdict = "bbbbbbbbbbbbbbbb"
    excluded_by_flag = "cccccccccccccccc"

    stats, output = _build(
        tmp_path,
        [
            _source_case(included),
            _source_case(excluded_by_verdict),
            _source_case(excluded_by_flag),
        ],
        [
            _manifest_row(included),
            _manifest_row(
                excluded_by_verdict,
                verdict="rejected",
                include="false",
            ),
            _manifest_row(excluded_by_flag, include="false"),
        ],
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert [item["case_id_hash"] for item in payload] == [included]
    assert stats["manifest_rows"] == 3
    assert stats["manifest_approved_rows"] == 2
    assert stats["excluded_manifest_rows"] == 2


@pytest.mark.parametrize("verdict", ["", "rejected"])
def test_export_forbids_including_non_approved_rows(
    tmp_path: Path,
    verdict: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"

    with pytest.raises(ValueError, match="includes a non-approved case"):
        _build(
            tmp_path,
            [_source_case(case_id_hash)],
            [_manifest_row(case_id_hash, verdict=verdict, include="true")],
        )


def test_export_rejects_unknown_nonempty_verdict(tmp_path: Path) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"

    with pytest.raises(ValueError, match="unknown label_verdict"):
        _build(
            tmp_path,
            [_source_case(case_id_hash)],
            [_manifest_row(case_id_hash, verdict="looks_good", include="false")],
        )


def test_export_requires_explicit_include_decision_for_approved_row(
    tmp_path: Path,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"

    with pytest.raises(ValueError, match="missing boolean include_in_calibration"):
        _build(
            tmp_path,
            [_source_case(case_id_hash)],
            [_manifest_row(case_id_hash, include="")],
        )


def test_excluded_approved_row_is_still_fully_validated(tmp_path: Path) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"

    with pytest.raises(ValueError, match="has no approved_chunk_ids"):
        _build(
            tmp_path,
            [_source_case(case_id_hash)],
            [
                _manifest_row(
                    case_id_hash,
                    include="false",
                    approved_chunk_ids="",
                )
            ],
        )


@pytest.mark.parametrize(
    ("reviewer", "reviewed_at", "message"),
    [
        ("", "2026-07-29T12:00:00+07:00", "pseudonymous reviewer"),
        (
            "reviewer@example.test",
            "2026-07-29T12:00:00+07:00",
            "pseudonymous reviewer",
        ),
        ("reviewer", "", "has no reviewed_at"),
        ("reviewer", "2026-07-29T12:00:00", "must include a timezone"),
        ("reviewer", "not-a-time", "invalid reviewed_at"),
    ],
)
def test_export_requires_auditable_approval(
    tmp_path: Path,
    reviewer: str,
    reviewed_at: str,
    message: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    manifest_row = _manifest_row(
        case_id_hash,
        reviewer=reviewer,
        reviewed_at=reviewed_at,
    )

    with pytest.raises(ValueError, match=message):
        _build(
            tmp_path,
            [_source_case(case_id_hash)],
            [manifest_row],
        )


@pytest.mark.parametrize(
    ("row_overrides", "message"),
    [
        ({"source_schema_version": "0.9.0"}, "stale source_schema_version"),
        ({"fingerprint": "0" * 64}, "stale source_case_fingerprint"),
        (
            {"approved_kb_seed_sha256": "0" * 64},
            "stale approved_kb_seed_sha256",
        ),
    ],
)
def test_export_rejects_stale_approval(
    tmp_path: Path,
    row_overrides: dict[str, str],
    message: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    row = _manifest_row(case_id_hash, **row_overrides)

    with pytest.raises(ValueError, match=message):
        _build(tmp_path, [_source_case(case_id_hash)], [row])


def test_export_rejects_approval_after_source_query_changes(tmp_path: Path) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    original = _source_case(case_id_hash, query="original query")
    row = _manifest_row(
        case_id_hash,
        source_schema_version=str(original["schema_version"]),
        fingerprint=source_case_fingerprint(original),
    )
    changed = dict(original)
    changed["query"] = "changed query"

    with pytest.raises(ValueError, match="stale source_case_fingerprint"):
        _build(tmp_path, [changed], [row])


def test_answer_requires_snapshot_answerability(tmp_path: Path) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    row = _manifest_row(case_id_hash)
    row["answerable_from_snapshot"] = "false"

    with pytest.raises(ValueError, match="answerable_from_snapshot=true"):
        _build(tmp_path, [_source_case(case_id_hash)], [row])


def test_answer_requires_approved_chunks(tmp_path: Path) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"

    with pytest.raises(ValueError, match="has no approved_chunk_ids"):
        _build(
            tmp_path,
            [_source_case(case_id_hash)],
            [_manifest_row(case_id_hash, approved_chunk_ids="")],
        )


@pytest.mark.parametrize(
    ("seed_rows", "message"),
    [
        (
            [
                {
                    "chunk_id": "yonote_other_chunk",
                    "status": "published",
                    "source_type": "yonote",
                }
            ],
            "references missing KB chunk",
        ),
        (
            [
                {
                    "chunk_id": DEFAULT_CHUNK_ID,
                    "status": "draft",
                    "source_type": "yonote",
                }
            ],
            "references unpublished KB chunk",
        ),
        (
            [
                {
                    "chunk_id": DEFAULT_CHUNK_ID,
                    "status": "published",
                    "source_type": "xlsx",
                }
            ],
            "references non-Yonote KB chunk",
        ),
    ],
)
def test_answer_chunks_must_be_published_yonote(
    tmp_path: Path,
    seed_rows: list[dict[str, object]],
    message: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"

    with pytest.raises(ValueError, match=message):
        _build(
            tmp_path,
            [_source_case(case_id_hash)],
            [_manifest_row(case_id_hash)],
            seed_rows=seed_rows,
        )


@pytest.mark.parametrize("route", ["clarify", "escalate"])
def test_non_answer_routes_forbid_approved_chunks(
    tmp_path: Path,
    route: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"

    with pytest.raises(ValueError, match="must not have approved_chunk_ids"):
        _build(
            tmp_path,
            [_source_case(case_id_hash, route=route)],
            [
                _manifest_row(
                    case_id_hash,
                    route=route,
                    approved_chunk_ids=DEFAULT_CHUNK_ID,
                )
            ],
        )


@pytest.mark.parametrize(
    ("manifest_rows", "message"),
    [
        (
            [
                _manifest_row("aaaaaaaaaaaaaaaa"),
                _manifest_row("aaaaaaaaaaaaaaaa"),
            ],
            "Duplicate manifest case_id_hash",
        ),
        (
            [_manifest_row("bbbbbbbbbbbbbbbb")],
            "missing source case",
        ),
    ],
)
def test_export_fails_on_invalid_manifest_reference(
    tmp_path: Path,
    manifest_rows: list[dict[str, str]],
    message: str,
) -> None:
    directory = _private_dir(tmp_path)
    source = directory / "product_calibration_cases.jsonl"
    manifest = directory / "top20_review_manifest.csv"
    output = directory / "reviewed.json"
    source_cases = [_source_case("aaaaaaaaaaaaaaaa")]
    _write_jsonl(source, source_cases)
    seed = _write_kb_seed(tmp_path, manifest_rows)
    _write_manifest(manifest, manifest_rows, source_cases, seed)

    with pytest.raises(ValueError, match=message):
        build_reviewed_ticket_ask_cases(source, manifest, output, seed)
    assert not output.exists()


@pytest.mark.parametrize(
    ("source_overrides", "message"),
    [
        ({"split": "validation"}, "not in the calibration split"),
        ({"label_status": "untrusted"}, "unsupported label_status"),
        ({"operator_answer_included": True}, "unsafe safety flag"),
        ({"operator_answer_used_as_fact": True}, "unsafe safety flag"),
    ],
)
def test_export_fails_closed_on_unsafe_source_case(
    tmp_path: Path,
    source_overrides: dict[str, object],
    message: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source_case = _source_case(case_id_hash)
    source_case.update(source_overrides)
    directory = _private_dir(tmp_path)
    source = directory / "product_calibration_cases.jsonl"
    manifest = directory / "top20_review_manifest.csv"
    output = directory / "reviewed.json"
    _write_jsonl(source, [source_case])
    manifest_rows = [_manifest_row(case_id_hash)]
    seed = _write_kb_seed(tmp_path, manifest_rows)
    _write_manifest(manifest, manifest_rows, [source_case], seed)

    with pytest.raises(ValueError, match=message):
        build_reviewed_ticket_ask_cases(source, manifest, output, seed)
    assert not output.exists()


def test_export_fails_when_required_safety_flag_is_missing(tmp_path: Path) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source_case = _source_case(case_id_hash)
    source_case.pop("operator_answer_included")
    directory = _private_dir(tmp_path)
    source = directory / "product_calibration_cases.jsonl"
    manifest = directory / "top20_review_manifest.csv"
    output = directory / "reviewed.json"
    _write_jsonl(source, [source_case])
    manifest_rows = [_manifest_row(case_id_hash)]
    seed = _write_kb_seed(tmp_path, manifest_rows)
    _write_manifest(manifest, manifest_rows, [source_case], seed)

    with pytest.raises(ValueError, match="missing required safety flag"):
        build_reviewed_ticket_ask_cases(source, manifest, output, seed)
    assert not output.exists()


def test_export_accepts_human_confirmed_partial_role(tmp_path: Path) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source_case = _source_case(case_id_hash)
    source_case["role_reconstruction_status"] = "partial"

    _, output = _build(
        tmp_path,
        [source_case],
        [_manifest_row(case_id_hash, role_status="partial")],
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload[0]["role_reconstruction_status"] == "partial"
    assert payload[0]["role_verdict"] == "confirmed_user_turn"


@pytest.mark.parametrize(
    "role_status",
    ["unknown", "ambiguous", "unresolved", "not_available", "typo"],
)
def test_export_rejects_unsafe_source_roles(
    tmp_path: Path,
    role_status: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source = directory / "product_calibration_cases.jsonl"
    manifest = directory / "top20_review_manifest.csv"
    output = directory / "reviewed.json"
    source_case = _source_case(case_id_hash)
    source_case["role_reconstruction_status"] = role_status
    source_cases = [source_case]
    manifest_rows = [_manifest_row(case_id_hash, role_status=role_status)]
    _write_jsonl(source, source_cases)
    seed = _write_kb_seed(tmp_path, manifest_rows)
    _write_manifest(
        manifest,
        manifest_rows,
        source_cases,
        seed,
    )

    with pytest.raises(
        ValueError,
        match="unsafe source role_reconstruction_status",
    ):
        build_reviewed_ticket_ask_cases(source, manifest, output, seed)
    assert not output.exists()


@pytest.mark.parametrize(
    "role_verdict",
    ["", "not_required", "confirmed_assistant_turn", "CONFIRMED_USER_TURN"],
)
def test_export_requires_explicit_user_turn_verdict(
    tmp_path: Path,
    role_verdict: str,
) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"

    with pytest.raises(ValueError, match="role_verdict=confirmed_user_turn"):
        _build(
            tmp_path,
            [_source_case(case_id_hash)],
            [_manifest_row(case_id_hash, role_verdict=role_verdict)],
        )


def test_export_rejects_manifest_role_status_drift(tmp_path: Path) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"

    with pytest.raises(ValueError, match="stale role_reconstruction_status"):
        _build(
            tmp_path,
            [_source_case(case_id_hash)],
            [_manifest_row(case_id_hash, role_status="partial")],
        )


def test_export_rejects_empty_approved_selection(tmp_path: Path) -> None:
    case_id_hash = "aaaaaaaaaaaaaaaa"
    directory = _private_dir(tmp_path)
    source = directory / "product_calibration_cases.jsonl"
    manifest = directory / "top20_review_manifest.csv"
    output = directory / "reviewed.json"
    source_cases = [_source_case(case_id_hash)]
    manifest_rows = [
        _manifest_row(case_id_hash, verdict="rejected", include="false")
    ]
    _write_jsonl(source, source_cases)
    seed = _write_kb_seed(tmp_path, manifest_rows)
    _write_manifest(
        manifest,
        manifest_rows,
        source_cases,
        seed,
    )

    with pytest.raises(ValueError, match="contains no rows"):
        build_reviewed_ticket_ask_cases(source, manifest, output, seed)
    assert not output.exists()


def test_export_requires_adjacent_data_private_paths(tmp_path: Path) -> None:
    directory = _private_dir(tmp_path)
    source = directory / "product_calibration_cases.jsonl"
    manifest = directory / "top20_review_manifest.csv"
    outside_output = tmp_path / "reviewed.json"
    case_id_hash = "aaaaaaaaaaaaaaaa"
    source_cases = [_source_case(case_id_hash)]
    manifest_rows = [_manifest_row(case_id_hash)]
    _write_jsonl(source, source_cases)
    seed = _write_kb_seed(tmp_path, manifest_rows)
    _write_manifest(manifest, manifest_rows, source_cases, seed)

    with pytest.raises(ValueError, match="adjacent"):
        build_reviewed_ticket_ask_cases(source, manifest, outside_output, seed)
    assert not outside_output.exists()


def test_export_rejects_lookalike_private_root(tmp_path: Path) -> None:
    lookalike = tmp_path / "other" / "data" / "private" / "ticket-review"
    source = lookalike / "product_calibration_cases.jsonl"
    manifest = lookalike / "top20_review_manifest.csv"
    output = lookalike / "reviewed.json"
    seed = tmp_path / "data" / "knowledge_base_seed.json"

    with pytest.raises(ValueError, match="project data/private"):
        build_reviewed_ticket_ask_cases(source, manifest, output, seed)


def test_export_requires_seed_under_versioned_data_root(tmp_path: Path) -> None:
    directory = _private_dir(tmp_path)
    source = directory / "product_calibration_cases.jsonl"
    manifest = directory / "top20_review_manifest.csv"
    output = directory / "reviewed.json"
    outside_seed = tmp_path / "other" / "knowledge_base_seed.json"

    with pytest.raises(ValueError, match="project versioned data root"):
        build_reviewed_ticket_ask_cases(
            source,
            manifest,
            output,
            outside_seed,
        )


def test_export_forbids_private_seed(tmp_path: Path) -> None:
    directory = _private_dir(tmp_path)
    source = directory / "product_calibration_cases.jsonl"
    manifest = directory / "top20_review_manifest.csv"
    output = directory / "reviewed.json"
    private_seed = directory / "knowledge_base_seed.json"

    with pytest.raises(ValueError, match="outside data/private"):
        build_reviewed_ticket_ask_cases(
            source,
            manifest,
            output,
            private_seed,
        )


def test_export_is_deterministic(tmp_path: Path) -> None:
    directory = _private_dir(tmp_path)
    source = directory / "product_calibration_cases.jsonl"
    manifest = directory / "top20_review_manifest.csv"
    first_output = directory / "reviewed-first.json"
    second_output = directory / "reviewed-second.json"
    first_hash = "bbbbbbbbbbbbbbbb"
    second_hash = "aaaaaaaaaaaaaaaa"
    source_cases = [_source_case(first_hash), _source_case(second_hash)]
    manifest_rows = [_manifest_row(first_hash), _manifest_row(second_hash)]
    _write_jsonl(
        source,
        source_cases,
    )
    seed = _write_kb_seed(tmp_path, manifest_rows)
    _write_manifest(
        manifest,
        manifest_rows,
        source_cases,
        seed,
    )

    build_reviewed_ticket_ask_cases(source, manifest, first_output, seed)
    build_reviewed_ticket_ask_cases(source, manifest, second_output, seed)

    assert first_output.read_bytes() == second_output.read_bytes()


def test_kb_seed_hash_is_stable_across_json_formatting(tmp_path: Path) -> None:
    payload = [
        {
            "chunk_id": DEFAULT_CHUNK_ID,
            "status": "published",
            "source_type": "yonote",
        }
    ]
    compact = tmp_path / "compact.json"
    pretty = tmp_path / "pretty.json"
    compact.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    pretty.write_text(
        json.dumps(payload, ensure_ascii=False, indent=4) + "\r\n",
        encoding="utf-8",
    )

    _, compact_hash = exporter._read_kb_seed(compact)
    _, pretty_hash = exporter._read_kb_seed(pretty)

    assert compact_hash == pretty_hash


def test_cli_runs_from_repository_root() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_reviewed_ticket_ask_cases.py",
            "--help",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert "--kb-seed" in result.stdout
    assert "--freeze" in result.stdout
    assert "--review-workbook" in result.stdout
    assert "--seal-review-payload-hashes" in result.stdout
    assert "--print-kb-seed-sha256" in result.stdout
