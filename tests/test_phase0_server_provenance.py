from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from eval import phase0_server_provenance


def test_validate_phase0_builder_snapshot_accepts_exact_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected: dict[str, str] = {}
    for relative_path, content in {
        "eval/run_ask.py": b"runner\n",
        "src/config.py": b"config\n",
    }.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        expected[relative_path] = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(
        phase0_server_provenance,
        "PHASE0_BUILDER_FILE_SHA256",
        expected,
    )

    result = phase0_server_provenance.validate_phase0_builder_snapshot(
        tmp_path,
        telemetry_git_sha=phase0_server_provenance.PHASE0_TELEMETRY_GIT_SHA,
    )

    assert result["files_verified"] == 2
    assert result["file_sha256"] == expected


def test_validate_phase0_builder_snapshot_accepts_crlf_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "eval" / "run_ask.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"line one\r\nline two\r\n")
    canonical = b"line one\nline two\n"
    expected = {"eval/run_ask.py": hashlib.sha256(canonical).hexdigest()}
    monkeypatch.setattr(
        phase0_server_provenance,
        "PHASE0_BUILDER_FILE_SHA256",
        expected,
    )

    result = phase0_server_provenance.validate_phase0_builder_snapshot(
        tmp_path,
        telemetry_git_sha=phase0_server_provenance.PHASE0_TELEMETRY_GIT_SHA,
    )

    assert result["file_sha256"] == expected


def test_validate_phase0_builder_snapshot_rejects_changed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "eval" / "run_ask.py"
    path.parent.mkdir(parents=True)
    path.write_text("changed\n", encoding="utf-8")
    monkeypatch.setattr(
        phase0_server_provenance,
        "PHASE0_BUILDER_FILE_SHA256",
        {"eval/run_ask.py": "0" * 64},
    )

    with pytest.raises(ValueError, match="differs from telemetry"):
        phase0_server_provenance.validate_phase0_builder_snapshot(
            tmp_path,
            telemetry_git_sha=phase0_server_provenance.PHASE0_TELEMETRY_GIT_SHA,
        )
