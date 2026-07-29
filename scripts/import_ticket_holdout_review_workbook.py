from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import posixpath
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.build_ticket_product_review import (  # noqa: E402
    MANIFEST_FIELDS,
    source_case_fingerprint,
)
from src.kb.source_extractors import SpreadsheetRow, read_xlsx_sheets  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DATA_ROOT = (PROJECT_ROOT / "data" / "private").resolve()

REVIEW_SHEET_NAME = "Pre-run review"
POST_REVIEW_SHEET_NAME = "Post-run verdict"
EXPECTED_HOLDOUT_CASES = 80

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_FREEZE_HASH_SCOPE = "canonical_json_without_freeze_contract_sha256_fields"
_FREEZE_HASH_FIELDS = frozenset(
    {
        "freeze_contract_sha256",
        "freeze_contract_hash_scope",
    }
)
_SAFE_HASH_RE = re.compile(r"^[0-9a-f]{12,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_POST_RAW_QUERY_REF_RE = re.compile(
    r"'Pre-run review'!\$?C(?:\$?\d+)?",
    re.IGNORECASE,
)
_DATE_PRIVACY_VERDICTS = frozenset(
    {
        "",
        "not_present",
        "event_date_only",
    }
)

_REQUIRED_HEADERS = (
    "№",
    "case_id_hash",
    "Приватный запрос НЕ ЭКСПОРТИРОВАТЬ",
    "Обезличенный запрос для теста",
    "Исходный intent",
    "Исправленный intent",
    "Предложенный profile",
    "Исправленный profile",
    "Предложенный entity class",
    "Исправленный entity class",
    "Предложенный route",
    "Исправленный route",
    "Предложенная причина escalation",
    "Исправленная причина escalation",
    "Time-sensitive",
    "Difficulty",
    "Role status",
    "Role verdict",
    "Answerable from snapshot",
    "Approved chunk IDs через |",
    "Forbidden profiles через |",
    "Label verdict",
    "Privacy verdict",
    "Date privacy verdict",
    "Include in holdout",
    "Reviewer",
    "Reviewed at ISO + timezone",
    "Source fingerprint",
    "Duplicate cluster",
    "Review note",
)

_IMMUTABLE_HEADER_TO_FIELD = {
    "Исходный intent": "intent",
    "Предложенный profile": "aspect",
    "Предложенный entity class": "entity_class",
    "Предложенный route": "expected_route",
    "Предложенная причина escalation": "expected_escalation_reason",
    "Time-sensitive": "time_sensitive",
    "Difficulty": "difficulty",
    "Role status": "role_reconstruction_status",
    "Source fingerprint": "source_case_fingerprint",
    "Duplicate cluster": "duplicate_cluster_id",
}

_REVIEW_HEADER_TO_FIELD = {
    "Обезличенный запрос для теста": "deidentified_query",
    "Исправленный intent": "corrected_intent",
    "Исправленный profile": "corrected_aspect",
    "Исправленный entity class": "corrected_entity_class",
    "Исправленный route": "corrected_route",
    "Исправленная причина escalation": "corrected_escalation_reason",
    "Role verdict": "role_verdict",
    "Answerable from snapshot": "answerable_from_snapshot",
    "Approved chunk IDs через |": "approved_chunk_ids",
    "Forbidden profiles через |": "forbidden_profiles",
    "Label verdict": "label_verdict",
    "Privacy verdict": "privacy_verdict",
    "Date privacy verdict": "date_privacy_verdict",
    "Include in holdout": "include_in_holdout",
    "Reviewer": "reviewer",
    "Reviewed at ISO + timezone": "reviewed_at",
}


def import_holdout_review_workbook(
    *,
    workbook_path: Path,
    selection_path: Path,
    source_path: Path,
    freeze_path: Path,
    output_path: Path,
    expected_total: int = EXPECTED_HOLDOUT_CASES,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Import only reviewed fields from the private XLSX into a new CSV manifest."""

    output = _private_output(output_path, suffix=".csv")
    input_paths = {
        Path(workbook_path).expanduser().resolve(),
        Path(selection_path).expanduser().resolve(),
        Path(source_path).expanduser().resolve(),
        Path(freeze_path).expanduser().resolve(),
    }
    if output in input_paths or len(input_paths) != 4:
        raise ValueError(
            "Workbook, selection, source, freeze and output paths must be distinct"
        )
    if output.exists() and not overwrite:
        raise ValueError(f"Reviewed manifest already exists: {output}")

    selection_fields, output_rows = load_holdout_review_workbook_rows(
        workbook_path=workbook_path,
        selection_path=selection_path,
        source_path=source_path,
        freeze_path=freeze_path,
        expected_total=expected_total,
    )
    _write_csv(
        output,
        selection_fields,
        output_rows,
        overwrite=overwrite,
    )
    return {
        "imported_rows": len(output_rows),
        "unique_case_ids": len(output_rows),
        "review_payload_hashes_sealed": False,
        "review_receipts_bound": True,
        "raw_queries_exported": False,
        "output": str(output),
    }


def load_holdout_review_workbook_rows(
    *,
    workbook_path: Path,
    selection_path: Path,
    source_path: Path,
    freeze_path: Path,
    expected_total: int = EXPECTED_HOLDOUT_CASES,
) -> tuple[list[str], list[dict[str, str]]]:
    """Validate a filled review workbook and derive its safe manifest rows."""

    workbook = _private_input(workbook_path, suffix=".xlsx")
    selection = _private_input(selection_path, suffix=".csv")
    source = _private_input(source_path, suffix=".jsonl")
    freeze = _private_input(freeze_path, suffix=".json")
    if len({workbook, selection, source, freeze}) != 4:
        raise ValueError(
            "Workbook, selection, source and freeze paths must be distinct"
        )
    if expected_total <= 0:
        raise ValueError("expected_total must be positive")
    _validate_workbook_package(workbook)
    sheets = read_xlsx_sheets(workbook)
    review_rows = sheets.get(REVIEW_SHEET_NAME)
    if not review_rows:
        raise ValueError(f"Workbook has no {REVIEW_SHEET_NAME!r} sheet")
    headers, workbook_rows = _read_review_rows(review_rows)
    selection_fields, selection_rows = _read_selection(selection)
    source_cases = _read_source_cases(source)
    freeze_payload = _read_freeze(freeze)
    receipt = _validated_freeze_binding(
        freeze_payload,
        source=source,
        selection=selection,
        source_cases=source_cases,
        selection_rows=selection_rows,
        expected_total=expected_total,
    )
    receipt["workbook_sha256"] = _file_sha256(workbook)
    if len(selection_rows) != expected_total:
        raise ValueError(
            f"Selection has {len(selection_rows)} rows; expected {expected_total}"
        )
    if len(workbook_rows) != expected_total:
        raise ValueError(
            f"Workbook has {len(workbook_rows)} review rows; expected {expected_total}"
        )

    selection_by_id: dict[str, dict[str, str]] = {}
    for row_number, row in enumerate(selection_rows, start=2):
        case_id = str(row.get("case_id_hash") or "").strip()
        if not case_id:
            raise ValueError(f"Selection row {row_number} has no case_id_hash")
        if case_id in selection_by_id:
            raise ValueError(f"Selection contains duplicate case_id_hash {case_id}")
        selection_by_id[case_id] = row

    imported_ids: set[str] = set()
    output_rows: list[dict[str, str]] = []
    for row_number, values in workbook_rows:
        case_id = _cell(values, headers, "case_id_hash")
        if not case_id:
            raise ValueError(f"Workbook row {row_number} has no case_id_hash")
        if case_id in imported_ids:
            raise ValueError(f"Workbook contains duplicate case_id_hash {case_id}")
        selection_row = selection_by_id.get(case_id)
        if selection_row is None:
            raise ValueError(
                f"Workbook row {row_number} is outside the frozen selection"
            )
        source_case = source_cases.get(case_id)
        if source_case is None:
            raise ValueError(
                f"Workbook case {case_id} is missing from the frozen source"
            )
        _validate_immutable_cells(
            values,
            headers=headers,
            selection_row=selection_row,
            case_id=case_id,
        )
        _validate_source_binding(
            values,
            headers=headers,
            selection_row=selection_row,
            source_case=source_case,
            case_id=case_id,
        )

        imported = dict(selection_row)
        for header, field in _REVIEW_HEADER_TO_FIELD.items():
            value = _cell(values, headers, header)
            _reject_formula_like_text(
                value,
                field=field,
                case_id=case_id,
            )
            imported[field] = value
        if imported["date_privacy_verdict"] not in _DATE_PRIVACY_VERDICTS:
            raise ValueError(
                f"Workbook case {case_id} has invalid date_privacy_verdict"
            )
        imported["review_workbook_sha256"] = receipt["workbook_sha256"]
        imported["review_source_sha256"] = receipt["source_sha256"]
        imported["review_selection_sha256"] = receipt["selection_sha256"]
        imported["review_freeze_contract_sha256"] = receipt[
            "freeze_contract_sha256"
        ]
        imported["approved_kb_seed_sha256"] = receipt[
            "approved_kb_seed_sha256"
        ]
        imported["review_payload_sha256"] = ""
        output_rows.append(imported)
        imported_ids.add(case_id)

    if imported_ids != set(selection_by_id):
        missing = len(set(selection_by_id) - imported_ids)
        raise ValueError(f"Workbook is missing {missing} frozen selection rows")

    output_rows.sort(key=lambda row: row["case_id_hash"])
    serialized = json.dumps(
        output_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    raw_queries = {
        _source_query(source_cases[case_id])
        for case_id in imported_ids
    }
    if any(query and query in serialized for query in raw_queries):
        raise ValueError("Reviewed manifest unexpectedly contains a private query")
    return selection_fields, output_rows


def _read_review_rows(
    rows: list[SpreadsheetRow],
) -> tuple[dict[str, int], list[tuple[int, tuple[str, ...]]]]:
    header_row = next(
        (
            row
            for row in rows
            if row.cell(0) == "№" and row.cell(1) == "case_id_hash"
        ),
        None,
    )
    if header_row is None:
        raise ValueError("Workbook review header was not found")
    header_indexes = {
        _normalized_header(value): index
        for index, value in enumerate(header_row.cells)
        if value.strip()
    }
    normalized_headers = [
        _normalized_header(value)
        for value in header_row.cells
        if value.strip()
    ]
    if len(normalized_headers) != len(set(normalized_headers)):
        raise ValueError("Workbook review sheet contains duplicate headers")
    missing = [
        header
        for header in _REQUIRED_HEADERS
        if header not in header_indexes
    ]
    if missing:
        raise ValueError(
            "Workbook review sheet is missing headers: " + ", ".join(missing)
        )
    data = [
        (row.row_number, row.cells)
        for row in rows
        if row.row_number > header_row.row_number and row.cell(1)
    ]
    return header_indexes, data


def _read_selection(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            fields = list(reader.fieldnames or [])
            expected_fields = list(MANIFEST_FIELDS)
            if fields != expected_fields:
                missing = sorted(set(expected_fields) - set(fields))
                unexpected = sorted(set(fields) - set(expected_fields))
                raise ValueError(
                    "Selection manifest fields must match the safe schema exactly: "
                    f"missing={missing}, unexpected={unexpected}"
                )
            return fields, list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(f"Could not read selection manifest: {path}") from exc


def _read_source_cases(path: Path) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    try:
        source_file = path.open("r", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"Could not read holdout source: {path}") from exc
    with source_file:
        for line_number, line in enumerate(source_file, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(
                    f"Invalid source JSON at line {line_number}"
                ) from None
            if not isinstance(payload, dict):
                raise ValueError(
                    f"Source line {line_number} must be an object"
                )
            case_id = str(payload.get("ticket_id_hash") or "").strip()
            if not _SAFE_HASH_RE.fullmatch(case_id):
                raise ValueError(
                    f"Source line {line_number} has an invalid ticket_id_hash"
                )
            if case_id in cases:
                raise ValueError(f"Source contains duplicate case {case_id}")
            _source_query(payload)
            cases[case_id] = payload
    if not cases:
        raise ValueError("Holdout source is empty")
    return cases


def _source_query(source_case: dict[str, Any]) -> str:
    query = str(source_case.get("query") or "").strip()
    if not query:
        raise ValueError("Frozen source case has no query")
    return query


def _read_freeze(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read freeze contract: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Freeze contract must be a JSON object")
    _verify_freeze_contract(payload)
    return payload


def _validated_freeze_binding(
    freeze: dict[str, Any],
    *,
    source: Path,
    selection: Path,
    source_cases: dict[str, dict[str, Any]],
    selection_rows: list[dict[str, str]],
    expected_total: int,
) -> dict[str, str]:
    if freeze.get("selection_status") != "sealed_pending_human_review":
        raise ValueError("Freeze contract is not pending human review")
    if freeze.get("execution_allowed") is not False:
        raise ValueError("Freeze contract unexpectedly allows execution")
    if freeze.get("cases_total") != expected_total:
        raise ValueError(
            "Freeze contract case count does not match expected_total"
        )

    source_sha256 = _file_sha256(source)
    selection_sha256 = _file_sha256(selection)
    source_evidence = freeze.get("source")
    selection_evidence = freeze.get("selection")
    if not isinstance(source_evidence, dict) or not isinstance(
        selection_evidence,
        dict,
    ):
        raise ValueError("Freeze contract has no source/selection evidence")
    if source_evidence.get("sha256") != source_sha256:
        raise ValueError("Freeze contract source SHA-256 mismatch")
    if source_evidence.get("cases_total") != len(source_cases):
        raise ValueError("Freeze contract source case count mismatch")
    if selection_evidence.get("sha256") != selection_sha256:
        raise ValueError("Freeze contract selection SHA-256 mismatch")

    frozen_ids = selection_evidence.get("case_id_hashes")
    if (
        not isinstance(frozen_ids, list)
        or len(frozen_ids) != expected_total
        or len(set(frozen_ids)) != expected_total
        or any(
            not isinstance(item, str)
            or not _SAFE_HASH_RE.fullmatch(item)
            for item in frozen_ids
        )
    ):
        raise ValueError("Freeze contract has invalid selected case IDs")
    frozen_id_set = set(frozen_ids)
    selection_ids = {
        str(row.get("case_id_hash") or "").strip()
        for row in selection_rows
    }
    if selection_ids != frozen_id_set or len(selection_rows) != len(
        selection_ids
    ):
        raise ValueError(
            "Selection case IDs do not match the freeze contract"
        )
    if not frozen_id_set.issubset(source_cases):
        raise ValueError(
            "Freeze contract contains cases absent from the source"
        )
    expected_ids_sha256 = hashlib.sha256(
        ("\n".join(sorted(frozen_ids)) + "\n").encode("utf-8")
    ).hexdigest()
    if (
        selection_evidence.get("selected_case_ids_sha256")
        != expected_ids_sha256
    ):
        raise ValueError(
            "Freeze contract selected case ID digest mismatch"
        )
    knowledge_evidence = freeze.get("knowledge_snapshot")
    if not isinstance(knowledge_evidence, dict):
        raise ValueError("Freeze contract has no knowledge snapshot evidence")
    approved_kb_seed_sha256 = str(
        knowledge_evidence.get("canonical_sha256") or ""
    ).strip()
    if not _SHA256_RE.fullmatch(approved_kb_seed_sha256):
        raise ValueError("Freeze contract has an invalid knowledge snapshot hash")
    return {
        "source_sha256": source_sha256,
        "selection_sha256": selection_sha256,
        "freeze_contract_sha256": str(freeze["freeze_contract_sha256"]),
        "approved_kb_seed_sha256": approved_kb_seed_sha256,
    }


def _verify_freeze_contract(payload: dict[str, Any]) -> None:
    if payload.get("freeze_contract_hash_scope") != _FREEZE_HASH_SCOPE:
        raise ValueError("Freeze contract has an unsupported hash scope")
    actual = str(payload.get("freeze_contract_sha256") or "").strip()
    if not _SHA256_RE.fullmatch(actual):
        raise ValueError("Freeze contract has an invalid self-hash")
    contract = {
        key: value
        for key, value in payload.items()
        if key not in _FREEZE_HASH_FIELDS
    }
    expected = hashlib.sha256(
        json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if actual != expected:
        raise ValueError("Freeze contract self-hash mismatch")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_immutable_cells(
    values: tuple[str, ...],
    *,
    headers: dict[str, int],
    selection_row: dict[str, str],
    case_id: str,
) -> None:
    for header, field in _IMMUTABLE_HEADER_TO_FIELD.items():
        workbook_value = _cell(values, headers, header)
        selection_value = str(selection_row.get(field) or "").strip()
        if workbook_value != selection_value:
            raise ValueError(
                f"Workbook case {case_id} changed immutable field {field}"
            )


def _validate_source_binding(
    values: tuple[str, ...],
    *,
    headers: dict[str, int],
    selection_row: dict[str, str],
    source_case: dict[str, Any],
    case_id: str,
) -> None:
    source_cluster = str(source_case.get("duplicate_cluster_id") or "").strip()
    selection_cluster = str(
        selection_row.get("duplicate_cluster_id") or ""
    ).strip()
    if (
        not _SAFE_HASH_RE.fullmatch(source_cluster)
        or source_cluster != selection_cluster
    ):
        raise ValueError(
            f"Frozen source case {case_id} has a stale duplicate cluster"
        )
    actual_fingerprint = source_case_fingerprint(source_case)
    selection_fingerprint = str(
        selection_row.get("source_case_fingerprint") or ""
    ).strip()
    if actual_fingerprint != selection_fingerprint:
        raise ValueError(
            f"Frozen source case {case_id} has a stale source fingerprint"
        )
    workbook_query = _cell_exact(
        values,
        headers,
        "Приватный запрос НЕ ЭКСПОРТИРОВАТЬ",
    )
    if workbook_query != _source_query(source_case):
        raise ValueError(
            f"Workbook case {case_id} changed the frozen raw query"
        )


def _cell(
    values: tuple[str, ...],
    headers: dict[str, int],
    header: str,
) -> str:
    index = headers[header]
    if index >= len(values):
        return ""
    return str(values[index] or "").strip()


def _cell_exact(
    values: tuple[str, ...],
    headers: dict[str, int],
    header: str,
) -> str:
    index = headers[header]
    if index >= len(values):
        return ""
    return str(values[index] or "")


def _normalized_header(value: str) -> str:
    return " ".join(str(value or "").split())


def _validate_workbook_package(path: Path) -> None:
    try:
        with ZipFile(path) as archive:
            names = {name.casefold() for name in archive.namelist()}
            if (
                "xl/vbaproject.bin" in names
                or "xl/connections.xml" in names
                or any(name.startswith("xl/externallinks/") for name in names)
            ):
                raise ValueError(
                    "Review workbook must not contain macros, connections "
                    "or external links"
                )
            sheet_target = _review_sheet_target(archive)
            root = ET.fromstring(archive.read(sheet_target))
            if root.find(f".//{{{_MAIN_NS}}}f") is not None:
                raise ValueError(
                    "Pre-run review sheet must not contain formulas"
                )
            post_target = _sheet_target(
                archive,
                sheet_name=POST_REVIEW_SHEET_NAME,
            )
            post_root = ET.fromstring(archive.read(post_target))
            formulas = (
                str(formula.text or "")
                for formula in post_root.findall(f".//{{{_MAIN_NS}}}f")
            )
            if any(
                _POST_RAW_QUERY_REF_RE.search(formula)
                for formula in formulas
            ):
                raise ValueError(
                    "Post-run verdict must not reference private raw queries"
                )
    except (OSError, BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"Could not inspect review workbook: {path}") from exc


def _review_sheet_target(archive: ZipFile) -> str:
    return _sheet_target(archive, sheet_name=REVIEW_SHEET_NAME)


def _sheet_target(archive: ZipFile, *, sheet_name: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheet = next(
        (
            item
            for item in workbook.findall(f".//{{{_MAIN_NS}}}sheet")
            if item.attrib.get("name") == sheet_name
        ),
        None,
    )
    if sheet is None:
        raise ValueError(f"Workbook has no {sheet_name!r} sheet")
    relationship_id = sheet.attrib.get(f"{{{_REL_NS}}}id")
    if not relationship_id:
        raise ValueError("Review worksheet has no relationship")
    relationships = ET.fromstring(
        archive.read("xl/_rels/workbook.xml.rels")
    )
    target = next(
        (
            item.attrib.get("Target")
            for item in relationships.findall(
                f".//{{{_PACKAGE_REL_NS}}}Relationship"
            )
            if item.attrib.get("Id") == relationship_id
        ),
        None,
    )
    if not target:
        raise ValueError("Review worksheet relationship is missing")
    normalized_target = target.replace("\\", "/")
    if normalized_target.startswith("/xl/"):
        normalized_target = normalized_target.removeprefix("/")
    elif normalized_target.startswith("/"):
        raise ValueError("Review worksheet relationship is unsafe")
    else:
        normalized_target = posixpath.join("xl", normalized_target)
    normalized = posixpath.normpath(normalized_target)
    if (
        not normalized.startswith("xl/worksheets/")
        or normalized.startswith("../")
        or normalized not in archive.namelist()
    ):
        raise ValueError("Review worksheet relationship is unsafe")
    return normalized


def _reject_formula_like_text(
    value: str,
    *,
    field: str,
    case_id: str,
) -> None:
    normalized = value.lstrip()
    if normalized.startswith(_FORMULA_PREFIXES):
        raise ValueError(
            f"Workbook case {case_id} has formula-like text in {field}"
        )


def _private_input(path: Path, *, suffix: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("Holdout review inputs must stay under data/private")
    if resolved.suffix.casefold() != suffix or not resolved.is_file():
        raise ValueError(f"Holdout review input must be an existing {suffix} file")
    return resolved


def _private_output(path: Path, *, suffix: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError("Holdout review output must stay under data/private")
    if resolved.suffix.casefold() != suffix:
        raise ValueError(f"Holdout review output must be a {suffix} file")
    return resolved


def _write_csv(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
    *,
    overwrite: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8-sig",
            newline="",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=fieldnames,
                extrasaction="raise",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
            file.flush()
            os.fsync(file.fileno())
        if path.exists() and not overwrite:
            raise ValueError(f"Reviewed manifest already exists: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Import reviewed holdout fields from a private XLSX into a "
            "manifest CSV without copying raw ticket queries."
        )
    )
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--expected-total",
        type=int,
        default=EXPECTED_HOLDOUT_CASES,
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    stats = import_holdout_review_workbook(
        workbook_path=args.workbook,
        selection_path=args.selection,
        source_path=args.source,
        freeze_path=args.freeze,
        output_path=args.output,
        expected_total=args.expected_total,
        overwrite=args.overwrite,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
