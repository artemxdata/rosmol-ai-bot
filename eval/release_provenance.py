from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_release_provenance(
    *,
    release_run_id: str,
    target: str,
    kb_seed_path: Path,
    case_paths: Mapping[str, Path] | None = None,
    expected_git_sha: str | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    git_sha, git_worktree_clean = _git_state()
    if git_sha is None:
        errors.append("git_sha_unavailable")
    if git_worktree_clean is not True:
        errors.append(
            "git_worktree_dirty" if git_worktree_clean is False else "git_status_unavailable"
        )
    if expected_git_sha is not None:
        if not valid_git_sha(expected_git_sha):
            errors.append("expected_git_sha_invalid")
        elif git_sha != expected_git_sha:
            errors.append("expected_git_sha_mismatch")

    kb_seed = _fingerprint_file(kb_seed_path, errors=errors, label="kb_seed")
    cases = {
        name: _fingerprint_file(path, errors=errors, label=f"case:{name}")
        for name, path in (case_paths or {}).items()
    }
    if case_paths is not None and not cases:
        errors.append("case_files_empty")

    return {
        "release_run_id": release_run_id,
        "target": target,
        "git_sha": git_sha,
        "expected_git_sha": expected_git_sha,
        "git_worktree_clean": git_worktree_clean,
        "kb_seed": kb_seed,
        "case_files": cases,
        "complete": not errors,
        "errors": errors,
    }


def _git_state() -> tuple[str | None, bool | None]:
    sha_result = _run_git("rev-parse", "HEAD")
    if sha_result is None:
        return None, None
    git_sha = sha_result.stdout.strip()
    if sha_result.returncode != 0 or not valid_git_sha(git_sha):
        return None, None

    status_result = _run_git("status", "--porcelain", "--untracked-files=normal")
    if status_result is None or status_result.returncode != 0:
        return git_sha, None
    return git_sha, not bool(status_result.stdout.strip())


def _run_git(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def valid_git_sha(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _fingerprint_file(
    path: Path,
    *,
    errors: list[str],
    label: str,
) -> dict[str, Any] | None:
    try:
        content = path.read_bytes()
    except OSError as exc:
        errors.append(f"{label}_unavailable:{type(exc).__name__}")
        return None
    return {
        "path": str(path).replace("\\", "/"),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
