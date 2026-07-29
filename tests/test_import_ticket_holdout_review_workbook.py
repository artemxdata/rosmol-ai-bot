from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

import scripts.import_ticket_holdout_review_workbook as importer
from scripts.build_ticket_product_review import (
    MANIFEST_FIELDS,
    source_case_fingerprint,
)
from scripts.import_ticket_holdout_review_workbook import (
    _REQUIRED_HEADERS,
    import_holdout_review_workbook,
)
from src.kb.source_extractors import SpreadsheetRow


@pytest.fixture(autouse=True)
def _patch_private_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    private_root = project_root / "data" / "private"
    private_root.mkdir(parents=True)
    monkeypatch.setattr(importer, "PROJECT_ROOT", project_root.resolve())
    monkeypatch.setattr(importer, "PRIVATE_DATA_ROOT", private_root.resolve())


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    directory = tmp_path / "project" / "data" / "private" / "holdout"
    directory.mkdir(parents=True, exist_ok=True)
    workbook = directory / "review.xlsx"
    workbook.write_bytes(b"filled review workbook")
    return (
        workbook,
        directory / "selection.csv",
        directory / "source.jsonl",
        directory / "freeze.json",
        directory / "reviewed.csv",
    )


def _source_case(case_id: str = "a" * 24) -> dict[str, Any]:
    return {
        "schema_version": "2.0.0",
        "ticket_id_hash": case_id,
        "duplicate_cluster_id": "b" * 24,
        "duplicate_component_id": "d" * 24,
        "split": "holdout",
        "query": "Иван Иванов, телефон +7 999 000-00-00",
        "category": "forums",
        "topic": "dates",
        "entity": "forum",
        "expected_response_profile": "dates",
        "expected_route": "answer",
        "expected_escalation_reason": "",
        "forbidden_response_profiles": ["travel"],
        "role_reconstruction_status": "complete",
        "multiturn_status": "single_turn",
        "time_sensitive": True,
        "operator_answer_included": False,
        "operator_answer_used_as_fact": False,
    }


def _selection_row(source: dict[str, Any]) -> dict[str, str]:
    row = {field: "" for field in MANIFEST_FIELDS}
    row.update(
        {
            "case_id_hash": str(source["ticket_id_hash"]),
            "duplicate_cluster_id": str(source["duplicate_cluster_id"]),
            "source_schema_version": str(source["schema_version"]),
            "source_case_fingerprint": source_case_fingerprint(source),
            "intent": "forums",
            "aspect": "dates",
            "entity_class": "forum",
            "expected_route": "answer",
            "expected_escalation_reason": "",
            "time_sensitive": "true",
            "difficulty": "simple",
            "role_reconstruction_status": "complete",
            "multiturn_status": "single_turn",
        }
    )
    return row


def _write_selection(path: Path, row: dict[str, str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(MANIFEST_FIELDS))
        writer.writeheader()
        writer.writerow(row)


def _write_source(path: Path, source: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(source, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seal_freeze(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload["freeze_contract_hash_scope"] = importer._FREEZE_HASH_SCOPE
    contract = {
        key: value
        for key, value in payload.items()
        if key not in importer._FREEZE_HASH_FIELDS
    }
    payload["freeze_contract_sha256"] = hashlib.sha256(
        json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return payload


def _write_freeze(
    path: Path,
    *,
    source: Path,
    selection: Path,
    case_ids: list[str],
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "selection_status": "sealed_pending_human_review",
        "execution_allowed": False,
        "cases_total": len(case_ids),
        "source": {
            "sha256": _file_sha256(source),
            "cases_total": len(case_ids),
        },
        "selection": {
            "sha256": _file_sha256(selection),
            "case_id_hashes": case_ids,
            "selected_case_ids_sha256": hashlib.sha256(
                ("\n".join(sorted(case_ids)) + "\n").encode("utf-8")
            ).hexdigest(),
        },
        "knowledge_snapshot": {
            "canonical_sha256": "f" * 64,
        },
    }
    payload.update(overrides or {})
    payload = _seal_freeze(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _prepare(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path, dict[str, Any], dict[str, str]]:
    workbook, selection, source_path, freeze, output = _paths(tmp_path)
    source = _source_case()
    selection_row = _selection_row(source)
    _write_source(source_path, source)
    _write_selection(selection, selection_row)
    _write_freeze(
        freeze,
        source=source_path,
        selection=selection,
        case_ids=[selection_row["case_id_hash"]],
    )
    return (
        workbook,
        selection,
        source_path,
        freeze,
        output,
        source,
        selection_row,
    )


def _review_sheet(
    row: dict[str, str],
    *,
    raw_query: str,
    deidentified_query: str = "Когда пройдёт форум?",
    date_privacy_verdict: str = "event_date_only",
    reviewed_at: str = "2026-07-29T12:00:00+07:00",
    immutable_overrides: dict[str, str] | None = None,
) -> list[SpreadsheetRow]:
    values = {
        "№": "1",
        "case_id_hash": row["case_id_hash"],
        "Приватный запрос НЕ ЭКСПОРТИРОВАТЬ": raw_query,
        "Обезличенный запрос для теста": deidentified_query,
        "Исходный intent": row["intent"],
        "Исправленный intent": "",
        "Предложенный profile": row["aspect"],
        "Исправленный profile": "",
        "Предложенный entity class": row["entity_class"],
        "Исправленный entity class": "",
        "Предложенный route": row["expected_route"],
        "Исправленный route": "",
        "Предложенная причина escalation": row[
            "expected_escalation_reason"
        ],
        "Исправленная причина escalation": "",
        "Time-sensitive": row["time_sensitive"],
        "Difficulty": row["difficulty"],
        "Role status": row["role_reconstruction_status"],
        "Role verdict": "confirmed_user_turn",
        "Answerable from snapshot": "true",
        "Approved chunk IDs через |": "yonote_chunk",
        "Forbidden profiles через |": "travel",
        "Label verdict": "approved",
        "Privacy verdict": "approved",
        "Date privacy verdict": date_privacy_verdict,
        "Include in holdout": "true",
        "Reviewer": "reviewer@example.test",
        "Reviewed at ISO + timezone": reviewed_at,
        "Source fingerprint": row["source_case_fingerprint"],
        "Duplicate cluster": row["duplicate_cluster_id"],
        "Review note": "",
    }
    values.update(immutable_overrides or {})
    return [
        SpreadsheetRow(
            sheet_name="Pre-run review",
            row_number=4,
            cells=tuple(_REQUIRED_HEADERS),
        ),
        SpreadsheetRow(
            sheet_name="Pre-run review",
            row_number=5,
            cells=tuple(values[header] for header in _REQUIRED_HEADERS),
        ),
    ]


def _import(
    paths: tuple[Path, Path, Path, Path, Path],
) -> dict[str, Any]:
    workbook, selection, source, freeze, output = paths
    return import_holdout_review_workbook(
        workbook_path=workbook,
        selection_path=selection,
        source_path=source,
        freeze_path=freeze,
        output_path=output,
        expected_total=1,
    )


def _mock_workbook(
    monkeypatch: pytest.MonkeyPatch,
    *,
    row: dict[str, str],
    raw_query: str,
    **review_kwargs: str,
) -> None:
    monkeypatch.setattr(
        importer,
        "_validate_workbook_package",
        lambda path: None,
    )
    monkeypatch.setattr(
        importer,
        "read_xlsx_sheets",
        lambda path: {
            "Pre-run review": _review_sheet(
                row,
                raw_query=raw_query,
                **review_kwargs,
            )
        },
    )


def test_import_binds_receipts_and_excludes_raw_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path)
    workbook, selection, source, freeze, output, source_case, source_row = prepared
    _mock_workbook(
        monkeypatch,
        row=source_row,
        raw_query=str(source_case["query"]),
    )

    stats = _import((workbook, selection, source, freeze, output))

    rows = list(
        csv.DictReader(output.read_text(encoding="utf-8-sig").splitlines())
    )
    serialized = output.read_text(encoding="utf-8-sig")
    freeze_payload = json.loads(freeze.read_text(encoding="utf-8"))
    assert stats == {
        "imported_rows": 1,
        "unique_case_ids": 1,
        "review_payload_hashes_sealed": False,
        "review_receipts_bound": True,
        "raw_queries_exported": False,
        "output": str(output.resolve()),
    }
    assert rows[0]["deidentified_query"] == "Когда пройдёт форум?"
    assert rows[0]["date_privacy_verdict"] == "event_date_only"
    assert rows[0]["review_workbook_sha256"] == _file_sha256(workbook)
    assert rows[0]["review_source_sha256"] == _file_sha256(source)
    assert rows[0]["review_selection_sha256"] == _file_sha256(selection)
    assert (
        rows[0]["review_freeze_contract_sha256"]
        == freeze_payload["freeze_contract_sha256"]
    )
    assert rows[0]["approved_kb_seed_sha256"] == "f" * 64
    assert rows[0]["review_payload_sha256"] == ""
    assert "Иван Иванов" not in serialized
    assert "+7 999 000-00-00" not in serialized


def test_import_normalizes_excel_review_timestamp_serial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path)
    workbook, selection, source, freeze, output, source_case, source_row = prepared
    _mock_workbook(
        monkeypatch,
        row=source_row,
        raw_query=str(source_case["query"]),
        reviewed_at="46232.5",
    )

    _import((workbook, selection, source, freeze, output))

    rows = list(
        csv.DictReader(output.read_text(encoding="utf-8-sig").splitlines())
    )
    assert rows[0]["reviewed_at"] == "2026-07-29T12:00:00Z"


def test_import_rejects_raw_query_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path)
    workbook, selection, source, freeze, output, _, source_row = prepared
    _mock_workbook(
        monkeypatch,
        row=source_row,
        raw_query="Подменённый приватный запрос",
    )

    with pytest.raises(ValueError, match="changed the frozen raw query"):
        _import((workbook, selection, source, freeze, output))


def test_import_rejects_immutable_workbook_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path)
    workbook, selection, source, freeze, output, source_case, source_row = prepared
    _mock_workbook(
        monkeypatch,
        row=source_row,
        raw_query=str(source_case["query"]),
        immutable_overrides={"Предложенный profile": "travel"},
    )

    with pytest.raises(ValueError, match="immutable field aspect"):
        _import((workbook, selection, source, freeze, output))


def test_import_rejects_formula_like_review_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path)
    workbook, selection, source, freeze, output, source_case, source_row = prepared
    _mock_workbook(
        monkeypatch,
        row=source_row,
        raw_query=str(source_case["query"]),
        deidentified_query='=HYPERLINK("https://example.test")',
    )

    with pytest.raises(ValueError, match="formula-like text"):
        _import((workbook, selection, source, freeze, output))


def test_import_rejects_invalid_date_privacy_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path)
    workbook, selection, source, freeze, output, source_case, source_row = prepared
    _mock_workbook(
        monkeypatch,
        row=source_row,
        raw_query=str(source_case["query"]),
        date_privacy_verdict="approved",
    )

    with pytest.raises(ValueError, match="invalid date_privacy_verdict"):
        _import((workbook, selection, source, freeze, output))


@pytest.mark.parametrize("artifact", ["source", "selection"])
def test_import_rejects_stale_freeze_artifact_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
) -> None:
    prepared = _prepare(tmp_path)
    workbook, selection, source, freeze, output, source_case, source_row = prepared
    payload = json.loads(freeze.read_text(encoding="utf-8"))
    payload[artifact]["sha256"] = "0" * 64
    freeze.write_text(
        json.dumps(_seal_freeze(payload), ensure_ascii=False),
        encoding="utf-8",
    )
    _mock_workbook(
        monkeypatch,
        row=source_row,
        raw_query=str(source_case["query"]),
    )

    with pytest.raises(ValueError, match=f"{artifact} SHA-256 mismatch"):
        _import((workbook, selection, source, freeze, output))


def test_import_rejects_mutated_freeze_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path)
    workbook, selection, source, freeze, output, source_case, source_row = prepared
    payload = json.loads(freeze.read_text(encoding="utf-8"))
    payload["cases_total"] = 2
    freeze.write_text(json.dumps(payload), encoding="utf-8")
    _mock_workbook(
        monkeypatch,
        row=source_row,
        raw_query=str(source_case["query"]),
    )

    with pytest.raises(ValueError, match="self-hash mismatch"):
        _import((workbook, selection, source, freeze, output))


def test_import_rejects_freeze_case_id_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path)
    workbook, selection, source, freeze, output, source_case, source_row = prepared
    payload = json.loads(freeze.read_text(encoding="utf-8"))
    payload["selection"]["case_id_hashes"] = ["e" * 24]
    payload["selection"]["selected_case_ids_sha256"] = hashlib.sha256(
        (("e" * 24) + "\n").encode("utf-8")
    ).hexdigest()
    freeze.write_text(
        json.dumps(_seal_freeze(payload), ensure_ascii=False),
        encoding="utf-8",
    )
    _mock_workbook(
        monkeypatch,
        row=source_row,
        raw_query=str(source_case["query"]),
    )

    with pytest.raises(ValueError, match="case IDs do not match"):
        _import((workbook, selection, source, freeze, output))


def test_import_rejects_unexpected_selection_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path)
    workbook, selection, source, freeze, output, source_case, source_row = prepared
    with selection.open("w", encoding="utf-8-sig", newline="") as file:
        fields = [*MANIFEST_FIELDS, "query"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerow({**source_row, "query": "private query"})
    _mock_workbook(
        monkeypatch,
        row=source_row,
        raw_query=str(source_case["query"]),
    )

    with pytest.raises(ValueError, match="safe schema exactly"):
        _import((workbook, selection, source, freeze, output))


def _write_workbook_package(
    workbook: Path,
    *,
    pre_formula: str = "",
    post_formula: str = "",
) -> None:
    pre_cell = (
        f'<c r="D5"><f>{pre_formula}</f><v>2</v></c>'
        if pre_formula
        else '<c r="D5"><v></v></c>'
    )
    post_cell = (
        f'<c r="C5"><f>{post_formula}</f><v></v></c>'
        if post_formula
        else '<c r="C5"><v></v></c>'
    )
    with ZipFile(workbook, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            (
                '<workbook xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships">'
                '<sheets><sheet name="Pre-run review" sheetId="1" '
                'r:id="rId1"/><sheet name="Post-run verdict" sheetId="2" '
                'r:id="rId2"/></sheets></workbook>'
            ),
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            (
                '<Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships">'
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
                "</Relationships>"
            ),
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            (
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData><row r="5">'
                f"{pre_cell}"
                "</row></sheetData></worksheet>"
            ),
        )
        archive.writestr(
            "xl/worksheets/sheet2.xml",
            (
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData><row r="5">'
                f"{post_cell}"
                "</row></sheetData></worksheet>"
            ),
        )


def test_workbook_package_rejects_formula_on_pre_run_sheet(
    tmp_path: Path,
) -> None:
    workbook, _, _, _, _ = _paths(tmp_path)
    workbook.unlink()
    _write_workbook_package(workbook, pre_formula="1+1")

    with pytest.raises(ValueError, match="must not contain formulas"):
        importer._validate_workbook_package(workbook)


def test_workbook_package_rejects_post_run_raw_query_reference(
    tmp_path: Path,
) -> None:
    workbook, _, _, _, _ = _paths(tmp_path)
    workbook.unlink()
    _write_workbook_package(
        workbook,
        post_formula="'Pre-run review'!C5",
    )

    with pytest.raises(ValueError, match="must not reference private raw"):
        importer._validate_workbook_package(workbook)
