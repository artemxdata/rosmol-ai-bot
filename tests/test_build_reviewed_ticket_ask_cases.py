from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.build_reviewed_ticket_ask_cases as exporter
from scripts.build_reviewed_ticket_ask_cases import (
    KB_SEED_HASH_CANONICALIZATION,
    build_reviewed_ticket_ask_cases,
)
from scripts.build_ticket_product_review import source_case_fingerprint

DEFAULT_CHUNK_ID = "yonote_chunk_a"


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
    "answerable_from_snapshot",
    "approved_chunk_ids",
    "forbidden_profiles",
    "include_in_calibration",
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
    approved_chunk_ids: str | None = None,
    forbidden_profiles: str = "",
    reviewer: str = "reviewer@example.test",
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
        "answerable_from_snapshot": "true" if route == "answer" else "false",
        "approved_chunk_ids": approved_chunk_ids,
        "forbidden_profiles": forbidden_profiles,
        "include_in_calibration": include,
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


def _build(
    tmp_path: Path,
    source_cases: list[dict[str, object]],
    manifest_rows: list[dict[str, str]],
    *,
    output_name: str = "reviewed.json",
    seed_rows: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], Path]:
    directory = _private_dir(tmp_path)
    source = directory / "product_calibration_cases.jsonl"
    manifest = directory / "top20_review_manifest.csv"
    output = directory / output_name
    _write_jsonl(source, source_cases)
    seed = _write_kb_seed(tmp_path, manifest_rows, seed_rows=seed_rows)
    _write_manifest(manifest, manifest_rows, source_cases, seed)
    stats = build_reviewed_ticket_ask_cases(source, manifest, output, seed)
    return stats, output


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
        "reviewer": "reviewer@example.test",
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
        ("", "2026-07-29T12:00:00+07:00", "invalid reviewer"),
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
    assert "--print-kb-seed-sha256" in result.stdout
