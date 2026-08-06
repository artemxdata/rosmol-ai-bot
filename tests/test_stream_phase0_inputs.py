from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

from scripts import stream_phase0_inputs


def test_stream_phase0_inputs_sends_only_approved_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cases = b'[{"query":"PRIVATE-CANARY"}]\n'
    manifest = b'{"manifest":"PRIVATE-MANIFEST-CANARY"}\n'
    cases_path = tmp_path / "cases.json"
    manifest_path = tmp_path / "manifest.json"
    cases_path.write_bytes(cases)
    manifest_path.write_bytes(manifest)
    monkeypatch.setattr(
        stream_phase0_inputs,
        "CASES_SHA256",
        hashlib.sha256(cases).hexdigest(),
    )
    monkeypatch.setattr(
        stream_phase0_inputs,
        "MANIFEST_SHA256",
        hashlib.sha256(manifest).hexdigest(),
    )
    calls: list[tuple[list[str], bytes]] = []

    def fake_run(args, *, input, check):
        assert check is False
        calls.append((args, input))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(stream_phase0_inputs.subprocess, "run", fake_run)

    stream_phase0_inputs.stream_phase0_inputs(
        cases_path=cases_path,
        manifest_path=manifest_path,
        ssh_target="rosmol",
    )

    assert len(calls) == 1
    args, archive = calls[0]
    assert args[:3] == ["ssh", "-T", "rosmol"]
    assert "PRIVATE-CANARY" not in args[-1]
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as bundle:
        assert bundle.getnames() == ["phase0-cases.json", "phase0-manifest.json"]
        assert bundle.extractfile("phase0-cases.json").read() == cases
        assert bundle.extractfile("phase0-manifest.json").read() == manifest


def test_server_launcher_supplies_all_acceptance_compose_variables() -> None:
    launcher = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_phase0_server_local.sh"
    ).read_text(encoding="utf-8")

    for variable in (
        "RELEASE_GIT_SHA",
        "ACCEPTANCE_SOURCE_DIR",
        "ACCEPTANCE_OUTPUT_DIR",
        "ACCEPTANCE_PROVENANCE_DIR",
        "ACCEPTANCE_COST_LEDGER_DIR",
        "PHASE0_RUNTIME_GIT_SHA",
        "PHASE0_RUNNER_SOURCE_DIR",
        "PHASE0_BUILDER_SOURCE_DIR",
        "PHASE0_PRIVATE_DIR",
        "PHASE0_COST_LEDGER_DIR",
    ):
        assert f'"{variable}=$' in launcher
    assert "  --profile ml\n  --profile phase0\n" in launcher
