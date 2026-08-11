from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/diagnose_pilot50_candidate_server_local.sh"
TOOLING_SHA = "a" * 40
RUNTIME_SHA = "64cc182d37a3c060439ed7a55f5cc875a27d786d"
MANIFEST_SHA = "6995b96b4658f53e40a0bb982145465cbc347d9df041fc4dd66a9d15687b822b"
CASES_SHA = "b027e469e062682b6dc341b2dd4c87440edffb1955c2111f38e6c44a92a3a14d"
REPORT_SHA = "07fdfebf505e3df9b2461386e37f89a836dd80f3a5c445ec93bfca765e47add9"
SAFE_SHA = "4e5b0ebb4e04b9d449e7ed54db9a1167c19cce02ef27839073fba280e435b61d"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function(text: str, name: str) -> str:
    marker = f"{name}() {{"
    start = text.index(marker)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated function: {name}")


def _bash() -> str:
    if os.name != "nt":
        return "bash"
    candidate = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
    )
    if not candidate.is_file():
        pytest.skip("Git Bash is unavailable")
    return str(candidate)


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), str(SCRIPT), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _diagnostic_validator_python() -> str:
    function = _function(_text(), "validate_diagnostic_stdout")
    marker = "3<<'PY' 2>/dev/null\n"
    start = function.index(marker) + len(marker)
    end = function.index("\nPY", start)
    return function[start:end]


def _row(ordinal: int) -> dict[str, object]:
    group = "typical" if ordinal <= 25 else "atypical"
    passed = ordinal <= 17 or 26 <= ordinal <= 33
    return {
        "ordinal": ordinal,
        "group": group,
        "passed": passed,
        "was_escalated": False,
        "escalation_reason": None,
        "observed_behavior": "answer",
        "failed_boolean_checks": [] if passed else ["behavior_match"],
        "generator_path": "simple",
        "generate_retry_reasons": [],
        "latency_bucket": "<5s",
    }


def _payload() -> dict[str, object]:
    return {
        "schema_version": "pilot50-v2-failure-diagnostics-v1",
        "bindings": {
            "manifest_sha256": MANIFEST_SHA,
            "cases_sha256": CASES_SHA,
            "report_sha256": REPORT_SHA,
            "safe_result_sha256": SAFE_SHA,
            "quality_status": "STOP",
        },
        "failure_matrix": [_row(ordinal) for ordinal in range(1, 51)],
    }


def _canonical_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _run_validator(
    tmp_path: Path,
    *,
    payload: object | None = None,
    raw: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if raw is None:
        raw = _canonical_bytes(_payload() if payload is None else payload)
    stdout = tmp_path / "diagnostic.stdout"
    stdout.write_bytes(raw)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _diagnostic_validator_python(),
            str(stdout),
            str(64 * 1024 + 1),
            MANIFEST_SHA,
            CASES_SHA,
            REPORT_SHA,
            SAFE_SHA,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_script_has_valid_bash_syntax() -> None:
    completed = subprocess.run(
        [_bash(), "-n", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ((), "usage"),
        (("bad",), "tooling_sha_invalid"),
        (("0" * 40,), "tooling_sha_invalid"),
        ((RUNTIME_SHA,), "tooling_sha_invalid"),
        ((TOOLING_SHA, TOOLING_SHA), "usage"),
    ],
)
def test_invalid_invocation_fails_before_server_or_docker_access(
    args: tuple[str, ...],
    reason: str,
) -> None:
    completed = _run_script(*args)
    assert completed.returncode == 1
    assert completed.stdout == (
        f"pilot50_candidate_diagnostics=FAIL reason={reason}\n"
    )
    assert completed.stderr == ""


def test_launcher_pins_the_exact_sealed_evidence_contract() -> None:
    text = _text()
    for value in (
        RUNTIME_SHA,
        MANIFEST_SHA,
        CASES_SHA,
        REPORT_SHA,
        SAFE_SHA,
        "150e8661257b7c7bd0495aec92476654d2aec156d090bc34a0373c551a20ad1a",
        "f753b69665f216039b944546886f611410107e1344e52b159ab3f221b60aefa5",
        "aead5e930c513d9d5aeaacd3f3d4b8ce99fab536434343e7fcd6e9917de93e8a",
    ):
        assert value in text
    assert (
        "/var/lib/rosmol/pilot50-candidate/runs/"
        "${DATASET_ID}-${SEALED_RUNTIME_SHA}"
    ) in text
    sealed = _function(text, "validate_sealed_run")
    assert '"quality_status": "STOP"' in sealed
    assert '"schema_version": "pilot50-candidate-run-completed-v1"' in sealed
    assert "metadata.st_nlink == 1" in sealed
    assert "metadata.st_uid == 10001 and metadata.st_gid == 10001" in sealed
    assert "stat.S_IMODE(metadata.st_mode) == 0o600" in sealed
    assert "stat.S_IMODE(evidence_dir.lstat().st_mode) == 0o700" in sealed
    assert "runtime_identity.get(field) == runtime_sha" in sealed


def test_tooling_is_a_clean_detached_exact_git_archive_without_private_files() -> None:
    text = _text()
    load = _function(text, "load_tooling")
    snapshot = _function(text, "create_tooling_snapshot")
    assert '"$TOOLING_ROOT" == "$SERVER_PROJECT_DIR"' in load
    assert "symbolic-ref -q HEAD" in load
    assert "--porcelain=v1 --untracked-files=no" in load
    assert "tooling_tracks_private_state" in load
    assert "tooling_source_has_symlink" in load
    assert 'archive --format=tar "$EXPECTED_TOOLING_SHA"' in snapshot
    assert "--work-tree=\"$TOOLING_SOURCE\"" in snapshot
    assert 'diff --quiet "$EXPECTED_TOOLING_SHA"' in snapshot
    assert '"$TOOLING_SOURCE/.env.production"' in snapshot
    assert "tooling_snapshot_contains_private_state" in snapshot


def test_diagnostic_container_is_read_only_offline_and_has_no_runtime_access() -> None:
    run = _function(_text(), "run_diagnostics")
    for required in (
        "docker run --rm --pull never --network none",
        "--user app --read-only",
        "--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m",
        "--cap-drop ALL --security-opt no-new-privileges=true",
        "-e PYTHONOPTIMIZE=",
        'src=$TOOLING_SOURCE,dst=/workspace,readonly',
        'src=$SEALED_RUN_DIR/evidence,dst=/evidence,readonly',
        "-E -m scripts.pilot50 diagnose",
        "--expected-runtime-git-sha \"$SEALED_RUNTIME_SHA\"",
    ):
        assert required in run
    for forbidden in (
        "docker build",
        "docker pull",
        "docker exec",
        "docker compose",
        "--env-file",
        "eval-cost-ledger",
        "rosmol-app-ml",
        "/ask",
        "HDE_",
        "VK_",
        "POSTGRES",
        "QDRANT_URL",
    ):
        assert forbidden not in run
    assert '>$RAW_STDOUT' not in run
    assert '>"$RAW_STDOUT"' in run


def test_stdout_validator_accepts_only_canonical_payload_free_matrix(
    tmp_path: Path,
) -> None:
    payload = _payload()
    completed = _run_validator(tmp_path, payload=payload)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.encode("ascii") == _canonical_bytes(payload)[:-1]
    output = json.loads(completed.stdout)
    serialized = completed.stdout
    for forbidden in (
        "query",
        "response",
        "user_id",
        "request_id",
        "case_id",
        "eval_run_id",
        "trace",
        "chunk_text",
        "approval_id",
        "timestamp",
        "cost_rub",
    ):
        assert forbidden not in serialized
    assert len(output["failure_matrix"]) == 50


def test_stdout_validator_fails_closed_under_python_optimize(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONOPTIMIZE"] = "1"
    completed = _run_validator(tmp_path, env=environment)
    assert completed.returncode != 0
    assert completed.stdout == ""


@pytest.mark.parametrize(
    ("binding", "value"),
    [
        ("manifest_sha256", "0" * 64),
        ("cases_sha256", "0" * 64),
        ("report_sha256", "0" * 64),
        ("safe_result_sha256", "0" * 64),
        ("quality_status", "GO"),
    ],
)
def test_stdout_validator_rejects_tampered_bindings(
    tmp_path: Path,
    binding: str,
    value: str,
) -> None:
    payload = _payload()
    bindings = payload["bindings"]
    assert isinstance(bindings, dict)
    bindings[binding] = value
    assert _run_validator(tmp_path, payload=payload).returncode != 0


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_field",
        "bad_ordinal",
        "bad_group",
        "bad_pass_count",
        "passed_with_failure",
        "unknown_check",
        "duplicate_check",
        "unsafe_reason",
        "unallowlisted_reason",
        "unknown_behavior",
        "unknown_generator_path",
        "unknown_latency_bucket",
        "unknown_retry_reason",
        "duplicate_retry_reason",
        "too_many_retry_reasons",
    ],
)
def test_stdout_validator_rejects_schema_privacy_and_enum_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    payload = _payload()
    rows = payload["failure_matrix"]
    assert isinstance(rows, list)
    row = rows[17]
    assert isinstance(row, dict)
    if mutation == "extra_field":
        row["query"] = "privacy-canary"
    elif mutation == "bad_ordinal":
        row["ordinal"] = 999
    elif mutation == "bad_group":
        row["group"] = "other"
    elif mutation == "bad_pass_count":
        row["passed"] = True
        row["failed_boolean_checks"] = []
    elif mutation == "passed_with_failure":
        rows[0]["failed_boolean_checks"] = ["behavior_match"]
    elif mutation == "unknown_check":
        row["failed_boolean_checks"] = ["raw_payload_check"]
    elif mutation == "duplicate_check":
        row["failed_boolean_checks"] = ["behavior_match", "behavior_match"]
    elif mutation == "unsafe_reason":
        row["was_escalated"] = True
        row["escalation_reason"] = "SECRET: raw exception"
    elif mutation == "unallowlisted_reason":
        row["was_escalated"] = True
        row["escalation_reason"] = "private_secret_canary"
    elif mutation == "unknown_behavior":
        row["observed_behavior"] = "raw_response"
    elif mutation == "unknown_generator_path":
        row["generator_path"] = "GigaChat/GigaChat-2-Max"
    elif mutation == "unknown_latency_bucket":
        row["latency_bucket"] = "40015ms"
    elif mutation == "unknown_retry_reason":
        row["generate_retry_reasons"] = ["raw_exception"]
    elif mutation == "duplicate_retry_reason":
        row["generate_retry_reasons"] = [
            "llm_response_too_long",
            "llm_response_too_long",
        ]
    else:
        row["generate_retry_reasons"] = [
            "llm_response_too_long",
            "llm_response_contract_failed",
            "llm_response_profile_failed",
        ]
    assert _run_validator(tmp_path, payload=payload).returncode != 0


@pytest.mark.parametrize(
    "raw_builder",
    [
        lambda payload: json.dumps(payload).encode("ascii") + b"\n",
        lambda payload: _canonical_bytes(payload) + b"{}\n",
        lambda payload: _canonical_bytes(payload) + b"\x00",
        lambda payload: b"x" * (64 * 1024 + 2),
    ],
)
def test_stdout_validator_rejects_noncanonical_or_oversized_stdout(
    tmp_path: Path,
    raw_builder: Any,
) -> None:
    assert (
        _run_validator(tmp_path, raw=raw_builder(_payload())).returncode != 0
    )
