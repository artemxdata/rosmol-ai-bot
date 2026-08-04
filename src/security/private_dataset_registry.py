from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

REGISTRY_SCHEMA = "private-dataset-registry-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_DATA_ROOT = (PROJECT_ROOT / "data" / "private").resolve()
DEFAULT_REGISTRY_PATH = PRIVATE_DATA_ROOT / "_registry" / "datasets.json"

DATASET_KINDS = frozenset(
    {
        "raw_ticket_export",
        "raw_operator_export",
        "yonote_snapshot",
        "derived_analysis",
        "review_queue",
        "gold_ticket",
        "eval_report",
        "runtime_evidence",
        "safe_aggregate",
        "legacy_private",
    }
)
DATASET_PURPOSES = frozenset(
    {
        "product_quality",
        "operator_quality",
        "knowledge_snapshot",
        "runtime_acceptance",
        "content_curation",
    }
)
PRIVACY_CLASSES = frozenset(
    {
        "raw_restricted",
        "pseudonymized_private",
        "deidentified_private",
        "aggregate",
    }
)
EXPORT_CLASSES = frozenset({"private_only", "aggregate_allowlisted"})
DATASET_STATES = frozenset(
    {
        "draft",
        "reviewing",
        "frozen",
        "superseded",
        "purge_pending",
        "purged",
        "quarantined",
    }
)
EVALUATION_ROLES = frozenset(
    {
        "none",
        "calibration_sanity",
        "calibration",
        "validation",
        "holdout",
        "exposed_calibration",
        "regression",
    }
)
HUMAN_REVIEW_STATUSES = frozenset(
    {"not_required", "pending", "complete", "rejected"}
)
OWNER_ROLES = frozenset(
    {
        "data_owner",
        "quality_owner",
        "content_owner",
        "security_owner",
        "engineering_owner",
    }
)
HOLD_REASONS = frozenset(
    {
        "none",
        "active_review",
        "sealed_evaluation",
        "security_incident",
        "legal_hold",
        "owner_hold",
    }
)

HASH_FIELDS = (
    "artifact_manifest_sha256",
    "source_snapshot_sha256",
    "kb_seed_sha256",
    "selection_manifest_sha256",
    "review_manifest_sha256",
    "case_ids_sha256",
    "duplicate_exclusion_sha256",
)

ENTRY_FIELDS = frozenset(
    {
        "dataset_id",
        "version",
        "relative_root",
        "kind",
        "purpose",
        "privacy_class",
        "export_class",
        "state",
        "created_at",
        "lifecycle_updated_at",
        "frozen_at",
        "delete_after",
        "retention_policy_ref",
        "owner_role",
        "source_dataset_ids",
        "requires_parent_bytes",
        "contains_raw_text",
        "pii_possible",
        "contains_operator_answers",
        "evaluation_role",
        "independent_evaluation",
        "human_review_status",
        "cases_total",
        "hashes",
        "supersedes",
        "superseded_by",
        "hold_reason",
        "frozen_payload_sha256",
    }
)

_DATASET_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{2,63}$")
_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_POLICY_REF_RE = re.compile(r"^[a-z][a-z0-9._:-]{2,95}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{12,64}$")
_REF_RE = re.compile(
    rf"{_DATASET_ID_RE.pattern.removeprefix('^').removesuffix('$')}@"
    rf"{_VERSION_RE.pattern.removeprefix('^').removesuffix('$')}"
)

_FROZEN_HASH_EXCLUDED_FIELDS = frozenset(
    {
        "state",
        "lifecycle_updated_at",
        "frozen_at",
        "delete_after",
        "retention_policy_ref",
        "superseded_by",
        "hold_reason",
        "frozen_payload_sha256",
    }
)


def empty_registry(*, now: datetime | None = None) -> dict[str, Any]:
    timestamp = _iso_timestamp(now or datetime.now(UTC), field="updated_at")
    return {"schema": REGISTRY_SCHEMA, "updated_at": timestamp, "datasets": []}


def dataset_ref(entry: Mapping[str, Any]) -> str:
    return f"{entry.get('dataset_id')}@{entry.get('version')}"


def validate_registry(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"schema", "updated_at", "datasets"}:
        raise ValueError("registry must contain only schema, updated_at, and datasets")
    if payload.get("schema") != REGISTRY_SCHEMA:
        raise ValueError("unsupported private dataset registry schema")
    _parse_timestamp(payload.get("updated_at"), field="updated_at")
    raw_datasets = payload.get("datasets")
    if not isinstance(raw_datasets, list):
        raise ValueError("registry datasets must be an array")

    datasets: list[dict[str, Any]] = []
    by_ref: dict[str, dict[str, Any]] = {}
    live_roots: list[tuple[tuple[str, ...], str]] = []
    for index, value in enumerate(raw_datasets, start=1):
        entry = _validate_entry(value, location=f"datasets[{index}]")
        ref = dataset_ref(entry)
        if ref in by_ref:
            raise ValueError(f"duplicate dataset reference: {ref}")
        if entry["state"] != "purged":
            root_parts = _relative_root_key(entry["relative_root"])
            for existing_parts, existing_root in live_roots:
                if _path_parts_overlap(root_parts, existing_parts):
                    raise ValueError(
                        "live dataset relative_root values must not be equal, "
                        "case aliases, or ancestor/descendant paths: "
                        f"{existing_root!r} and {entry['relative_root']!r}"
                    )
            live_roots.append((root_parts, entry["relative_root"]))
        datasets.append(entry)
        by_ref[ref] = entry

    _validate_lineage(by_ref)
    _validate_frozen_hashes(datasets)
    return {
        "schema": REGISTRY_SCHEMA,
        "updated_at": str(payload["updated_at"]),
        "datasets": datasets,
    }


def load_registry(
    path: Path = DEFAULT_REGISTRY_PATH,
    *,
    private_root: Path = PRIVATE_DATA_ROOT,
    allow_missing: bool = True,
) -> dict[str, Any]:
    registry_path = _private_registry_path(path, private_root=private_root)
    if not registry_path.exists():
        if allow_missing:
            return empty_registry()
        raise ValueError("private dataset registry does not exist")
    _ensure_regular_single_link(registry_path, label="registry")
    try:
        with registry_path.open("r", encoding="utf-8-sig") as source:
            payload = json.load(
                source,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("private dataset registry must be readable UTF-8 JSON") from exc
    return validate_registry(payload)


def save_registry(
    path: Path,
    payload: Mapping[str, Any],
    *,
    private_root: Path = PRIVATE_DATA_ROOT,
) -> None:
    registry_path = _private_registry_path(path, private_root=private_root)
    normalized = validate_registry(dict(payload))
    if registry_path.exists():
        _ensure_regular_single_link(registry_path, label="registry")
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=registry_path.parent,
        prefix=f".{registry_path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                descriptor = -1
                destination.write(encoded)
                destination.flush()
                os.fsync(destination.fileno())
            if registry_path.exists():
                _ensure_regular_single_link(registry_path, label="registry")
            os.replace(temporary, registry_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def register_dataset(
    registry: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized = validate_registry(dict(registry))
    candidate = _validate_entry(dict(entry), location="new dataset")
    ref = dataset_ref(candidate)
    if any(dataset_ref(item) == ref for item in normalized["datasets"]):
        raise ValueError(f"dataset already registered: {ref}")
    if candidate["state"] != "draft":
        raise ValueError("new datasets must be registered in draft state")
    if candidate["frozen_payload_sha256"] is not None:
        raise ValueError("draft dataset cannot have a frozen payload hash")
    normalized["datasets"].append(candidate)
    normalized["datasets"].sort(key=lambda item: dataset_ref(item))
    normalized["updated_at"] = _iso_timestamp(now or datetime.now(UTC), field="updated_at")
    return validate_registry(normalized)


def start_review(
    registry: Mapping[str, Any],
    ref: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    return _transition(
        registry,
        ref,
        expected_states={"draft"},
        target_state="reviewing",
        now=now,
    )


def complete_human_review(
    registry: Mapping[str, Any],
    ref: str,
    *,
    gold_artifact_path: Path,
    private_root: Path = PRIVATE_DATA_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind a reviewed GoldTicket JSONL artifact to one reviewing dataset.

    The transition is deliberately filesystem-backed: a caller cannot attest review
    completion with a hash string alone. The sealed records, selection membership,
    count, component IDs, and registry provenance are checked before the registry is
    changed.
    """

    normalized = validate_registry(dict(registry))
    result = deepcopy(normalized)
    entry = _entry_by_ref(result, ref)
    if entry["kind"] != "gold_ticket":
        raise ValueError("human review completion is supported only for gold_ticket")
    if entry["state"] != "reviewing":
        raise ValueError("gold_ticket must be in reviewing state to complete review")
    if entry["human_review_status"] != "pending":
        raise ValueError("gold_ticket human review must be pending")
    if entry["hold_reason"] not in {"none", "active_review"}:
        raise ValueError("dataset has a hold that review completion cannot clear")

    evidence = _verify_gold_dataset_artifacts(
        entry,
        private_root=private_root,
        gold_artifact_path=gold_artifact_path,
        verify_review_manifest_hash=False,
    )
    timestamp = _iso_timestamp(
        now or datetime.now(UTC), field="lifecycle_updated_at"
    )
    entry["hashes"]["review_manifest_sha256"] = evidence["gold_artifact_sha256"]
    entry["hashes"]["case_ids_sha256"] = evidence["case_ids_sha256"]
    entry["human_review_status"] = "complete"
    entry["hold_reason"] = "none"
    entry["lifecycle_updated_at"] = timestamp
    result["updated_at"] = timestamp
    return validate_registry(result)


def freeze_dataset(
    registry: Mapping[str, Any],
    ref: str,
    *,
    private_root: Path = PRIVATE_DATA_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized = validate_registry(dict(registry))
    result = deepcopy(normalized)
    entry = _entry_by_ref(result, ref)
    if entry["state"] not in {"draft", "reviewing"}:
        raise ValueError("only draft or reviewing datasets can be frozen")
    if entry["human_review_status"] == "pending":
        raise ValueError("dataset cannot be frozen while human review is pending")
    if entry["kind"] != "gold_ticket":
        raise ValueError("artifact verification is unsupported for this dataset kind")
    if entry["state"] != "reviewing":
        raise ValueError("completed gold_ticket must pass the reviewing lifecycle")
    if entry["human_review_status"] != "complete":
        raise ValueError("gold_ticket requires completed human review before freeze")
    if entry["hold_reason"] != "none":
        raise ValueError("dataset cannot be frozen while a hold is active")
    _verify_gold_dataset_artifacts(
        entry,
        private_root=private_root,
        verify_review_manifest_hash=True,
    )
    timestamp = _iso_timestamp(now or datetime.now(UTC), field="frozen_at")
    entry["state"] = "frozen"
    entry["frozen_at"] = timestamp
    entry["lifecycle_updated_at"] = timestamp
    entry["frozen_payload_sha256"] = _frozen_payload_sha256(entry)
    result["updated_at"] = timestamp
    return validate_registry(result)


def supersede_dataset(
    registry: Mapping[str, Any],
    ref: str,
    successor_ref: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized = validate_registry(dict(registry))
    result = deepcopy(normalized)
    current = _entry_by_ref(result, ref)
    successor = _entry_by_ref(result, successor_ref)
    if ref == successor_ref:
        raise ValueError("dataset cannot supersede itself")
    if current["dataset_id"] != successor["dataset_id"]:
        raise ValueError("successor must be another version of the same dataset_id")
    if current["state"] != "frozen" or successor["state"] != "frozen":
        raise ValueError("both current and successor datasets must be frozen")
    if successor["supersedes"] != ref:
        raise ValueError("successor must declare the current dataset in supersedes")
    if current["superseded_by"] is not None:
        raise ValueError("dataset is already linked to a successor")
    timestamp = _iso_timestamp(now or datetime.now(UTC), field="lifecycle_updated_at")
    current["state"] = "superseded"
    current["superseded_by"] = successor_ref
    current["lifecycle_updated_at"] = timestamp
    result["updated_at"] = timestamp
    return validate_registry(result)


def build_retention_plan(
    registry: Mapping[str, Any],
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    normalized = validate_registry(dict(registry))
    instant = as_of or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    instant = instant.astimezone(UTC)
    by_ref = {dataset_ref(entry): entry for entry in normalized["datasets"]}
    candidates: list[str] = []
    blocked: list[dict[str, Any]] = []
    ignored: list[str] = []
    for ref, entry in sorted(by_ref.items()):
        if entry["state"] == "purged":
            ignored.append(ref)
            continue
        reasons: list[str] = []
        if entry["retention_policy_ref"] == "unapproved" or entry["delete_after"] is None:
            reasons.append("retention_unapproved")
        else:
            delete_after = _parse_timestamp(
                entry["delete_after"], field=f"{ref}.delete_after"
            )
            if delete_after > instant:
                ignored.append(ref)
                continue
        if entry["state"] not in {"superseded", "quarantined", "purge_pending"}:
            reasons.append("dataset_not_superseded_or_quarantined")
        if entry["hold_reason"] != "none":
            reasons.append(f"hold:{entry['hold_reason']}")
        active_children = sorted(
            child_ref
            for child_ref, child in by_ref.items()
            if ref in child["source_dataset_ids"]
            and child["requires_parent_bytes"]
            and child["state"] not in {"superseded", "purged", "quarantined"}
        )
        if active_children:
            reasons.append("active_children_require_parent_bytes")
        if reasons:
            blocked.append(
                {
                    "dataset_ref": ref,
                    "reasons": reasons,
                    "blocking_children": active_children,
                }
            )
        else:
            candidates.append(ref)
    return {
        "schema": "private-dataset-retention-plan-v1",
        "as_of": _iso_timestamp(instant, field="as_of"),
        "deletion_performed": False,
        "candidates": candidates,
        "blocked": blocked,
        "ignored": ignored,
        "counts": {
            "candidates": len(candidates),
            "blocked": len(blocked),
            "ignored": len(ignored),
        },
    }


def inventory_private_datasets(
    private_root: Path = PRIVATE_DATA_ROOT,
    *,
    registry: Mapping[str, Any] | None = None,
    areas: Iterable[str] = ("tickets", "operator_qa", "yonote", "eval"),
) -> dict[str, Any]:
    root = private_root.expanduser().resolve()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("private data root must be an existing regular directory")
    registered_roots: set[str] = set()
    if registry is not None:
        normalized = validate_registry(dict(registry))
        registered_roots = {
            entry["relative_root"]
            for entry in normalized["datasets"]
            if entry["state"] != "purged"
        }

    rows: list[dict[str, Any]] = []
    unsafe_entries_skipped = 0
    for area_name in sorted(set(areas)):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", area_name):
            raise ValueError("inventory areas must contain safe directory names")
        area = root / area_name
        if not area.exists():
            continue
        if _is_link_or_reparse_point(area) or not area.is_dir():
            raise ValueError(f"inventory area is not a regular directory: {area_name}")
        area_files, area_directories, area_skipped = _directory_entries(area)
        unsafe_entries_skipped += area_skipped
        candidates = [area]
        candidates.extend(
            sorted(area_directories, key=lambda item: item.name.casefold())
        )
        for candidate in candidates:
            if candidate == area:
                files = area_files
                skipped = 0
            else:
                files, skipped = _regular_files(candidate)
                unsafe_entries_skipped += skipped
            if candidate == area and not files:
                continue
            extensions = Counter((path.suffix.casefold() or "[none]") for path in files)
            relative = candidate.relative_to(root).as_posix()
            relative_root = f"data/private/{relative}"
            rows.append(
                {
                    "relative_root": relative_root,
                    "files_count": len(files),
                    "bytes_total": sum(path.stat().st_size for path in files),
                    "extensions": dict(sorted(extensions.items())),
                    "artifact_manifest_present": _is_regular_file(
                        candidate / "artifact_manifest.json"
                    ),
                    "unsafe_entries_skipped": skipped,
                    "registered": relative_root in registered_roots,
                }
            )
    return {
        "schema": "private-dataset-inventory-v1",
        "content_read": False,
        "mutation_performed": False,
        "roots": rows,
        "counts": {
            "roots": len(rows),
            "registered": sum(row["registered"] for row in rows),
            "unregistered": sum(not row["registered"] for row in rows),
            "unsafe_entries_skipped": unsafe_entries_skipped,
        },
    }


def _regular_files(root: Path) -> tuple[list[Path], int]:
    files: list[Path] = []
    skipped = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        direct_files, direct_directories, direct_skipped = _directory_entries(directory)
        files.extend(direct_files)
        pending.extend(direct_directories)
        skipped += direct_skipped
    return files, skipped


def _directory_entries(directory: Path) -> tuple[list[Path], list[Path], int]:
    files: list[Path] = []
    directories: list[Path] = []
    skipped = 0
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        raise ValueError("could not inventory private dataset directory") from exc
    for entry in entries:
        path = Path(entry.path)
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            skipped += 1
            continue
        if entry.is_symlink() or _stat_is_reparse_point(info):
            skipped += 1
        elif entry.is_file(follow_symlinks=False):
            files.append(path)
        elif entry.is_dir(follow_symlinks=False):
            directories.append(path)
        else:
            skipped += 1
    return files, directories, skipped


def _is_regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return path.is_file() and not path.is_symlink() and not _stat_is_reparse_point(info)


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or _stat_is_reparse_point(info)


def _stat_is_reparse_point(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0))
    return bool(attributes & 0x400)


def _transition(
    registry: Mapping[str, Any],
    ref: str,
    *,
    expected_states: set[str],
    target_state: str,
    now: datetime | None,
) -> dict[str, Any]:
    normalized = validate_registry(dict(registry))
    result = deepcopy(normalized)
    entry = _entry_by_ref(result, ref)
    if entry["state"] not in expected_states:
        expected = ", ".join(sorted(expected_states))
        raise ValueError(f"dataset must be in one of these states: {expected}")
    timestamp = _iso_timestamp(now or datetime.now(UTC), field="lifecycle_updated_at")
    entry["state"] = target_state
    entry["lifecycle_updated_at"] = timestamp
    result["updated_at"] = timestamp
    return validate_registry(result)


def _validate_entry(value: Any, *, location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != ENTRY_FIELDS:
        missing = sorted(ENTRY_FIELDS - set(value)) if isinstance(value, dict) else []
        extra = sorted(set(value) - ENTRY_FIELDS) if isinstance(value, dict) else []
        raise ValueError(f"{location} has invalid fields; missing={missing}, extra={extra}")
    entry = deepcopy(value)
    if not _DATASET_ID_RE.fullmatch(str(entry["dataset_id"])):
        raise ValueError(f"{location}.dataset_id is invalid")
    if not _VERSION_RE.fullmatch(str(entry["version"])):
        raise ValueError(f"{location}.version is invalid")
    _validate_relative_root(entry["relative_root"], location=location)
    _enum(entry["kind"], DATASET_KINDS, field=f"{location}.kind")
    _enum(entry["purpose"], DATASET_PURPOSES, field=f"{location}.purpose")
    _enum(entry["privacy_class"], PRIVACY_CLASSES, field=f"{location}.privacy_class")
    _enum(entry["export_class"], EXPORT_CLASSES, field=f"{location}.export_class")
    _enum(entry["state"], DATASET_STATES, field=f"{location}.state")
    _enum(entry["evaluation_role"], EVALUATION_ROLES, field=f"{location}.evaluation_role")
    _enum(
        entry["human_review_status"],
        HUMAN_REVIEW_STATUSES,
        field=f"{location}.human_review_status",
    )
    _enum(entry["owner_role"], OWNER_ROLES, field=f"{location}.owner_role")
    _enum(entry["hold_reason"], HOLD_REASONS, field=f"{location}.hold_reason")
    _parse_timestamp(entry["created_at"], field=f"{location}.created_at")
    _parse_timestamp(
        entry["lifecycle_updated_at"], field=f"{location}.lifecycle_updated_at"
    )
    if entry["frozen_at"] is not None:
        _parse_timestamp(entry["frozen_at"], field=f"{location}.frozen_at")
    if entry["delete_after"] is not None:
        _parse_timestamp(entry["delete_after"], field=f"{location}.delete_after")
    policy_ref = str(entry["retention_policy_ref"])
    if not _POLICY_REF_RE.fullmatch(policy_ref):
        raise ValueError(f"{location}.retention_policy_ref is invalid")
    if (entry["delete_after"] is None) != (policy_ref == "unapproved"):
        raise ValueError(
            f"{location} must pair delete_after=null with retention_policy_ref=unapproved"
        )
    for field in (
        "requires_parent_bytes",
        "contains_raw_text",
        "pii_possible",
        "contains_operator_answers",
        "independent_evaluation",
    ):
        if type(entry[field]) is not bool:
            raise ValueError(f"{location}.{field} must be a boolean")
    if isinstance(entry["cases_total"], bool) or not isinstance(entry["cases_total"], int):
        raise ValueError(f"{location}.cases_total must be an integer")
    if entry["cases_total"] < 0:
        raise ValueError(f"{location}.cases_total must be non-negative")
    if not isinstance(entry["source_dataset_ids"], list):
        raise ValueError(f"{location}.source_dataset_ids must be an array")
    if len(entry["source_dataset_ids"]) != len(set(entry["source_dataset_ids"])):
        raise ValueError(f"{location}.source_dataset_ids contains duplicates")
    for ref in entry["source_dataset_ids"]:
        _validate_ref(ref, field=f"{location}.source_dataset_ids")
    for field in ("supersedes", "superseded_by"):
        if entry[field] is not None:
            _validate_ref(entry[field], field=f"{location}.{field}")
    if not isinstance(entry["hashes"], dict) or set(entry["hashes"]) != set(HASH_FIELDS):
        raise ValueError(f"{location}.hashes must contain the exact v1 hash fields")
    for field in HASH_FIELDS:
        value = entry["hashes"][field]
        if value is not None and not _SHA256_RE.fullmatch(str(value)):
            raise ValueError(f"{location}.hashes.{field} must be SHA-256 or null")
    frozen_hash = entry["frozen_payload_sha256"]
    if frozen_hash is not None and not _SHA256_RE.fullmatch(str(frozen_hash)):
        raise ValueError(f"{location}.frozen_payload_sha256 is invalid")
    if entry["state"] in {"frozen", "superseded"}:
        if entry["frozen_at"] is None or frozen_hash is None:
            raise ValueError(f"{location} frozen lifecycle state lacks freeze evidence")
    elif entry["state"] in {"purge_pending", "purged", "quarantined"}:
        if (frozen_hash is None) != (entry["frozen_at"] is None):
            raise ValueError(f"{location} has incomplete optional freeze evidence")
    elif frozen_hash is not None or entry["frozen_at"] is not None:
        raise ValueError(f"{location} non-frozen lifecycle state has freeze evidence")
    if entry["state"] == "superseded" and entry["superseded_by"] is None:
        raise ValueError(f"{location} superseded state requires superseded_by")
    if entry["state"] not in {"superseded", "purge_pending", "purged"}:
        if entry["superseded_by"] is not None:
            raise ValueError(f"{location} superseded_by is invalid for this state")
    if entry["kind"] in {"raw_ticket_export", "raw_operator_export"}:
        if (
            entry["privacy_class"] != "raw_restricted"
            or entry["export_class"] != "private_only"
            or not entry["contains_raw_text"]
            or not entry["pii_possible"]
        ):
            raise ValueError(f"{location} raw dataset classification is unsafe")
        if entry["kind"] == "raw_operator_export" and not entry[
            "contains_operator_answers"
        ]:
            raise ValueError(
                f"{location} raw operator export must declare operator answers"
            )
    if entry["export_class"] == "aggregate_allowlisted":
        if entry["kind"] != "safe_aggregate" or entry["privacy_class"] != "aggregate" or any(
            entry[field]
            for field in (
                "contains_raw_text",
                "pii_possible",
                "contains_operator_answers",
            )
        ):
            raise ValueError(f"{location} aggregate export classification is unsafe")
    elif entry["kind"] == "safe_aggregate" or entry["privacy_class"] == "aggregate":
        raise ValueError(
            f"{location} safe aggregate must be aggregate and explicitly allowlisted"
        )
    if entry["independent_evaluation"] and entry["evaluation_role"] != "holdout":
        raise ValueError(f"{location} independent evaluation must be a holdout")
    if entry["evaluation_role"] in {"calibration_sanity", "exposed_calibration"}:
        if entry["independent_evaluation"]:
            raise ValueError(f"{location} calibration cannot be independent")
    if entry["kind"] == "gold_ticket":
        _validate_gold_ticket(entry, location=location)
    return entry


def _validate_gold_ticket(entry: Mapping[str, Any], *, location: str) -> None:
    if entry["cases_total"] <= 0:
        raise ValueError(f"{location} gold_ticket must contain cases")
    if entry["privacy_class"] != "deidentified_private" or entry["export_class"] != "private_only":
        raise ValueError(f"{location} gold_ticket must remain deidentified private")
    if entry["state"] == "draft":
        if entry["human_review_status"] != "pending":
            raise ValueError(f"{location} draft gold_ticket review must be pending")
        return
    if entry["state"] == "reviewing":
        if entry["human_review_status"] not in {"pending", "complete"}:
            raise ValueError(f"{location} reviewing gold_ticket has invalid review state")
        return
    if entry["human_review_status"] != "complete":
        raise ValueError(f"{location} gold_ticket must be human reviewed")
    required_hashes = (
        "source_snapshot_sha256",
        "kb_seed_sha256",
        "selection_manifest_sha256",
        "review_manifest_sha256",
        "case_ids_sha256",
        "duplicate_exclusion_sha256",
    )
    if any(entry["hashes"][field] is None for field in required_hashes):
        raise ValueError(f"{location} gold_ticket lacks exact provenance hashes")


def _verify_gold_dataset_artifacts(
    entry: Mapping[str, Any],
    *,
    private_root: Path,
    gold_artifact_path: Path | None = None,
    verify_review_manifest_hash: bool,
) -> dict[str, str]:
    required_hashes = (
        "artifact_manifest_sha256",
        "source_snapshot_sha256",
        "kb_seed_sha256",
        "selection_manifest_sha256",
        "case_ids_sha256",
        "duplicate_exclusion_sha256",
    )
    if any(entry["hashes"][field] is None for field in required_hashes):
        raise ValueError("gold_ticket lacks exact provenance hashes")
    if verify_review_manifest_hash and entry["hashes"]["review_manifest_sha256"] is None:
        raise ValueError("gold_ticket lacks a sealed review artifact hash")

    dataset_root = _dataset_artifact_root(entry, private_root=private_root)
    _verify_artifact_tree(dataset_root)
    selection_path = dataset_root / f"{entry['dataset_id']}_selection.json"
    expected_gold_path = dataset_root / f"{entry['dataset_id']}_gold.jsonl"
    if gold_artifact_path is not None:
        supplied = _strict_artifact_path(
            gold_artifact_path,
            dataset_root=dataset_root,
            label="sealed GoldTicket artifact",
        )
        if supplied != expected_gold_path:
            raise ValueError(
                "sealed GoldTicket artifact must use the governed *_gold.jsonl name"
            )
    _ensure_regular_single_link(selection_path, label="selection manifest")
    _ensure_regular_single_link(expected_gold_path, label="sealed GoldTicket artifact")

    selection_digest = _file_sha256(selection_path)
    if selection_digest != entry["hashes"]["selection_manifest_sha256"]:
        raise ValueError("selection manifest differs from registry evidence")
    selection = _read_strict_json_object(selection_path, label="selection manifest")
    selected, normalized_source_sha256 = _validate_selection_manifest(
        selection, entry=entry
    )
    gold = _read_sealed_gold_records(
        expected_gold_path,
        entry=entry,
        normalized_source_sha256=normalized_source_sha256,
    )
    if set(selected) != set(gold):
        raise ValueError("sealed GoldTicket membership differs from selection manifest")
    if any(selected[case_id] != gold[case_id] for case_id in selected):
        raise ValueError("sealed GoldTicket duplicate components differ from selection")

    case_ids_sha256 = hashlib.sha256(
        ("\n".join(sorted(gold)) + "\n").encode("utf-8")
    ).hexdigest()
    if case_ids_sha256 != entry["hashes"]["case_ids_sha256"]:
        raise ValueError("sealed GoldTicket case IDs differ from registry evidence")
    gold_digest = _file_sha256(expected_gold_path)
    if (
        verify_review_manifest_hash
        and gold_digest != entry["hashes"]["review_manifest_sha256"]
    ):
        raise ValueError("sealed GoldTicket artifact differs from registry evidence")
    return {
        "gold_artifact_sha256": gold_digest,
        "case_ids_sha256": case_ids_sha256,
    }


def _dataset_artifact_root(
    entry: Mapping[str, Any],
    *,
    private_root: Path,
) -> Path:
    expanded_root = private_root.expanduser()
    if _is_link_or_reparse_point(expanded_root) or not expanded_root.is_dir():
        raise ValueError("private data root must be an existing regular directory")
    root = expanded_root.resolve()
    relative = PurePosixPath(entry["relative_root"])
    current = root
    for part in relative.parts[2:]:
        current = current / part
        if _is_link_or_reparse_point(current):
            raise ValueError("dataset artifact root must not traverse links")
    if not current.is_dir():
        raise ValueError("dataset artifact root does not exist")
    return current


def _strict_artifact_path(path: Path, *, dataset_root: Path, label: str) -> Path:
    expanded = path.expanduser()
    if _is_link_or_reparse_point(expanded):
        raise ValueError(f"{label} must not be a link")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} does not exist") from exc
    if resolved.parent != dataset_root:
        raise ValueError(f"{label} must stay directly under the dataset root")
    return resolved


def _verify_artifact_tree(root: Path) -> None:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise ValueError("could not inspect dataset artifact tree") from exc
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError("could not inspect dataset artifact") from exc
            if entry.is_symlink() or _stat_is_reparse_point(info):
                raise ValueError("dataset artifact tree contains a link")
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                try:
                    links = path.stat().st_nlink
                except OSError as exc:
                    raise ValueError("could not inspect dataset artifact links") from exc
                if links != 1:
                    raise ValueError("dataset artifact tree contains a hardlink")
            else:
                raise ValueError("dataset artifact tree contains a special file")


def _validate_selection_manifest(
    payload: Mapping[str, Any],
    *,
    entry: Mapping[str, Any],
) -> tuple[dict[str, str], str | None]:
    if payload.get("schema_version") != "gold-ticket-selection.v1":
        raise ValueError("unsupported selection manifest schema")
    if payload.get("dataset_id") != entry["dataset_id"]:
        raise ValueError("selection manifest dataset ID mismatch")
    if payload.get("selected_total") != entry["cases_total"]:
        raise ValueError("selection manifest case count mismatch")
    records = payload.get("records")
    if not isinstance(records, list) or len(records) != entry["cases_total"]:
        raise ValueError("selection manifest records are incomplete")
    declared_hash = payload.get("selection_sha256")
    hash_payload = dict(payload)
    hash_payload.pop("selection_sha256", None)
    if declared_hash != _canonical_sha256(hash_payload):
        raise ValueError("selection manifest canonical hash mismatch")

    normalized_source_sha256: str | None = None
    source_artifacts = payload.get("source_artifacts")
    if source_artifacts is not None:
        if not isinstance(source_artifacts, Mapping):
            raise ValueError("selection source artifacts must be an object")
        for key, value in source_artifacts.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or _SHA256_RE.fullmatch(value) is None
            ):
                raise ValueError("selection source artifact evidence is invalid")
        source_manifest_sha256 = source_artifacts.get("artifact_manifest_sha256")
        if (
            source_manifest_sha256 is not None
            and source_manifest_sha256
            != entry["hashes"]["artifact_manifest_sha256"]
        ):
            raise ValueError("selection source manifest differs from registry evidence")
        if _canonical_sha256(dict(source_artifacts)) != entry["hashes"][
            "source_snapshot_sha256"
        ]:
            raise ValueError("selection source snapshot differs from registry evidence")
        candidate = source_artifacts.get("normalized_source_sha256")
        if candidate is not None:
            normalized_source_sha256 = candidate

    selected: dict[str, str] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("selection manifest record must be an object")
        case_id = record.get("case_id_hash")
        component_id = record.get("duplicate_component_id")
        if not isinstance(case_id, str) or _CONTENT_HASH_RE.fullmatch(case_id) is None:
            raise ValueError("selection manifest case ID is invalid")
        if (
            not isinstance(component_id, str)
            or _CONTENT_HASH_RE.fullmatch(component_id) is None
        ):
            raise ValueError("selection manifest component ID is invalid")
        if case_id in selected or component_id in selected.values():
            raise ValueError("selection manifest contains duplicate membership")
        selected[case_id] = component_id
    return selected, normalized_source_sha256


def _read_sealed_gold_records(
    path: Path,
    *,
    entry: Mapping[str, Any],
    normalized_source_sha256: str | None,
) -> dict[str, str]:
    from eval.gold_ticket import GoldTicketV1

    records: dict[str, str] = {}
    components: set[str] = set()
    expected_split = {
        "calibration_sanity": "calibration",
        "calibration": "calibration",
        "exposed_calibration": "calibration",
        "regression": "calibration",
        "validation": "validation",
        "holdout": "holdout",
    }.get(entry["evaluation_role"])
    try:
        source = path.open("r", encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError("sealed GoldTicket artifact is not readable UTF-8 JSONL") from exc
    with source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                raise ValueError("sealed GoldTicket artifact contains an empty line")
            try:
                payload = json.loads(
                    line,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_non_finite,
                )
                ticket = GoldTicketV1.model_validate(payload)
            except (ValueError, TypeError, json.JSONDecodeError):
                raise ValueError(
                    f"invalid sealed GoldTicket record at line {line_number}"
                ) from None
            if ticket.dataset_id != entry["dataset_id"]:
                raise ValueError("sealed GoldTicket dataset ID mismatch")
            if expected_split is not None and ticket.split.value != expected_split:
                raise ValueError("sealed GoldTicket split differs from registry role")
            if (
                ticket.source_binding.artifact_manifest_sha256
                != entry["hashes"]["artifact_manifest_sha256"]
            ):
                raise ValueError("sealed GoldTicket source manifest mismatch")
            if (
                normalized_source_sha256 is not None
                and ticket.source_binding.normalized_source_sha256
                != normalized_source_sha256
            ):
                raise ValueError("sealed GoldTicket normalized source mismatch")
            if (
                ticket.knowledge_snapshot.canonical_seed_sha256
                != entry["hashes"]["kb_seed_sha256"]
            ):
                raise ValueError("sealed GoldTicket knowledge snapshot mismatch")
            if ticket.ticket_id_hash in records:
                raise ValueError("sealed GoldTicket contains duplicate ticket IDs")
            if ticket.duplicate_component_id in components:
                raise ValueError("sealed GoldTicket contains duplicate components")
            records[ticket.ticket_id_hash] = ticket.duplicate_component_id
            components.add(ticket.duplicate_component_id)
    if len(records) != entry["cases_total"]:
        raise ValueError("sealed GoldTicket case count differs from registry")
    return records


def _read_strict_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            payload = json.load(
                source,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} must be readable strict UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return payload


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError("could not hash dataset artifact") from exc
    return digest.hexdigest()


def _validate_lineage(by_ref: Mapping[str, Mapping[str, Any]]) -> None:
    for ref, entry in by_ref.items():
        for parent_ref in entry["source_dataset_ids"]:
            if parent_ref not in by_ref:
                raise ValueError(f"{ref} references unknown source dataset {parent_ref}")
            if parent_ref == ref:
                raise ValueError(f"{ref} cannot reference itself as a source")
        supersedes = entry["supersedes"]
        if supersedes is not None:
            if supersedes not in by_ref:
                raise ValueError(f"{ref} supersedes unknown dataset {supersedes}")
            if by_ref[supersedes]["dataset_id"] != entry["dataset_id"]:
                raise ValueError(f"{ref} supersedes a different dataset_id")
        successor_ref = entry["superseded_by"]
        if successor_ref is not None:
            successor = by_ref.get(successor_ref)
            if successor is None or successor["supersedes"] != ref:
                raise ValueError(f"{ref} has a non-reciprocal superseded_by link")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(ref: str) -> None:
        if ref in visiting:
            raise ValueError("dataset lineage contains a cycle")
        if ref in visited:
            return
        visiting.add(ref)
        for parent in by_ref[ref]["source_dataset_ids"]:
            visit(parent)
        visiting.remove(ref)
        visited.add(ref)

    for ref in sorted(by_ref):
        visit(ref)


def _validate_frozen_hashes(entries: Iterable[Mapping[str, Any]]) -> None:
    for entry in entries:
        expected = entry.get("frozen_payload_sha256")
        if expected is not None and expected != _frozen_payload_sha256(entry):
            raise ValueError(f"frozen dataset was modified: {dataset_ref(entry)}")


def _frozen_payload_sha256(entry: Mapping[str, Any]) -> str:
    payload = {
        key: entry[key]
        for key in sorted(ENTRY_FIELDS - _FROZEN_HASH_EXCLUDED_FIELDS)
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _entry_by_ref(registry: Mapping[str, Any], ref: str) -> dict[str, Any]:
    _validate_ref(ref, field="dataset_ref")
    for entry in registry["datasets"]:
        if dataset_ref(entry) == ref:
            return entry
    raise ValueError(f"unknown dataset: {ref}")


def _validate_relative_root(value: Any, *, location: str) -> None:
    if not isinstance(value, str) or "\\" in value:
        raise ValueError(f"{location}.relative_root must be a POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"{location}.relative_root is unsafe")
    if len(path.parts) < 3 or path.parts[:2] != ("data", "private"):
        raise ValueError(f"{location}.relative_root must stay under data/private")
    if value != path.as_posix():
        raise ValueError(f"{location}.relative_root must be a canonical POSIX path")


def _relative_root_key(value: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PurePosixPath(value).parts)


def _path_parts_overlap(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    shorter = min(len(first), len(second))
    return first[:shorter] == second[:shorter]


def _private_registry_path(path: Path, *, private_root: Path) -> Path:
    root = private_root.expanduser().resolve()
    resolved = path.expanduser().resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("registry path must stay under data/private")
    if path.is_symlink():
        raise ValueError("registry path must not be a symlink")
    return resolved


def _ensure_regular_single_link(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    try:
        links = path.stat().st_nlink
    except OSError as exc:
        raise ValueError(f"could not inspect {label}") from exc
    if links != 1:
        raise ValueError(f"{label} must not be a hardlink")


def _validate_ref(value: Any, *, field: str) -> None:
    if not isinstance(value, str) or _REF_RE.fullmatch(value) is None:
        raise ValueError(f"{field} contains an invalid dataset reference")


def _enum(value: Any, allowed: frozenset[str], *, field: str) -> None:
    if type(value) is not str or value not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(sorted(allowed))}")


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an ISO timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _iso_timestamp(value: datetime, *, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC).isoformat()


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON keys are not allowed")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> Any:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def ensure_finite_numbers(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers are not allowed")
    if isinstance(value, dict):
        for item in value.values():
            ensure_finite_numbers(item)
    elif isinstance(value, list):
        for item in value:
            ensure_finite_numbers(item)
