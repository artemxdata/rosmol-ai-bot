from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_fact_core_qdrant_diagnostic_server_local.sh"
CANDIDATE_SHA = "a" * 40
PRODUCTION_SHA = "c38f0e055630fae2af50720fae81acee20ff4f6a"
MANIFEST_SHA = "12747d62190cc5e70d70490e9a649d91596ec69a316b5c2de3843ac3df6f85b4"
CASES_SHA = "9d53114722191330214f5917ee3baf4ccfcf4eb644be34a0253c60531b225529"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


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


def _validator_python(function_name: str = "validate_diagnostic_stdout") -> str:
    text = _text()
    function_start = text.index(f"{function_name}() {{")
    marker = "3<<'PY' 2>/dev/null\n"
    start = text.index(marker, function_start) + len(marker)
    end = text.index("\nPY\n}", start)
    return text[start:end]


def _payload(*, status: str = "GO") -> dict[str, Any]:
    return {
        "schema_version": "fact-core-qdrant-postprocess-calibration-v1",
        "classification": "calibration_only",
        "disclaimer": (
            "Mechanical first-turn regression calibration; not an independent "
            "holdout, human product verdict, or production traffic conversion."
        ),
        "candidate_sha": CANDIDATE_SHA,
        "dataset_id": "pilot50_balanced_v5",
        "manifest_sha256": MANIFEST_SHA,
        "cases_sha256": CASES_SHA,
        "minimum_passed": 49,
        "counts": {
            "total": 50,
            "passed": 49,
            "typical_passed": 25,
            "atypical_passed": 24,
            "no_operator": 50,
            "typical_no_operator": 25,
            "atypical_no_operator": 25,
            "retrieval_complete": 50,
            "citation_complete": 50,
            "llm_calls": 0,
        },
        "failures": [{"ordinal": 50, "reasons": ["answer_contains_match"]}],
        "status": status,
    }


def _preview_payload() -> dict[str, Any]:
    ordinals = [1, 9, 26, 44, 48]
    previews = [
        {
            "ordinal": ordinal,
            "group": "typical" if ordinal <= 25 else "atypical",
            "query": f"Вопрос {ordinal}",
            "response": f"Короткий подтверждённый ответ {ordinal}.",
            "response_chars": len(f"Короткий подтверждённый ответ {ordinal}."),
            "link_count": 0,
            "passed": True,
            "was_escalated": False,
        }
        for ordinal in ordinals
    ]
    return {
        "schema_version": "fact-core-qdrant-owner-preview-v1",
        "classification": "calibration_only",
        "disclaimer": (
            "Five bounded examples from the exposed Pilot50 calibration set; "
            "not an independent holdout or production traffic conversion."
        ),
        "candidate_sha": CANDIDATE_SHA,
        "dataset_id": "pilot50_balanced_v5",
        "manifest_sha256": MANIFEST_SHA,
        "cases_sha256": CASES_SHA,
        "counts": {
            "total": 50,
            "passed": 49,
            "no_operator": 50,
            "llm_calls": 0,
        },
        "response_shape": {
            "min_chars": 120,
            "median_chars": 220,
            "p95_chars": 480,
            "max_chars": 650,
            "empty_responses": 0,
            "responses_over_1000_chars": 0,
            "max_links": 1,
            "max_paragraphs": 2,
        },
        "preview_ordinals": ordinals,
        "previews": previews,
    }


def _run_validator(
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    function_name: str = "validate_diagnostic_stdout",
) -> subprocess.CompletedProcess[str]:
    output = tmp_path / "diagnostic.stdout"
    output.write_bytes(
        (
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _validator_python(function_name),
            str(output),
            str(64 * 1024 + 1),
            CANDIDATE_SHA,
            MANIFEST_SHA,
            CASES_SHA,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_launcher_has_valid_bash_syntax() -> None:
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
        (("bad",), "candidate_sha_invalid"),
        (("0" * 40,), "candidate_sha_invalid"),
        ((PRODUCTION_SHA,), "candidate_sha_invalid"),
        ((CANDIDATE_SHA, CANDIDATE_SHA), "output_mode_invalid"),
        ((CANDIDATE_SHA, "preview", "extra"), "usage"),
    ],
)
def test_invalid_invocation_stops_before_sudo_or_docker(
    args: tuple[str, ...],
    reason: str,
) -> None:
    completed = _run_script(*args)
    assert completed.returncode == 1
    assert completed.stdout == f"fact_core_qdrant_diagnostic=FAIL reason={reason}\n"
    assert completed.stderr == ""


def test_launcher_pins_runtime_dataset_and_qdrant_baseline() -> None:
    text = _text()
    for value in (
        PRODUCTION_SHA,
        MANIFEST_SHA,
        CASES_SHA,
        "2152",
        "f753b69665f216039b944546886f611410107e1344e52b159ab3f221b60aefa5",
        "aead5e930c513d9d5aeaacd3f3d4b8ce99fab536434343e7fcd6e9917de93e8a",
    ):
        assert value in text
    assert 'post_qdrant="$(qdrant_snapshot)"' in text
    assert '[[ "$post_qdrant" == "$pre_qdrant" ]]' in text
    assert '[[ "$post_prod" == "$pre_prod" ]]' in text


def test_launcher_uses_exact_private_free_candidate_snapshot() -> None:
    text = _text()
    assert 'archive --format=tar "$EXPECTED_SHA"' in text
    assert 'diff --quiet "$EXPECTED_SHA"' in text
    assert '"$SOURCE_DIR/data/private"' in text
    assert 'candidate_dependency_contract_changed' in text
    assert 'Dockerfile pyproject.toml requirements' in text
    for path in (
        "scripts/check_fact_pipeline_qdrant.py",
        "scripts/qdrant_readonly_proxy.py",
        "eval/run_ask.py",
        "src/graph/nodes/respond.py",
    ):
        assert path in text


def test_launcher_enforces_read_only_network_and_runtime_boundaries() -> None:
    text = _text()
    assert "docker network create --internal" in text
    assert "--network-alias qdrant-readonly" in text
    assert 'docker network connect "$DATA_NETWORK" "$PROXY_ID"' in text
    assert 'diagnostic_networks == {diagnostic_network}' in text
    assert 'proxy_networks == {diagnostic_network, data_network}' in text
    assert "--user app --read-only" in text
    assert "--cap-drop ALL --security-opt no-new-privileges=true" in text
    assert "dst=/workspace,readonly" in text
    assert "dst=/opt/models,readonly" in text
    assert "QDRANT_URL=http://qdrant-readonly:6333" in text
    assert '"QDRANT_UPSTREAM_API_KEY" not in diagnostic_env' in text
    for forbidden in (
        "CLOUD_RU_API_KEY=${",
        "POSTGRES_DSN=${",
        "REDIS_URL=${",
        "/ask",
        "HIGH_COST_APPROVAL_ID",
    ):
        assert forbidden not in text


def test_stdout_validator_accepts_only_canonical_aggregate(tmp_path: Path) -> None:
    payload = _payload()
    completed = _run_validator(tmp_path, payload)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == payload
    for forbidden in ("query", "response", "chunk_id", "user_id", "api_key"):
        assert forbidden not in completed.stdout.casefold()


def test_stdout_validator_rejects_forged_status(tmp_path: Path) -> None:
    completed = _run_validator(tmp_path, _payload(status="STOP"))
    assert completed.returncode != 0
    assert completed.stdout == ""


def test_preview_stdout_validator_accepts_bounded_owner_examples(tmp_path: Path) -> None:
    payload = _preview_payload()
    completed = _run_validator(
        tmp_path,
        payload,
        function_name="validate_preview_stdout",
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == payload


def test_preview_stdout_validator_rejects_internal_source_markers(tmp_path: Path) -> None:
    payload = _preview_payload()
    payload["previews"][0]["response"] += " [src:private]"
    payload["previews"][0]["response_chars"] = len(
        payload["previews"][0]["response"]
    )
    completed = _run_validator(
        tmp_path,
        payload,
        function_name="validate_preview_stdout",
    )
    assert completed.returncode != 0
    assert completed.stdout == ""
