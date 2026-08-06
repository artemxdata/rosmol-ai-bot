from __future__ import annotations

import hashlib
from pathlib import Path

PHASE0_TELEMETRY_GIT_SHA = "7d244e4fdee21a36a609e6f1cd0012e198746376"

# SHA-256 of the approval-bound builder files as stored in the telemetry Git blobs.
# Git stores text files with LF line endings; normalize a Windows CRLF checkout to
# the same representation before comparing it with these immutable hashes.
PHASE0_BUILDER_FILE_SHA256 = {
    "eval/cost_governance.py": (
        "4688fcf9b452bee4c4d08d7c2b7d4901da4814ef268a62bbd6595aca139b9cd3"
    ),
    "eval/run_ask.py": (
        "a69314a1e1301022e5006333938f85fedc0061dfa717d55199dc0268fd6dc9a0"
    ),
    "eval/social_ticket_benchmark.py": (
        "971a0ef75d44d02f7a822e0e2b3d26c4fef3b3b3c240181d92cc54be9f82187b"
    ),
    "scripts/analyze_ticket_dataset.py": (
        "1986b9fffa3c772ab60fd1ffea08b0e4cc08e7362108fa29c7a46b3cbe3d0ffb"
    ),
    "src/config.py": (
        "c70b2616db76d96be1e9086512cd331909445834807d968f4dd54bf03721f6e6"
    ),
    "src/graph/nodes/analyze.py": (
        "778c161870aa308133033486a7925bedbd20dd3df8d53da7a42c088d0e1e140c"
    ),
    "src/kb/source_extractors.py": (
        "6241ac602a36961f5c7e9e789e688bae58811635d56c3ae47c91bdf1f375a627"
    ),
    "src/security/eval_cache_bypass.py": (
        "3c09650dc6925fb7cbe6cf69af9395f2249e61a86cdd86b6b8751d96fa16d528"
    ),
    "src/security/pii_masker.py": (
        "490e8713c915cd18af8c5097c5d0bcbc3c228a127113516d76c94b61bf85020b"
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
        canonical_payload = resolved.read_bytes().replace(b"\r\n", b"\n")
        actual_sha256 = hashlib.sha256(canonical_payload).hexdigest()
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
