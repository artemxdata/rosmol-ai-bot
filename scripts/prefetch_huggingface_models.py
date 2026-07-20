from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_MANIFEST = Path("deploy/huggingface_models.lock.json")
DEFAULT_OUTPUT_ROOT = Path("/opt/models")
RECEIPT_NAME = ".verified-models.json"
PICKLE_SUFFIXES = {".bin", ".pickle", ".pkl", ".pt", ".pth"}
EXECUTABLE_SUFFIXES = {
    ".bat",
    ".cmd",
    ".dll",
    ".dylib",
    ".exe",
    ".py",
    ".pyc",
    ".pyo",
    ".ps1",
    ".sh",
    ".so",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
TARGET_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("model file path must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe model file path: {value!r}")
    return path.as_posix()


def load_manifest(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("unsupported Hugging Face model manifest schema")
    models = raw.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("model manifest must contain at least one model")

    seen_targets: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("model manifest entry must be an object")
        repo_id = model.get("repo_id")
        revision = model.get("revision")
        target = model.get("target")
        if not isinstance(repo_id, str) or not REPO_RE.fullmatch(repo_id):
            raise ValueError(f"invalid Hugging Face repo_id: {repo_id!r}")
        if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
            raise ValueError(f"model {repo_id} revision must be a full lowercase commit SHA")
        if not isinstance(target, str) or not TARGET_RE.fullmatch(target):
            raise ValueError(f"invalid model target: {target!r}")
        if target in seen_targets:
            raise ValueError(f"duplicate model target: {target}")
        seen_targets.add(target)

        files = model.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError(f"model {repo_id} has no locked files")
        seen_files: set[str] = set()
        for item in files:
            if not isinstance(item, dict):
                raise ValueError(f"model {repo_id} file entry must be an object")
            relative = _safe_relative_path(item.get("path"))
            if relative in seen_files:
                raise ValueError(f"model {repo_id} has duplicate file {relative}")
            seen_files.add(relative)
            size = item.get("size")
            digest = item.get("sha256")
            allow_pickle = item.get("allow_pickle")
            if not isinstance(size, int) or size < 0:
                raise ValueError(f"model {repo_id} file {relative} has invalid size")
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise ValueError(f"model {repo_id} file {relative} has invalid SHA-256")
            if not isinstance(allow_pickle, bool):
                raise ValueError(f"model {repo_id} file {relative} lacks allow_pickle boolean")
            suffix = PurePosixPath(relative).suffix.casefold()
            if suffix in EXECUTABLE_SUFFIXES:
                raise ValueError(f"executable model artifact is forbidden: {relative}")
            if suffix in PICKLE_SUFFIXES and not allow_pickle:
                raise ValueError(f"pickle model artifact is not explicitly approved: {relative}")
            if allow_pickle and suffix not in PICKLE_SUFFIXES:
                raise ValueError(f"allow_pickle is only valid for pickle artifacts: {relative}")
    return raw


def _expected_files(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["path"]): item for item in model["files"]}


def verify_model_directory(root: Path, model: dict[str, Any]) -> None:
    expected = _expected_files(model)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"model target is not a regular directory: {root}")

    found: set[str] = set()
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            directory = current_path / name
            if directory.is_symlink():
                raise ValueError(f"model directory symlink is forbidden: {directory}")
        for name in file_names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"model artifact must be a regular file: {relative}")
            if metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                raise ValueError(f"executable model artifact is forbidden: {relative}")
            locked = expected.get(relative)
            if locked is None:
                raise ValueError(f"unexpected model artifact: {relative}")
            if metadata.st_size != locked["size"]:
                raise ValueError(f"model artifact size mismatch: {relative}")
            if _sha256(path) != locked["sha256"]:
                raise ValueError(f"model artifact SHA-256 mismatch: {relative}")
            found.add(relative)
    missing = set(expected) - found
    if missing:
        raise ValueError(f"model artifacts are missing: {sorted(missing)}")


def _download_snapshot(model: dict[str, Any], staging: Path) -> None:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("huggingface_hub is required in the ML image") from exc

    snapshot_download(
        repo_id=model["repo_id"],
        repo_type="model",
        revision=model["revision"],
        allow_patterns=sorted(_expected_files(model)),
        local_dir=staging,
    )


def _remove_local_download_metadata(staging: Path) -> None:
    cache = staging / ".cache"
    if not cache.exists():
        return
    if cache.is_symlink() or not cache.is_dir():
        raise ValueError("unexpected Hugging Face local metadata path")
    shutil.rmtree(cache)


def _cleanup_owned_staging(path: Path, root: Path) -> None:
    if path.parent == root and path.name.startswith(".prefetch-") and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)


def prefetch_models(manifest_path: Path, output_root: Path) -> None:
    manifest_path = manifest_path.resolve(strict=True)
    manifest = load_manifest(manifest_path)
    output_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    output_root = output_root.resolve(strict=True)
    if output_root.is_symlink():
        raise ValueError("model output root cannot be a symlink")

    for model in manifest["models"]:
        target = output_root / model["target"]
        if target.exists():
            verify_model_directory(target, model)
            print(f"model={model['name']} revision={model['revision']} status=verified")
            continue

        staging = output_root / f".prefetch-{model['target']}-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            _download_snapshot(model, staging)
            _remove_local_download_metadata(staging)
            verify_model_directory(staging, model)
            staging.replace(target)
        except Exception:
            _cleanup_owned_staging(staging, output_root)
            raise
        print(f"model={model['name']} revision={model['revision']} status=downloaded_verified")

    receipt = {
        "schema_version": 1,
        "manifest_sha256": _manifest_sha256(manifest_path),
        "models": [
            {
                "name": model["name"],
                "repo_id": model["repo_id"],
                "revision": model["revision"],
                "target": model["target"],
            }
            for model in manifest["models"]
        ],
    }
    receipt_path = output_root / RECEIPT_NAME
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if receipt_path.exists():
        if receipt_path.is_symlink() or receipt_path.read_text(encoding="utf-8") != serialized:
            raise ValueError("model verification receipt does not match locked manifest")
    else:
        temporary = output_root / f".{RECEIPT_NAME}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(serialized, encoding="utf-8")
        temporary.chmod(0o640)
        temporary.replace(receipt_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prefetch and verify immutable HF snapshots.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    prefetch_models(args.manifest, args.output_root)
