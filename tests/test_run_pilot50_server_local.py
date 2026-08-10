import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_pilot50_server_local.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function(text: str, name: str) -> str:
    declarations = list(re.finditer(r"(?m)^([a-z_]+)\(\) \{$", text))
    for index, declaration in enumerate(declarations):
        if declaration.group(1) != name:
            continue
        end = declarations[index + 1].start() if index + 1 < len(declarations) else len(text)
        return text[declaration.end() : end]
    raise AssertionError(name)


def _validator_source(text: str) -> str:
    body = _function(text, "validate_safe_stdout")
    prefix = "  python3 -c '\n"
    start = body.index(prefix) + len(prefix)
    end = body.index("\n' \"$expected_cases_sha\"", start)
    return body[start:end]


def _review_validator_source(text: str) -> str:
    body = _function(text, "validate_review_stdout")
    prefix = "  python3 -c '\n"
    start = body.index(prefix) + len(prefix)
    end = body.index("\n' 2>/dev/null", start)
    return body[start:end]


def _safe_payload() -> dict[str, object]:
    return {
        "schema_version": "pilot50-safe-result-v1",
        "dataset_id": "pilot50_balanced_v1",
        "eval_run_id": "ask-eval-00000000-0000-4000-8000-000000000001",
        "runtime_git_sha": "1" * 40,
        "approval_id": "pilot50-owner-approval-20260810",
        "run_window_utc": {
            "started_at": "2026-08-10T12:00:00+00:00",
            "completed_at": "2026-08-10T12:30:00+00:00",
        },
        "billing_status": "pending_provider_reconciliation",
        "status": "OK",
        "classification": "calibration_only",
        "human_product_verdict": False,
        "denominator": 50,
        "counts": {"typical": 25, "atypical": 25},
        "mechanical_first_turn_closure": {
            "typical": {"closed": 22, "total": 25, "rate": 0.88},
            "atypical": {"closed": 18, "total": 25, "rate": 0.72},
            "overall": {"closed": 40, "total": 50, "rate": 0.8},
        },
        "policy_pass": {
            "typical": {"passed": 22, "total": 25, "rate": 0.88},
            "atypical": {"passed": 18, "total": 25, "rate": 0.72},
            "overall": {"passed": 40, "total": 50, "rate": 0.8},
        },
        "trace_coverage": {"found": 50, "total": 50, "rate": 1.0},
        "cache_hits": 0,
        "budget": {"max_rub": 20, "exceeded": False, "stopped": False},
        "pricing": {"complete": True, "stopped": False},
        "latency_ms": {"p50": 100, "p95": 250},
        "llm_cost_rub": 9.5,
        "cases_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "disclaimer": (
            "Tracked regression calibration only. This is a mechanical first-turn "
            "closure result for the balanced Pilot50 set, not an independent holdout, "
            "a human product verdict, ticket-level conversion, or production traffic "
            "conversion."
        ),
    }


def _run_validator(
    payload: dict[str, object],
    *,
    cases_sha: str = "a" * 64,
    report_sha: str = "b" * 64,
    runtime_sha: str = "1" * 40,
    approval_id: str = "pilot50-owner-approval-20260810",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _validator_source(_text()),
            cases_sha,
            report_sha,
            runtime_sha,
            approval_id,
        ],
        input=json.dumps(payload, ensure_ascii=False),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _review_rows() -> list[dict[str, object]]:
    return [
        {
            "ordinal": ordinal,
            "group": "typical" if ordinal <= 25 else "atypical",
            "query": f"Вопрос {ordinal}",
            "response": "" if ordinal == 50 else f"Ответ {ordinal}",
            "was_escalated": ordinal % 7 == 0,
            "escalation_reason": "low_confidence" if ordinal % 7 == 0 else None,
            "passed": ordinal % 5 != 0,
            "observed_behavior": "escalate" if ordinal % 7 == 0 else "answer",
        }
        for ordinal in range(1, 51)
    ]


def _run_review_validator(
    rows: list[dict[str, object]],
    *,
    raw_suffix: str = "",
) -> subprocess.CompletedProcess[str]:
    jsonl = "\n".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in rows
    )
    payload = (
        f"{jsonl}{raw_suffix}\npilot50-review-stream-complete-v1\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", _review_validator_source(_text())],
        input=payload.encode("utf-8"),
        check=False,
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return subprocess.CompletedProcess(
        args=result.args,
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8"),
        stderr=result.stderr.decode("utf-8"),
    )


def test_script_is_fail_closed_and_server_local_only() -> None:
    text = _text()

    assert text.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
    assert "umask 077" in text
    assert "exec 2>/dev/null" in text
    assert 'PILOT50_SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"' in text
    assert 'PILOT50_RUNTIME_CONTAINER="rosmol-app-ml"' in text
    assert 'PILOT50_TARGET="http://app-ml:8000/ask"' in text
    assert 'PILOT50_LEDGER_DIR="/var/lib/rosmol/eval-cost-ledger-v1"' in text
    assert 'pilot50_server_local=FAIL reason=%s' in text
    assert "acceptance_image_sha_mismatch" in text
    assert "acceptance_image_identity_mismatch" in text
    assert 'readlink -f -- "$TOOLING_ROOT"' in text
    assert 'sudo test ! -L "$PILOT50_ENV_FILE"' in text

    forbidden = (
        "ssh ",
        "scp ",
        "rsync ",
        "git pull",
        "docker compose up",
        "docker compose build",
        "docker compose restart",
        "docker restart",
        "/webhook/",
    )
    lowered = text.casefold()
    assert all(value.casefold() not in lowered for value in forbidden)


def test_modes_are_explicit_and_read_only_modes_have_no_ask_or_approval() -> None:
    text = _text()
    preflight = _function(text, "preflight_mode")
    run = _function(text, "run_mode")
    review = _function(text, "review_mode")

    assert "preflight | run | review" in _function(text, "validate_mode")
    assert "eval.run_ask" not in preflight
    assert "PILOT50_TARGET" not in preflight
    assert "HIGH_COST_APPROVAL_ID" not in preflight
    assert "PHASE0_BILLING_VERDICT" not in preflight
    assert "scripts/pilot50.py prepare" in preflight
    assert "eval.run_ask" in run
    assert "scripts/pilot50.py summarize" in run
    assert "scripts/pilot50.py show-safe" in run
    assert '--expected-runtime-git-sha "$RUNTIME_SHA"' in run
    assert '--expected-approval-id "$approval_id"' in run
    assert "eval.run_ask" not in review
    assert "PILOT50_TARGET" not in review
    assert "HIGH_COST_APPROVAL_ID" not in review
    assert "PHASE0_BILLING_VERDICT" not in review
    assert "export_phase0" not in review.casefold()
    assert "phase_a" not in review.casefold()
    assert "scripts/pilot50.py show-review" in review


def test_preflight_freezes_exact_clean_git_archive_and_balanced_cases() -> None:
    text = _text()
    common = _function(text, "load_common_state")
    preflight = _function(text, "preflight_mode")

    assert "status --porcelain=v1 --untracked-files=all" in common
    assert "tooling_source_not_clean" in common
    assert "ls-files --error-unmatch \"$PILOT50_MANIFEST_REL\"" in common
    assert "tooling_source_has_symlink" in common
    assert "git -C \"$TOOLING_ROOT\" archive --format=tar \"$TOOLING_SHA\"" in preflight
    assert "source_snapshot_mismatch" in _function(text, "verify_source_snapshot")
    assert "source_snapshot_extra_or_missing_path" in _function(
        text, "verify_source_snapshot"
    )
    assert "sudo git -c core.fileMode=false" in _function(
        text, "verify_source_snapshot"
    )
    assert "ls-tree -r -t -z --name-only" in _function(
        text, "verify_source_snapshot"
    )
    assert "find \"$SOURCE_DIR\" -mindepth 1 -printf '%P\\0'" in _function(
        text, "verify_source_snapshot"
    )
    assert 'sudo test ! -e "$SOURCE_DIR/.env.production"' in _function(
        text, "verify_source_snapshot"
    )
    assert "source_snapshot_writable" in _function(text, "verify_source_snapshot")
    assert "pilot50-cases.json" in preflight
    assert '"cases_total": 50' in _function(text, "validate_prepare_receipt")
    assert '"type_counts": {"typical": 25, "atypical": 25}' in _function(
        text, "validate_prepare_receipt"
    )
    assert "manifest_sha256=%s" in preflight
    assert "cases_sha256=%s" in preflight
    assert 'PILOT50_FORECAST_COST_RUB="10"' in text
    assert "forecast_llm_cost_rub=%s" in preflight
    assert text.count("validate_preflight_receipt") >= 3
    assert 'sudo mv -T -- "$STAGING_DIR" "$RUN_DIR"' in preflight
    assert "pilot50_base_not_regular" in preflight
    assert "cost_ledger_not_regular" in preflight
    assert "run_already_prepared_or_executed" in preflight


def test_acceptance_container_is_read_only_bound_and_never_built() -> None:
    text = _text()
    compose = _function(text, "build_compose_command")
    preflight = _function(text, "preflight_mode")
    run = _function(text, "run_mode")
    review = _function(text, "review_mode")

    assert '"ACCEPTANCE_SOURCE_DIR=$SOURCE_DIR"' in compose
    assert '"ACCEPTANCE_OUTPUT_DIR=$EVIDENCE_DIR"' in compose
    assert '"ACCEPTANCE_PROVENANCE_DIR=$PROVENANCE_DIR"' in compose
    assert '"ACCEPTANCE_COST_LEDGER_DIR=$PILOT50_LEDGER_DIR"' in compose
    assert "docker-compose.acceptance.yml" in compose
    assert "--profile acceptance" in compose
    for section in (preflight, run, review):
        assert "run --rm --no-deps --pull never" in section
        assert "--entrypoint python quality-acceptance" in section
    assert " up " not in text
    assert " build " not in text
    assert " restart " not in text


def test_compose_command_satisfies_inactive_phase0_required_bindings() -> None:
    text = _text()
    compose = _function(text, "build_compose_command")
    acceptance_compose = (ROOT / "docker-compose.acceptance.yml").read_text(
        encoding="utf-8"
    )
    required_phase0_variables = set(
        re.findall(r"\$\{(PHASE0_[A-Z0-9_]+):\?", acceptance_compose)
    )

    assert required_phase0_variables == {
        "PHASE0_RUNTIME_GIT_SHA",
        "PHASE0_RUNNER_SOURCE_DIR",
        "PHASE0_BUILDER_SOURCE_DIR",
        "PHASE0_PRIVATE_DIR",
        "PHASE0_COST_LEDGER_DIR",
    }
    assert all(f'"{name}=' in compose for name in required_phase0_variables)
    assert '"PHASE0_RUNTIME_GIT_SHA=$RUNTIME_SHA"' in compose
    assert '"PHASE0_RUNNER_SOURCE_DIR=$SOURCE_DIR"' in compose
    assert '"PHASE0_BUILDER_SOURCE_DIR=$PROVENANCE_DIR"' in compose
    assert '"PHASE0_PRIVATE_DIR=$EVIDENCE_DIR"' in compose
    assert '"PHASE0_COST_LEDGER_DIR=$PILOT50_LEDGER_DIR"' in compose
    assert "--profile phase0" not in compose


def test_run_is_sequential_bounded_trace_complete_and_cache_bypassed() -> None:
    text = _text()
    run = _function(text, "run_mode")

    assert 'PILOT50_COST_CAP_RUB="20"' in text
    assert '"${PHASE0_BILLING_VERDICT:-}" == "PASS"' in run
    assert 'approval_id="${HIGH_COST_APPROVAL_ID:-}"' in run
    assert "--concurrency 1" in run
    assert "--user-prefix" not in run
    assert '--max-llm-cost-rub "$PILOT50_COST_CAP_RUB"' in run
    assert '--high-cost-approval-id "$approval_id"' in run
    assert run.count('--expected-runtime-git-sha "$RUNTIME_SHA"') == 1
    assert "--bypass-cache" in run
    assert "--require-complete-traces" in run
    assert "--no-markdown" in run
    assert "--no-db-traces" not in run
    assert run.index("eval.run_ask") < run.index("scripts/pilot50.py summarize")


def test_run_is_one_shot_and_keeps_raw_and_safe_outputs_private() -> None:
    text = _text()
    run = _function(text, "run_mode")

    assert "run.started" in run
    assert "run.completed" in run
    assert "run_replay_refused" in run
    assert 'readlink -f -- "$PILOT50_LEDGER_DIR"' in run
    assert '"10001:10001:700"' in run
    assert "pilot50-ask-report.json" in run
    assert "pilot50-safe-result.json" in run
    assert 'report_sha="$(sudo sha256sum "$raw_report"' in run
    assert 'safe_result_sha="$(sudo sha256sum "$safe_result"' in run
    assert '"$final_cases_sha" "$report_sha" "$RUNTIME_SHA" "$approval_id"' in run
    assert "safe_result_sha256=%s" in run
    assert '"report_sha256=$report_sha"' in run
    assert "sudo chmod 0600" in run
    assert "ask_eval_failed" in run
    assert "summarize_failed" in run
    assert ">/dev/null 2>&1" in run
    assert "validate_completed_receipt" in run


def test_review_requires_exact_completed_current_run_and_writes_no_artifact() -> None:
    text = _text()
    review = _function(text, "review_mode")

    assert 'RUN_DIR="$PILOT50_BASE_DIR/runs/${PILOT50_DATASET_ID}-${RUNTIME_SHA}"' in text
    assert "verify_source_snapshot" in review
    assert "preflight.receipt" in review
    assert "run.completed" in review
    assert "validate_preflight_receipt" in review
    assert "validate_completed_receipt" in review
    assert review.count("sha256sum") >= 4
    assert "pilot50-ask-report.json" in review
    assert "pilot50-safe-result.json" in review
    assert "--output" not in review
    assert "eval.run_ask" not in review
    assert "scripts/pilot50.py prepare" not in review
    assert "scripts/pilot50.py summarize" not in review
    assert 'printf \'pilot50-review-stream-complete-v1\\n\'' in review
    tty_check = '[[ -t 1 ]] || fail "review_requires_owner_terminal"'
    assert tty_check in review
    assert review.index(tty_check) < review.index('sudo test -d "$RUN_DIR"')


def test_review_refuses_redirected_stdout_before_private_reads() -> None:
    bash = "bash"
    if os.name == "nt":
        bash_path = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if not bash_path.is_file():
            pytest.skip("Git Bash is unavailable")
        bash = str(bash_path)
    result = subprocess.run(
        [bash, str(SCRIPT), "review"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 1
    assert result.stdout == (
        "pilot50_server_local=FAIL reason=review_requires_owner_terminal\n"
    )
    assert result.stderr == ""


def test_review_cli_and_stdout_projection_are_exact_and_owner_only() -> None:
    text = _text()
    review = _function(text, "review_mode")
    validator = _function(text, "validate_review_stdout")

    expected_args = (
        'scripts/pilot50.py show-review \\\n'
        '      --manifest "/workspace/$PILOT50_MANIFEST_REL" \\\n'
        "      --cases /evidence/pilot50-cases.json \\\n"
        "      --report /evidence/pilot50-ask-report.json \\\n"
        "      --safe-result /evidence/pilot50-safe-result.json \\\n"
        '      --expected-runtime-git-sha "$RUNTIME_SHA"'
    )
    assert expected_args in review
    assert "validate_review_stdout" in review
    assert "set(row) == allowed" in validator
    assert "list(range(1, 51))" in validator
    assert '{"typical": 25, "atypical": 25}' in validator
    assert "max_bytes = 32 * 1024 * 1024" in validator
    assert "byte == 10 or (byte >= 32 and byte != 127)" in validator


def test_review_validator_accepts_exact_50_row_contract() -> None:
    result = _run_review_validator(_review_rows())

    assert result.returncode == 0, result.stderr
    output = [json.loads(line) for line in result.stdout.splitlines()]
    assert output == _review_rows()


def test_review_validator_rejects_extra_non_typed_unbalanced_or_control_rows() -> None:
    extra = copy.deepcopy(_review_rows())
    extra[0]["request_id"] = "private"
    non_typed = copy.deepcopy(_review_rows())
    non_typed[0]["was_escalated"] = 0
    unbalanced = copy.deepcopy(_review_rows())
    unbalanced[24]["group"] = "atypical"

    assert _run_review_validator(extra).returncode != 0
    assert _run_review_validator(non_typed).returncode != 0
    assert _run_review_validator(unbalanced).returncode != 0
    assert _run_review_validator(_review_rows(), raw_suffix="\t").returncode != 0


def test_final_stdout_is_strictly_allowlisted_safe_json() -> None:
    text = _text()
    validator = _function(text, "validate_safe_stdout")
    run = _function(text, "run_mode")

    for forbidden_key in ("query", "response", "request_id", "error", "trace_events"):
        assert f'"{forbidden_key}"' not in validator
    assert "set(payload) == allowed" in validator
    assert 'payload.get("schema_version") == "pilot50-safe-result-v1"' in validator
    assert 'payload.get("runtime_git_sha") == expected_runtime_sha' in validator
    assert 'payload.get("approval_id") == expected_approval_id' in validator
    assert 'payload.get("billing_status") == "pending_provider_reconciliation"' in validator
    assert 'set(run_window) == {"started_at", "completed_at"}' in validator
    assert 'payload.get("classification") == "calibration_only"' in validator
    assert 'payload.get("human_product_verdict") is False' in validator
    assert 'payload.get("denominator") == 50' in validator
    assert 'payload.get("counts") == {"typical": 25, "atypical": 25}' in validator
    assert 'payload.get("cache_hits") == 0' in validator
    assert 'payload.get("trace_coverage") == {"found": 50, "total": 50, "rate": 1.0}' in validator
    assert 'payload.get("pricing") == {"complete": True, "stopped": False}' in validator
    assert "0 <= cost <= 20" in validator
    assert "expected_disclaimer" in validator
    assert "safe_output_oversized" in run
    assert "safe_output_invalid" in run
    assert "runtime_not_ready_after_run" in run
    assert text.count("verify_runtime_ready") >= 3
    assert "printf 'pilot50_server_local=OK\\n'" in run
    assert "printf '%s\\n' \"$validated_safe\"" in run


def test_safe_validator_accepts_exact_contract_and_binds_actual_hashes() -> None:
    payload = _safe_payload()

    result = _run_validator(payload)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == payload


def test_safe_validator_rejects_nested_extra_or_free_text() -> None:
    nested_extra = copy.deepcopy(_safe_payload())
    nested_extra["mechanical_first_turn_closure"]["typical"]["query"] = "raw"  # type: ignore[index]
    free_text = _safe_payload()
    free_text["disclaimer"] = "arbitrary free text"

    assert _run_validator(nested_extra).returncode != 0
    assert _run_validator(free_text).returncode != 0


def test_safe_validator_rejects_actual_artifact_hash_mismatch() -> None:
    payload = _safe_payload()

    assert _run_validator(payload, cases_sha="c" * 64).returncode != 0
    assert _run_validator(payload, report_sha="d" * 64).returncode != 0


def test_safe_validator_rejects_metric_invariant_mismatches() -> None:
    bad_sum = copy.deepcopy(_safe_payload())
    bad_sum["policy_pass"]["overall"] = {"passed": 39, "total": 50, "rate": 0.78}  # type: ignore[index]
    closure_policy_mismatch = copy.deepcopy(_safe_payload())
    closure_policy_mismatch["mechanical_first_turn_closure"]["typical"] = {  # type: ignore[index]
        "closed": 21,
        "total": 25,
        "rate": 0.84,
    }
    closure_policy_mismatch["mechanical_first_turn_closure"]["overall"] = {  # type: ignore[index]
        "closed": 39,
        "total": 50,
        "rate": 0.78,
    }
    reversed_latency = copy.deepcopy(_safe_payload())
    reversed_latency["latency_ms"] = {"p50": 251, "p95": 250}

    assert _run_validator(bad_sum).returncode != 0
    assert _run_validator(closure_policy_mismatch).returncode != 0
    assert _run_validator(reversed_latency).returncode != 0


def test_safe_validator_rejects_run_identity_or_window_mismatch() -> None:
    payload = _safe_payload()
    bad_run_id = copy.deepcopy(payload)
    bad_run_id["eval_run_id"] = "ask-eval-not-a-uuid"
    bad_window = copy.deepcopy(payload)
    bad_window["run_window_utc"] = {
        "started_at": "2026-08-10T12:00:00+00:00",
        "completed_at": "2026-08-10T16:00:01+00:00",
    }
    non_utc_window = copy.deepcopy(payload)
    non_utc_window["run_window_utc"] = {
        "started_at": "2026-08-10T12:00:00+03:00",
        "completed_at": "2026-08-10T12:30:00+03:00",
    }
    bad_billing_status = copy.deepcopy(payload)
    bad_billing_status["billing_status"] = "reconciled"

    assert _run_validator(bad_run_id).returncode != 0
    assert _run_validator(bad_window).returncode != 0
    assert _run_validator(non_utc_window).returncode != 0
    assert _run_validator(bad_billing_status).returncode != 0
    assert _run_validator(payload, runtime_sha="2" * 40).returncode != 0
    assert _run_validator(payload, approval_id="different-approval-id").returncode != 0
