from __future__ import annotations

import hashlib
from pathlib import Path

PHASE0_TELEMETRY_GIT_SHA = "7d244e4fdee21a36a609e6f1cd0012e198746376"

# SHA-256 of the approval-bound builder files as stored in the telemetry commit.
# The server-local runner may evolve independently, but the exact code that built
# the private cases and telemetry contract must remain byte-for-byte identical.
PHASE0_BUILDER_FILE_SHA256 = {
    "eval/cost_governance.py": (
        "7e1c48fbb0304406cf3a036b7781ed7fbc48e0ddf0b9c90d34c0403092350d9d"
    ),
    "eval/run_ask.py": (
        "e131e51cda7163ab33de9828d619558ffeb8c4c9df804838a66b885820e898d2"
    ),
    "eval/social_ticket_benchmark.py": (
        "a9e960cd510f3c549300acdbac5ae76ea32ba70114a8f87249aca9da2c5e8f20"
    ),
    "scripts/analyze_ticket_dataset.py": (
        "899a98a980c14ff9bce0558dc96b01150cfe255d420423ec0ea0a931fd30fde9"
    ),
    "src/config.py": (
        "447e56fd1200fb21c2089ca40b3d8b898c41d97552b09b84555ac9031ebc58fa"
    ),
    "src/graph/nodes/analyze.py": (
        "7c341a20fbb52cb59fca0f73bc70a567ebc44cc025e5e0c8e7830dc5c04b2c6d"
    ),
    "src/kb/source_extractors.py": (
        "01cb8d76d94512e05fd8a177c99e119b3e181b56e2fc0d7d3e492735e6b155d6"
    ),
    "src/security/eval_cache_bypass.py": (
        "9324134cf443c88b771fc9153091965f66b0efac85c20dfc7d7517cbce6ea60a"
    ),
    "src/security/pii_masker.py": (
        "d293da5b88b5a49b2371aa8c589e499b37df08b17a9b551ebd149cbbea4dd335"
    ),
}


def validate_phase0_builder_snapshot(
    source_root: Path,
    *,
    telemetry_git_sha: str,
) -> dict[str, object]:
    """Validate a read-only snapshot of the approval-bound Phase 0 builder."""

    if telemetry_git_sha != PHASE0_TELEMETRY_GIT_SHA:
        raise ValueError("Phase 0 server-local telemetry SHA is not approved")
    try:
        root = source_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Phase 0 builder snapshot is unavailable") from exc
    if not root.is_dir():
        raise ValueError("Phase 0 builder snapshot must be a directory")

    verified: dict[str, str] = {}
    for relative_path, expected_sha256 in PHASE0_BUILDER_FILE_SHA256.items():
        candidate = root / relative_path
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                f"Phase 0 builder snapshot is missing {relative_path}"
            ) from exc
        if (
            candidate.is_symlink()
            or not resolved.is_file()
            or not resolved.is_relative_to(root)
        ):
            raise ValueError(
                f"Phase 0 builder snapshot path is unsafe: {relative_path}"
            )
        actual_sha256 = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"Phase 0 builder snapshot differs from telemetry: {relative_path}"
            )
        verified[relative_path] = actual_sha256

    return {
        "telemetry_git_sha": telemetry_git_sha,
        "files_verified": len(verified),
        "file_sha256": verified,
    }
