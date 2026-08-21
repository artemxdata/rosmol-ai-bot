from __future__ import annotations

import json
import os
import re
import secrets
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from scripts.build_yonote_kb_seed import (
    MAX_CHUNK_CHARS,
    clean_markdown_text,
    merge_records,
)
from scripts.index_kb import (
    load_forum_registry,
    validate_seed_items,
)
from scripts.sync_yonote_kb import (
    DEFAULT_COLLECTION_NAMES,
    YonoteClient,
    YonoteDocument,
    build_records_from_api_documents,
    load_yonote_documents,
    split_collection_selectors,
)
from src.admin.kb_store import write_seed_records
from src.kb.audit import semantic_integrity_findings

COMPARE_FIELDS = (
    "text_clean",
    "status",
    "category",
    "forum_normalized",
    "topic",
    "intent_name",
    "intent_examples",
    "source_category",
    "source_url",
    "source_document_id",
    "source_collection_id",
    "source_collection_name",
    "source_heading_path",
    "source_document_updated_at",
    "registration_deadline",
    "valid_from",
    "valid_to",
)
METADATA_ONLY_CHANGE_FIELDS = frozenset(
    {
        "extraction_date",
        "source_document_updated_at",
        "source_row",
        "updated_at",
    }
)
RECEIPT_SCHEMA_VERSION = "yonote-sync-receipt-v2"
RECEIPT_TTL = timedelta(hours=24)
MAX_RECEIPT_BYTES = 64 * 1024 * 1024
MAX_PREVIEW_TEXT_BYTES = 32 * 1024 * 1024
MAX_PREVIEW_DURATION_SECONDS = 240.0
MAX_REMOVAL_RATIO_WITHOUT_WAIVER = 0.25
MIN_REMOVALS_FOR_RATIO_GUARD = 10
MAX_REMOVALS_WITHOUT_WAIVER = 100
_RECEIPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_STATE_FILE_RE = re.compile(
    r"^[0-9a-f]{32}\.[0-9a-f]{64}\.(?P<state>json|applying|applied)$"
)
_SEMANTIC_FINDING_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class YonoteSyncConfigError(RuntimeError):
    pass


class YonoteReceiptError(ValueError):
    pass


class YonoteReceiptNotFound(YonoteReceiptError):
    pass


class YonoteReceiptExpired(YonoteReceiptError):
    pass


class YonoteReceiptConflict(YonoteReceiptError):
    pass


def preview_sync(
    seed_path: Path,
    settings: Any,
    *,
    limit_documents: int | None = None,
    receipt_dir: Path | None = None,
) -> dict[str, Any]:
    resolved_receipt_dir = receipt_dir or seed_path.parent / ".yonote-sync-receipts"
    _ensure_no_unresolved_applying(resolved_receipt_dir)
    current_seed_bytes = seed_path.read_bytes()
    current_seed_sha256 = sha256(current_seed_bytes).hexdigest()
    current_records = _parse_seed_records(current_seed_bytes)
    documents, fresh_yonote_records = _load_fresh_yonote_records(
        settings,
        limit_documents=limit_documents,
    )
    try:
        validate_seed_items(fresh_yonote_records)
        raw_yonote_snapshot_sha256 = _provider_snapshot_sha256(
            fresh_yonote_records
        )
        fresh_yonote_records, identity_reconciliation = (
            _reconcile_exact_content_chunk_ids(
                current_records,
                fresh_yonote_records,
            )
        )
        fresh_yonote_records = _preserve_unchanged_yonote_records(
            current_records,
            fresh_yonote_records,
        )
        merged_records = merge_records(
            current_records,
            fresh_yonote_records,
            replace_existing_yonote=True,
        )
        semantic_integrity = _validate_merged_seed(seed_path, merged_records)
        report = _build_sync_report(
            current_records=current_records,
            fresh_yonote_records=fresh_yonote_records,
            merged_records=merged_records,
            documents_count=len(documents),
            loaded_documents=documents,
            applied=False,
            seed_path=seed_path,
            identity_reconciliation=identity_reconciliation,
        )
    except Exception as error:
        if limit_documents is None:
            try:
                _invalidate_active_receipts(resolved_receipt_dir)
            except Exception as invalidation_error:
                error.add_note(
                    "active Yonote Preview receipts could not be invalidated: "
                    f"{type(invalidation_error).__name__}"
                )
        raise
    report["semantic_integrity"] = semantic_integrity
    report["hashes"] = {
        "current_seed_sha256": current_seed_sha256,
        "yonote_snapshot_sha256": raw_yonote_snapshot_sha256,
        "merged_seed_sha256": _seed_sha256(merged_records),
    }
    if limit_documents is not None:
        report["snapshot_scope"] = "partial"
        report["receipt"] = {
            "apply_ready": False,
            "reason": "partial_preview_cannot_be_applied",
        }
        return report

    snapshot_safety = _snapshot_safety(report)
    report["snapshot_safety"] = snapshot_safety
    chunk_audit_stopped = not _chunk_audit_allows_apply(report)
    if _file_sha256(seed_path) != current_seed_sha256:
        raise YonoteReceiptConflict(
            "knowledge base changed during preview; run preview again"
        )
    if (
        snapshot_safety["status"] != "GO"
        or semantic_integrity["status"] != "GO"
        or chunk_audit_stopped
    ):
        _invalidate_active_receipts(resolved_receipt_dir)
        report["snapshot_scope"] = "full"
        receipt_reason = (
            "semantic_integrity_failed"
            if semantic_integrity["status"] == "STOP"
            else (
                "destructive_snapshot_requires_owner_waiver"
                if snapshot_safety["status"] == "STOP"
                else "chunk_audit_failed"
            )
        )
        report["receipt"] = {
            "apply_ready": False,
            "reason": receipt_reason,
        }
        return report

    report["snapshot_scope"] = "full"
    return _seal_preview_receipt(
        seed_path=seed_path,
        receipt_dir=resolved_receipt_dir,
        current_seed_sha256=current_seed_sha256,
        yonote_snapshot_sha256=raw_yonote_snapshot_sha256,
        merged_records=merged_records,
        report=report,
    )


def apply_sync(
    seed_path: Path,
    *,
    receipt_dir: Path,
    receipt_id: str,
    receipt_sha256: str,
) -> dict[str, Any]:
    normalized_id, normalized_sha256 = _normalize_receipt_identity(
        receipt_id,
        receipt_sha256,
    )
    receipt_state, receipt_path, receipt = _load_receipt_state(
        receipt_dir=receipt_dir,
        receipt_id=normalized_id,
        receipt_sha256=normalized_sha256,
    )
    bindings, merged_records = _validated_receipt_contents(receipt)
    expected_seed_path = str(bindings.get("seed_path") or "")
    if expected_seed_path != str(seed_path.resolve(strict=True)):
        raise YonoteReceiptConflict("receipt is bound to another knowledge base")
    # Structural corruption is never recoverable, including for a receipt whose
    # seed write already committed. Semantic rules may legitimately evolve after
    # that commit, so they must not strand an `.applying` receipt forever.
    validate_seed_items(merged_records)

    current_seed_sha256 = _file_sha256(seed_path)
    expected_current_sha256 = str(bindings["current_seed_sha256"])
    expected_merged_sha256 = str(bindings["merged_seed_sha256"])
    if receipt_state == "applied":
        if current_seed_sha256 != expected_merged_sha256:
            raise YonoteReceiptConflict(
                "consumed receipt cannot be replayed after knowledge base changed"
            )
        return _applied_receipt_report(
            receipt,
            seed_path=seed_path,
            receipt_id=normalized_id,
            receipt_sha256=normalized_sha256,
            idempotent=True,
            finalization={"state": "applied", "pending": False},
        )

    if receipt_state == "applying":
        if current_seed_sha256 == expected_merged_sha256:
            finalization = _finalize_claimed_receipt(receipt_path)
            return _applied_receipt_report(
                receipt,
                seed_path=seed_path,
                receipt_id=normalized_id,
                receipt_sha256=normalized_sha256,
                idempotent=True,
                finalization=finalization,
            )
        if current_seed_sha256 == expected_current_sha256:
            raise YonoteReceiptConflict("preview receipt apply is already in progress")
        raise YonoteReceiptConflict(
            "knowledge base changed while preview receipt was being applied"
        )

    if current_seed_sha256 == expected_merged_sha256:
        applying_path = receipt_path.with_suffix(".applying")
        try:
            os.replace(receipt_path, applying_path)
        except FileNotFoundError:
            return apply_sync(
                seed_path,
                receipt_dir=receipt_dir,
                receipt_id=normalized_id,
                receipt_sha256=normalized_sha256,
            )
        try:
            _sync_directory(applying_path.parent)
        except Exception:
            _restore_active_receipt(applying_path)
            raise
        finalization = _finalize_claimed_receipt(applying_path)
        return _applied_receipt_report(
            receipt,
            seed_path=seed_path,
            receipt_id=normalized_id,
            receipt_sha256=normalized_sha256,
            idempotent=True,
            finalization=finalization,
        )

    if current_seed_sha256 != expected_current_sha256:
        raise YonoteReceiptConflict(
            "knowledge base changed after preview; run preview again"
        )

    semantic_integrity = _validate_merged_seed(seed_path, merged_records)
    if semantic_integrity["status"] != "GO":
        raise YonoteReceiptError("preview receipt failed semantic integrity")

    applying_path = receipt_path.with_suffix(".applying")
    try:
        os.replace(receipt_path, applying_path)
    except FileNotFoundError:
        # Another exact caller won the atomic claim. Re-read its durable state;
        # it may already have committed the seed and only need finalization.
        return apply_sync(
            seed_path,
            receipt_dir=receipt_dir,
            receipt_id=normalized_id,
            receipt_sha256=normalized_sha256,
        )
    try:
        _sync_directory(applying_path.parent)
    except Exception:
        _restore_active_receipt(applying_path)
        raise

    if _file_sha256(seed_path) != expected_current_sha256:
        _restore_active_receipt(applying_path)
        raise YonoteReceiptConflict(
            "knowledge base changed while preview receipt was being claimed"
        )

    try:
        write_seed_records(seed_path, merged_records)
    except Exception:
        written_sha256 = _file_sha256(seed_path)
        if written_sha256 == expected_merged_sha256:
            # `write_seed_records` may fail in chmod/fsync after os.replace.
            # The data commit is authoritative; never report it as retryable.
            finalization = _finalize_claimed_receipt(applying_path)
            return _applied_receipt_report(
                receipt,
                seed_path=seed_path,
                receipt_id=normalized_id,
                receipt_sha256=normalized_sha256,
                idempotent=False,
                finalization=finalization,
            )
        if written_sha256 == expected_current_sha256:
            _restore_active_receipt(applying_path)
        raise

    written_sha256 = _file_sha256(seed_path)
    if written_sha256 != expected_merged_sha256:
        if written_sha256 == expected_current_sha256:
            _restore_active_receipt(applying_path)
        raise YonoteReceiptError("written knowledge base hash mismatch")

    finalization = _finalize_claimed_receipt(applying_path)
    return _applied_receipt_report(
        receipt,
        seed_path=seed_path,
        receipt_id=normalized_id,
        receipt_sha256=normalized_sha256,
        idempotent=False,
        finalization=finalization,
    )


def _seal_preview_receipt(
    *,
    seed_path: Path,
    receipt_dir: Path,
    current_seed_sha256: str,
    yonote_snapshot_sha256: str,
    merged_records: list[dict[str, Any]],
    report: dict[str, Any],
) -> dict[str, Any]:
    created_at = datetime.now(UTC)
    receipt_id = secrets.token_hex(16)
    bindings = {
        "seed_path": str(seed_path.resolve(strict=True)),
        "current_seed_sha256": current_seed_sha256,
        "yonote_snapshot_sha256": yonote_snapshot_sha256,
        "merged_seed_sha256": _seed_sha256(merged_records),
    }
    sealed_report = dict(report)
    sealed_report["hashes"] = dict(bindings)
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "created_at": created_at.isoformat(),
        "expires_at": (created_at + RECEIPT_TTL).isoformat(),
        "bindings": bindings,
        "report": sealed_report,
        "merged_records": merged_records,
    }
    rendered = _canonical_json_bytes(receipt)
    receipt_sha256 = sha256(rendered).hexdigest()
    _write_receipt(
        receipt_dir / f"{receipt_id}.{receipt_sha256}.json",
        rendered,
    )
    sealed_report["receipt"] = {
        "id": receipt_id,
        "sha256": receipt_sha256,
        "created_at": receipt["created_at"],
        "expires_at": receipt["expires_at"],
        "apply_ready": True,
        "consumed": False,
    }
    return sealed_report


def _normalize_receipt_identity(
    receipt_id: str,
    receipt_sha256: str,
) -> tuple[str, str]:
    normalized_id = str(receipt_id or "").strip().casefold()
    normalized_sha256 = str(receipt_sha256 or "").strip().casefold()
    if not _RECEIPT_ID_RE.fullmatch(normalized_id):
        raise YonoteReceiptError("invalid receipt id")
    if not _SHA256_RE.fullmatch(normalized_sha256):
        raise YonoteReceiptError("invalid receipt hash")
    return normalized_id, normalized_sha256


def _load_receipt_state(
    *,
    receipt_dir: Path,
    receipt_id: str,
    receipt_sha256: str,
) -> tuple[str, Path, dict[str, Any]]:
    if receipt_dir.is_symlink():
        raise YonoteReceiptError("receipt directory must not be a symlink")
    prefix = f"{receipt_id}.{receipt_sha256}"
    candidates = {
        "active": receipt_dir / f"{prefix}.json",
        "applying": receipt_dir / f"{prefix}.applying",
        "applied": receipt_dir / f"{prefix}.applied",
    }
    existing = [
        (state, path)
        for state, path in candidates.items()
        if path.exists() or path.is_symlink()
    ]
    if not existing:
        raise YonoteReceiptNotFound("preview receipt not found or already consumed")
    if len(existing) != 1:
        raise YonoteReceiptConflict("preview receipt has conflicting durable states")
    state, receipt_path = existing[0]
    receipt = _load_receipt_file(
        receipt_path,
        receipt_id=receipt_id,
        receipt_sha256=receipt_sha256,
        enforce_expiry=state == "active",
    )
    return state, receipt_path, receipt


def _load_receipt_file(
    receipt_path: Path,
    *,
    receipt_id: str,
    receipt_sha256: str,
    enforce_expiry: bool,
) -> dict[str, Any]:
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise YonoteReceiptError("preview receipt must be a regular file")
    if receipt_path.stat().st_size > MAX_RECEIPT_BYTES:
        raise YonoteReceiptError("preview receipt is too large")
    rendered = receipt_path.read_bytes()
    if sha256(rendered).hexdigest() != receipt_sha256:
        raise YonoteReceiptError("preview receipt hash mismatch")
    try:
        payload = json.loads(rendered.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise YonoteReceiptError("preview receipt is invalid") from exc
    receipt = _receipt_object(payload, "preview receipt")
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise YonoteReceiptError("unsupported preview receipt schema")
    if receipt.get("receipt_id") != receipt_id:
        raise YonoteReceiptError("preview receipt id mismatch")
    try:
        expires_at = datetime.fromisoformat(str(receipt.get("expires_at") or ""))
    except ValueError as exc:
        raise YonoteReceiptError("preview receipt expiry is invalid") from exc
    if expires_at.tzinfo is None:
        raise YonoteReceiptError("preview receipt expiry must be timezone-aware")
    if enforce_expiry and expires_at <= datetime.now(UTC):
        receipt_path.unlink(missing_ok=True)
        raise YonoteReceiptExpired("preview receipt expired; run preview again")
    return receipt


def _validated_receipt_contents(
    receipt: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bindings = _receipt_object(receipt.get("bindings"), "receipt bindings")
    for field in (
        "current_seed_sha256",
        "yonote_snapshot_sha256",
        "merged_seed_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(bindings.get(field) or "")):
            raise YonoteReceiptError(f"receipt {field} is invalid")
    seed_path = str(bindings.get("seed_path") or "")
    if not seed_path or not Path(seed_path).is_absolute():
        raise YonoteReceiptError("receipt seed path is invalid")
    merged_records = _receipt_records(receipt.get("merged_records"))
    if _seed_sha256(merged_records) != bindings["merged_seed_sha256"]:
        raise YonoteReceiptError("receipt merged seed hash mismatch")
    _receipt_object(receipt.get("report"), "receipt report")
    return bindings, merged_records


def _restore_active_receipt(applying_path: Path) -> None:
    active_path = applying_path.with_suffix(".json")
    if active_path.exists() or active_path.is_symlink():
        raise YonoteReceiptConflict("active receipt already exists during rollback")
    os.replace(applying_path, active_path)
    _sync_directory(active_path.parent)


def _finalize_claimed_receipt(applying_path: Path) -> dict[str, Any]:
    applied_path = applying_path.with_suffix(".applied")
    try:
        os.replace(applying_path, applied_path)
    except Exception as exc:
        return {
            "state": "applying",
            "pending": True,
            "warning": type(exc).__name__,
        }

    warning: str | None = None
    try:
        if os.name == "posix":
            os.chmod(applied_path, 0o600)
        _sync_directory(applied_path.parent)
        _remove_superseded_receipts(
            applied_path.parent,
            keep=applied_path.name,
            removable_states=frozenset({"applied"}),
        )
        _sync_directory(applied_path.parent)
    except Exception as exc:
        warning = type(exc).__name__
    result: dict[str, Any] = {"state": "applied", "pending": False}
    if warning:
        result["warning"] = warning
    return result


def _applied_receipt_report(
    receipt: dict[str, Any],
    *,
    seed_path: Path,
    receipt_id: str,
    receipt_sha256: str,
    idempotent: bool,
    finalization: dict[str, Any],
) -> dict[str, Any]:
    report = _receipt_object(receipt.get("report"), "receipt report")
    report["applied"] = True
    report["applied_at"] = datetime.now(UTC).isoformat()
    report["seed_path"] = str(seed_path)
    report["receipt"] = {
        "id": receipt_id,
        "sha256": receipt_sha256,
        "apply_ready": False,
        "consumed": True,
        "idempotent": idempotent,
        "state": finalization["state"],
        "finalization_pending": finalization["pending"],
    }
    if warning := finalization.get("warning"):
        report["receipt"]["finalization_warning"] = warning
    return report


def _sync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(directory, directory_flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_receipt(path: Path, rendered: bytes) -> None:
    if len(rendered) > MAX_RECEIPT_BYTES:
        raise YonoteReceiptError("preview receipt is too large")
    directory = path.parent
    if directory.exists() and directory.is_symlink():
        raise YonoteReceiptError("receipt directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _ensure_no_unresolved_applying(directory)
    if os.name == "posix":
        os.chmod(directory, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    try:
        _remove_superseded_receipts(
            directory,
            keep=path.name,
            removable_states=frozenset({"json", "applied"}),
        )
        _sync_directory(directory)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _ensure_no_unresolved_applying(directory: Path) -> None:
    if directory.is_symlink():
        raise YonoteReceiptError("receipt directory must not be a symlink")
    if not directory.exists():
        return
    for candidate in directory.iterdir():
        match = _RECEIPT_STATE_FILE_RE.fullmatch(candidate.name)
        if match is not None and match.group("state") == "applying":
            raise YonoteReceiptConflict(
                "unfinished Yonote Apply requires exact receipt recovery"
            )


def _remove_superseded_receipts(
    directory: Path,
    *,
    keep: str,
    removable_states: frozenset[str],
) -> None:
    if directory.is_symlink():
        raise YonoteReceiptError("receipt directory must not be a symlink")
    for candidate in directory.iterdir():
        if candidate.name == keep:
            continue
        match = _RECEIPT_STATE_FILE_RE.fullmatch(candidate.name)
        if match is None or match.group("state") not in removable_states:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        candidate.unlink()


def _invalidate_active_receipts(directory: Path) -> None:
    """Invalidate older GO receipts when the newest full Preview is STOP."""

    if not directory.exists():
        return
    _remove_superseded_receipts(
        directory,
        keep="",
        removable_states=frozenset({"json"}),
    )
    _sync_directory(directory)


def _receipt_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise YonoteReceiptError(f"{label} must be an object")
    return dict(value)


def _receipt_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise YonoteReceiptError("receipt merged records must be objects")
    return [dict(item) for item in value]


def _canonical_json_bytes(value: Any) -> bytes:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{rendered}\n".encode()


def _canonical_sha256(value: Any) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _provider_snapshot_sha256(records: list[dict[str, Any]]) -> str:
    """Hash provider-derived records without pull-time fallback metadata."""

    stable_records: list[dict[str, Any]] = []
    for record in records:
        stable = {
            key: value
            for key, value in record.items()
            if key != "extraction_date"
        }
        if _uses_acquisition_date_as_updated_at(record):
            stable.pop("updated_at", None)
        stable_records.append(stable)
    stable_records.sort(key=_canonical_json_bytes)
    return _canonical_sha256(stable_records)


def _seed_bytes(records: list[dict[str, Any]]) -> bytes:
    return f"{json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True)}\n".encode()


def _seed_sha256(records: list[dict[str, Any]]) -> str:
    return sha256(_seed_bytes(records)).hexdigest()


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _load_fresh_yonote_records(
    settings: Any,
    *,
    limit_documents: int | None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    documents = load_yonote_documents_from_settings(
        settings,
        limit_documents=limit_documents,
        include_empty=True,
        max_duration_seconds=MAX_PREVIEW_DURATION_SECONDS,
        max_total_text_bytes=MAX_PREVIEW_TEXT_BYTES,
    )
    base_url = str(getattr(settings, "yonote_base_url", "") or "").strip()
    records = build_records_from_api_documents(
        documents,
        base_url=base_url,
        extraction_date=date.today(),
    )
    return documents, records


def load_yonote_documents_from_settings(
    settings: Any,
    *,
    limit_documents: int | None = None,
    include_empty: bool = False,
    max_duration_seconds: float | None = None,
    max_total_text_bytes: int | None = None,
) -> list[YonoteDocument]:
    base_url = str(getattr(settings, "yonote_base_url", "") or "").strip()
    api_token = str(getattr(settings, "yonote_api_token", "") or "").strip()
    timeout_seconds = float(getattr(settings, "yonote_request_timeout_seconds", 30.0))
    max_retries = int(getattr(settings, "yonote_max_retries", 2))
    min_request_interval_seconds = float(
        getattr(settings, "yonote_min_request_interval_seconds", 0.15)
    )

    if not base_url:
        raise YonoteSyncConfigError("YONOTE_BASE_URL is not configured")
    if not api_token:
        raise YonoteSyncConfigError("YONOTE_API_TOKEN is not configured")
    configured_collections = str(
        getattr(settings, "yonote_collection_names", "") or ""
    ).strip()
    collection_selectors = (
        split_collection_selectors(configured_collections)
        if configured_collections
        else DEFAULT_COLLECTION_NAMES
    )

    with YonoteClient(
        base_url=base_url,
        api_token=api_token,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        min_request_interval_seconds=min_request_interval_seconds,
        max_duration_seconds=max_duration_seconds,
    ) as client:
        return load_yonote_documents(
            client,
            collection_selectors,
            limit_documents=limit_documents,
            include_empty=include_empty,
            max_total_text_bytes=max_total_text_bytes,
        )


def _reconcile_exact_content_chunk_ids(
    current_records: list[dict[str, Any]],
    fresh_yonote_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Preserve unique exact-content identity inside one provider document.

    Yonote section ordinals are part of legacy chunk IDs. A heading insertion
    can therefore rotate content through IDs that still exist in both raw ID
    sets. Match unique exact content across the complete immutable document
    scope, then give displaced new content a deterministic vacated raw ID.
    Duplicate/ambiguous content and missing scope metadata are never guessed.
    """

    old_by_id = {
        str(record.get("chunk_id") or ""): record
        for record in current_records
        if str(record.get("source_type") or "") == "yonote"
        and str(record.get("chunk_id") or "")
    }
    fresh_by_id = {
        str(record.get("chunk_id") or ""): record
        for record in fresh_yonote_records
        if str(record.get("chunk_id") or "")
    }
    old_ids = set(old_by_id)
    fresh_ids = set(fresh_by_id)
    raw_removed_ids = old_ids - fresh_ids
    raw_added_ids = fresh_ids - old_ids

    old_by_content: dict[tuple[str, str, str], list[str]] = {}
    fresh_by_content: dict[tuple[str, str, str], list[str]] = {}
    for chunk_id, record in old_by_id.items():
        key = _exact_document_content_key(record)
        if key is not None:
            old_by_content.setdefault(key, []).append(chunk_id)
    for chunk_id, record in fresh_by_id.items():
        key = _exact_document_content_key(record)
        if key is not None:
            fresh_by_content.setdefault(key, []).append(chunk_id)

    fresh_to_old: dict[str, str] = {}
    ambiguous_groups = 0
    for key in sorted(old_by_content.keys() & fresh_by_content.keys()):
        old_candidates = sorted(old_by_content[key])
        fresh_candidates = sorted(fresh_by_content[key])
        if len(old_candidates) == 1 and len(fresh_candidates) == 1:
            fresh_to_old[fresh_candidates[0]] = old_candidates[0]
        else:
            ambiguous_groups += 1

    matched_targets = set(fresh_to_old.values())
    assigned_ids: dict[str, str] = dict(fresh_to_old)
    unmatched_fresh_ids = sorted(fresh_ids - set(fresh_to_old))
    reserved_unmatched_ids = {
        chunk_id for chunk_id in unmatched_fresh_ids if chunk_id not in matched_targets
    }
    available_vacated_ids = sorted(
        fresh_ids - matched_targets - reserved_unmatched_ids
    )
    displaced_fresh_ids = [
        chunk_id
        for chunk_id in unmatched_fresh_ids
        if chunk_id in matched_targets
    ]
    available_by_scope: dict[tuple[str, str], list[str]] = {}
    for chunk_id in available_vacated_ids:
        scope = _immutable_document_scope(fresh_by_id[chunk_id])
        if scope is None:
            raise ValueError(
                "Yonote identity reconciliation has an unscoped vacated ID"
            )
        available_by_scope.setdefault(scope, []).append(chunk_id)
    displaced_by_scope: dict[tuple[str, str], list[str]] = {}
    for chunk_id in displaced_fresh_ids:
        scope = _immutable_document_scope(fresh_by_id[chunk_id])
        if scope is None:
            raise ValueError(
                "Yonote identity reconciliation has an unscoped displaced ID"
            )
        displaced_by_scope.setdefault(scope, []).append(chunk_id)
    for scope in sorted(displaced_by_scope):
        displaced = displaced_by_scope[scope]
        available = available_by_scope.get(scope, [])
        if len(available) < len(displaced):
            raise ValueError(
                "Yonote identity reconciliation has no collision-free ID "
                "inside one document scope"
            )
        assigned_ids.update(zip(displaced, available, strict=False))
    for chunk_id in reserved_unmatched_ids:
        assigned_ids[chunk_id] = chunk_id

    reconciled: list[dict[str, Any]] = []
    for record in fresh_yonote_records:
        rendered = dict(record)
        raw_chunk_id = str(record.get("chunk_id") or "")
        rendered["chunk_id"] = assigned_ids.get(raw_chunk_id, raw_chunk_id)
        reconciled.append(rendered)

    reconciled_ids = [str(record.get("chunk_id") or "") for record in reconciled]
    if len(reconciled_ids) != len(set(reconciled_ids)):
        raise ValueError("Yonote identity reconciliation produced duplicate chunk IDs")

    reconciled_id_set = set(reconciled_ids)
    exact_rekeys_from_added = len(raw_added_ids) - len(reconciled_id_set - old_ids)
    exact_rekeys_from_removed = len(raw_removed_ids) - len(old_ids - reconciled_id_set)
    if (
        exact_rekeys_from_added != exact_rekeys_from_removed
        or exact_rekeys_from_added < 0
    ):
        raise ValueError("Yonote identity reconciliation set arithmetic is inconsistent")
    exact_content_rekeys = exact_rekeys_from_added
    same_set_identity_rotations = sum(
        not (fresh_id in raw_added_ids and old_id in raw_removed_ids)
        for fresh_id, old_id in fresh_to_old.items()
        if fresh_id != old_id
    )

    return reconciled, {
        "raw_id_added": len(raw_added_ids),
        "raw_id_removed": len(raw_removed_ids),
        "exact_content_rekeys": exact_content_rekeys,
        "same_set_identity_rotations": same_set_identity_rotations,
        "ambiguous_exact_content_groups": ambiguous_groups,
    }


def _exact_document_content_key(
    record: dict[str, Any],
) -> tuple[str, str, str] | None:
    scope = _immutable_document_scope(record)
    text = str(record.get("text_clean") or record.get("text_raw") or "").strip()
    if scope is None or not text:
        return None
    return (*scope, sha256(text.encode("utf-8")).hexdigest())


def _immutable_document_scope(
    record: dict[str, Any],
) -> tuple[str, str] | None:
    collection_id = str(record.get("source_collection_id") or "").strip()
    document_id = str(record.get("source_document_id") or "").strip()
    if not collection_id or not document_id:
        return None
    return collection_id, document_id


def _validate_merged_seed(
    seed_path: Path,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    validate_seed_items(records)
    findings = semantic_integrity_findings(
        records,
        forum_registry=load_forum_registry(seed_path),
    )
    counts: Counter[str] = Counter()
    affected_chunk_ids: dict[str, set[str]] = {}
    for finding in findings:
        if finding.get("severity") != "error":
            continue
        raw_code = str(finding.get("code") or "").strip()
        code = (
            raw_code
            if _SEMANTIC_FINDING_CODE_RE.fullmatch(raw_code)
            else "unclassified_semantic_error"
        )
        raw_count = finding.get("count")
        count = (
            raw_count
            if isinstance(raw_count, int)
            and not isinstance(raw_count, bool)
            and raw_count > 0
            else 1
        )
        counts[code] += count
        code_chunk_ids = affected_chunk_ids.setdefault(code, set())
        for item in finding.get("records") or []:
            if isinstance(item, dict):
                chunk_id = str(item.get("chunk_id") or "").strip()
                if chunk_id:
                    code_chunk_ids.add(chunk_id)
        for item in finding.get("chunk_ids") or []:
            chunk_id = str(item or "").strip()
            if chunk_id:
                code_chunk_ids.add(chunk_id)
    safe_counts = dict(sorted(counts.items()))
    return {
        "status": "STOP" if safe_counts else "GO",
        "codes": safe_counts,
        "errors_total": sum(safe_counts.values()),
        "affected_chunk_ids": {
            code: sorted(chunk_ids)[:50]
            for code, chunk_ids in sorted(affected_chunk_ids.items())
            if chunk_ids
        },
    }


def _build_sync_report(
    *,
    current_records: list[dict[str, Any]],
    fresh_yonote_records: list[dict[str, Any]],
    merged_records: list[dict[str, Any]],
    documents_count: int,
    loaded_documents: list[Any],
    applied: bool,
    seed_path: Path,
    identity_reconciliation: dict[str, int] | None = None,
) -> dict[str, Any]:
    old_yonote_records = [
        record
        for record in current_records
        if str(record.get("source_type") or "") == "yonote"
    ]
    old_by_id = {str(record["chunk_id"]): record for record in old_yonote_records}
    fresh_by_id = {str(record["chunk_id"]): record for record in fresh_yonote_records}

    old_ids = set(old_by_id)
    fresh_ids = set(fresh_by_id)
    added_ids = sorted(fresh_ids - old_ids)
    removed_ids = sorted(old_ids - fresh_ids)
    changed_ids = sorted(
        chunk_id
        for chunk_id in old_ids & fresh_ids
        if _record_changed(old_by_id[chunk_id], fresh_by_id[chunk_id])
    )
    unchanged_count = len((old_ids & fresh_ids) - set(changed_ids))

    added_items = [_record_summary(fresh_by_id[chunk_id]) for chunk_id in added_ids]
    removed_items = [_record_summary(old_by_id[chunk_id]) for chunk_id in removed_ids]
    changed_items = [
        _changed_record_summary(old_by_id[chunk_id], fresh_by_id[chunk_id])
        for chunk_id in changed_ids
    ]
    changed_field_counts = Counter(
        field
        for item in changed_items
        for field in item.get("changed_fields") or []
    )
    metadata_only_changed = sum(
        bool(fields) and set(fields) <= METADATA_ONLY_CHANGE_FIELDS
        for fields in (
            item.get("changed_fields") or []
            for item in changed_items
        )
    )
    changed_total = len(added_ids) + len(changed_ids) + len(removed_ids)
    identity_report = dict(identity_reconciliation or {})
    identity_report.update(
        {
            "logical_added": len(added_ids),
            "logical_removed": len(removed_ids),
        }
    )
    raw_added = identity_report.get("raw_id_added", len(added_ids))
    raw_removed = identity_report.get("raw_id_removed", len(removed_ids))
    exact_rekeys = identity_report.get("exact_content_rekeys", 0)
    if (
        raw_added - exact_rekeys != len(added_ids)
        or raw_removed - exact_rekeys != len(removed_ids)
    ):
        raise ValueError("Yonote identity reconciliation counts are inconsistent")

    return {
        "ok": True,
        "applied": applied,
        "seed_path": str(seed_path),
        "index_required": changed_total > 0,
        "documents": documents_count,
        "current_records": len(current_records),
        "current_yonote_records": len(old_yonote_records),
        "fresh_yonote_records": len(fresh_yonote_records),
        "merged_records": len(merged_records),
        "added": len(added_ids),
        "changed": len(changed_ids),
        "removed": len(removed_ids),
        "unchanged": unchanged_count,
        "added_sample": added_ids[:10],
        "changed_sample": changed_ids[:10],
        "removed_sample": removed_ids[:10],
        "added_items": added_items,
        "changed_items": changed_items,
        "removed_items": removed_items,
        "identity_reconciliation": identity_report,
        "change_classification": {
            "metadata_only": metadata_only_changed,
            "content_or_source": len(changed_ids) - metadata_only_changed,
            "field_counts": dict(sorted(changed_field_counts.items())),
        },
        "category_counts": _count_field(fresh_yonote_records, "category"),
        "forum_counts": _count_field(fresh_yonote_records, "forum_normalized"),
        "collection_counts": _count_field(
            fresh_yonote_records,
            "source_collection_name",
        ),
        "chunk_audit": _chunk_audit(
            documents_count=documents_count,
            loaded_documents=loaded_documents,
            current_yonote_records=old_yonote_records,
            fresh_yonote_records=fresh_yonote_records,
            merged_records=merged_records,
        ),
        "index_projection": {
            "current_published_points": _published_count(current_records),
            "expected_published_points": _published_count(merged_records),
            "stale_prune_required": bool(removed_ids),
            "full_reindex_required": changed_total > 0,
        },
        "message": _human_message(
            applied=applied,
            changed=len(changed_ids),
            added=len(added_ids),
            removed=len(removed_ids),
        ),
    }


def _snapshot_safety(report: dict[str, Any]) -> dict[str, Any]:
    current_count = int(report.get("current_yonote_records") or 0)
    fresh_count = int(report.get("fresh_yonote_records") or 0)
    removed_count = int(report.get("removed") or 0)
    identity = report.get("identity_reconciliation")
    if not isinstance(identity, dict):
        identity = {}
    raw_removed_count = int(identity.get("raw_id_removed") or removed_count)
    exact_content_rekeys = int(identity.get("exact_content_rekeys") or 0)
    removal_ratio = (removed_count / current_count) if current_count else 0.0
    reasons: list[str] = []
    if current_count > 0 and fresh_count == 0:
        reasons.append("yonote_snapshot_empty")
    if removed_count > MAX_REMOVALS_WITHOUT_WAIVER:
        reasons.append("absolute_removal_limit_exceeded")
    if (
        removed_count >= MIN_REMOVALS_FOR_RATIO_GUARD
        and removal_ratio > MAX_REMOVAL_RATIO_WITHOUT_WAIVER
    ):
        reasons.append("removal_ratio_limit_exceeded")
    return {
        "status": "STOP" if reasons else "GO",
        "reasons": reasons,
        "removed": removed_count,
        "raw_id_removed": raw_removed_count,
        "exact_content_rekeys": exact_content_rekeys,
        "current_yonote_records": current_count,
        "fresh_yonote_records": fresh_count,
        "removal_ratio": round(removal_ratio, 6),
        "maximum_removal_ratio_without_waiver": MAX_REMOVAL_RATIO_WITHOUT_WAIVER,
        "maximum_removals_without_waiver": MAX_REMOVALS_WITHOUT_WAIVER,
    }


def _chunk_audit_allows_apply(report: dict[str, Any]) -> bool:
    audit = report.get("chunk_audit")
    if not isinstance(audit, dict):
        return False
    blocking = audit.get("blocking")
    return (
        audit.get("policy_version") == "yonote-chunk-audit-v1"
        and audit.get("status") == "GO"
        and isinstance(blocking, dict)
        and blocking.get("total") == 0
    )


def _chunk_audit(
    *,
    documents_count: int,
    loaded_documents: list[Any],
    current_yonote_records: list[dict[str, Any]],
    fresh_yonote_records: list[dict[str, Any]],
    merged_records: list[dict[str, Any]],
) -> dict[str, Any]:
    fresh_lengths = [_record_text_length(record) for record in fresh_yonote_records]
    merged_lengths = [_record_text_length(record) for record in merged_records]
    duplicate_text_groups = _duplicate_text_groups(fresh_yonote_records)
    chunks_by_document = Counter(
        _record_document_scope(record)
        for record in fresh_yonote_records
        if _record_document_scope(record) is not None
    )
    current_document_scopes = {
        scope
        for record in current_yonote_records
        if (scope := _record_document_scope(record)) is not None
    }
    findings = {
        "empty_text": sum(length == 0 for length in fresh_lengths),
        "too_short_under_20_chars": sum(0 < length < 20 for length in fresh_lengths),
        "oversized_over_max_chars": sum(
            length > MAX_CHUNK_CHARS for length in fresh_lengths
        ),
        "duplicate_text_groups": len(duplicate_text_groups),
        "missing_source_url": sum(
            not str(record.get("source_url") or "").strip()
            for record in fresh_yonote_records
        ),
        "missing_source_document_id": sum(
            not str(record.get("source_document_id") or "").strip()
            for record in fresh_yonote_records
        ),
        "missing_source_updated_at": sum(
            not str(record.get("source_document_updated_at") or "").strip()
            for record in fresh_yonote_records
        ),
    }
    (
        without_chunk_sample,
        existing_documents_without_chunks,
        new_documents_without_chunks,
        substantive_new_documents_without_chunks,
        unresolved_inventory_sample,
    ) = _classify_documents_without_chunks(
        loaded_documents=loaded_documents,
        chunk_document_scopes=set(chunks_by_document),
        current_document_scopes=current_document_scopes,
    )
    known_documents_without_chunks = (
        existing_documents_without_chunks
        + new_documents_without_chunks
        + substantive_new_documents_without_chunks
    )
    unclassified_documents_without_chunks = max(
        0,
        documents_count - len(chunks_by_document),
        known_documents_without_chunks,
    ) - known_documents_without_chunks
    documents_without_chunks = (
        known_documents_without_chunks + unclassified_documents_without_chunks
    )
    if unclassified_documents_without_chunks:
        without_chunk_sample.extend(
            unresolved_inventory_sample[:unclassified_documents_without_chunks]
        )
        del without_chunk_sample[20:]
    blocking_findings = {
        name: findings[name]
        for name in (
            "empty_text",
            "oversized_over_max_chars",
            "missing_source_url",
            "missing_source_document_id",
            "missing_source_updated_at",
        )
    }
    blocking_findings.update(
        {
            "existing_documents_without_chunks": existing_documents_without_chunks,
            "new_substantive_documents_without_chunks": (
                substantive_new_documents_without_chunks
            ),
            "unclassified_documents_without_chunks": (
                unclassified_documents_without_chunks
            ),
        }
    )
    advisory_findings = {
        "too_short_under_20_chars": findings["too_short_under_20_chars"],
        "duplicate_text_groups": findings["duplicate_text_groups"],
        "new_documents_without_chunks": new_documents_without_chunks,
    }
    blocking_total = sum(blocking_findings.values())
    advisory_total = sum(advisory_findings.values())
    return {
        "policy_version": "yonote-chunk-audit-v1",
        "status": "STOP" if blocking_total else "GO",
        "max_chunk_chars": MAX_CHUNK_CHARS,
        "fresh_lengths": _length_summary(fresh_lengths),
        "merged_lengths": _length_summary(merged_lengths),
        "blocking": {
            "total": blocking_total,
            "findings": blocking_findings,
        },
        "advisory": {
            "total": advisory_total,
            "findings": advisory_findings,
        },
        "documents": {
            "read": documents_count,
            "with_chunks": len(chunks_by_document),
            "without_chunks": documents_without_chunks,
            "existing_without_chunks": existing_documents_without_chunks,
            "new_without_chunks": new_documents_without_chunks,
            "new_substantive_without_chunks": (
                substantive_new_documents_without_chunks
            ),
            "unclassified_without_chunks": unclassified_documents_without_chunks,
            "without_chunks_sample": without_chunk_sample,
            "chunks_per_document": _length_summary(list(chunks_by_document.values())),
            "largest_documents": [
                {
                    "source_collection_id": scope[0],
                    "source_document_id": scope[1],
                    "chunks": count,
                }
                for scope, count in chunks_by_document.most_common(10)
            ],
        },
        "findings": findings,
        "duplicate_text_sample": duplicate_text_groups[:10],
        "warnings_total": sum(findings.values()),
    }


def _classify_documents_without_chunks(
    *,
    loaded_documents: list[Any],
    chunk_document_scopes: set[tuple[str, str]],
    current_document_scopes: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], int, int, int, list[dict[str, Any]]]:
    known_scopes = chunk_document_scopes | current_document_scopes
    loaded_by_scope: dict[tuple[str, str], Any] = {}
    sample: list[dict[str, Any]] = []
    unresolved_sample: list[dict[str, Any]] = []
    for document in loaded_documents:
        document_id = str(getattr(document, "id", "") or "").strip()
        collection_id = str(
            getattr(document, "collection_id", "") or ""
        ).strip()
        if not document_id:
            if len(unresolved_sample) < 20:
                unresolved_sample.append(
                    {
                        "source_collection_id": collection_id[:128],
                        "source_document_id": "",
                        "reason": "missing_document_identity",
                        "cleaned_chars": 0,
                    }
                )
            continue
        if not collection_id:
            candidates = sorted(scope for scope in known_scopes if scope[1] == document_id)
            if len(candidates) == 1:
                collection_id = candidates[0][0]
        if not collection_id:
            if len(unresolved_sample) < 20:
                unresolved_sample.append(
                    {
                        "source_collection_id": "",
                        "source_document_id": document_id[:128],
                        "reason": "missing_collection_identity",
                        "cleaned_chars": len(
                            clean_markdown_text(
                                str(getattr(document, "text", "") or "").strip()
                            )
                        ),
                    }
                )
            continue
        loaded_by_scope.setdefault((collection_id, document_id), document)

    existing = 0
    advisory_new = 0
    substantive_new = 0
    for scope in sorted(set(loaded_by_scope) - chunk_document_scopes):
        collection_id, document_id = scope
        document = loaded_by_scope[scope]
        raw_text = str(getattr(document, "text", "") or "").strip()
        cleaned_text = clean_markdown_text(raw_text)
        if scope in current_document_scopes:
            existing += 1
            reason = "existing_document_lost_all_chunks"
        elif len(cleaned_text) >= 20:
            substantive_new += 1
            reason = "new_substantive_document_without_chunks"
        else:
            advisory_new += 1
            reason = (
                "new_raw_empty_container"
                if not raw_text
                else "new_below_minimum_container"
            )
        if len(sample) < 20:
            sample.append(
                {
                    "source_collection_id": collection_id[:128],
                    "source_document_id": document_id[:128],
                    "reason": reason,
                    "cleaned_chars": len(cleaned_text),
                }
            )
    return (
        sample,
        existing,
        advisory_new,
        substantive_new,
        unresolved_sample,
    )


def _record_document_scope(record: dict[str, Any]) -> tuple[str, str] | None:
    collection_id = str(record.get("source_collection_id") or "").strip()
    document_id = str(record.get("source_document_id") or "").strip()
    if not document_id:
        return None
    return collection_id, document_id


def _record_text_length(record: dict[str, Any]) -> int:
    return len(str(record.get("text_clean") or record.get("text_raw") or "").strip())


def _length_summary(lengths: list[int]) -> dict[str, int]:
    if not lengths:
        return {"count": 0, "minimum": 0, "p50": 0, "p95": 0, "maximum": 0}
    ordered = sorted(lengths)
    return {
        "count": len(ordered),
        "minimum": ordered[0],
        "p50": _nearest_rank(ordered, 0.50),
        "p95": _nearest_rank(ordered, 0.95),
        "maximum": ordered[-1],
    }


def _nearest_rank(ordered: list[int], quantile: float) -> int:
    index = max(0, min(len(ordered) - 1, int(len(ordered) * quantile + 0.999999) - 1))
    return ordered[index]


def _duplicate_text_groups(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for record in records:
        normalized = " ".join(
            str(record.get("text_clean") or record.get("text_raw") or "")
            .casefold()
            .split()
        )
        if not normalized:
            continue
        grouped.setdefault(normalized, []).append(str(record.get("chunk_id") or ""))
    return [
        {"count": len(chunk_ids), "chunk_ids": sorted(chunk_ids)[:10]}
        for chunk_ids in sorted(grouped.values(), key=lambda values: (-len(values), values))
        if len(chunk_ids) > 1
    ]


def _published_count(records: list[dict[str, Any]]) -> int:
    return sum(
        str(record.get("status") or "published").strip() == "published"
        for record in records
    )


def _record_summary(record: dict[str, Any]) -> dict[str, Any]:
    heading_path = record.get("source_heading_path")
    heading = ""
    if isinstance(heading_path, list):
        heading = " / ".join(str(item).strip() for item in heading_path if str(item).strip())

    title = str(record.get("intent_name") or "").strip()
    if not title and isinstance(heading_path, list) and heading_path:
        title = str(heading_path[-1] or "").strip()
    if not title:
        title = str(record.get("topic") or record.get("chunk_id") or "Без названия").strip()

    return {
        "chunk_id": str(record.get("chunk_id") or ""),
        "title": title,
        "heading": heading,
        "collection": str(record.get("source_collection_name") or "").strip(),
        "forum": str(record.get("forum_normalized") or "").strip(),
        "category": str(record.get("category") or "").strip(),
        "source_url": str(record.get("source_url") or "").strip(),
        "updated_at": str(
            record.get("source_document_updated_at") or record.get("updated_at") or ""
        ).strip(),
        "text_preview": _text_preview(record.get("text_clean") or record.get("text_raw") or ""),
    }


def _changed_record_summary(
    old: dict[str, Any],
    fresh: dict[str, Any],
) -> dict[str, Any]:
    summary = _record_summary(fresh)
    preferred_fields = [
        field
        for field in COMPARE_FIELDS
        if _normalize_compare(old.get(field)) != _normalize_compare(fresh.get(field))
    ]
    extra_fields = sorted(
        field
        for field in set(old) | set(fresh)
        if field not in COMPARE_FIELDS
        and field != "chunk_id"
        and _normalize_compare(old.get(field)) != _normalize_compare(fresh.get(field))
    )
    summary["changed_fields"] = [*preferred_fields, *extra_fields]
    summary["before_text"] = _text_preview(
        old.get("text_clean") or old.get("text_raw") or ""
    )
    summary["after_text"] = _text_preview(
        fresh.get("text_clean") or fresh.get("text_raw") or ""
    )
    return summary


def _text_preview(value: Any, *, limit: int = 360) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _record_changed(old: dict[str, Any], fresh: dict[str, Any]) -> bool:
    return _canonical_json_bytes(old) != _canonical_json_bytes(fresh)


def _preserve_unchanged_yonote_records(
    current_records: list[dict[str, Any]],
    fresh_yonote_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reuse exact old records when only pull-time metadata changed.

    ``extraction_date`` describes acquisition, not a knowledge revision. For
    documents without a provider ``updatedAt``, ``updated_at`` also falls back
    to that acquisition date. Reusing the old object keeps the merged seed and
    its hash stable across next-day Preview runs while any content or source
    metadata change still selects the fresh record.
    """

    current_by_id = {
        str(record.get("chunk_id") or ""): record
        for record in current_records
        if str(record.get("source_type") or "") == "yonote"
        and str(record.get("chunk_id") or "")
    }
    stabilized: list[dict[str, Any]] = []
    for fresh in fresh_yonote_records:
        old = current_by_id.get(str(fresh.get("chunk_id") or ""))
        if old is not None and _same_without_acquisition_metadata(old, fresh):
            stabilized.append(dict(old))
        else:
            stabilized.append(dict(fresh))
    return stabilized


def _same_without_acquisition_metadata(
    old: dict[str, Any],
    fresh: dict[str, Any],
) -> bool:
    ignored_fields = {"extraction_date"}
    if _uses_acquisition_date_as_updated_at(old) and _uses_acquisition_date_as_updated_at(
        fresh
    ):
        ignored_fields.add("updated_at")
    old_stable = {key: value for key, value in old.items() if key not in ignored_fields}
    fresh_stable = {
        key: value for key, value in fresh.items() if key not in ignored_fields
    }
    return _canonical_json_bytes(old_stable) == _canonical_json_bytes(fresh_stable)


def _uses_acquisition_date_as_updated_at(record: dict[str, Any]) -> bool:
    if str(record.get("source_document_updated_at") or "").strip():
        return False
    extraction_date = str(record.get("extraction_date") or "").strip()
    return bool(extraction_date) and str(record.get("updated_at") or "").strip() == (
        extraction_date
    )


def _normalize_compare(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value or "").strip()


def _count_field(records: list[dict[str, Any]], field: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for record in records:
        value = str(record.get(field) or "unknown").strip() or "unknown"
        counter[value] += 1
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:30])


def _human_message(*, applied: bool, changed: int, added: int, removed: int) -> str:
    action = "Yonote applied to KB seed" if applied else "Yonote preview loaded"
    return f"{action}: added={added}, changed={changed}, removed={removed}"


def _load_seed_records(path: Path) -> list[dict[str, Any]]:
    return _parse_seed_records(path.read_bytes())


def _parse_seed_records(rendered: bytes) -> list[dict[str, Any]]:
    payload = json.loads(rendered.decode("utf-8-sig"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("knowledge_base_seed.json must contain a JSON array of objects")
    return [dict(item) for item in payload]
