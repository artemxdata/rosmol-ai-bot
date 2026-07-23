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
        "verification_mode": "direct_git",
        "complete": not errors,
        "errors": errors,
    }


def validate_release_provenance_attestation(
    attestation: Mapping[str, Any],
    *,
    release_run_id: str,
    target: str,
    kb_seed_path: Path,
    case_paths: Mapping[str, Path],
    expected_git_sha: str,
) -> dict[str, Any]:
    """Validate a host-created Git attestation against files visible to the caller."""

    errors: list[str] = []
    if attestation.get("verification_mode") != "direct_git":
        errors.append("attestation_not_direct_git")
    if attestation.get("complete") is not True or attestation.get("errors") not in ([], ()):
        errors.append("attestation_incomplete")
    if attestation.get("release_run_id") != release_run_id:
        errors.append("attestation_release_run_id_mismatch")
    if attestation.get("target") != target:
        errors.append("attestation_target_mismatch")
    if not valid_git_sha(expected_git_sha):
        errors.append("expected_git_sha_invalid")
    if attestation.get("git_sha") != expected_git_sha:
        errors.append("attestation_git_sha_mismatch")
    if attestation.get("expected_git_sha") != expected_git_sha:
        errors.append("attestation_expected_git_sha_mismatch")
    if attestation.get("git_worktree_clean") is not True:
        errors.append("attestation_worktree_not_clean")

    fingerprint_errors: list[str] = []
    kb_seed = _fingerprint_file(
        kb_seed_path,
        errors=fingerprint_errors,
        label="kb_seed",
    )
    cases = {
        name: _fingerprint_file(
            path,
            errors=fingerprint_errors,
            label=f"case:{name}",
        )
        for name, path in case_paths.items()
    }
    errors.extend(fingerprint_errors)

    if not _fingerprint_matches(attestation.get("kb_seed"), kb_seed):
        errors.append("attestation_kb_seed_mismatch")
    attested_cases = attestation.get("case_files")
    if not isinstance(attested_cases, Mapping) or set(attested_cases) != set(cases):
        errors.append("attestation_case_set_mismatch")
    else:
        for name, fingerprint in cases.items():
            if not _fingerprint_matches(attested_cases.get(name), fingerprint):
                errors.append(f"attestation_case_mismatch:{name}")

    return {
        "release_run_id": release_run_id,
        "target": target,
        "git_sha": expected_git_sha if valid_git_sha(expected_git_sha) else None,
        "expected_git_sha": expected_git_sha,
        "git_worktree_clean": attestation.get("git_worktree_clean") is True,
        "kb_seed": kb_seed,
        "case_files": cases,
        "verification_mode": "host_git_attestation_with_local_hash_verification",
        "complete": not errors,
        "errors": errors,
    }


def _fingerprint_matches(expected: Any, actual: Any) -> bool:
    if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
        return False
    return all(expected.get(key) == actual.get(key) for key in ("path", "sha256", "size_bytes"))


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
