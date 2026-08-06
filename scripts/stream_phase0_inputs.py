from __future__ import annotations

import argparse
import hashlib
import io
import re
import shlex
import subprocess
import tarfile
from pathlib import Path

CASES_SHA256 = "aff198bbc98d07894a3e1676e3457891e3a38f674315051505b681641fe9d02d"
MANIFEST_SHA256 = "8cf9959aaf9caf8728b386214ebba826f7bb0eb349f27fd2737e2830eb353264"
REMOTE_DIR = "/dev/shm/rosmol-phase0-30-20260805"
MAX_INPUT_BYTES = 2 * 1024 * 1024
SSH_TARGET_RE = re.compile(r"[A-Za-z0-9_.@-]{1,128}")


def stream_phase0_inputs(
    *,
    cases_path: Path,
    manifest_path: Path,
    ssh_target: str,
) -> None:
    """Stream approved deidentified inputs to server RAM without any secrets."""

    if SSH_TARGET_RE.fullmatch(ssh_target) is None:
        raise ValueError("SSH target contains unsupported characters")
    inputs = (
        (cases_path, "phase0-cases.json", CASES_SHA256),
        (manifest_path, "phase0-manifest.json", MANIFEST_SHA256),
    )
    payloads: list[tuple[str, bytes]] = []
    for path, archive_name, expected_sha256 in inputs:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{archive_name} must be a regular local file")
        payload = path.read_bytes()
        if not payload or len(payload) > MAX_INPUT_BYTES:
            raise ValueError(f"{archive_name} has an invalid size")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError(f"{archive_name} SHA-256 differs from the approval")
        payloads.append((archive_name, payload))

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for archive_name, payload in payloads:
            info = tarfile.TarInfo(archive_name)
            info.size = len(payload)
            info.mode = 0o600
            bundle.addfile(info, io.BytesIO(payload))

    remote_script = f"""
set -eu
umask 077
d='{REMOTE_DIR}'
test ! -e "$d"
mkdir -m 0700 "$d"
cleanup() {{
  rm -f "$d/phase0-cases.json" "$d/phase0-manifest.json"
  rmdir "$d" 2>/dev/null || true
}}
trap cleanup EXIT HUP INT TERM
tar -xf - -C "$d"
test "$(sha256sum "$d/phase0-cases.json" | cut -d ' ' -f 1)" = '{CASES_SHA256}'
test "$(sha256sum "$d/phase0-manifest.json" | cut -d ' ' -f 1)" = '{MANIFEST_SHA256}'
trap - EXIT HUP INT TERM
printf 'phase0_inputs=OK location=server_tmpfs files=2\\n'
"""
    remote_command = "sh -c " + shlex.quote(remote_script)
    completed = subprocess.run(
        ["ssh", "-T", ssh_target, remote_command],
        input=archive.getvalue(),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"SSH input stream failed with exit code {completed.returncode}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stream the approval-bound deidentified Phase 0 inputs to server RAM. "
            "This command reads no tokens, env files, DSNs or API keys."
        )
    )
    parser.add_argument("--ssh-target", default="rosmol")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    stream_phase0_inputs(
        cases_path=args.cases,
        manifest_path=args.manifest,
        ssh_target=args.ssh_target,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
