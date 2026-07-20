from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from src.rag.errors import MLDependencyError

OFFLINE_VALUES = {"1", "on", "true", "yes"}
DEFAULT_LOCK_PATH = Path("deploy/huggingface_models.lock.json")


def _offline_enabled() -> bool:
    return any(
        os.getenv(name, "").strip().casefold() in OFFLINE_VALUES
        for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    )


def resolve_model_location(
    *,
    environment_name: str,
    default_repo_id: str,
    expected_revision: str,
    expected_target: str,
) -> str:
    value = os.getenv(environment_name, "").strip() or default_repo_id
    if not _offline_enabled():
        return value

    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise MLDependencyError(
            f"Offline ML runtime requires a verified local directory in {environment_name}."
        )

    lock_path = Path(os.getenv("HF_MODEL_LOCK_PATH", str(DEFAULT_LOCK_PATH)))
    receipt_path = Path(
        os.getenv("HF_MODEL_VERIFICATION_RECEIPT", str(path.parent / ".verified-models.json"))
    )
    try:
        lock_digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MLDependencyError("Offline model verification receipt is unavailable.") from exc

    if receipt.get("manifest_sha256") != lock_digest:
        raise MLDependencyError("Offline model verification receipt has the wrong manifest hash.")
    models = receipt.get("models")
    if not isinstance(models, list):
        raise MLDependencyError("Offline model verification receipt is invalid.")
    expected = {
        "repo_id": default_repo_id,
        "revision": expected_revision,
        "target": expected_target,
    }
    if not any(
        isinstance(item, dict) and all(item.get(key) == val for key, val in expected.items())
        for item in models
    ):
        raise MLDependencyError("Offline model revision does not match the locked provenance.")
    return str(path)
