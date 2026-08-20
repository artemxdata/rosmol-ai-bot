from __future__ import annotations

import json
import os
import subprocess
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


def _preview(*, too_short: int = 0) -> dict[str, Any]:
    findings = {
        "empty_text": 0,
        "too_short_under_20_chars": too_short,
        "oversized_over_max_chars": 0,
        "duplicate_text_groups": 0,
        "missing_source_url": 0,
        "missing_source_document_id": 0,
        "missing_source_updated_at": 0,
    }
    length_summary = {"count": 2152, "minimum": 20, "p50": 240, "p95": 900, "maximum": 1800}
    return {
        "ok": True,
        "applied": False,
        "snapshot_scope": "full",
        "snapshot_safety": {
            "status": "GO",
            "reasons": [],
        },
        "receipt": {
            "apply_ready": True,
            "id": RECEIPT_ID,
            "sha256": RECEIPT_SHA,
        },
        "hashes": {
            "current_seed_sha256": "c" * 64,
            "yonote_snapshot_sha256": "d" * 64,
            "merged_seed_sha256": "e" * 64,
        },
        "documents": 117,
        "current_records": 2186,
        "current_yonote_records": 2152,
        "fresh_yonote_records": 2160,
        "merged_records": 2194,
        "added": 10,
        "changed": 7,
        "removed": 2,
        "unchanged": 2143,
        "added_items": [
            {
                "chunk_id": "private-source-id",
                "text_preview": "sensitive source answer must never be printed",
            }
        ],
        "chunk_audit": {
            "fresh_lengths": length_summary,
            "merged_lengths": {**length_summary, "count": 2194},
            "documents": {
                "read": 117,
                "with_chunks": 117,
                "without_chunks": 0,
                "chunks_per_document": {
                    "count": 117,
                    "minimum": 1,
                    "p50": 12,
                    "p95": 40,
                    "maximum": 81,
                },
                "largest_documents": [
                    {"source_document_id": "private-document-id", "chunks": 81}
                ],
            },
            "findings": findings,
            "duplicate_text_sample": [],
            "warnings_total": sum(findings.values()),
        },
        "index_projection": {
            "current_published_points": 2152,
            "expected_published_points": 2160,
            "stale_prune_required": True,
            "full_reindex_required": True,
        },
    }


class FakeRequester:
    def __init__(
        self,
        *,
        too_short: int = 0,
        change_runtime_after_preview: bool = False,
        admin_read_only: bool = True,
        revoke_session_on_logout: bool = True,
    ) -> None:
        self.too_short = too_short
        self.change_runtime_after_preview = change_runtime_after_preview
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
            return self._json(200, _preview(too_short=self.too_short))
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

    assert report["status"] == "GO"
    assert report["channels"] == {
        "status": "HDE_VK_DISABLED",
        "owner_attested_external_rules_disabled": True,
        "direct_vk_webhook_disabled": True,
        "vk_credentials_absent": True,
        "hde_queue_empty_and_unchanged": True,
    }
    assert report["yonote_preview"]["counts"]["added"] == 10
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
        "must not be printed",
    ):
        assert forbidden not in serialized
    assert not any("/admin/kb/yonote/apply" in path for _method, path in requester.paths)
    assert not any("/reindex" in path for _method, path in requester.paths)
    assert not any(path == "/ask" for _method, path in requester.paths)
    assert not any(path == "/webhook/hde" for _method, path in requester.paths)


def test_acceptance_stops_on_chunk_audit_findings_without_losing_safe_report() -> None:
    report = gate.run_acceptance(
        expected_git_sha=EXPECTED_SHA,
        channels_disabled_attestation=gate.CHANNELS_DISABLED_ATTESTATION,
        environ=_environment(),
        requester=FakeRequester(too_short=1),
    )

    assert report["status"] == "STOP"
    assert report["yonote_preview"]["quality_status"] == "STOP"
    assert report["yonote_preview"]["chunk_audit"]["warnings_total"] == 1


def test_preview_parser_rejects_destructive_snapshot_stop() -> None:
    payload = _preview()
    payload["snapshot_safety"] = {
        "status": "STOP",
        "reasons": ["yonote_snapshot_empty"],
    }

    with pytest.raises(gate.AcceptanceError, match="yonote_snapshot_safety_stopped"):
        gate._safe_preview(payload)


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
