#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
exec 2>/dev/null

readonly SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"
readonly DATASET_ID="pilot50_balanced_v2"
readonly MANIFEST_REL="eval/cases/pilot50_balanced_v2.json"
readonly SEALED_RUNTIME_SHA="64cc182d37a3c060439ed7a55f5cc875a27d786d"
readonly SEALED_MANIFEST_SHA256="6995b96b4658f53e40a0bb982145465cbc347d9df041fc4dd66a9d15687b822b"
readonly SEALED_CASES_SHA256="b027e469e062682b6dc341b2dd4c87440edffb1955c2111f38e6c44a92a3a14d"
readonly SEALED_REPORT_SHA256="07fdfebf505e3df9b2461386e37f89a836dd80f3a5c445ec93bfca765e47add9"
readonly SEALED_SAFE_RESULT_SHA256="4e5b0ebb4e04b9d449e7ed54db9a1167c19cce02ef27839073fba280e435b61d"
readonly SEALED_PRODUCTION_SNAPSHOT_SHA256="150e8661257b7c7bd0495aec92476654d2aec156d090bc34a0373c551a20ad1a"
readonly SEALED_QDRANT_COUNT="2152"
readonly SEALED_QDRANT_FINGERPRINT_SHA256="f753b69665f216039b944546886f611410107e1344e52b159ab3f221b60aefa5"
readonly SEALED_QDRANT_SEED_SHA256="aead5e930c513d9d5aeaacd3f3d4b8ce99fab536434343e7fcd6e9917de93e8a"
readonly SEALED_RUN_DIR="/var/lib/rosmol/pilot50-candidate/runs/${DATASET_ID}-${SEALED_RUNTIME_SHA}"
readonly SEALED_IMAGE="rosmol-ai-bot-pilot50-candidate:${SEALED_RUNTIME_SHA}"
readonly MAX_DIAGNOSTIC_STDOUT_BYTES="$((64 * 1024 + 1))"

EXPECTED_TOOLING_SHA="${1:-}"
TOOLING_ROOT=""
TOOLING_SOURCE=""
TEMP_ROOT=""
RAW_STDOUT=""

fail() {
  printf 'pilot50_candidate_diagnostics=FAIL reason=%s\n' "$1"
  exit 1
}

cleanup_on_exit() {
  local cleanup_failed=0 exit_code="$?" resolved=""
  trap - EXIT
  if [[ -n "$TEMP_ROOT" ]]; then
    resolved="$(readlink -f -- "$TEMP_ROOT" 2>/dev/null || true)"
    if [[ "$TEMP_ROOT" == /run/pilot50-candidate-diagnostics.* && \
      "$resolved" == "$TEMP_ROOT" && "$resolved" != "/run" ]]; then
      sudo rm -rf --one-file-system -- "$TEMP_ROOT" >/dev/null 2>&1 \
        || cleanup_failed=1
    else
      cleanup_failed=1
    fi
  fi
  if [[ "$cleanup_failed" -ne 0 ]]; then
    printf 'pilot50_candidate_diagnostics=FAIL reason=temp_cleanup_failed\n'
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
    "scripts/diagnose_pilot50_candidate_server_local.sh" \
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
  grep -Fq -- 'subparsers.add_parser("diagnose")' \
    "$TOOLING_ROOT/scripts/pilot50.py" || fail "diagnostic_tooling_unavailable"
}

create_tooling_snapshot() {
  local actual_paths_sha expected_paths_sha owner_gid owner_uid
  owner_uid="$(id -u)"
  owner_gid="$(id -g)"
  TEMP_ROOT="$(sudo mktemp -d /run/pilot50-candidate-diagnostics.XXXXXX \
    2>/dev/null)" || fail "temp_create_failed"
  [[ "$TEMP_ROOT" == /run/pilot50-candidate-diagnostics.* ]] \
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
    "$SEALED_REPORT_SHA256" "$SEALED_SAFE_RESULT_SHA256" \
    "$SEALED_PRODUCTION_SNAPSHOT_SHA256" "$SEALED_QDRANT_COUNT" \
    "$SEALED_QDRANT_FINGERPRINT_SHA256" "$SEALED_QDRANT_SEED_SHA256" \
    <<'PY' >/dev/null 2>&1
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

if not __debug__:
    raise SystemExit(1)

(
    run_dir_raw,
    manifest_rel,
    runtime_sha,
    manifest_sha,
    cases_sha,
    report_sha,
    safe_sha,
    production_sha,
    qdrant_count,
    qdrant_fingerprint,
    qdrant_seed,
) = sys.argv[1:]
run_dir = Path(run_dir_raw)
source_dir = run_dir / "source"
evidence_dir = run_dir / "evidence"
manifest = source_dir / manifest_rel
cases = evidence_dir / "pilot50-cases.json"
report = evidence_dir / "pilot50-ask-report.json"
safe_result = evidence_dir / "pilot50-safe-result.json"
completed = run_dir / "run.completed"


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

manifest_bytes = require_regular(manifest, maximum=128 * 1024, app_owned=False)
cases_bytes = require_regular(cases, maximum=4 * 1024 * 1024, app_owned=True)
report_bytes = require_regular(report, maximum=32 * 1024 * 1024, app_owned=True)
safe_bytes = require_regular(safe_result, maximum=128 * 1024, app_owned=True)
completed_bytes = require_regular(completed, maximum=4096, app_owned=False)
assert stat.S_IMODE(manifest.lstat().st_mode) == 0o444
assert stat.S_IMODE(completed.lstat().st_mode) == 0o600
assert completed.lstat().st_uid == run_dir.lstat().st_uid
assert completed.lstat().st_gid == run_dir.lstat().st_gid
assert digest(manifest_bytes) == manifest_sha
assert digest(cases_bytes) == cases_sha
assert digest(report_bytes) == report_sha
assert digest(safe_bytes) == safe_sha

receipt_text = completed_bytes.decode("ascii")
assert receipt_text.endswith("\n") and "\r" not in receipt_text
lines = receipt_text.splitlines()
assert len(lines) == 10 and all(line.count("=") == 1 for line in lines)
receipt = dict(line.split("=", 1) for line in lines)
assert len(receipt) == len(lines)
assert receipt == {
    "schema_version": "pilot50-candidate-run-completed-v1",
    "candidate_sha": runtime_sha,
    "cases_sha256": cases_sha,
    "report_sha256": report_sha,
    "safe_result_sha256": safe_sha,
    "quality_status": "STOP",
    "production_snapshot_sha256": production_sha,
    "qdrant_count": qdrant_count,
    "qdrant_fingerprint_sha256": qdrant_fingerprint,
    "qdrant_seed_sha256": qdrant_seed,
}

safe = json.loads(safe_bytes)
assert isinstance(safe, dict)
assert safe.get("dataset_id") == "pilot50_balanced_v2"
assert safe.get("runtime_git_sha") == runtime_sha
assert safe.get("cases_sha256") == cases_sha
assert safe.get("report_sha256") == report_sha
quality_gate = safe.get("quality_gate")
assert isinstance(quality_gate, dict) and quality_gate.get("status") == "STOP"

report_payload = json.loads(report_bytes)
assert isinstance(report_payload, dict)
assert report_payload.get("cases_file_sha256") == cases_sha
runtime_identity = report_payload.get("runtime_identity")
assert isinstance(runtime_identity, dict)
assert runtime_identity.get("status") == "verified"
for field in (
    "expected_runtime_git_sha",
    "preflight_release_git_sha",
    "postflight_release_git_sha",
    "verified_release_git_sha",
):
    assert runtime_identity.get(field) == runtime_sha
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
    --mount "type=bind,src=$SEALED_RUN_DIR/evidence,dst=/evidence,readonly" \
    -w /workspace --entrypoint python "$SEALED_IMAGE" \
    -E -m scripts.pilot50 diagnose \
    --manifest /sealed-manifest.json \
    --cases /evidence/pilot50-cases.json \
    --report /evidence/pilot50-ask-report.json \
    --safe-result /evidence/pilot50-safe-result.json \
    --expected-manifest-sha256 "$SEALED_MANIFEST_SHA256" \
    --expected-cases-sha256 "$SEALED_CASES_SHA256" \
    --expected-report-sha256 "$SEALED_REPORT_SHA256" \
    --expected-safe-result-sha256 "$SEALED_SAFE_RESULT_SHA256" \
    --expected-runtime-git-sha "$SEALED_RUNTIME_SHA" \
    >"$RAW_STDOUT" 2>/dev/null; then
    fail "diagnostic_execution_failed"
  fi
}

validate_diagnostic_stdout() {
  python3 -E /dev/fd/3 "$1" "$MAX_DIAGNOSTIC_STDOUT_BYTES" \
    "$SEALED_MANIFEST_SHA256" "$SEALED_CASES_SHA256" \
    "$SEALED_REPORT_SHA256" "$SEALED_SAFE_RESULT_SHA256" \
    3<<'PY' 2>/dev/null
import json
import re
import sys
from pathlib import Path

if not __debug__:
    raise SystemExit(1)

path = Path(sys.argv[1])
maximum = int(sys.argv[2])
manifest_sha, cases_sha, report_sha, safe_sha = sys.argv[3:]
raw = path.read_bytes()
assert 0 < len(raw) <= maximum
assert raw.isascii() and raw.endswith(b"\n") and raw.count(b"\n") == 1
body = raw[:-1]
assert len(body) <= maximum - 1
payload = json.loads(body.decode("ascii"))
canonical = json.dumps(
    payload,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
assert body == canonical
assert type(payload) is dict
assert set(payload) == {"schema_version", "bindings", "failure_matrix"}
assert payload["schema_version"] == "pilot50-v2-failure-diagnostics-v1"
bindings = payload["bindings"]
assert type(bindings) is dict
assert bindings == {
    "manifest_sha256": manifest_sha,
    "cases_sha256": cases_sha,
    "report_sha256": report_sha,
    "safe_result_sha256": safe_sha,
    "quality_status": "STOP",
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
allowed_escalation_reasons = {
    "analyzer_failed",
    "attachment_only",
    "empty_generated_response",
    "final_response_empty",
    "final_response_too_long",
    "final_response_too_many_links",
    "final_response_unapproved_emoji",
    "hallucination_detected",
    "insufficient_sources",
    "llm_generation_failed",
    "llm_not_configured",
    "llm_response_contract_failed",
    "llm_response_profile_failed",
    "llm_response_too_long",
    "llm_source_citation_failed",
    "llm_source_coverage_failed",
    "llm_source_fact_binding_failed",
    "low_confidence",
    "missing_source_citations",
    "ml_dependency_missing",
    "needs_operator",
    "no_relevant_chunks",
    "no_sources_for_generation",
    "non_yonote_source",
    "operator_requested",
    "other",
    "partial_source_coverage",
    "personal_status",
    "rate_limited",
    "repeated_support_failure",
    "request_timeout",
    "rerank_failed",
    "retrieval_failed",
    "safety_abuse",
    "safety_bullying",
    "safety_dangerous_instruction",
    "safety_medical_emergency",
    "safety_psychological_crisis",
    "safety_self_harm",
    "safety_threat",
    "source_response_contract_failed",
    "technical_issue",
    "unknown_source_citation",
    "unsafe_sensitive_data_request",
    "unsupported_instruction",
    "upstream_escalation",
}
allowed_retry_reasons = {
    "empty_generated_response",
    "final_response_empty",
    "final_response_too_long",
    "final_response_too_many_links",
    "final_response_unapproved_emoji",
    "llm_generation_failed",
    "llm_response_contract_failed",
    "llm_response_profile_failed",
    "llm_response_too_long",
    "llm_source_citation_failed",
    "llm_source_coverage_failed",
    "llm_source_fact_binding_failed",
    "source_response_contract_failed",
}
row_fields = {
    "ordinal",
    "group",
    "passed",
    "was_escalated",
    "escalation_reason",
    "observed_behavior",
    "failed_boolean_checks",
    "generator_path",
    "generate_retry_reasons",
    "latency_bucket",
}
rows = payload["failure_matrix"]
assert type(rows) is list and len(rows) == 50
passed_by_group = {"typical": 0, "atypical": 0}
for ordinal, row in enumerate(rows, start=1):
    assert type(row) is dict and set(row) == row_fields
    assert type(row["ordinal"]) is int and row["ordinal"] == ordinal
    expected_group = "typical" if ordinal <= 25 else "atypical"
    assert row["group"] == expected_group
    assert type(row["passed"]) is bool
    assert type(row["was_escalated"]) is bool
    assert row["observed_behavior"] in allowed_behaviors
    assert row["generator_path"] in allowed_generator_paths
    assert row["latency_bucket"] in allowed_latency_buckets
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
    assert all(
        type(retry) is str and retry in allowed_retry_reasons for retry in retries
    )
    assert row["passed"] is (not checks)
    if row["passed"]:
        passed_by_group[expected_group] += 1
assert passed_by_group == {"typical": 17, "atypical": 8}
sys.stdout.write(canonical.decode("ascii"))
PY
}

main() {
  local validated_stdout
  validate_invocation "$@"
  load_tooling
  create_tooling_snapshot
  validate_sealed_run || fail "sealed_evidence_invalid"
  validate_sealed_image
  run_diagnostics
  validate_sealed_run || fail "sealed_evidence_changed"
  validated_stdout="$(validate_diagnostic_stdout "$RAW_STDOUT")" \
    || fail "diagnostic_output_invalid"
  printf 'pilot50_candidate_diagnostics=OK\n'
  printf 'tooling_sha=%s\n' "$EXPECTED_TOOLING_SHA"
  printf 'runtime_sha=%s\n' "$SEALED_RUNTIME_SHA"
  printf 'cases_sha256=%s\n' "$SEALED_CASES_SHA256"
  printf 'report_sha256=%s\n' "$SEALED_REPORT_SHA256"
  printf 'safe_result_sha256=%s\n' "$SEALED_SAFE_RESULT_SHA256"
  printf '%s\n' "$validated_stdout"
}

main "$@"
