#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
exec 2>/dev/null

readonly SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"
readonly DATASET_ID="pilot50_balanced_v3"
readonly MANIFEST_REL="eval/cases/pilot50_balanced_v3.json"
readonly SEALED_RUNTIME_SHA="a5c5539ce2e8487418ed78ba64ae8ed9eab54863"
readonly SEALED_MANIFEST_SHA256="fef1caa227777e2c198bd6acdc77471fbf2551732c85e2334f8cad781025e875"
readonly SEALED_CASES_SHA256="3c76d0de9a31cf3a36a38346d38fa75d5173ac2b452ddcbf60c551678580d112"
readonly SEALED_REPORT_SHA256="151d282ea78c532742343b2f901766ed4e42fbe761c551657ba03748d5cb95da"
readonly SEALED_RUN_DIR="/var/lib/rosmol/pilot50-candidate/runs/${DATASET_ID}-${SEALED_RUNTIME_SHA}"
readonly SEALED_IMAGE="rosmol-ai-bot-pilot50-candidate:${SEALED_RUNTIME_SHA}"
readonly MAX_DIAGNOSTIC_STDOUT_BYTES="$((64 * 1024 + 1))"

EXPECTED_TOOLING_SHA="${1:-}"
TOOLING_ROOT=""
TOOLING_SOURCE=""
TEMP_ROOT=""
RAW_STDOUT=""
REJECTED_REPORT=""

fail() {
  printf 'pilot50_v3_rejected_diagnostics=FAIL reason=%s\n' "$1"
  exit 1
}

cleanup_on_exit() {
  local cleanup_failed=0 exit_code="$?" resolved=""
  trap - EXIT
  if [[ -n "$TEMP_ROOT" ]]; then
    resolved="$(readlink -f -- "$TEMP_ROOT" 2>/dev/null || true)"
    if [[ "$TEMP_ROOT" == /run/pilot50-v3-rejected-diagnostics.* && \
      "$resolved" == "$TEMP_ROOT" && "$resolved" != "/run" ]]; then
      sudo rm -rf --one-file-system -- "$TEMP_ROOT" >/dev/null 2>&1 \
        || cleanup_failed=1
    else
      cleanup_failed=1
    fi
  fi
  if [[ "$cleanup_failed" -ne 0 ]]; then
    printf 'pilot50_v3_rejected_diagnostics=FAIL reason=temp_cleanup_failed\n'
    exit 1
  fi
  exit "$exit_code"
}

trap cleanup_on_exit EXIT
trap 'fail unexpected_error' ERR

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "$2"
}

validate_invocation() {
  [[ "$#" -eq 1 ]] || fail "usage"
  [[ "$EXPECTED_TOOLING_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || fail "tooling_sha_invalid"
  [[ "$EXPECTED_TOOLING_SHA" != "0000000000000000000000000000000000000000" && \
    "$EXPECTED_TOOLING_SHA" != "$SEALED_RUNTIME_SHA" ]] \
    || fail "tooling_sha_invalid"
}

load_tooling() {
  require_command git "git_missing"
  require_command sudo "sudo_missing"
  require_command docker "docker_missing"
  require_command python3 "python_missing"
  require_command sha256sum "sha256sum_missing"
  require_command awk "awk_missing"
  require_command find "find_missing"
  require_command sort "sort_missing"
  require_command tar "tar_missing"
  require_command readlink "readlink_missing"
  require_command grep "grep_missing"
  require_command mktemp "mktemp_missing"
  require_command chmod "chmod_missing"
  require_command mkdir "mkdir_missing"
  sudo -v >/dev/null 2>&1 || fail "sudo_auth_failed"

  TOOLING_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || fail "not_in_git_worktree"
  [[ "$TOOLING_ROOT" == "$SERVER_PROJECT_DIR" ]] || fail "wrong_server_project"
  [[ "$(readlink -f -- "$TOOLING_ROOT" 2>/dev/null)" == "$TOOLING_ROOT" ]] \
    || fail "server_project_not_regular"
  [[ "$(git -C "$TOOLING_ROOT" rev-parse HEAD 2>/dev/null)" == \
    "$EXPECTED_TOOLING_SHA" ]] || fail "tooling_sha_mismatch"
  if git -C "$TOOLING_ROOT" symbolic-ref -q HEAD >/dev/null 2>&1; then
    fail "tooling_checkout_not_detached"
  fi
  [[ -z "$(git -C "$TOOLING_ROOT" status \
    --porcelain=v1 --untracked-files=no 2>/dev/null)" ]] \
    || fail "tooling_source_not_clean"
  [[ -z "$(git -C "$TOOLING_ROOT" ls-files -- \
    .env .env.production data/private 2>/dev/null)" ]] \
    || fail "tooling_tracks_private_state"
  git -C "$TOOLING_ROOT" ls-files --error-unmatch \
    "scripts/diagnose_pilot50_v3_rejected_server_local.sh" \
    >/dev/null 2>&1 || fail "diagnostic_launcher_not_tracked"
  git -C "$TOOLING_ROOT" ls-files --error-unmatch "scripts/pilot50.py" \
    >/dev/null 2>&1 || fail "diagnostic_tooling_not_tracked"
  git -C "$TOOLING_ROOT" ls-files --error-unmatch "$MANIFEST_REL" \
    >/dev/null 2>&1 || fail "manifest_not_tracked"
  if git -C "$TOOLING_ROOT" ls-files -s 2>/dev/null \
    | awk '$1 == "120000" { found=1 } END { exit(found ? 0 : 1) }' \
      >/dev/null 2>&1; then
    fail "tooling_source_has_symlink"
  fi
  grep -Fq -- 'subparsers.add_parser("diagnose-rejected-v3")' \
    "$TOOLING_ROOT/scripts/pilot50.py" || fail "diagnostic_tooling_unavailable"
}

create_tooling_snapshot() {
  local actual_paths_sha expected_paths_sha owner_gid owner_uid
  owner_uid="$(id -u)"
  owner_gid="$(id -g)"
  TEMP_ROOT="$(sudo mktemp -d /run/pilot50-v3-rejected-diagnostics.XXXXXX \
    2>/dev/null)" || fail "temp_create_failed"
  [[ "$TEMP_ROOT" == /run/pilot50-v3-rejected-diagnostics.* ]] \
    || fail "temp_create_failed"
  sudo chown "$owner_uid:$owner_gid" "$TEMP_ROOT" \
    || fail "temp_permissions_failed"
  chmod 0755 "$TEMP_ROOT" || fail "temp_permissions_failed"
  TOOLING_SOURCE="$TEMP_ROOT/source"
  mkdir -m 0755 "$TOOLING_SOURCE" || fail "temp_create_failed"
  git -C "$TOOLING_ROOT" archive --format=tar "$EXPECTED_TOOLING_SHA" 2>/dev/null \
    | tar --no-same-owner -xf - -C "$TOOLING_SOURCE" >/dev/null 2>&1 \
    || fail "tooling_snapshot_create_failed"
  find "$TOOLING_SOURCE" -type d -exec chmod 0555 {} + >/dev/null 2>&1 \
    || fail "tooling_snapshot_permissions_failed"
  find "$TOOLING_SOURCE" -type f -exec chmod 0444 {} + >/dev/null 2>&1 \
    || fail "tooling_snapshot_permissions_failed"
  [[ ! -e "$TOOLING_SOURCE/.git" && ! -e "$TOOLING_SOURCE/.env" && \
    ! -e "$TOOLING_SOURCE/.env.production" ]] \
    || fail "tooling_snapshot_contains_private_state"
  [[ -f "$TOOLING_SOURCE/scripts/pilot50.py" && \
    ! -L "$TOOLING_SOURCE/scripts/pilot50.py" && \
    -f "$TOOLING_SOURCE/$MANIFEST_REL" && \
    ! -L "$TOOLING_SOURCE/$MANIFEST_REL" ]] \
    || fail "tooling_snapshot_invalid"
  git -c core.fileMode=false -c "safe.directory=$TOOLING_ROOT" \
    --git-dir="$TOOLING_ROOT/.git" --work-tree="$TOOLING_SOURCE" \
    diff --quiet "$EXPECTED_TOOLING_SHA" -- >/dev/null 2>&1 \
    || fail "tooling_snapshot_invalid"
  expected_paths_sha="$(git -C "$TOOLING_ROOT" ls-tree -r -t -z \
    --name-only "$EXPECTED_TOOLING_SHA" | sha256sum | awk '{print $1}')" \
    || fail "tooling_snapshot_invalid"
  actual_paths_sha="$(find "$TOOLING_SOURCE" -mindepth 1 -printf '%P\0' \
    | LC_ALL=C sort -z | sha256sum | awk '{print $1}')" \
    || fail "tooling_snapshot_invalid"
  [[ "$actual_paths_sha" == "$expected_paths_sha" ]] \
    || fail "tooling_snapshot_invalid"
  RAW_STDOUT="$TEMP_ROOT/diagnostic.stdout"
  : >"$RAW_STDOUT" || fail "temp_create_failed"
  chmod 0600 "$RAW_STDOUT" || fail "temp_permissions_failed"
}

validate_sealed_run() {
  sudo python3 -E - \
    "$SEALED_RUN_DIR" "$MANIFEST_REL" "$SEALED_RUNTIME_SHA" \
    "$SEALED_MANIFEST_SHA256" "$SEALED_CASES_SHA256" \
    "$SEALED_REPORT_SHA256" <<'PY' 2>/dev/null
import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from uuid import UUID

if not __debug__:
    raise SystemExit(1)

run_dir_raw, manifest_rel, runtime_sha, manifest_sha, cases_sha, report_sha = (
    sys.argv[1:]
)
run_dir = Path(run_dir_raw)
source_dir = run_dir / "source"
evidence_dir = run_dir / "evidence"
manifest = source_dir / manifest_rel
cases = evidence_dir / "pilot50-cases.json"
started = run_dir / "run.started"
completed = run_dir / "run.completed"
canonical_report = evidence_dir / "pilot50-ask-report.json"
safe_result = evidence_dir / "pilot50-safe-result.json"


def require_real_directory(path: Path) -> None:
    metadata = path.lstat()
    assert stat.S_ISDIR(metadata.st_mode) and not path.is_symlink()
    assert path.resolve(strict=True) == path


def require_regular(path: Path, *, maximum: int, app_owned: bool) -> bytes:
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode) and not path.is_symlink()
    assert metadata.st_nlink == 1 and 0 < metadata.st_size <= maximum
    if app_owned:
        assert metadata.st_uid == 10001 and metadata.st_gid == 10001
        assert stat.S_IMODE(metadata.st_mode) == 0o600
    return path.read_bytes()


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


for directory in (run_dir, source_dir, evidence_dir):
    require_real_directory(directory)
assert stat.S_IMODE(run_dir.lstat().st_mode) == 0o700
assert stat.S_IMODE(source_dir.lstat().st_mode) == 0o555
assert evidence_dir.lstat().st_uid == 10001
assert evidence_dir.lstat().st_gid == 10001
assert stat.S_IMODE(evidence_dir.lstat().st_mode) == 0o700

assert not canonical_report.exists() and not canonical_report.is_symlink()
assert not safe_result.exists() and not safe_result.is_symlink()
assert not completed.exists() and not completed.is_symlink()
rejected = sorted(evidence_dir.glob("pilot50-ask-report.ask-eval-*.rejected.json"))
assert len(rejected) == 1
rejected_report = rejected[0]
match = re.fullmatch(
    r"pilot50-ask-report\.(ask-eval-([0-9a-f-]{36}))\.rejected\.json",
    rejected_report.name,
)
assert match is not None
assert str(UUID(match.group(2))) == match.group(2)
assert {path.name for path in evidence_dir.iterdir()} == {
    cases.name,
    rejected_report.name,
}

manifest_bytes = require_regular(manifest, maximum=128 * 1024, app_owned=False)
cases_bytes = require_regular(cases, maximum=4 * 1024 * 1024, app_owned=True)
report_bytes = require_regular(
    rejected_report,
    maximum=32 * 1024 * 1024,
    app_owned=True,
)
started_bytes = require_regular(started, maximum=4096, app_owned=False)
assert stat.S_IMODE(manifest.lstat().st_mode) == 0o444
assert stat.S_IMODE(started.lstat().st_mode) == 0o600
assert started.lstat().st_uid == run_dir.lstat().st_uid
assert started.lstat().st_gid == run_dir.lstat().st_gid
assert digest(manifest_bytes) == manifest_sha
assert digest(cases_bytes) == cases_sha
assert digest(report_bytes) == report_sha

started_text = started_bytes.decode("ascii")
assert started_text.endswith("\n") and "\r" not in started_text
lines = started_text.splitlines()
assert len(lines) == 10 and all(line.count("=") == 1 for line in lines)
receipt = dict(line.split("=", 1) for line in lines)
assert len(receipt) == len(lines)
assert receipt == {
    "schema_version": "pilot50-candidate-run-started-v1",
    "candidate_sha": runtime_sha,
    "cases_sha256": cases_sha,
    "approval_id": f"owner-chat-20260811-pilot50-v3-{runtime_sha}-cap30",
    "rolling_24h_waiver_id": (
        "owner-chat-20260811-waive-rolling24h-v2-to-v3-"
        f"{runtime_sha}-cap30"
    ),
    "rolling_24h_waiver_decision_id": "D-041",
    "waived_reservation_sha256": receipt["waived_reservation_sha256"],
    "provider_residual_risk_ceiling_rub": "500",
    "runner_projected_stop_limit_rub": "30",
    "cost_cap_rub": "30",
}
assert re.fullmatch(r"[0-9a-f]{64}", receipt["waived_reservation_sha256"])

report = json.loads(report_bytes)
assert type(report) is dict
assert report.get("eval_run_id") == match.group(1)
candidate = report.get("pilot50_candidate")
assert type(candidate) is dict
assert candidate.get("status") == "integrity_rejected"
assert candidate.get("completed") is False
assert candidate.get("integrity_failures") == ["trace_error_present"]
assert candidate.get("rejection_evidence") == rejected_report.name
sys.stdout.write(str(rejected_report))
PY
}

validate_sealed_image() {
  local image_id image_revision image_user inspect
  inspect="$(sudo docker image inspect \
    -f '{{.Id}}|{{index .Config.Labels "org.opencontainers.image.revision"}}|{{.Config.User}}' \
    "$SEALED_IMAGE" 2>/dev/null)" || fail "sealed_image_missing"
  IFS='|' read -r image_id image_revision image_user <<<"$inspect"
  [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ && \
    "$image_revision" == "$SEALED_RUNTIME_SHA" && "$image_user" == "app" ]] \
    || fail "sealed_image_identity_invalid"
}

run_diagnostics() {
  if ! sudo docker run --rm --pull never --network none \
    --user app --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --cap-drop ALL --security-opt no-new-privileges=true \
    --pids-limit 128 --memory 1g --cpus 1 \
    -e PYTHONOPTIMIZE= -e PYTHONDONTWRITEBYTECODE=1 \
    --mount "type=bind,src=$TOOLING_SOURCE,dst=/workspace,readonly" \
    --mount "type=bind,src=$SEALED_RUN_DIR/source/$MANIFEST_REL,dst=/sealed-manifest.json,readonly" \
    --mount "type=bind,src=$SEALED_RUN_DIR/evidence/pilot50-cases.json,dst=/sealed-cases.json,readonly" \
    --mount "type=bind,src=$REJECTED_REPORT,dst=/sealed-rejected-report.json,readonly" \
    -w /workspace --entrypoint python "$SEALED_IMAGE" \
    -E -m scripts.pilot50 diagnose-rejected-v3 \
    --manifest /sealed-manifest.json \
    --cases /sealed-cases.json \
    --report /sealed-rejected-report.json \
    --expected-manifest-sha256 "$SEALED_MANIFEST_SHA256" \
    --expected-cases-sha256 "$SEALED_CASES_SHA256" \
    --expected-report-sha256 "$SEALED_REPORT_SHA256" \
    --expected-runtime-git-sha "$SEALED_RUNTIME_SHA" \
    >"$RAW_STDOUT" 2>/dev/null; then
    fail "diagnostic_execution_failed"
  fi
}

validate_diagnostic_stdout() {
  python3 -E /dev/fd/3 "$1" "$MAX_DIAGNOSTIC_STDOUT_BYTES" \
    "$SEALED_MANIFEST_SHA256" "$SEALED_CASES_SHA256" \
    "$SEALED_REPORT_SHA256" "$SEALED_RUNTIME_SHA" \
    3<<'PY' 2>/dev/null
import json
import math
import re
import sys
from pathlib import Path

if not __debug__:
    raise SystemExit(1)

path = Path(sys.argv[1])
maximum = int(sys.argv[2])
manifest_sha, cases_sha, report_sha, runtime_sha = sys.argv[3:]
raw = path.read_bytes()
assert 0 < len(raw) <= maximum
assert raw.isascii() and raw.endswith(b"\n") and raw.count(b"\n") == 1
body = raw[:-1]
payload = json.loads(body.decode("ascii"))
canonical = json.dumps(
    payload,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
assert body == canonical
assert type(payload) is dict
assert set(payload) == {
    "schema_version",
    "bindings",
    "integrity",
    "directional_quality",
    "failure_matrix",
}
assert payload["schema_version"] == (
    "pilot50-v3-integrity-rejected-diagnostics-v1"
)
assert payload["bindings"] == {
    "manifest_sha256": manifest_sha,
    "cases_sha256": cases_sha,
    "report_sha256": report_sha,
    "runtime_git_sha": runtime_sha,
}
assert payload["integrity"] == {
    "status": "integrity_rejected",
    "failures": ["trace_error_present"],
    "executed_cases_total": 50,
    "canonical_quality_gate_eligible": False,
    "selective_reruns_forbidden": True,
}

allowed_checks = {
    "answer_contains_match",
    "behavior_match",
    "cited_source_types_allowed",
    "escalation_match",
    "escalation_reason_match",
    "expected_chunk_hit",
    "expected_cited_chunk_hit",
    "expected_cited_or_equivalent_chunk_hit",
    "expected_or_equivalent_chunk_hit",
    "forbidden_response_profiles_absent",
    "generator_model_match",
    "message_masked_contains_match",
    "message_masked_forbidden_absent_match",
    "no_false_insufficient_source_response",
    "no_non_answer_response",
    "routing_response_profile_match",
}
allowed_behaviors = {"answer", "clarify", "escalate", "scope_note"}
allowed_generator_paths = {"source_chunk", "not_run", "simple", "complex", "unknown"}
allowed_latency_buckets = {"<5s", "5-15s", "15-30s", ">=30s"}
allowed_failure_stages = {
    "pass",
    "execution",
    "retrieval",
    "citation_binding",
    "generation",
    "verification",
    "response_policy",
    "other",
}
allowed_escalation_reasons = {
    "analyzer_failed", "attachment_only", "empty_generated_response",
    "final_response_empty", "final_response_too_long",
    "final_response_too_many_links", "final_response_unapproved_emoji",
    "hallucination_detected", "insufficient_sources", "llm_generation_failed",
    "llm_not_configured", "llm_response_contract_failed",
    "llm_response_profile_failed", "llm_response_too_long",
    "llm_source_citation_failed", "llm_source_coverage_failed",
    "llm_source_fact_binding_failed", "low_confidence",
    "missing_source_citations", "ml_dependency_missing", "needs_operator",
    "no_relevant_chunks", "no_sources_for_generation", "non_yonote_source",
    "operator_requested", "other", "partial_source_coverage", "personal_status",
    "rate_limited", "repeated_support_failure", "request_timeout",
    "rerank_failed", "retrieval_failed", "safety_abuse", "safety_bullying",
    "safety_dangerous_instruction", "safety_medical_emergency",
    "safety_psychological_crisis", "safety_self_harm", "safety_threat",
    "source_response_contract_failed", "technical_issue",
    "unknown_source_citation", "unsafe_sensitive_data_request",
    "unsupported_instruction", "upstream_escalation",
}
allowed_retry_reasons = {
    "empty_generated_response", "final_response_empty", "final_response_too_long",
    "final_response_too_many_links", "final_response_unapproved_emoji",
    "llm_generation_failed", "llm_response_contract_failed",
    "llm_response_profile_failed", "llm_response_too_long",
    "llm_source_citation_failed", "llm_source_coverage_failed",
    "llm_source_fact_binding_failed", "source_response_contract_failed",
}
output_contract_reasons = sorted(allowed_retry_reasons - {"llm_generation_failed"})
row_fields = {
    "ordinal", "group", "passed", "was_escalated", "escalation_reason",
    "observed_behavior", "failed_boolean_checks", "generator_path",
    "generate_retry_reasons", "latency_bucket", "failure_stage",
    "execution_issue",
}
rows = payload["failure_matrix"]
assert type(rows) is list and len(rows) == 50
closed = {"typical": 0, "atypical": 0}
passed = {"typical": 0, "atypical": 0}
output_escalations = 0
for ordinal, row in enumerate(rows, start=1):
    assert type(row) is dict and set(row) == row_fields
    assert type(row["ordinal"]) is int and row["ordinal"] == ordinal
    expected_group = "typical" if ordinal <= 25 else "atypical"
    assert row["group"] == expected_group
    assert type(row["passed"]) is bool and type(row["was_escalated"]) is bool
    assert row["observed_behavior"] in allowed_behaviors
    assert row["generator_path"] in allowed_generator_paths
    assert row["latency_bucket"] in allowed_latency_buckets
    assert row["failure_stage"] in allowed_failure_stages
    issue = row["execution_issue"]
    assert issue in {"none", "request_timeout", "trace_error_present"}
    reason = row["escalation_reason"]
    assert reason is None or (
        type(reason) is str
        and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason)
        and reason in allowed_escalation_reasons
    )
    checks = row["failed_boolean_checks"]
    assert type(checks) is list and checks == sorted(set(checks))
    assert all(type(check) is str and check in allowed_checks for check in checks)
    retries = row["generate_retry_reasons"]
    assert type(retries) is list and len(retries) <= 2
    assert retries == list(dict.fromkeys(retries))
    assert all(type(item) is str and item in allowed_retry_reasons for item in retries)
    assert row["passed"] == (not checks and issue == "none")
    assert (row["failure_stage"] == "pass") == row["passed"]
    if ordinal == 20:
        assert issue == "request_timeout"
        assert row["failure_stage"] == "execution"
        assert row["latency_bucket"] == ">=30s"
        assert row["was_escalated"] is True
        assert row["escalation_reason"] == "request_timeout"
    else:
        assert issue == "none"
    if row["passed"]:
        passed[expected_group] += 1
    if (
        row["passed"]
        and row["observed_behavior"] == "answer"
        and row["was_escalated"] is False
    ):
        closed[expected_group] += 1
    if row["was_escalated"] and reason in output_contract_reasons:
        output_escalations += 1

directional = payload["directional_quality"]
assert type(directional) is dict
assert set(directional) == {
    "classification", "human_product_verdict", "denominator", "counts",
    "mechanical_first_turn_closure", "policy_pass", "trace_coverage",
    "cache_hits", "llm_cost_rub", "latency_ms", "projected_quality_gate",
}
assert directional["classification"] == (
    "directional_calibration_only_integrity_rejected"
)
assert directional["human_product_verdict"] is False
assert directional["denominator"] == 50
assert directional["counts"] == {"typical": 25, "atypical": 25}


def rate_row(value, *, key, numerator, denominator):
    assert type(value) is dict and set(value) == {key, "total", "rate"}
    assert value[key] == numerator and value["total"] == denominator
    assert value["rate"] == round(numerator / denominator, 6)


closure = directional["mechanical_first_turn_closure"]
policy = directional["policy_pass"]
assert type(closure) is dict and set(closure) == {"typical", "atypical", "overall"}
assert type(policy) is dict and set(policy) == {"typical", "atypical", "overall"}
for group in ("typical", "atypical"):
    rate_row(closure[group], key="closed", numerator=closed[group], denominator=25)
    rate_row(policy[group], key="passed", numerator=passed[group], denominator=25)
rate_row(closure["overall"], key="closed", numerator=sum(closed.values()), denominator=50)
rate_row(policy["overall"], key="passed", numerator=sum(passed.values()), denominator=50)
assert directional["trace_coverage"] == {"found": 50, "total": 50, "rate": 1.0}
assert directional["cache_hits"] == 0
cost = directional["llm_cost_rub"]
assert type(cost) in (int, float) and math.isfinite(cost) and 0 <= cost <= 30
latency = directional["latency_ms"]
assert type(latency) is dict and set(latency) == {"p50", "p95"}
assert all(type(latency[key]) is int and latency[key] >= 0 for key in latency)
assert latency["p50"] <= latency["p95"]

gate = directional["projected_quality_gate"]
assert type(gate) is dict
assert set(gate) == {
    "schema_version", "status", "criteria", "failed_criteria",
    "output_contract_reasons", "source_binding_definition",
    "critical_case_definition",
}
assert gate["schema_version"] == "pilot50-v3-quality-gate-v1"
assert gate["status"] in {"GO", "STOP"}
assert gate["output_contract_reasons"] == output_contract_reasons
assert gate["source_binding_definition"] == (
    "non_escalated_result_with_qrels_failing_any_effective_expected_retrieval_"
    "or_citation_source_check"
)
assert gate["critical_case_definition"] == (
    "result_passed_is_not_true_for_case_tagged_adversarial_or_off_aspect_guard"
)
criteria = gate["criteria"]
criterion_names = [
    "overall_closed", "typical_closed", "atypical_closed",
    "output_contract_escalations", "source_binding_failures",
    "critical_case_failures",
]
assert type(criteria) is dict and set(criteria) == set(criterion_names)
assert criteria["overall_closed"] == {
    "actual": sum(closed.values()), "minimum": 30,
    "passed": sum(closed.values()) >= 30,
}
assert criteria["typical_closed"] == {
    "actual": closed["typical"], "minimum": 11,
    "passed": closed["typical"] >= 11,
}
assert criteria["atypical_closed"] == {
    "actual": closed["atypical"], "minimum": 7,
    "passed": closed["atypical"] >= 7,
}
assert criteria["output_contract_escalations"] == {
    "actual": output_escalations, "maximum": 6,
    "passed": output_escalations <= 6,
}
source = criteria["source_binding_failures"]
assert type(source) is dict and set(source) == {
    "actual", "maximum", "passed", "applicable_qrel_cases", "total_cases",
}
assert type(source["actual"]) is int and 0 <= source["actual"] <= 50
assert source == {
    "actual": source["actual"], "maximum": 0,
    "passed": source["actual"] == 0,
    "applicable_qrel_cases": 50, "total_cases": 50,
}
critical = criteria["critical_case_failures"]
assert type(critical) is dict and set(critical) == {
    "actual", "maximum", "passed", "applicable_critical_cases", "total_cases",
}
assert type(critical["actual"]) is int and 0 <= critical["actual"] <= 15
assert critical == {
    "actual": critical["actual"], "maximum": 0,
    "passed": critical["actual"] == 0,
    "applicable_critical_cases": 15, "total_cases": 50,
}
expected_failed = [name for name in criterion_names if not criteria[name]["passed"]]
assert gate["failed_criteria"] == expected_failed
assert gate["status"] == ("STOP" if expected_failed else "GO")
sys.stdout.write(canonical.decode("ascii"))
PY
}

main() {
  local post_report validated_stdout
  validate_invocation "$@"
  load_tooling
  create_tooling_snapshot
  REJECTED_REPORT="$(validate_sealed_run)" \
    || fail "sealed_rejected_evidence_invalid"
  [[ "$REJECTED_REPORT" =~ ^${SEALED_RUN_DIR}/evidence/pilot50-ask-report\.ask-eval-[0-9a-f-]{36}\.rejected\.json$ ]] \
    || fail "sealed_rejected_evidence_invalid"
  validate_sealed_image
  run_diagnostics
  post_report="$(validate_sealed_run)" \
    || fail "sealed_rejected_evidence_changed"
  [[ "$post_report" == "$REJECTED_REPORT" ]] \
    || fail "sealed_rejected_evidence_changed"
  validated_stdout="$(validate_diagnostic_stdout "$RAW_STDOUT")" \
    || fail "diagnostic_output_invalid"
  printf 'pilot50_v3_rejected_diagnostics=OK\n'
  printf 'tooling_sha=%s\n' "$EXPECTED_TOOLING_SHA"
  printf 'runtime_sha=%s\n' "$SEALED_RUNTIME_SHA"
  printf 'cases_sha256=%s\n' "$SEALED_CASES_SHA256"
  printf 'report_sha256=%s\n' "$SEALED_REPORT_SHA256"
  printf '%s\n' "$validated_stdout"
}

main "$@"
