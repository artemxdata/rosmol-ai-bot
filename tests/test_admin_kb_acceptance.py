from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts import admin_kb_acceptance as gate

ROOT = Path(__file__).resolve().parents[1]
SHELL_SCRIPT = ROOT / "scripts" / "run_admin_kb_acceptance_server_local.sh"
PYTHON_HELPER = ROOT / "scripts" / "admin_kb_acceptance.py"
EXPECTED_SHA = "a" * 40
ADMIN_TOKEN = "admin-secret-" + "x" * 40
RECEIPT_ID = "f" * 32
RECEIPT_SHA = "1" * 64


def _environment() -> dict[str, str]:
    return {
        "RELEASE_GIT_SHA": EXPECTED_SHA,
        "APP_ENV": "production",
        "RUNTIME_ROLE": "ml",
        "ADMIN_READ_ONLY": "true",
        "ADMIN_MUTATIONS_ENABLED": "false",
        "YONOTE_SYNC_ENABLED": "true",
        "YONOTE_API_TOKEN": "yonote-secret-value",
        "ADMIN_AUTH_TOKEN": ADMIN_TOKEN,
        "VK_API_TOKEN": "",
        "VK_GROUP_TOKEN": "",
        "VK_CONFIRMATION_CODE": "",
        "VK_SECRET": "",
    }


def _ready_payload() -> dict[str, Any]:
    return {
        "status": "ready",
        "release_git_sha": EXPECTED_SHA,
        "checks": {name: "ok" for name in gate.READY_CHECKS},
        "hde_transport_counts": {name: 0 for name in gate.QUEUE_ZERO_FIELDS},
    }


def _runtime_status(
    *,
    fingerprint: str = "b" * 64,
    admin_read_only: bool = True,
) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "GO",
        "failure_reasons": [],
        "seed": {
            "sha256": "c" * 64,
            "post_scan_sha256": "c" * 64,
            "changed_during_scan": False,
            "published": 2152,
            "payload_fingerprint_sha256": fingerprint,
        },
        "qdrant": {
            "points": 2152,
            "payload_fingerprint_sha256": fingerprint,
            "exact_payload_match": True,
            "snapshot_payload_match": True,
            "missing": 0,
            "stale": 0,
            "changed": 0,
            "invalid_or_duplicate_points": 0,
        },
        "response_cache": {"points": 0},
        "runtime": {
            "release_git_sha": EXPECTED_SHA,
            "role": "ml",
            "admin_read_only": admin_read_only,
            "admin_mutations_enabled": not admin_read_only,
            "yonote_sync_enabled": True,
        },
    }


def _preview(
    *,
    too_short: int = 0,
    oversized: int = 0,
    duplicate_groups: int = 0,
    documents_without_chunks: int = 0,
    existing_documents_without_chunks: int = 0,
    new_substantive_documents_without_chunks: int = 0,
    unclassified_documents_without_chunks: int = 0,
    semantic_codes: dict[str, int] | None = None,
    snapshot_reasons: list[str] | None = None,
    legacy_chunk_audit: bool = False,
) -> dict[str, Any]:
    semantic_codes = dict(semantic_codes or {})
    snapshot_reasons = list(snapshot_reasons or [])
    current_records = 2186
    current_yonote_records = 2152
    fresh_yonote_records = 2160
    added = 10
    changed = 7
    removed = 2
    unchanged = 2143
    merged_records = 2194
    documents = 117
    metadata_only = 3
    content_or_source = 4
    field_counts = {"text_clean": 4, "updated_at": 3}
    if snapshot_reasons == ["yonote_snapshot_empty"]:
        current_records = 39
        current_yonote_records = 5
        fresh_yonote_records = 0
        added = changed = unchanged = 0
        removed = 5
        merged_records = 34
        documents = 0
        metadata_only = content_or_source = 0
        field_counts = {}
    elif snapshot_reasons == ["removal_ratio_limit_exceeded"]:
        current_records = 74
        current_yonote_records = 40
        fresh_yonote_records = 29
        added = changed = 0
        removed = 11
        unchanged = 29
        merged_records = 63
        documents = 29
        metadata_only = content_or_source = 0
        field_counts = {}
    elif snapshot_reasons == ["absolute_removal_limit_exceeded"]:
        current_records = 1034
        current_yonote_records = 1000
        fresh_yonote_records = 899
        added = changed = 0
        removed = 101
        unchanged = 899
        merged_records = 933
        documents = 117
        metadata_only = content_or_source = 0
        field_counts = {}
    semantic_stopped = bool(semantic_codes)
    snapshot_stopped = bool(snapshot_reasons)
    blocking_findings = {
        "empty_text": 0,
        "oversized_over_max_chars": oversized,
        "missing_source_url": 0,
        "missing_source_document_id": 0,
        "missing_source_updated_at": 0,
        "existing_documents_without_chunks": existing_documents_without_chunks,
        "new_substantive_documents_without_chunks": (
            new_substantive_documents_without_chunks
        ),
        "unclassified_documents_without_chunks": (
            unclassified_documents_without_chunks
        ),
    }
    advisory_findings = {
        "too_short_under_20_chars": too_short,
        "duplicate_text_groups": duplicate_groups,
        "new_documents_without_chunks": documents_without_chunks,
    }
    blocking_total = sum(blocking_findings.values())
    advisory_total = sum(advisory_findings.values())
    audit_stopped = (
        blocking_total > 0
        if not legacy_chunk_audit
        else blocking_total + advisory_total > 0
    )
    preview_stopped = semantic_stopped or snapshot_stopped or audit_stopped
    findings = {
        "empty_text": 0,
        "too_short_under_20_chars": too_short,
        "oversized_over_max_chars": oversized,
        "duplicate_text_groups": duplicate_groups,
        "missing_source_url": 0,
        "missing_source_document_id": 0,
        "missing_source_updated_at": 0,
    }
    fresh_length_summary = {
        "count": fresh_yonote_records,
        "minimum": 20 if fresh_yonote_records else 0,
        "p50": 240 if fresh_yonote_records else 0,
        "p95": 900 if fresh_yonote_records else 0,
        "maximum": 1800 if fresh_yonote_records else 0,
    }
    merged_length_summary = {
        **fresh_length_summary,
        "count": merged_records,
        "minimum": 20 if merged_records else 0,
        "p50": 240 if merged_records else 0,
        "p95": 900 if merged_records else 0,
        "maximum": 1800 if merged_records else 0,
    }
    documents_without_chunks_total = (
        documents_without_chunks
        + existing_documents_without_chunks
        + new_substantive_documents_without_chunks
        + unclassified_documents_without_chunks
    )
    without_chunks_sample: list[dict[str, Any]] = []
    if documents_without_chunks:
        without_chunks_sample.append(
            {
                "source_collection_id": "private-collection-id",
                "source_document_id": "private-empty-document-id",
                "reason": "new_below_minimum_container",
                "cleaned_chars": 12,
            }
        )
    elif existing_documents_without_chunks:
        without_chunks_sample.append(
            {
                "source_collection_id": "private-collection-id",
                "source_document_id": "private-existing-document-id",
                "reason": "existing_document_lost_all_chunks",
                "cleaned_chars": 120,
            }
        )
    elif new_substantive_documents_without_chunks:
        without_chunks_sample.append(
            {
                "source_collection_id": "private-collection-id",
                "source_document_id": "private-substantive-document-id",
                "reason": "new_substantive_document_without_chunks",
                "cleaned_chars": 120,
            }
        )
    elif unclassified_documents_without_chunks:
        without_chunks_sample.append(
            {
                "source_collection_id": "private-collection-id",
                "source_document_id": "",
                "reason": "missing_document_identity",
                "cleaned_chars": 0,
            }
        )
    chunk_audit = {
        "fresh_lengths": fresh_length_summary,
        "merged_lengths": merged_length_summary,
        "documents": {
            "read": documents,
            "with_chunks": documents - documents_without_chunks_total,
            "without_chunks": documents_without_chunks_total,
            "existing_without_chunks": existing_documents_without_chunks,
            "new_without_chunks": documents_without_chunks,
            "new_substantive_without_chunks": (
                new_substantive_documents_without_chunks
            ),
            "unclassified_without_chunks": (
                unclassified_documents_without_chunks
            ),
            "chunks_per_document": {
                "count": documents - documents_without_chunks_total,
                "minimum": 1 if documents - documents_without_chunks_total else 0,
                "p50": 12 if documents - documents_without_chunks_total else 0,
                "p95": 40 if documents - documents_without_chunks_total else 0,
                "maximum": 81 if documents - documents_without_chunks_total else 0,
            },
            "largest_documents": [
                {"source_document_id": "private-document-id", "chunks": 81}
            ],
            "without_chunks_sample": without_chunks_sample,
        },
        "findings": findings,
        "duplicate_text_sample": [],
        "warnings_total": sum(findings.values()),
    }
    if not legacy_chunk_audit:
        chunk_audit.update(
            {
                "policy_version": gate.CHUNK_AUDIT_POLICY_VERSION,
                "status": "STOP" if audit_stopped else "GO",
                "blocking": {
                    "total": blocking_total,
                    "findings": blocking_findings,
                },
                "advisory": {
                    "total": advisory_total,
                    "findings": advisory_findings,
                },
            }
        )
    return {
        "ok": True,
        "applied": False,
        "snapshot_scope": "full",
        "snapshot_safety": {
            "status": "STOP" if snapshot_stopped else "GO",
            "reasons": snapshot_reasons,
            "removed": removed,
            "current_yonote_records": current_yonote_records,
            "fresh_yonote_records": fresh_yonote_records,
            "removal_ratio": round(
                removed / current_yonote_records if current_yonote_records else 0.0,
                6,
            ),
            "maximum_removal_ratio_without_waiver": 0.25,
            "maximum_removals_without_waiver": 100,
        },
        "semantic_integrity": {
            "status": "STOP" if semantic_stopped else "GO",
            "codes": semantic_codes,
            "errors_total": sum(semantic_codes.values()),
            "affected_chunk_ids": {
                code: ["semantic-private-id"] for code in semantic_codes
            },
            "records": [
                {
                    "chunk_id": "semantic-private-id",
                    "text": "semantic source text must never be printed",
                }
            ],
        },
        "receipt": (
            {
                "apply_ready": False,
                "reason": (
                    "semantic_integrity_failed"
                    if semantic_stopped
                    else (
                        "destructive_snapshot_requires_owner_waiver"
                        if snapshot_stopped
                        else "chunk_audit_failed"
                    )
                ),
            }
            if preview_stopped
            else {
                "apply_ready": True,
                "id": RECEIPT_ID,
                "sha256": RECEIPT_SHA,
            }
        ),
        "hashes": {
            "current_seed_sha256": "c" * 64,
            "yonote_snapshot_sha256": "d" * 64,
            "merged_seed_sha256": "e" * 64,
        },
        "documents": documents,
        "current_records": current_records,
        "current_yonote_records": current_yonote_records,
        "fresh_yonote_records": fresh_yonote_records,
        "merged_records": merged_records,
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged": unchanged,
        "identity_reconciliation": {
            "raw_id_added": added,
            "raw_id_removed": removed,
            "exact_content_rekeys": 0,
            "same_set_identity_rotations": 0,
            "ambiguous_exact_content_groups": 0,
            "logical_added": added,
            "logical_removed": removed,
        },
        "change_classification": {
            "metadata_only": metadata_only,
            "content_or_source": content_or_source,
            "field_counts": field_counts,
        },
        "added_items": [
            {
                "chunk_id": "private-source-id",
                "text_preview": "sensitive source answer must never be printed",
            }
        ],
        "chunk_audit": chunk_audit,
        "index_projection": {
            "current_published_points": current_yonote_records,
            "expected_published_points": merged_records,
            "stale_prune_required": removed > 0,
            "full_reindex_required": added + changed + removed > 0,
        },
    }


class FakeRequester:
    def __init__(
        self,
        *,
        too_short: int = 0,
        oversized: int = 0,
        duplicate_groups: int = 0,
        documents_without_chunks: int = 0,
        semantic_codes: dict[str, int] | None = None,
        snapshot_reasons: list[str] | None = None,
        change_runtime_after_preview: bool = False,
        preview_current_published_points: int | None = None,
        admin_read_only: bool = True,
        revoke_session_on_logout: bool = True,
    ) -> None:
        self.too_short = too_short
        self.oversized = oversized
        self.duplicate_groups = duplicate_groups
        self.documents_without_chunks = documents_without_chunks
        self.semantic_codes = dict(semantic_codes or {})
        self.snapshot_reasons = list(snapshot_reasons or [])
        self.change_runtime_after_preview = change_runtime_after_preview
        self.preview_current_published_points = preview_current_published_points
        self.admin_read_only = admin_read_only
        self.revoke_session_on_logout = revoke_session_on_logout
        self.session_revoked = False
        self.runtime_status_calls = 0
        self.paths: list[tuple[str, str]] = []

    def __call__(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> gate.HttpSnapshot:
        self.paths.append((method, path))
        request_headers = headers or {}
        if path == "/ready":
            return self._json(200, _ready_payload())
        if path == "/admin/kb":
            return gate.HttpSnapshot(
                200,
                {},
                "\n".join(gate.ADMIN_UI_MARKERS).encode(),
            )
        if path == "/admin/kb/login":
            assert method == "POST"
            assert payload is not None
            if payload["token"] != ADMIN_TOKEN:
                return self._json(401, {"detail": "Unauthorized"})
            assert request_headers["X-Forwarded-Proto"] == "https"
            self.session_revoked = False
            return gate.HttpSnapshot(
                200,
                {
                    "set-cookie": (
                        "rosmol_admin_session=opaque-session; Path=/admin/kb; "
                        "Secure; HttpOnly; SameSite=Lax"
                    )
                },
                b'{"ok":true}',
            )
        if path == "/admin/kb/logout":
            assert method == "POST"
            assert request_headers["Cookie"] == "rosmol_admin_session=opaque-session"
            if self.revoke_session_on_logout:
                self.session_revoked = True
            return gate.HttpSnapshot(
                200,
                {"set-cookie": "rosmol_admin_session=\"\"; Path=/admin/kb; Max-Age=0"},
                b'{"ok":true}',
            )
        if path == "/admin/kb/chunks?limit=1":
            authenticated = (
                request_headers.get("X-Admin-Token") == ADMIN_TOKEN
                or (
                    request_headers.get("Cookie") == "rosmol_admin_session=opaque-session"
                    and not self.session_revoked
                )
            )
            if not authenticated:
                return self._json(401, {"detail": "Unauthorized"})
            return self._json(
                200,
                {
                    "total": 2186,
                    "items": [{"text_preview": "must not be printed"}],
                },
            )
        if path == "/admin/kb/validate":
            assert method == "POST"
            self._assert_admin_header(request_headers)
            return self._json(
                200,
                {
                    "ok": True,
                    "seed_sha256": "c" * 64,
                    "valid_records": 2186,
                    "status_counts": {"published": 2152, "draft": 34},
                    "semantic_warning_count": 2,
                    "semantic_error_count": 0,
                    "semantic_findings": [
                        {"sample": "must not be printed from validation"}
                    ],
                },
            )
        if path == "/admin/kb/runtime-status":
            self._assert_admin_header(request_headers)
            self.runtime_status_calls += 1
            fingerprint = (
                "9" * 64
                if self.change_runtime_after_preview and self.runtime_status_calls == 2
                else "b" * 64
            )
            return self._json(
                200,
                _runtime_status(
                    fingerprint=fingerprint,
                    admin_read_only=self.admin_read_only,
                ),
            )
        if path == "/admin/kb/yonote/preview":
            assert method == "POST"
            assert payload == {}
            self._assert_admin_header(request_headers)
            preview = _preview(
                too_short=self.too_short,
                oversized=self.oversized,
                duplicate_groups=self.duplicate_groups,
                documents_without_chunks=self.documents_without_chunks,
                semantic_codes=self.semantic_codes,
                snapshot_reasons=self.snapshot_reasons,
            )
            if self.preview_current_published_points is not None:
                preview["index_projection"]["current_published_points"] = (
                    self.preview_current_published_points
                )
            return self._json(200, preview)
        if path == "/webhook/vk":
            assert method == "POST"
            return self._json(404, {"detail": "Not Found"})
        raise AssertionError(f"unexpected request: {method} {path}")

    @staticmethod
    def _assert_admin_header(headers: dict[str, str]) -> None:
        assert headers["X-Admin-Token"] == ADMIN_TOKEN

    @staticmethod
    def _json(status: int, payload: dict[str, Any]) -> gate.HttpSnapshot:
        return gate.HttpSnapshot(status, {}, json.dumps(payload).encode())


def test_acceptance_returns_only_safe_aggregates_and_hashes() -> None:
    requester = FakeRequester()

    report = gate.run_acceptance(
        expected_git_sha=EXPECTED_SHA,
        channels_disabled_attestation=gate.CHANNELS_DISABLED_ATTESTATION,
        environ=_environment(),
        requester=requester,
    )

    assert report["schema_version"] == "admin-kb-server-local-acceptance-v2"
    assert report["status"] == "GO"
    assert report["channels"] == {
        "status": "HDE_VK_DISABLED",
        "owner_attested_external_rules_disabled": True,
        "direct_vk_webhook_disabled": True,
        "vk_credentials_absent": True,
        "hde_queue_empty_and_unchanged": True,
    }
    assert report["yonote_preview"]["counts"]["added"] == 10
    assert report["yonote_preview"]["identity_reconciliation"] == {
        "raw_id_added": 10,
        "raw_id_removed": 2,
        "exact_content_rekeys": 0,
        "same_set_identity_rotations": 0,
        "ambiguous_exact_content_groups": 0,
        "logical_added": 10,
        "logical_removed": 2,
    }
    assert report["yonote_preview"]["change_classification"] == {
        "metadata_only": 3,
        "content_or_source": 4,
        "field_counts": {"text_clean": 4, "updated_at": 3},
    }
    assert report["yonote_preview"]["receipt_created"] is True
    serialized = json.dumps(report, ensure_ascii=False)
    for forbidden in (
        ADMIN_TOKEN,
        _environment()["YONOTE_API_TOKEN"],
        RECEIPT_ID,
        RECEIPT_SHA,
        "sensitive source answer",
        "private-source-id",
        "private-document-id",
        "private-collection-id",
        "private-empty-document-id",
        "new_below_minimum_container",
        "must not be printed",
        "semantic-private-id",
        "semantic source text",
    ):
        assert forbidden not in serialized
    assert not any("/admin/kb/yonote/apply" in path for _method, path in requester.paths)
    assert not any("/reindex" in path for _method, path in requester.paths)
    assert not any(path == "/ask" for _method, path in requester.paths)
    assert not any(path == "/webhook/hde" for _method, path in requester.paths)


def test_acceptance_keeps_advisory_chunk_findings_visible_without_stopping() -> None:
    report = gate.run_acceptance(
        expected_git_sha=EXPECTED_SHA,
        channels_disabled_attestation=gate.CHANNELS_DISABLED_ATTESTATION,
        environ=_environment(),
        requester=FakeRequester(
            too_short=1,
            duplicate_groups=2,
            documents_without_chunks=3,
        ),
    )

    assert report["status"] == "GO"
    assert report["yonote_preview"]["quality_status"] == "GO"
    audit = report["yonote_preview"]["chunk_audit"]
    assert audit["status"] == "GO"
    assert audit["blocking"]["total"] == 0
    assert audit["advisory"] == {
        "total": 6,
        "findings": {
            "too_short_under_20_chars": 1,
            "duplicate_text_groups": 2,
            "new_documents_without_chunks": 3,
        },
    }
    assert report["yonote_preview"]["receipt_created"] is True
    serialized = json.dumps(report, ensure_ascii=False)
    for private_value in (
        "private-collection-id",
        "private-empty-document-id",
        "new_below_minimum_container",
    ):
        assert private_value not in serialized


def test_acceptance_stops_on_blocking_chunk_findings_without_losing_safe_report() -> None:
    report = gate.run_acceptance(
        expected_git_sha=EXPECTED_SHA,
        channels_disabled_attestation=gate.CHANNELS_DISABLED_ATTESTATION,
        environ=_environment(),
        requester=FakeRequester(oversized=2, duplicate_groups=3),
    )

    assert report["status"] == "STOP"
    assert report["yonote_preview"]["quality_status"] == "STOP"
    audit = report["yonote_preview"]["chunk_audit"]
    assert audit["status"] == "STOP"
    assert audit["blocking"]["total"] == 2
    assert audit["advisory"]["total"] == 3
    assert report["yonote_preview"]["receipt_created"] is False


def test_preview_parser_blocks_new_substantive_document_without_chunks() -> None:
    preview, clean = gate._safe_preview(
        _preview(new_substantive_documents_without_chunks=1)
    )

    assert clean is False
    assert preview["quality_status"] == "STOP"
    assert preview["chunk_audit"]["blocking"] == {
        "total": 1,
        "findings": {
            "empty_text": 0,
            "oversized_over_max_chars": 0,
            "missing_source_url": 0,
            "missing_source_document_id": 0,
            "missing_source_updated_at": 0,
            "existing_documents_without_chunks": 0,
            "new_substantive_documents_without_chunks": 1,
            "unclassified_documents_without_chunks": 0,
        },
    }
    assert preview["chunk_audit"]["documents"][
        "new_substantive_without_chunks"
    ] == 1
    assert preview["receipt_created"] is False


def test_preview_parser_returns_safe_destructive_snapshot_stop() -> None:
    preview, clean = gate._safe_preview(
        _preview(snapshot_reasons=["yonote_snapshot_empty"])
    )

    assert clean is False
    assert preview["quality_status"] == "STOP"
    assert preview["snapshot_safety"] == {
        "status": "STOP",
        "reasons": ["yonote_snapshot_empty"],
    }
    assert preview["semantic_integrity"]["status"] == "GO"
    assert preview["receipt_created"] is False


def test_preview_parser_keeps_legacy_chunk_audit_fail_closed() -> None:
    preview, clean = gate._safe_preview(
        _preview(too_short=1, legacy_chunk_audit=True)
    )

    assert clean is False
    assert preview["quality_status"] == "STOP"
    assert preview["chunk_audit"]["policy_version"] == "legacy-conservative"
    assert preview["chunk_audit"]["blocking"] == {
        "total": 1,
        "findings": {
            "legacy_warnings": 1,
            "documents_without_chunks": 0,
        },
    }
    assert preview["chunk_audit"]["advisory"]["total"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["identity_reconciliation"].update(
            exact_content_rekeys=1
        ),
        lambda payload: payload["identity_reconciliation"].update(logical_added=9),
        lambda payload: payload["identity_reconciliation"].pop(
            "same_set_identity_rotations"
        ),
        lambda payload: payload["identity_reconciliation"].update(
            ambiguous_exact_content_groups=9999
        ),
        lambda payload: payload["change_classification"].update(metadata_only=99),
        lambda payload: payload["change_classification"]["field_counts"].update(
            private_text=1
        ),
    ],
)
def test_preview_parser_rejects_inconsistent_diff_aggregates(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    payload = _preview()
    mutation(payload)

    with pytest.raises(gate.AcceptanceError, match="yonote_.*_invalid"):
        gate._safe_preview(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(current_records=2187),
        lambda payload: payload.update(current_yonote_records=2153),
        lambda payload: payload.update(fresh_yonote_records=2161),
        lambda payload: payload.update(merged_records=2195),
        lambda payload: payload.update(added=11),
        lambda payload: payload.update(changed=8),
        lambda payload: payload.update(removed=3),
        lambda payload: payload.update(unchanged=2144),
    ],
)
def test_preview_parser_rejects_inconsistent_diff_count_equations(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    payload = _preview()
    mutation(payload)

    with pytest.raises(
        gate.AcceptanceError, match="yonote_preview_counts_inconsistent"
    ):
        gate._safe_preview(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda audit: audit["documents"].update(read=118),
        lambda audit: audit["documents"].update(with_chunks=116),
        lambda audit: audit["documents"]["chunks_per_document"].update(count=116),
        lambda audit: audit["fresh_lengths"].update(count=2159),
        lambda audit: audit["merged_lengths"].update(count=2193),
        lambda audit: audit["fresh_lengths"].update(p50=10),
        lambda audit: audit["merged_lengths"].update(maximum=100),
        lambda audit: audit["documents"].update(
            new_substantive_without_chunks=1
        ),
    ],
)
def test_preview_parser_rejects_inconsistent_chunk_document_and_length_counts(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    payload = _preview()
    mutation(payload["chunk_audit"])

    with pytest.raises(
        gate.AcceptanceError,
        match="yonote_chunk_audit_(?:invalid|counts_inconsistent|policy_invalid)",
    ):
        gate._safe_preview(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda safety: safety.update(
            status="STOP", reasons=["absolute_removal_limit_exceeded"]
        ),
        lambda safety: safety.update(removed=101),
        lambda safety: safety.update(current_yonote_records=999),
        lambda safety: safety.update(fresh_yonote_records=0),
        lambda safety: safety.update(removal_ratio=0.5),
        lambda safety: safety.update(maximum_removal_ratio_without_waiver=0.5),
        lambda safety: safety.update(maximum_removals_without_waiver=101),
    ],
)
def test_preview_parser_recomputes_snapshot_safety_instead_of_trusting_payload(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    payload = _preview()
    mutation(payload["snapshot_safety"])

    with pytest.raises(gate.AcceptanceError, match="yonote_snapshot_safety_invalid"):
        gate._safe_preview(payload)


def test_preview_parser_accepts_recomputed_absolute_removal_stop() -> None:
    preview, clean = gate._safe_preview(
        _preview(snapshot_reasons=["absolute_removal_limit_exceeded"])
    )

    assert clean is False
    assert preview["snapshot_safety"] == {
        "status": "STOP",
        "reasons": ["absolute_removal_limit_exceeded"],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda audit: audit["blocking"].update(total=99),
        lambda audit: audit["advisory"]["findings"].update(
            new_documents_without_chunks=99
        ),
        lambda audit: audit.update(status="GO"),
        lambda audit: audit.update(policy_version="unknown-policy"),
        lambda audit: audit.pop("advisory"),
        lambda audit: audit["documents"].pop("without_chunks_sample"),
        lambda audit: audit["documents"]["without_chunks_sample"].append(
            {
                "source_collection_id": "collection",
                "source_document_id": "document",
                "reason": "unknown_reason",
                "cleaned_chars": 0,
            }
        ),
    ],
)
def test_preview_parser_rejects_inconsistent_chunk_audit_policy(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    payload = _preview(oversized=1)
    mutation(payload["chunk_audit"])

    with pytest.raises(gate.AcceptanceError, match="yonote_chunk_audit_policy_invalid"):
        gate._safe_preview(payload)


@pytest.mark.parametrize(
    "field",
    ["stale_prune_required", "full_reindex_required"],
)
def test_preview_parser_recomputes_index_projection_flags(field: str) -> None:
    payload = _preview()
    payload["index_projection"][field] = False

    with pytest.raises(gate.AcceptanceError, match="yonote_projection_invalid"):
        gate._safe_preview(payload)


def test_acceptance_returns_semantic_stop_as_safe_content_verdict() -> None:
    report = gate.run_acceptance(
        expected_git_sha=EXPECTED_SHA,
        channels_disabled_attestation=gate.CHANNELS_DISABLED_ATTESTATION,
        environ=_environment(),
        requester=FakeRequester(
            semantic_codes={"forum_text_conflict": 2},
        ),
    )

    assert report["status"] == "STOP"
    assert report["yonote_preview"]["quality_status"] == "STOP"
    assert report["yonote_preview"]["semantic_integrity"] == {
        "status": "STOP",
        "codes": {"forum_text_conflict": 2},
        "errors_total": 2,
    }
    assert report["yonote_preview"]["snapshot_safety"] == {
        "status": "GO",
        "reasons": [],
    }
    assert report["yonote_preview"]["receipt_created"] is False
    assert report["non_mutation"]["only_private_preview_receipt_created"] is False
    serialized = json.dumps(report, ensure_ascii=False)
    for forbidden in (
        RECEIPT_ID,
        RECEIPT_SHA,
        "semantic-private-id",
        "semantic source text",
    ):
        assert forbidden not in serialized


def test_preview_parser_preserves_simultaneous_semantic_and_snapshot_stops() -> None:
    preview, clean = gate._safe_preview(
        _preview(
            semantic_codes={"malformed_link": 1},
            snapshot_reasons=["removal_ratio_limit_exceeded"],
        )
    )

    assert clean is False
    assert preview["quality_status"] == "STOP"
    assert preview["semantic_integrity"] == {
        "status": "STOP",
        "codes": {"malformed_link": 1},
        "errors_total": 1,
    }
    assert preview["snapshot_safety"] == {
        "status": "STOP",
        "reasons": ["removal_ratio_limit_exceeded"],
    }
    assert preview["receipt_created"] is False


def test_runtime_status_parser_rejects_seed_changed_during_scan() -> None:
    payload = _runtime_status()
    payload["ok"] = False
    payload["status"] = "STOP"
    payload["failure_reasons"] = ["seed_changed_during_scan"]
    payload["seed"]["changed_during_scan"] = True
    payload["seed"]["post_scan_sha256"] = "d" * 64
    payload["qdrant"]["exact_payload_match"] = False

    with pytest.raises(gate.AcceptanceError, match="runtime_seed_qdrant_not_aligned"):
        gate._safe_runtime_status(payload, EXPECTED_SHA)


def test_logout_cookie_requires_explicit_deletion_semantics() -> None:
    gate._require_deleted_session_cookie(
        'rosmol_admin_session=""; Path=/admin/kb; Max-Age=0'
    )
    gate._require_deleted_session_cookie(
        'rosmol_admin_session=""; Path=/admin/kb; '
        "Expires=Thu, 01 Jan 1970 00:00:00 GMT"
    )

    with pytest.raises(gate.AcceptanceError, match="admin_logout_cookie_invalid"):
        gate._require_deleted_session_cookie(
            "rosmol_admin_session=replacement; Path=/admin/kb"
        )


def test_acceptance_stops_if_preview_changes_runtime_state() -> None:
    report = gate.run_acceptance(
        expected_git_sha=EXPECTED_SHA,
        channels_disabled_attestation=gate.CHANNELS_DISABLED_ATTESTATION,
        environ=_environment(),
        requester=FakeRequester(change_runtime_after_preview=True),
    )

    assert report["status"] == "STOP"
    assert report["non_mutation"]["qdrant_and_cache_unchanged"] is False


def test_acceptance_rejects_projection_bound_to_another_runtime_snapshot() -> None:
    with pytest.raises(
        gate.AcceptanceError, match="yonote_projection_runtime_mismatch"
    ):
        gate.run_acceptance(
            expected_git_sha=EXPECTED_SHA,
            channels_disabled_attestation=gate.CHANNELS_DISABLED_ATTESTATION,
            environ=_environment(),
            requester=FakeRequester(preview_current_published_points=2151),
        )


def test_acceptance_rejects_logout_without_server_side_session_revocation() -> None:
    with pytest.raises(gate.AcceptanceError, match="admin_logout_session_not_revoked"):
        gate.run_acceptance(
            expected_git_sha=EXPECTED_SHA,
            channels_disabled_attestation=gate.CHANNELS_DISABLED_ATTESTATION,
            environ=_environment(),
            requester=FakeRequester(revoke_session_on_logout=False),
        )


def test_acceptance_can_audit_writable_admin_without_calling_mutation_endpoints() -> None:
    environment = _environment()
    environment["ADMIN_READ_ONLY"] = "false"
    environment["ADMIN_MUTATIONS_ENABLED"] = "true"
    requester = FakeRequester(admin_read_only=False)

    report = gate.run_acceptance(
        expected_git_sha=EXPECTED_SHA,
        channels_disabled_attestation=gate.CHANNELS_DISABLED_ATTESTATION,
        environ=environment,
        requester=requester,
    )

    assert report["status"] == "GO"
    assert report["admin"]["read_only"] is False
    assert report["admin"]["mutations_enabled"] is True
    assert not any("/apply" in path for _method, path in requester.paths)
    assert not any("/reindex" in path for _method, path in requester.paths)
    assert not any(method in {"PATCH", "PUT", "DELETE"} for method, _path in requester.paths)


def test_acceptance_requires_explicit_owner_channel_attestation() -> None:
    with pytest.raises(gate.AcceptanceError, match="channels_disabled_attestation_required"):
        gate.run_acceptance(
            expected_git_sha=EXPECTED_SHA,
            channels_disabled_attestation="MANUAL_CONFIRMATION_REQUIRED",
            environ=_environment(),
            requester=FakeRequester(),
        )


def test_cli_uses_distinct_exit_code_for_safe_content_stop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        gate,
        "run_acceptance",
        lambda **_kwargs: {
            "status": "STOP",
            "yonote_preview": {
                "quality_status": "STOP",
                "semantic_integrity": {
                    "status": "STOP",
                    "codes": {"forum_text_conflict": 1},
                    "errors_total": 1,
                },
            },
            "non_mutation": {
                "seed_unchanged": True,
                "qdrant_and_cache_unchanged": True,
                "hde_queue_unchanged": True,
            },
        },
    )

    exit_code = gate.main([EXPECTED_SHA, gate.CHANNELS_DISABLED_ATTESTATION])

    assert exit_code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "STOP"
    assert output["yonote_preview"]["quality_status"] == "STOP"


def test_cli_keeps_infrastructure_stop_as_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        gate,
        "run_acceptance",
        lambda **_kwargs: {
            "status": "STOP",
            "yonote_preview": {"quality_status": "STOP"},
            "non_mutation": {
                "seed_unchanged": True,
                "qdrant_and_cache_unchanged": False,
                "hde_queue_unchanged": True,
            },
        },
    )

    exit_code = gate.main([EXPECTED_SHA, gate.CHANNELS_DISABLED_ATTESTATION])

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "STOP"


def _bash() -> str:
    if os.name != "nt":
        return "bash"
    candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
    if not candidate.is_file():
        pytest.skip("Git Bash is unavailable")
    return str(candidate)


def test_server_local_wrapper_has_valid_bash_syntax_and_is_fail_closed() -> None:
    result = subprocess.run(
        [_bash(), "-n", str(SHELL_SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

    missing_args = subprocess.run(
        [_bash(), str(SHELL_SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert missing_args.returncode != 0
    assert missing_args.stderr == "admin_kb_acceptance_server_local=FAIL reason=usage\n"


def test_server_local_wrapper_is_exact_sha_bound_and_does_not_use_remote_access() -> None:
    shell = SHELL_SCRIPT.read_text(encoding="utf-8")
    helper = PYTHON_HELPER.read_text(encoding="utf-8")

    assert 'readonly PROJECT_DIR="/opt/rosmol-ai-bot"' in shell
    assert '[[ "$(git rev-parse HEAD 2>/dev/null)" == "$EXPECTED_SHA" ]]' in shell
    assert "git symbolic-ref -q HEAD" in shell
    assert "git status --porcelain=v1 --untracked-files=all" in shell
    assert "org.opencontainers.image.revision" in shell
    assert 'sudo docker exec -i "$ADMIN_CONTAINER" python -' in shell
    assert '"$EXPECTED_SHA" "$OWNER_CHANNELS_ATTESTATION" < "$PYTHON_HELPER"' in shell
    assert '[[ "$helper_status" -eq 2 ]]' in shell
    assert (
        "admin_kb_acceptance_server_local=STOP reason=yonote_preview_quality_stopped"
        in shell
    )
    assert "exit 2" in shell
    assert "ssh " not in shell
    assert "scp " not in shell
    assert "rsync " not in shell
    assert "docker compose" not in shell
    assert "never forwards EXPECTED_KB_SEED_SHA256" in shell
    assert "ADMIN_AUTH_TOKEN" not in shell
    assert "YONOTE_API_TOKEN" not in shell

    assert 'values.get("ADMIN_AUTH_TOKEN"' in helper
    assert 'values.get("YONOTE_API_TOKEN"' in helper
    assert 'requester(\n                "/admin/kb/yonote/preview"' in helper
    assert 'requester(\n                "/admin/kb/yonote/apply"' not in helper
    assert 'requester(\n                "/admin/kb/chunks/' not in helper
    assert 'requester("/ask"' not in helper
    assert 'requester("/webhook/hde"' not in helper
    assert "owner_attested_external_rules_disabled" in helper
    assert "provider-side dispatcher rules are not observable" in helper
