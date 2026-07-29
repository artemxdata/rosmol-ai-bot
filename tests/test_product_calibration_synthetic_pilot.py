from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from eval.run_ask import _normalize_case

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = (
    PROJECT_ROOT
    / "eval"
    / "cases"
    / "product_calibration_synthetic_pilot_20.json"
)
KB_SEED_PATH = PROJECT_ROOT / "data" / "knowledge_base_seed.json"

ALLOWED_CASE_FIELDS = {
    "schema_version",
    "id",
    "query",
    "user_id",
    "channel",
    "privacy_class",
    "label_status",
    "requires_human_review",
    "split",
    "dataset_kind",
    "evidence_scope",
    "aggregate_rank",
    "aggregate_intent",
    "aggregate_aspect",
    "aggregate_entity_class",
    "expected_behavior",
    "expected_response_profile",
    "forbidden_response_profiles",
    "expected_chunk_ids",
    "expected_cited_chunk_ids",
    "allowed_cited_source_types",
    "expected_answer_contains",
    "expected_escalated",
    "expected_escalation_reason",
    "tags",
}
FORBIDDEN_PRIVATE_FIELDS = {
    "ticket_id",
    "ticket_id_hash",
    "case_id_hash",
    "duplicate_cluster_id",
    "duplicate_component_id",
    "source_ticket_ids",
    "source_case_fingerprint",
    "source_turn_index",
    "reviewer",
    "reviewed_at",
    "manifest",
    "messages",
    "dialogue",
    "operator_answer",
    "raw_answer",
}
FORBIDDEN_CLAIM_TAGS = {
    "closure",
    "conversion",
    "holdout",
    "independent",
    "sealed",
    "validation",
}
FORBIDDEN_TEXT_MARKERS = (
    "data/private",
    "data\\private",
    "rag_dataset",
    "top20_review_manifest",
    "product_calibration_cases",
    "ticket_id",
    "duplicate_cluster",
    "source_case_fingerprint",
    "<email>",
    "<phone>",
    "<id>",
)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?7|8)[\s()/-]*\d(?:[\s()/-]*\d){9}(?!\w)")
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
LONG_HEX_RE = re.compile(r"\b[0-9a-f]{12,64}\b", re.IGNORECASE)
LONG_DIGIT_RE = re.compile(r"\b\d{9,}\b")


def _load_cases() -> list[dict[str, Any]]:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert all(isinstance(item, dict) for item in payload)
    return payload


def _all_strings(value: Any, *, key: str = "") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(key, value)]
    if isinstance(value, list):
        return [
            item
            for index, child in enumerate(value)
            for item in _all_strings(child, key=f"{key}[{index}]")
        ]
    if isinstance(value, dict):
        return [
            item
            for child_key, child in value.items()
            for item in _all_strings(
                child,
                key=f"{key}.{child_key}" if key else str(child_key),
            )
        ]
    return []


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(normalized.split())


def test_synthetic_pilot_has_exact_top20_contract() -> None:
    cases = _load_cases()

    assert len(cases) == 20
    assert [case["aggregate_rank"] for case in cases] == list(range(1, 21))
    assert len({case["id"] for case in cases}) == 20
    assert len({case["user_id"] for case in cases}) == 20
    assert len(
        {
            (
                case["aggregate_intent"],
                case["aggregate_aspect"],
                case["aggregate_entity_class"],
            )
            for case in cases
        }
    ) == 20

    for case in cases:
        assert not (set(case) - ALLOWED_CASE_FIELDS)
        assert not (set(case) & FORBIDDEN_PRIVATE_FIELDS)
        assert case["schema_version"] == "1.0.0"
        assert case["id"].startswith("synthetic_")
        assert case["user_id"].startswith("synthetic-pilot-")
        assert case["channel"] == "api"
        assert case["privacy_class"] == "standard"
        assert case["label_status"] == "synthetic_curated"
        assert case["requires_human_review"] is False
        assert case["split"] == "calibration"
        assert case["dataset_kind"] == "synthetic_calibration_pilot"
        assert case["evidence_scope"] == "directional_calibration_only"
        assert {"synthetic", "calibration", "pilot", "top20"} <= set(case["tags"])
        assert not (set(case["tags"]) & FORBIDDEN_CLAIM_TAGS)


def test_synthetic_pilot_contains_no_private_handles_or_pii() -> None:
    cases = _load_cases()

    for case in cases:
        for location, value in _all_strings(case):
            normalized = _normalized_text(value)
            assert not any(marker in normalized for marker in FORBIDDEN_TEXT_MARKERS), (
                case["id"],
                location,
            )
            if (
                "expected_chunk_ids[" in location
                or "expected_cited_chunk_ids[" in location
            ):
                continue
            assert EMAIL_RE.search(value) is None, (case["id"], location)
            assert PHONE_RE.search(value) is None, (case["id"], location)
            assert UUID_RE.search(value) is None, (case["id"], location)
            assert LONG_HEX_RE.search(value) is None, (case["id"], location)
            assert LONG_DIGIT_RE.search(value) is None, (case["id"], location)


def test_synthetic_pilot_uses_only_published_yonote_evidence() -> None:
    cases = _load_cases()
    kb_payload = json.loads(KB_SEED_PATH.read_text(encoding="utf-8-sig"))
    kb_by_id = {
        str(record["chunk_id"]): record
        for record in kb_payload
        if isinstance(record, dict) and record.get("chunk_id")
    }

    for case in cases:
        expected_chunks = case.get("expected_chunk_ids", [])
        expected_citations = case.get("expected_cited_chunk_ids", [])
        if case["expected_behavior"] != "answer":
            assert expected_chunks == []
            assert expected_citations == []
            continue
        if not expected_chunks:
            assert "service_copy" in case["tags"]
            assert case.get("expected_answer_contains")
            continue
        assert expected_citations == expected_chunks
        assert case.get("allowed_cited_source_types") == ["yonote"]
        assert case.get("expected_answer_contains")
        for chunk_id in expected_chunks:
            record = kb_by_id[chunk_id]
            assert record.get("status") == "published"
            assert record.get("source_type") == "yonote"


def test_synthetic_pilot_is_executable_by_ask_eval_contract() -> None:
    cases = _load_cases()

    normalized = [_normalize_case(case) for case in cases]

    assert len(normalized) == 20
    for raw, executable in zip(cases, normalized, strict=True):
        assert executable["id"] == raw["id"]
        assert executable["query"] == raw["query"]
        assert executable["privacy_class"] == "standard"
        assert executable["expected_behavior"] == raw["expected_behavior"]
        assert (
            executable["expected_response_profile"]
            == raw["expected_response_profile"]
        )
        assert executable["expected_chunk_ids"] == raw.get(
            "expected_chunk_ids",
            [],
        )


def test_synthetic_pilot_covers_critical_off_aspect_boundaries() -> None:
    normalized = {
        case["id"]: _normalize_case(case)
        for case in _load_cases()
    }

    assert {
        "application",
        "dates",
        "selection_status",
    } <= {
        case["expected_response_profile"]
        for case in normalized.values()
    }
    assert {
        "application",
        "dates",
        "travel",
    } <= set(
        normalized["synthetic_mashuk_selection_timing"][
            "forbidden_response_profiles"
        ]
    )
    assert {
        "application",
        "selection_status",
        "travel",
    } <= set(
        normalized["synthetic_unspecified_event_dates"][
            "forbidden_response_profiles"
        ]
    )
    assert {
        "dates",
        "selection_status",
        "travel",
    } <= set(
        normalized["synthetic_unspecified_forum_application"][
            "forbidden_response_profiles"
        ]
    )
