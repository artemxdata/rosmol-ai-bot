from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

BASE_URL = "http://127.0.0.1:8000"
CHANNELS_DISABLED_ATTESTATION = "HDE_VK_DISABLED"
FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RECEIPT_ID = re.compile(r"^[0-9a-f]{32}$")
SEMANTIC_FINDING_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SNAPSHOT_SAFETY_REASONS = frozenset(
    {
        "yonote_snapshot_empty",
        "absolute_removal_limit_exceeded",
        "removal_ratio_limit_exceeded",
    }
)
MAX_REMOVAL_RATIO_WITHOUT_WAIVER = 0.25
MIN_REMOVALS_FOR_RATIO_GUARD = 10
MAX_REMOVALS_WITHOUT_WAIVER = 100
CHUNK_AUDIT_POLICY_VERSION = "yonote-chunk-audit-v1"
CHUNK_AUDIT_LEGACY_FINDINGS = (
    "empty_text",
    "too_short_under_20_chars",
    "oversized_over_max_chars",
    "duplicate_text_groups",
    "missing_source_url",
    "missing_source_document_id",
    "missing_source_updated_at",
)
CHUNK_AUDIT_BLOCKING_FINDINGS = (
    "empty_text",
    "oversized_over_max_chars",
    "missing_source_url",
    "missing_source_document_id",
    "missing_source_updated_at",
    "existing_documents_without_chunks",
    "new_substantive_documents_without_chunks",
    "unclassified_documents_without_chunks",
)
CHUNK_AUDIT_ADVISORY_FINDINGS = (
    "too_short_under_20_chars",
    "duplicate_text_groups",
    "new_documents_without_chunks",
)
WITHOUT_CHUNKS_SAMPLE_REASONS = frozenset(
    {
        "existing_document_lost_all_chunks",
        "new_substantive_document_without_chunks",
        "new_raw_empty_container",
        "new_below_minimum_container",
        "missing_document_identity",
        "missing_collection_identity",
    }
)
YONOTE_CHANGE_FIELDS = frozenset(
    {
        "category",
        "char_count",
        "conditions_summary",
        "dates",
        "dates_mentioned",
        "emails",
        "extraction_date",
        "forum",
        "forum_normalized",
        "has_conditional_logic",
        "intent_examples",
        "intent_examples_count",
        "intent_name",
        "is_generic",
        "links",
        "parent_chunk_id",
        "phones",
        "quality_review_reason",
        "registration_deadline",
        "source",
        "source_category",
        "source_collection_id",
        "source_collection_name",
        "source_columns",
        "source_document_id",
        "source_document_updated_at",
        "source_file",
        "source_heading_path",
        "source_paragraph_end",
        "source_paragraph_start",
        "source_row",
        "source_sheet",
        "source_table_index",
        "source_type",
        "source_url",
        "status",
        "text_clean",
        "text_raw",
        "topic",
        "updated_at",
        "valid_from",
        "valid_to",
        "version",
    }
)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
READY_CHECKS = (
    "config",
    "redis",
    "postgres",
    "knowledge_base",
    "ml_prewarm",
    "hde_transport",
)
QUEUE_ZERO_FIELDS = (
    "inbox_backlog",
    "inbox_processing",
    "inbox_dead_letter",
    "outbox_backlog",
    "outbox_sending",
    "outbox_dead_letter",
)
ADMIN_UI_MARKERS = (
    "/admin/kb/chunks",
    "/admin/kb/validate",
    "/admin/kb/runtime-status",
    "/admin/kb/yonote/preview",
    "/admin/kb/yonote/apply",
)


class AcceptanceError(RuntimeError):
    """A payload-free, allowlisted acceptance failure."""


@dataclass(frozen=True)
class HttpSnapshot:
    status: int
    headers: Mapping[str, str]
    body: bytes


Requester = Callable[..., HttpSnapshot]


def _request(
    path: str,
    *,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 120.0,
) -> HttpSnapshot:
    if not path.startswith("/") or "://" in path:
        raise AcceptanceError("invalid_internal_path")
    body = None
    request_headers = {"User-Agent": "rosmol-admin-kb-acceptance/1"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = Request(
        f"{BASE_URL}{path}",
        data=body,
        headers=request_headers,
        method=method,
    )
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise AcceptanceError("response_too_large")
            return HttpSnapshot(
                status=int(response.status),
                headers={key.casefold(): value for key, value in response.headers.items()},
                body=response_body,
            )
    except HTTPError as exc:
        try:
            response_body = exc.read(MAX_RESPONSE_BYTES + 1)
        finally:
            exc.close()
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise AcceptanceError("response_too_large") from None
        return HttpSnapshot(
            status=int(exc.code),
            headers={key.casefold(): value for key, value in exc.headers.items()},
            body=response_body,
        )
    except (OSError, URLError, ValueError) as exc:
        raise AcceptanceError("internal_http_unavailable") from exc


def _expect_status(response: HttpSnapshot, expected: int, reason: str) -> None:
    if response.status != expected:
        raise AcceptanceError(reason)


def _json_object(response: HttpSnapshot, expected: int, reason: str) -> dict[str, Any]:
    _expect_status(response, expected, reason)
    try:
        payload = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"{reason}_json_invalid") from exc
    if not isinstance(payload, dict):
        raise AcceptanceError(f"{reason}_json_invalid")
    return payload


def _require_non_negative_int(value: Any, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AcceptanceError(reason)
    return value


def _require_non_negative_number(value: Any, reason: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise AcceptanceError(reason)
    return float(value)


def _require_sha256(value: Any, reason: str) -> str:
    normalized = str(value or "").strip().casefold()
    if SHA256.fullmatch(normalized) is None:
        raise AcceptanceError(reason)
    return normalized


def _require_deleted_session_cookie(set_cookie: str) -> None:
    cookies = SimpleCookie()
    try:
        cookies.load(set_cookie)
        session_cookie = cookies["rosmol_admin_session"]
    except (KeyError, TypeError) as exc:
        raise AcceptanceError("admin_logout_cookie_invalid") from exc
    if session_cookie["max-age"].strip() == "0":
        return
    expires = session_cookie["expires"].strip()
    if not expires:
        raise AcceptanceError("admin_logout_cookie_invalid")
    try:
        expires_at = parsedate_to_datetime(expires)
    except (TypeError, ValueError) as exc:
        raise AcceptanceError("admin_logout_cookie_invalid") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at > datetime.now(UTC):
        raise AcceptanceError("admin_logout_cookie_invalid")


def _ready_snapshot(payload: Mapping[str, Any], expected_git_sha: str) -> dict[str, int]:
    checks = payload.get("checks")
    queue = payload.get("hde_transport_counts")
    if (
        payload.get("status") != "ready"
        or payload.get("release_git_sha") != expected_git_sha
        or not isinstance(checks, dict)
        or not all(checks.get(name) == "ok" for name in READY_CHECKS)
        or not isinstance(queue, dict)
    ):
        raise AcceptanceError("runtime_not_ready")
    safe_queue = {
        name: _require_non_negative_int(queue.get(name), "hde_queue_invalid")
        for name in QUEUE_ZERO_FIELDS
    }
    if any(safe_queue.values()):
        raise AcceptanceError("hde_queue_not_empty")
    return safe_queue


def _length_summary(value: Any, reason: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise AcceptanceError(reason)
    summary = {
        key: _require_non_negative_int(value.get(key), reason)
        for key in ("count", "minimum", "p50", "p95", "maximum")
    }
    ordered = (
        summary["minimum"],
        summary["p50"],
        summary["p95"],
        summary["maximum"],
    )
    if (
        (summary["count"] == 0 and any(ordered))
        or (summary["count"] > 0 and not (ordered[0] <= ordered[1] <= ordered[2] <= ordered[3]))
    ):
        raise AcceptanceError(reason)
    return summary


def _finding_counts(
    value: Any,
    *,
    expected: tuple[str, ...],
    reason: str,
) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise AcceptanceError(reason)
    return {
        name: _require_non_negative_int(value.get(name), reason)
        for name in expected
    }


def _safe_validation(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("ok") is not True:
        raise AcceptanceError("seed_validation_failed")
    status_counts = payload.get("status_counts")
    if not isinstance(status_counts, dict):
        raise AcceptanceError("seed_validation_invalid")
    safe_status_counts = {
        str(key): _require_non_negative_int(value, "seed_validation_invalid")
        for key, value in status_counts.items()
    }
    semantic_errors = _require_non_negative_int(
        payload.get("semantic_error_count"), "seed_validation_invalid"
    )
    if semantic_errors:
        raise AcceptanceError("seed_semantic_errors")
    return {
        "seed_sha256": _require_sha256(
            payload.get("seed_sha256"), "seed_validation_hash_invalid"
        ),
        "valid_records": _require_non_negative_int(
            payload.get("valid_records"), "seed_validation_invalid"
        ),
        "status_counts": safe_status_counts,
        "semantic_warning_count": _require_non_negative_int(
            payload.get("semantic_warning_count"), "seed_validation_invalid"
        ),
        "semantic_error_count": semantic_errors,
    }


def _safe_runtime_status(
    payload: Mapping[str, Any], expected_git_sha: str
) -> dict[str, Any]:
    seed = payload.get("seed")
    qdrant = payload.get("qdrant")
    cache = payload.get("response_cache")
    runtime = payload.get("runtime")
    if not all(isinstance(item, dict) for item in (seed, qdrant, cache, runtime)):
        raise AcceptanceError("runtime_status_invalid")
    assert isinstance(seed, dict)
    assert isinstance(qdrant, dict)
    assert isinstance(cache, dict)
    assert isinstance(runtime, dict)
    admin_read_only = runtime.get("admin_read_only")
    admin_mutations_enabled = runtime.get("admin_mutations_enabled")
    failure_reasons = payload.get("failure_reasons")
    if (
        payload.get("status") != "GO"
        or payload.get("ok") is not True
        or failure_reasons != []
        or seed.get("changed_during_scan") is not False
        or qdrant.get("exact_payload_match") is not True
        or qdrant.get("snapshot_payload_match") is not True
        or runtime.get("release_git_sha") != expected_git_sha
        or runtime.get("role") != "ml"
        or runtime.get("yonote_sync_enabled") is not True
        or not isinstance(admin_read_only, bool)
        or not isinstance(admin_mutations_enabled, bool)
        or admin_read_only == admin_mutations_enabled
    ):
        raise AcceptanceError("runtime_seed_qdrant_not_aligned")
    divergence = {
        name: _require_non_negative_int(qdrant.get(name), "runtime_status_invalid")
        for name in ("missing", "stale", "changed", "invalid_or_duplicate_points")
    }
    if any(divergence.values()):
        raise AcceptanceError("runtime_seed_qdrant_not_aligned")
    seed_sha256 = _require_sha256(
        seed.get("sha256"), "runtime_status_hash_invalid"
    )
    if (
        _require_sha256(
            seed.get("post_scan_sha256"), "runtime_status_hash_invalid"
        )
        != seed_sha256
    ):
        raise AcceptanceError("runtime_seed_changed_during_scan")
    return {
        "seed_sha256": seed_sha256,
        "seed_changed_during_scan": False,
        "seed_payload_fingerprint_sha256": _require_sha256(
            seed.get("payload_fingerprint_sha256"), "runtime_status_hash_invalid"
        ),
        "qdrant_payload_fingerprint_sha256": _require_sha256(
            qdrant.get("payload_fingerprint_sha256"), "runtime_status_hash_invalid"
        ),
        "published_records": _require_non_negative_int(
            seed.get("published"), "runtime_status_invalid"
        ),
        "qdrant_points": _require_non_negative_int(
            qdrant.get("points"), "runtime_status_invalid"
        ),
        "response_cache_points": _require_non_negative_int(
            cache.get("points"), "runtime_status_invalid"
        ),
        "admin_read_only": admin_read_only,
        "admin_mutations_enabled": admin_mutations_enabled,
        "divergence": divergence,
    }


def _safe_preview(payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    receipt = payload.get("receipt")
    hashes = payload.get("hashes")
    audit = payload.get("chunk_audit")
    projection = payload.get("index_projection")
    snapshot_safety = payload.get("snapshot_safety")
    semantic_integrity = payload.get("semantic_integrity")
    identity_reconciliation = payload.get("identity_reconciliation")
    change_classification = payload.get("change_classification")
    if not all(
        isinstance(item, dict)
        for item in (
            receipt,
            hashes,
            audit,
            projection,
            snapshot_safety,
            semantic_integrity,
            identity_reconciliation,
            change_classification,
        )
    ):
        raise AcceptanceError("yonote_preview_invalid")
    assert isinstance(receipt, dict)
    assert isinstance(hashes, dict)
    assert isinstance(audit, dict)
    assert isinstance(projection, dict)
    assert isinstance(snapshot_safety, dict)
    assert isinstance(semantic_integrity, dict)
    assert isinstance(identity_reconciliation, dict)
    assert isinstance(change_classification, dict)
    if (
        payload.get("ok") is not True
        or payload.get("applied") is not False
        or payload.get("snapshot_scope") != "full"
    ):
        raise AcceptanceError("yonote_preview_invalid")

    safe_counts = {
        name: _require_non_negative_int(
            payload.get(name), "yonote_preview_counts_invalid"
        )
        for name in (
            "documents",
            "current_records",
            "current_yonote_records",
            "fresh_yonote_records",
            "merged_records",
            "added",
            "changed",
            "removed",
            "unchanged",
        )
    }
    if (
        safe_counts["current_yonote_records"] > safe_counts["current_records"]
        or safe_counts["current_yonote_records"]
        != safe_counts["removed"]
        + safe_counts["changed"]
        + safe_counts["unchanged"]
        or safe_counts["fresh_yonote_records"]
        != safe_counts["added"]
        + safe_counts["changed"]
        + safe_counts["unchanged"]
        or safe_counts["merged_records"]
        != safe_counts["current_records"]
        - safe_counts["current_yonote_records"]
        + safe_counts["fresh_yonote_records"]
    ):
        raise AcceptanceError("yonote_preview_counts_inconsistent")

    semantic_status = semantic_integrity.get("status")
    raw_semantic_codes = semantic_integrity.get("codes")
    if (
        semantic_status not in {"GO", "STOP"}
        or not isinstance(raw_semantic_codes, dict)
        or len(raw_semantic_codes) > 64
    ):
        raise AcceptanceError("yonote_semantic_integrity_invalid")
    safe_semantic_codes: dict[str, int] = {}
    for raw_code, raw_count in raw_semantic_codes.items():
        if (
            not isinstance(raw_code, str)
            or SEMANTIC_FINDING_CODE.fullmatch(raw_code) is None
        ):
            raise AcceptanceError("yonote_semantic_integrity_invalid")
        count = _require_non_negative_int(
            raw_count,
            "yonote_semantic_integrity_invalid",
        )
        if count == 0:
            raise AcceptanceError("yonote_semantic_integrity_invalid")
        safe_semantic_codes[raw_code] = count
    semantic_errors_total = _require_non_negative_int(
        semantic_integrity.get("errors_total"),
        "yonote_semantic_integrity_invalid",
    )
    if (
        semantic_errors_total != sum(safe_semantic_codes.values())
        or (semantic_status == "GO" and semantic_errors_total != 0)
        or (semantic_status == "STOP" and semantic_errors_total == 0)
    ):
        raise AcceptanceError("yonote_semantic_integrity_invalid")

    snapshot_status = snapshot_safety.get("status")
    snapshot_reasons = snapshot_safety.get("reasons")
    current_yonote_records = safe_counts["current_yonote_records"]
    fresh_yonote_records = safe_counts["fresh_yonote_records"]
    removed_records = safe_counts["removed"]
    removal_ratio = (
        removed_records / current_yonote_records if current_yonote_records else 0.0
    )
    expected_snapshot_reasons: list[str] = []
    if current_yonote_records > 0 and fresh_yonote_records == 0:
        expected_snapshot_reasons.append("yonote_snapshot_empty")
    if removed_records > MAX_REMOVALS_WITHOUT_WAIVER:
        expected_snapshot_reasons.append("absolute_removal_limit_exceeded")
    if (
        removed_records >= MIN_REMOVALS_FOR_RATIO_GUARD
        and removal_ratio > MAX_REMOVAL_RATIO_WITHOUT_WAIVER
    ):
        expected_snapshot_reasons.append("removal_ratio_limit_exceeded")
    expected_snapshot_status = "STOP" if expected_snapshot_reasons else "GO"
    if (
        snapshot_status != expected_snapshot_status
        or not isinstance(snapshot_reasons, list)
        or not all(isinstance(reason, str) for reason in snapshot_reasons)
        or len(snapshot_reasons) != len(set(snapshot_reasons))
        or any(reason not in SNAPSHOT_SAFETY_REASONS for reason in snapshot_reasons)
        or snapshot_reasons != expected_snapshot_reasons
        or _require_non_negative_int(
            snapshot_safety.get("removed"), "yonote_snapshot_safety_invalid"
        )
        != removed_records
        or _require_non_negative_int(
            snapshot_safety.get("current_yonote_records"),
            "yonote_snapshot_safety_invalid",
        )
        != current_yonote_records
        or _require_non_negative_int(
            snapshot_safety.get("fresh_yonote_records"),
            "yonote_snapshot_safety_invalid",
        )
        != fresh_yonote_records
        or _require_non_negative_number(
            snapshot_safety.get("removal_ratio"),
            "yonote_snapshot_safety_invalid",
        )
        != round(removal_ratio, 6)
        or _require_non_negative_number(
            snapshot_safety.get("maximum_removal_ratio_without_waiver"),
            "yonote_snapshot_safety_invalid",
        )
        != MAX_REMOVAL_RATIO_WITHOUT_WAIVER
        or _require_non_negative_int(
            snapshot_safety.get("maximum_removals_without_waiver"),
            "yonote_snapshot_safety_invalid",
        )
        != MAX_REMOVALS_WITHOUT_WAIVER
    ):
        raise AcceptanceError("yonote_snapshot_safety_invalid")
    semantic_stopped = semantic_status == "STOP"
    snapshot_stopped = snapshot_status == "STOP"

    findings = audit.get("findings")
    documents = audit.get("documents")
    if not isinstance(findings, dict) or not isinstance(documents, dict):
        raise AcceptanceError("yonote_chunk_audit_invalid")
    safe_findings = _finding_counts(
        findings,
        expected=CHUNK_AUDIT_LEGACY_FINDINGS,
        reason="yonote_chunk_audit_invalid",
    )
    safe_documents = {
        "read": _require_non_negative_int(documents.get("read"), "yonote_chunk_audit_invalid"),
        "with_chunks": _require_non_negative_int(
            documents.get("with_chunks"), "yonote_chunk_audit_invalid"
        ),
        "without_chunks": _require_non_negative_int(
            documents.get("without_chunks"), "yonote_chunk_audit_invalid"
        ),
        "chunks_per_document": _length_summary(
            documents.get("chunks_per_document"), "yonote_chunk_audit_invalid"
        ),
    }
    safe_fresh_lengths = _length_summary(
        audit.get("fresh_lengths"), "yonote_chunk_audit_invalid"
    )
    safe_merged_lengths = _length_summary(
        audit.get("merged_lengths"), "yonote_chunk_audit_invalid"
    )
    if (
        safe_documents["read"] != safe_counts["documents"]
        or safe_documents["with_chunks"] + safe_documents["without_chunks"]
        != safe_documents["read"]
        or safe_documents["chunks_per_document"]["count"]
        != safe_documents["with_chunks"]
        or safe_documents["with_chunks"] > safe_counts["fresh_yonote_records"]
        or (
            safe_documents["with_chunks"] > 0
            and safe_documents["chunks_per_document"]["minimum"] < 1
        )
        or safe_fresh_lengths["count"] != safe_counts["fresh_yonote_records"]
        or safe_merged_lengths["count"] != safe_counts["merged_records"]
    ):
        raise AcceptanceError("yonote_chunk_audit_counts_inconsistent")
    warnings_total = _require_non_negative_int(
        audit.get("warnings_total"), "yonote_chunk_audit_invalid"
    )
    if warnings_total != sum(safe_findings.values()):
        raise AcceptanceError("yonote_chunk_audit_invalid")
    policy_version = audit.get("policy_version")
    blocking = audit.get("blocking")
    advisory = audit.get("advisory")
    audit_status = audit.get("status")
    if policy_version is None and blocking is None and advisory is None and audit_status is None:
        # Older runtimes did not distinguish advisory observations from blockers.
        # Keep that contract fail-closed: every legacy warning remains blocking.
        legacy_blocking_findings = {
            "legacy_warnings": warnings_total,
            "documents_without_chunks": safe_documents["without_chunks"],
        }
        blocking_total = sum(legacy_blocking_findings.values())
        advisory_total = 0
        safe_blocking = legacy_blocking_findings
        safe_advisory: dict[str, int] = {}
        safe_policy_version = "legacy-conservative"
        audit_status = "STOP" if blocking_total else "GO"
        safe_documents.update(
            {
                "existing_without_chunks": 0,
                "new_without_chunks": 0,
                "new_substantive_without_chunks": 0,
                "unclassified_without_chunks": safe_documents["without_chunks"],
            }
        )
    else:
        if (
            policy_version != CHUNK_AUDIT_POLICY_VERSION
            or audit_status not in {"GO", "STOP"}
            or not isinstance(blocking, dict)
            or not isinstance(advisory, dict)
        ):
            raise AcceptanceError("yonote_chunk_audit_policy_invalid")
        safe_blocking = _finding_counts(
            blocking.get("findings"),
            expected=CHUNK_AUDIT_BLOCKING_FINDINGS,
            reason="yonote_chunk_audit_policy_invalid",
        )
        safe_advisory = _finding_counts(
            advisory.get("findings"),
            expected=CHUNK_AUDIT_ADVISORY_FINDINGS,
            reason="yonote_chunk_audit_policy_invalid",
        )
        document_without_chunk_classes = {
            name: _require_non_negative_int(
                documents.get(name), "yonote_chunk_audit_policy_invalid"
            )
            for name in (
                "existing_without_chunks",
                "new_without_chunks",
                "new_substantive_without_chunks",
                "unclassified_without_chunks",
            )
        }
        if (
            sum(document_without_chunk_classes.values())
            != safe_documents["without_chunks"]
        ):
            raise AcceptanceError("yonote_chunk_audit_policy_invalid")
        without_chunks_sample = documents.get("without_chunks_sample")
        if (
            not isinstance(without_chunks_sample, list)
            or len(without_chunks_sample) > 20
            or len(without_chunks_sample) > safe_documents["without_chunks"]
        ):
            raise AcceptanceError("yonote_chunk_audit_policy_invalid")
        sample_reason_counts = {
            reason: 0 for reason in WITHOUT_CHUNKS_SAMPLE_REASONS
        }
        for item in without_chunks_sample:
            if not isinstance(item, dict) or set(item) != {
                "source_collection_id",
                "source_document_id",
                "reason",
                "cleaned_chars",
            }:
                raise AcceptanceError("yonote_chunk_audit_policy_invalid")
            collection_id = item.get("source_collection_id")
            document_id = item.get("source_document_id")
            reason = item.get("reason")
            if (
                not isinstance(collection_id, str)
                or len(collection_id) > 128
                or not isinstance(document_id, str)
                or len(document_id) > 128
                or reason not in WITHOUT_CHUNKS_SAMPLE_REASONS
            ):
                raise AcceptanceError("yonote_chunk_audit_policy_invalid")
            _require_non_negative_int(
                item.get("cleaned_chars"), "yonote_chunk_audit_policy_invalid"
            )
            sample_reason_counts[reason] += 1
        if (
            sample_reason_counts["existing_document_lost_all_chunks"]
            > document_without_chunk_classes["existing_without_chunks"]
            or sample_reason_counts["new_substantive_document_without_chunks"]
            > document_without_chunk_classes["new_substantive_without_chunks"]
            or sample_reason_counts["new_raw_empty_container"]
            + sample_reason_counts["new_below_minimum_container"]
            > document_without_chunk_classes["new_without_chunks"]
            or sample_reason_counts["missing_document_identity"]
            + sample_reason_counts["missing_collection_identity"]
            > document_without_chunk_classes["unclassified_without_chunks"]
        ):
            raise AcceptanceError("yonote_chunk_audit_policy_invalid")
        safe_documents.update(document_without_chunk_classes)
        expected_blocking = {
            name: safe_findings[name]
            for name in CHUNK_AUDIT_BLOCKING_FINDINGS
            if name in safe_findings
        }
        expected_blocking.update(
            {
                "existing_documents_without_chunks": safe_documents[
                    "existing_without_chunks"
                ],
                "new_substantive_documents_without_chunks": safe_documents[
                    "new_substantive_without_chunks"
                ],
                "unclassified_documents_without_chunks": safe_documents[
                    "unclassified_without_chunks"
                ],
            }
        )
        expected_advisory = {
            "too_short_under_20_chars": safe_findings[
                "too_short_under_20_chars"
            ],
            "duplicate_text_groups": safe_findings["duplicate_text_groups"],
            "new_documents_without_chunks": safe_documents["new_without_chunks"],
        }
        blocking_total = _require_non_negative_int(
            blocking.get("total"), "yonote_chunk_audit_policy_invalid"
        )
        advisory_total = _require_non_negative_int(
            advisory.get("total"), "yonote_chunk_audit_policy_invalid"
        )
        if (
            safe_blocking != expected_blocking
            or safe_advisory != expected_advisory
            or blocking_total != sum(safe_blocking.values())
            or advisory_total != sum(safe_advisory.values())
            or (audit_status == "GO") != (blocking_total == 0)
        ):
            raise AcceptanceError("yonote_chunk_audit_policy_invalid")
        safe_policy_version = CHUNK_AUDIT_POLICY_VERSION
    audit_stopped = audit_status == "STOP"
    preview_stopped = semantic_stopped or snapshot_stopped or audit_stopped
    if preview_stopped:
        expected_receipt_reason = (
            "semantic_integrity_failed"
            if semantic_stopped
            else (
                "destructive_snapshot_requires_owner_waiver"
                if snapshot_stopped
                else "chunk_audit_failed"
            )
        )
        if receipt != {
            "apply_ready": False,
            "reason": expected_receipt_reason,
        }:
            raise AcceptanceError("yonote_stopped_preview_receipt_invalid")
        receipt_created = False
    else:
        receipt_id = str(receipt.get("id") or "")
        receipt_sha256 = str(receipt.get("sha256") or "")
        if (
            receipt.get("apply_ready") is not True
            or RECEIPT_ID.fullmatch(receipt_id) is None
            or SHA256.fullmatch(receipt_sha256) is None
        ):
            raise AcceptanceError("yonote_preview_not_sealed")
        receipt_created = True

    safe_projection = {
        "current_published_points": _require_non_negative_int(
            projection.get("current_published_points"), "yonote_projection_invalid"
        ),
        "expected_published_points": _require_non_negative_int(
            projection.get("expected_published_points"), "yonote_projection_invalid"
        ),
        "stale_prune_required": projection.get("stale_prune_required") is True,
        "full_reindex_required": projection.get("full_reindex_required") is True,
    }
    for name in ("stale_prune_required", "full_reindex_required"):
        if not isinstance(projection.get(name), bool):
            raise AcceptanceError("yonote_projection_invalid")
    if (
        safe_projection["stale_prune_required"] != (safe_counts["removed"] > 0)
        or safe_projection["full_reindex_required"]
        != (safe_counts["added"] + safe_counts["changed"] + safe_counts["removed"] > 0)
    ):
        raise AcceptanceError("yonote_projection_invalid")

    safe_identity = {
        name: _require_non_negative_int(
            identity_reconciliation.get(name), "yonote_identity_reconciliation_invalid"
        )
        for name in (
            "raw_id_added",
            "raw_id_removed",
            "exact_content_rekeys",
            "same_set_identity_rotations",
            "ambiguous_exact_content_groups",
            "logical_added",
            "logical_removed",
        )
    }
    if (
        safe_identity["raw_id_added"] - safe_identity["exact_content_rekeys"]
        != safe_identity["logical_added"]
        or safe_identity["raw_id_removed"]
        - safe_identity["exact_content_rekeys"]
        != safe_identity["logical_removed"]
        or safe_identity["logical_added"] != safe_counts["added"]
        or safe_identity["logical_removed"] != safe_counts["removed"]
        or safe_identity["same_set_identity_rotations"]
        > min(
            safe_counts["current_yonote_records"],
            safe_counts["fresh_yonote_records"],
        )
        or safe_identity["ambiguous_exact_content_groups"]
        > min(
            safe_counts["current_yonote_records"],
            safe_counts["fresh_yonote_records"],
        )
    ):
        raise AcceptanceError("yonote_identity_reconciliation_invalid")

    metadata_only = _require_non_negative_int(
        change_classification.get("metadata_only"),
        "yonote_change_classification_invalid",
    )
    content_or_source = _require_non_negative_int(
        change_classification.get("content_or_source"),
        "yonote_change_classification_invalid",
    )
    raw_field_counts = change_classification.get("field_counts")
    if not isinstance(raw_field_counts, dict):
        raise AcceptanceError("yonote_change_classification_invalid")
    safe_field_counts: dict[str, int] = {}
    for raw_name, raw_count in raw_field_counts.items():
        if not isinstance(raw_name, str) or raw_name not in YONOTE_CHANGE_FIELDS:
            raise AcceptanceError("yonote_change_classification_invalid")
        count = _require_non_negative_int(
            raw_count, "yonote_change_classification_invalid"
        )
        if count == 0 or count > safe_counts["changed"]:
            raise AcceptanceError("yonote_change_classification_invalid")
        safe_field_counts[raw_name] = count
    if (
        metadata_only + content_or_source != safe_counts["changed"]
        or (safe_counts["changed"] == 0) != (not safe_field_counts)
        or sum(safe_field_counts.values()) < safe_counts["changed"]
    ):
        raise AcceptanceError("yonote_change_classification_invalid")

    safe = {
        "snapshot_scope": "full",
        "snapshot_safety": {
            "status": snapshot_status,
            "reasons": list(snapshot_reasons),
        },
        "receipt_created": receipt_created,
        "semantic_integrity": {
            "status": semantic_status,
            "codes": dict(sorted(safe_semantic_codes.items())),
            "errors_total": semantic_errors_total,
        },
        "hashes": {
            "current_seed_sha256": _require_sha256(
                hashes.get("current_seed_sha256"), "yonote_preview_hash_invalid"
            ),
            "yonote_snapshot_sha256": _require_sha256(
                hashes.get("yonote_snapshot_sha256"), "yonote_preview_hash_invalid"
            ),
            "merged_seed_sha256": _require_sha256(
                hashes.get("merged_seed_sha256"), "yonote_preview_hash_invalid"
            ),
        },
        "counts": safe_counts,
        "identity_reconciliation": safe_identity,
        "change_classification": {
            "metadata_only": metadata_only,
            "content_or_source": content_or_source,
            "field_counts": dict(sorted(safe_field_counts.items())),
        },
        "chunk_audit": {
            "policy_version": safe_policy_version,
            "status": audit_status,
            "blocking": {
                "total": blocking_total,
                "findings": safe_blocking,
            },
            "advisory": {
                "total": advisory_total,
                "findings": safe_advisory,
            },
            "fresh_lengths": safe_fresh_lengths,
            "merged_lengths": safe_merged_lengths,
            "documents": safe_documents,
            "findings": safe_findings,
            "warnings_total": warnings_total,
        },
        "index_projection": safe_projection,
        "quality_status": "STOP" if preview_stopped else "GO",
    }
    return safe, not preview_stopped


def _same_runtime_status(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    immutable_fields = (
        "seed_sha256",
        "seed_changed_during_scan",
        "seed_payload_fingerprint_sha256",
        "qdrant_payload_fingerprint_sha256",
        "published_records",
        "qdrant_points",
        "response_cache_points",
        "admin_read_only",
        "admin_mutations_enabled",
        "divergence",
    )
    return all(before.get(field) == after.get(field) for field in immutable_fields)


def run_acceptance(
    *,
    expected_git_sha: str,
    channels_disabled_attestation: str,
    environ: Mapping[str, str] | None = None,
    requester: Requester = _request,
) -> dict[str, Any]:
    values = dict(os.environ if environ is None else environ)
    if (
        FULL_GIT_SHA.fullmatch(expected_git_sha) is None
        or expected_git_sha == "0" * 40
    ):
        raise AcceptanceError("candidate_sha_invalid")
    if channels_disabled_attestation != CHANNELS_DISABLED_ATTESTATION:
        raise AcceptanceError("channels_disabled_attestation_required")
    if (
        values.get("RELEASE_GIT_SHA") != expected_git_sha
        or values.get("APP_ENV", "").casefold() != "production"
        or values.get("RUNTIME_ROLE", "").casefold() != "ml"
    ):
        raise AcceptanceError("runtime_identity_mismatch")
    admin_read_only_value = values.get("ADMIN_READ_ONLY", "").casefold()
    admin_mutations_value = values.get("ADMIN_MUTATIONS_ENABLED", "").casefold()
    if (admin_read_only_value, admin_mutations_value) not in {
        ("true", "false"),
        ("false", "true"),
    }:
        raise AcceptanceError("admin_capability_invalid")
    if (
        values.get("YONOTE_SYNC_ENABLED", "").casefold() != "true"
        or len(values.get("YONOTE_API_TOKEN", "")) < 8
    ):
        raise AcceptanceError("yonote_read_only_preview_unavailable")
    admin_token = values.get("ADMIN_AUTH_TOKEN", "")
    if len(admin_token) < 32:
        raise AcceptanceError("admin_auth_unavailable")
    if any(
        values.get(name, "").strip()
        for name in ("VK_API_TOKEN", "VK_GROUP_TOKEN", "VK_CONFIRMATION_CODE", "VK_SECRET")
    ):
        raise AcceptanceError("vk_credentials_present")

    ready_before = _ready_snapshot(
        _json_object(requester("/ready"), 200, "runtime_ready_failed"),
        expected_git_sha,
    )

    ui = requester("/admin/kb")
    _expect_status(ui, 200, "admin_ui_unavailable")
    try:
        ui_text = ui.body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AcceptanceError("admin_ui_invalid") from exc
    if not all(marker in ui_text for marker in ADMIN_UI_MARKERS):
        raise AcceptanceError("admin_ui_incomplete")

    _expect_status(
        requester("/admin/kb/chunks?limit=1"),
        401,
        "admin_missing_auth_not_rejected",
    )
    wrong_token = secrets.token_urlsafe(48)
    while wrong_token == admin_token:
        wrong_token = secrets.token_urlsafe(48)
    _expect_status(
        requester(
            "/admin/kb/chunks?limit=1",
            headers={"X-Admin-Token": wrong_token},
        ),
        401,
        "admin_wrong_auth_not_rejected",
    )
    _expect_status(
        requester(
            "/admin/kb/login",
            method="POST",
            payload={"token": wrong_token},
            headers={"X-Forwarded-Proto": "https"},
        ),
        401,
        "admin_wrong_login_not_rejected",
    )
    login = requester(
        "/admin/kb/login",
        method="POST",
        payload={"token": admin_token},
        headers={"X-Forwarded-Proto": "https"},
    )
    _expect_status(login, 200, "admin_login_failed")
    set_cookie = login.headers.get("set-cookie", "")
    lowered_cookie = set_cookie.casefold()
    if not all(
        marker in lowered_cookie
        for marker in (
            "rosmol_admin_session=",
            "secure",
            "httponly",
            "samesite=lax",
            "path=/admin/kb",
        )
    ):
        raise AcceptanceError("admin_cookie_flags_invalid")
    cookies = SimpleCookie()
    try:
        cookies.load(set_cookie)
        session = cookies["rosmol_admin_session"].value
    except (KeyError, TypeError) as exc:
        raise AcceptanceError("admin_cookie_invalid") from exc
    if not session or any(character in session for character in "\r\n;"):
        raise AcceptanceError("admin_cookie_invalid")
    session_header = {"Cookie": f"rosmol_admin_session={session}"}
    _expect_status(
        requester("/admin/kb/chunks?limit=1", headers=session_header),
        200,
        "admin_cookie_session_failed",
    )
    logout = requester(
        "/admin/kb/logout",
        method="POST",
        payload={},
        headers=session_header,
    )
    _expect_status(logout, 200, "admin_logout_failed")
    _require_deleted_session_cookie(logout.headers.get("set-cookie", ""))
    _expect_status(
        requester("/admin/kb/chunks?limit=1", headers=session_header),
        401,
        "admin_logout_session_not_revoked",
    )

    auth_headers = {"X-Admin-Token": admin_token}
    chunks = _json_object(
        requester("/admin/kb/chunks?limit=1", headers=auth_headers),
        200,
        "admin_chunks_failed",
    )
    chunk_total = _require_non_negative_int(chunks.get("total"), "admin_chunks_invalid")
    items = chunks.get("items")
    if not isinstance(items, list) or len(items) > 1:
        raise AcceptanceError("admin_chunks_invalid")

    validation = _safe_validation(
        _json_object(
            requester(
                "/admin/kb/validate",
                method="POST",
                payload={},
                headers=auth_headers,
                timeout=180,
            ),
            200,
            "admin_validate_failed",
        )
    )
    runtime_before = _safe_runtime_status(
        _json_object(
            requester(
                "/admin/kb/runtime-status",
                headers=auth_headers,
                timeout=300,
            ),
            200,
            "admin_runtime_status_failed",
        ),
        expected_git_sha,
    )
    if (
        runtime_before["admin_read_only"] != (admin_read_only_value == "true")
        or runtime_before["admin_mutations_enabled"]
        != (admin_mutations_value == "true")
    ):
        raise AcceptanceError("runtime_admin_capability_mismatch")

    _expect_status(
        requester(
            "/webhook/vk",
            method="POST",
            payload={"acceptance_probe": True},
            headers={"X-Webhook-Secret": wrong_token},
        ),
        404,
        "direct_vk_webhook_enabled",
    )

    preview, preview_clean = _safe_preview(
        _json_object(
            requester(
                "/admin/kb/yonote/preview",
                method="POST",
                payload={},
                headers=auth_headers,
                timeout=600,
            ),
            200,
            "yonote_preview_failed",
        )
    )
    if (
        preview["index_projection"]["current_published_points"]
        != runtime_before["published_records"]
    ):
        raise AcceptanceError("yonote_projection_runtime_mismatch")
    runtime_after = _safe_runtime_status(
        _json_object(
            requester(
                "/admin/kb/runtime-status",
                headers=auth_headers,
                timeout=300,
            ),
            200,
            "admin_runtime_status_after_failed",
        ),
        expected_git_sha,
    )
    ready_after = _ready_snapshot(
        _json_object(requester("/ready"), 200, "runtime_ready_after_failed"),
        expected_git_sha,
    )

    seed_unchanged = (
        validation["seed_sha256"]
        == runtime_before["seed_sha256"]
        == preview["hashes"]["current_seed_sha256"]
        == runtime_after["seed_sha256"]
    )
    runtime_unchanged = _same_runtime_status(runtime_before, runtime_after)
    queue_unchanged = ready_before == ready_after
    passed = preview_clean and seed_unchanged and runtime_unchanged and queue_unchanged

    return {
        "schema_version": "admin-kb-server-local-acceptance-v2",
        "status": "GO" if passed else "STOP",
        "candidate_sha": expected_git_sha,
        "channels": {
            "status": CHANNELS_DISABLED_ATTESTATION,
            "owner_attested_external_rules_disabled": True,
            "direct_vk_webhook_disabled": True,
            "vk_credentials_absent": True,
            "hde_queue_empty_and_unchanged": queue_unchanged,
        },
        "admin": {
            "ui_complete": True,
            "missing_and_wrong_auth_rejected": True,
            "login_cookie_logout": True,
            "read_only": runtime_after["admin_read_only"],
            "mutations_enabled": runtime_after["admin_mutations_enabled"],
            "listed_records": chunk_total,
        },
        "validation": validation,
        "runtime_status": runtime_after,
        "yonote_preview": preview,
        "non_mutation": {
            "seed_unchanged": seed_unchanged,
            "qdrant_and_cache_unchanged": runtime_unchanged,
            "hde_queue_unchanged": queue_unchanged,
            "only_private_preview_receipt_created": preview["receipt_created"],
        },
        "limitations": [
            "HDE/VK provider-side dispatcher rules are not observable from the server; "
            "their disabled state is an explicit owner attestation.",
            "This gate does not call Apply, reindex, PATCH, /ask or an HDE webhook.",
            "Qdrant payloads are compared exactly; vectors are not recomputed.",
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a read-only server-local admin/KB acceptance. The admin and Yonote "
            "tokens are read only from the runtime container environment and never printed."
        )
    )
    parser.add_argument("expected_git_sha")
    parser.add_argument("channels_disabled_attestation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = run_acceptance(
            expected_git_sha=args.expected_git_sha,
            channels_disabled_attestation=args.channels_disabled_attestation,
        )
    except AcceptanceError as exc:
        print(f"admin_kb_acceptance=FAIL reason={exc}")
        return 1
    except Exception as exc:  # pragma: no cover - fail-closed CLI boundary
        print(f"admin_kb_acceptance=FAIL reason=unexpected_{type(exc).__name__}")
        return 1

    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if report["status"] == "GO":
        return 0
    preview = report.get("yonote_preview")
    non_mutation = report.get("non_mutation")
    if (
        isinstance(preview, dict)
        and preview.get("quality_status") == "STOP"
        and isinstance(non_mutation, dict)
        and non_mutation.get("seed_unchanged") is True
        and non_mutation.get("qdrant_and_cache_unchanged") is True
        and non_mutation.get("hde_queue_unchanged") is True
    ):
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
