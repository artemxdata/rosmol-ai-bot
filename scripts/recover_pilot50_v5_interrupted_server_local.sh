#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
exec 2>/dev/null

readonly MODE="${1:-}"
readonly EXPECTED_TOOLING_SHA="${2:-}"
readonly SEALED_CANDIDATE_SHA="${3:-}"
readonly ALLOWED_CANDIDATE_SHA="e3277e88ee3bf46ab3d375beed740f96248d53bc"
readonly SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"
readonly BASE_DIR="/var/lib/rosmol/pilot50-candidate"
readonly DATASET_ID="pilot50_balanced_v5"
readonly MANIFEST_REL="eval/cases/pilot50_balanced_v5.json"
readonly EXPECTED_MANIFEST_SHA256="12747d62190cc5e70d70490e9a649d91596ec69a316b5c2de3843ac3df6f85b4"
readonly EXPECTED_CASES_SHA256="9d53114722191330214f5917ee3baf4ccfcf4eb644be34a0253c60531b225529"
readonly CANDIDATE_CONTRACT_ID="pilot50-v5-recheck-v1"
readonly APPROVAL_ID="owner-chat-20260816-pilot50-v5-${SEALED_CANDIDATE_SHA}-cap30"
readonly CANDIDATE_IMAGE="rosmol-ai-bot-pilot50-candidate:${SEALED_CANDIDATE_SHA}"
readonly CANDIDATE_CONTAINER="rosmol-pilot50-candidate-ml"
readonly RUN_DIR="${BASE_DIR}/runs/${DATASET_ID}-${SEALED_CANDIDATE_SHA}"
readonly SOURCE_DIR="${RUN_DIR}/source"
readonly EVIDENCE_DIR="${RUN_DIR}/evidence"
readonly RECOVERY_ROOT="${BASE_DIR}/recoveries"
readonly RECOVERY_DIR="${RECOVERY_ROOT}/${DATASET_ID}-${SEALED_CANDIDATE_SHA}-${EXPECTED_TOOLING_SHA}"

TOOLING_ROOT=""
STAGING_DIR=""

fail() {
  printf 'pilot50_v5_interrupted_recovery=FAIL reason=%s\n' "$1"
  exit 1
}

cleanup_staging() {
  [[ -n "$STAGING_DIR" ]] || return 0
  if [[ "$STAGING_DIR" != "$RECOVERY_ROOT/.staging-${SEALED_CANDIDATE_SHA}-"* ]]; then
    return 1
  fi
  if sudo test -e "$STAGING_DIR" || sudo test -L "$STAGING_DIR"; then
    sudo test -d "$STAGING_DIR" && sudo test ! -L "$STAGING_DIR" || return 1
    [[ "$(sudo readlink -f -- "$STAGING_DIR" 2>/dev/null)" == "$STAGING_DIR" ]] \
      || return 1
    sudo rm -rf --one-file-system -- "$STAGING_DIR" >/dev/null 2>&1 || return 1
  fi
  STAGING_DIR=""
}

cleanup_on_exit() {
  local exit_code="$?"
  trap - EXIT
  if ! cleanup_staging; then
    printf 'pilot50_v5_interrupted_recovery_cleanup=FAIL\n'
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
  [[ "$#" -eq 3 && "$MODE" == "recover" ]] || fail "usage"
  [[ "$EXPECTED_TOOLING_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || fail "tooling_sha_invalid"
  [[ "$SEALED_CANDIDATE_SHA" == "$ALLOWED_CANDIDATE_SHA" ]] \
    || fail "candidate_sha_invalid"
  [[ -t 1 ]] || fail "owner_terminal_required"
}

load_tooling() {
  for command_name in git sudo docker python3 sha256sum find sort readlink awk cut; do
    require_command "$command_name" "${command_name}_missing"
  done
  sudo -v >/dev/null 2>&1 || fail "sudo_auth_failed"
  TOOLING_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || fail "not_in_git_worktree"
  [[ "$TOOLING_ROOT" == "$SERVER_PROJECT_DIR" ]] || fail "wrong_server_project"
  [[ "$(readlink -f -- "$TOOLING_ROOT" 2>/dev/null)" == "$TOOLING_ROOT" ]] \
    || fail "server_project_not_regular"
  [[ "$(git -C "$TOOLING_ROOT" rev-parse HEAD 2>/dev/null)" == \
    "$EXPECTED_TOOLING_SHA" ]] || fail "tooling_sha_mismatch"
  ! git -C "$TOOLING_ROOT" symbolic-ref -q HEAD >/dev/null 2>&1 \
    || fail "tooling_checkout_not_detached"
  [[ -z "$(git -C "$TOOLING_ROOT" status \
    --porcelain=v1 --untracked-files=all 2>/dev/null)" ]] \
    || fail "tooling_worktree_not_clean"
  git -C "$TOOLING_ROOT" cat-file -e \
    "${SEALED_CANDIDATE_SHA}^{commit}" 2>/dev/null \
    || fail "candidate_commit_missing"
  git -C "$TOOLING_ROOT" ls-files --error-unmatch \
    scripts/recover_pilot50_v5_interrupted_server_local.sh >/dev/null 2>&1 \
    || fail "recovery_tool_not_tracked"
}

source_paths_sha() {
  git -C "$TOOLING_ROOT" ls-tree -r -t -z --name-only \
    "$SEALED_CANDIDATE_SHA" | sha256sum | cut -d ' ' -f 1
}

verify_source_snapshot() {
  local actual bad_permissions expected
  sudo test -d "$SOURCE_DIR" && sudo test ! -L "$SOURCE_DIR" || return 1
  sudo test ! -e "$SOURCE_DIR/.git" || return 1
  sudo test ! -e "$SOURCE_DIR/.env" || return 1
  sudo test ! -e "$SOURCE_DIR/.env.production" || return 1
  bad_permissions="$(sudo find "$SOURCE_DIR" \
    \( -type d ! -perm 0555 -o -type f ! -perm 0444 -o -type l \) \
    -print -quit 2>/dev/null)" || return 1
  [[ -z "$bad_permissions" ]] || return 1
  expected="$(source_paths_sha)" || return 1
  actual="$(sudo find "$SOURCE_DIR" -mindepth 1 -printf '%P\0' 2>/dev/null \
    | LC_ALL=C sort -z | sha256sum | cut -d ' ' -f 1)" || return 1
  [[ "$actual" == "$expected" ]] || return 1
  sudo git -c core.fileMode=false -c "safe.directory=$TOOLING_ROOT" \
    --git-dir="$TOOLING_ROOT/.git" --work-tree="$SOURCE_DIR" \
    diff --quiet "$SEALED_CANDIDATE_SHA" -- >/dev/null 2>&1
}

validate_sealed_interruption() {
  sudo python3 -I -S - "$RUN_DIR" "$MANIFEST_REL" \
    "$SEALED_CANDIDATE_SHA" "$EXPECTED_MANIFEST_SHA256" \
    "$EXPECTED_CASES_SHA256" "$APPROVAL_ID" 2>/dev/null <<'PY'
import hashlib
import json
import stat
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
manifest_rel = sys.argv[2]
runtime_sha, manifest_sha, cases_sha, approval_id = sys.argv[3:]
source = run_dir / "source"
evidence = run_dir / "evidence"
manifest = source / manifest_rel
cases = evidence / "pilot50-cases.json"
report = evidence / "pilot50-ask-report.json"
preflight = run_dir / "preflight.receipt"
started = run_dir / "run.started"

def require_dir(path, *, mode, app_owned=False):
    metadata = path.lstat()
    assert stat.S_ISDIR(metadata.st_mode) and not path.is_symlink()
    assert path.resolve(strict=True) == path
    assert stat.S_IMODE(metadata.st_mode) == mode
    if app_owned:
        assert metadata.st_uid == 10001 and metadata.st_gid == 10001

def require_file(path, *, maximum, mode, app_owned=False):
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode) and not path.is_symlink()
    assert metadata.st_nlink == 1 and 0 < metadata.st_size <= maximum
    assert stat.S_IMODE(metadata.st_mode) == mode
    if app_owned:
        assert metadata.st_uid == 10001 and metadata.st_gid == 10001
    return path.read_bytes()

def receipt(payload, *, maximum):
    text = require_file(payload, maximum=maximum, mode=0o600).decode("ascii")
    assert text.endswith("\n") and "\r" not in text
    lines = text.splitlines()
    assert lines and all(line.count("=") == 1 for line in lines)
    result = dict(line.split("=", 1) for line in lines)
    assert len(result) == len(lines)
    return result

require_dir(run_dir, mode=0o700)
require_dir(source, mode=0o555)
require_dir(evidence, mode=0o700, app_owned=True)
for absent in (
    run_dir / "run.completed",
    evidence / "pilot50-safe-result.json",
):
    assert not absent.exists() and not absent.is_symlink()

manifest_bytes = require_file(manifest, maximum=128 * 1024, mode=0o444)
cases_bytes = require_file(
    cases, maximum=4 * 1024 * 1024, mode=0o600, app_owned=True
)
report_bytes = require_file(
    report, maximum=32 * 1024 * 1024, mode=0o600, app_owned=True
)
assert hashlib.sha256(manifest_bytes).hexdigest() == manifest_sha
assert hashlib.sha256(cases_bytes).hexdigest() == cases_sha

preflight_row = receipt(preflight, maximum=8192)
assert preflight_row["schema_version"] == "pilot50-candidate-preflight-v1"
assert preflight_row["dataset_id"] == "pilot50_balanced_v5"
assert preflight_row["candidate_sha"] == runtime_sha
assert preflight_row["manifest_sha256"] == manifest_sha
assert preflight_row["cases_sha256"] == cases_sha
assert preflight_row["cases_total"] == "50"
assert preflight_row["runtime_smoke_status"] == "OK"
assert preflight_row["cost_precheck_status"] == "GO"
assert preflight_row["cost_cap_rub"] == "30"
assert preflight_row["approval_id"] == approval_id

started_row = receipt(started, maximum=4096)
assert started_row == {
    "schema_version": "pilot50-candidate-run-started-v1",
    "candidate_sha": runtime_sha,
    "cases_sha256": cases_sha,
    "approval_id": approval_id,
    "cost_precheck_status": "GO",
    "cost_cap_rub": "30",
}

report_payload = json.loads(report_bytes)
assert isinstance(report_payload, dict)
assert report_payload.get("cases_file_sha256") == cases_sha
assert report_payload.get("cases_total") == 50
assert len(report_payload.get("results") or []) == 50
runtime = report_payload.get("runtime_identity")
assert isinstance(runtime, dict)
assert runtime.get("status") == "verified"
assert runtime.get("matched_expected_runtime") is True
for key in (
    "expected_runtime_git_sha",
    "preflight_release_git_sha",
    "postflight_release_git_sha",
    "verified_release_git_sha",
):
    assert runtime.get(key) == runtime_sha
print(hashlib.sha256(report_bytes).hexdigest())
PY
}

validate_candidate_image() {
  local inspect image_id image_revision image_user
  inspect="$(sudo docker image inspect \
    -f '{{.Id}}|{{index .Config.Labels "org.opencontainers.image.revision"}}|{{.Config.User}}' \
    "$CANDIDATE_IMAGE" 2>/dev/null)" || return 1
  IFS='|' read -r image_id image_revision image_user <<<"$inspect"
  [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ && \
    "$image_revision" == "$SEALED_CANDIDATE_SHA" && "$image_user" == "app" ]]
}

recover_safe_result() {
  sudo docker run --rm --pull never --network none \
    --user app --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --cap-drop ALL --security-opt no-new-privileges=true \
    --pids-limit 128 --memory 1g --cpus 1 \
    -e PYTHONOPTIMIZE= -e PYTHONDONTWRITEBYTECODE=1 \
    --mount "type=bind,src=$SOURCE_DIR,dst=/workspace,readonly" \
    --mount "type=bind,src=$EVIDENCE_DIR,dst=/evidence,readonly" \
    --mount "type=bind,src=$STAGING_DIR,dst=/recovery" \
    -w /workspace --entrypoint python "$CANDIDATE_IMAGE" \
    -E -m scripts.pilot50 summarize \
    --manifest "/workspace/$MANIFEST_REL" \
    --cases /evidence/pilot50-cases.json \
    --report /evidence/pilot50-ask-report.json \
    --output /recovery/pilot50-safe-result.json \
    --expected-runtime-git-sha "$SEALED_CANDIDATE_SHA" \
    --expected-approval-id "$APPROVAL_ID" \
    --candidate-contract "$CANDIDATE_CONTRACT_ID" \
    >/dev/null 2>&1
}

show_safe_result() {
  sudo docker run --rm --pull never --network none \
    --user app --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --cap-drop ALL --security-opt no-new-privileges=true \
    --pids-limit 128 --memory 1g --cpus 1 \
    -e PYTHONOPTIMIZE= -e PYTHONDONTWRITEBYTECODE=1 \
    --mount "type=bind,src=$SOURCE_DIR,dst=/workspace,readonly" \
    --mount "type=bind,src=$STAGING_DIR,dst=/recovery,readonly" \
    -w /workspace --entrypoint python "$CANDIDATE_IMAGE" \
    -E -m scripts.pilot50 show-safe \
    --input /recovery/pilot50-safe-result.json 2>/dev/null
}

validate_safe_summary() {
  local cases_sha="$1" report_sha="$2" safe_sha="$3"
  python3 -I -S /dev/fd/3 "$SEALED_CANDIDATE_SHA" "$cases_sha" "$report_sha" \
    "$safe_sha" "$APPROVAL_ID" 3<<'PY' 2>/dev/null
import json
import re
import sys

runtime_sha, cases_sha, report_sha, safe_sha, approval_id = sys.argv[1:]
payload = json.load(sys.stdin)
assert isinstance(payload, dict)
assert payload.get("schema_version") == "pilot50-safe-result-v1"
assert payload.get("dataset_id") == "pilot50_balanced_v5"
assert payload.get("runtime_git_sha") == runtime_sha
assert payload.get("cases_sha256") == cases_sha
assert payload.get("report_sha256") == report_sha
assert payload.get("approval_id") == approval_id
assert payload.get("classification") == "calibration_only"
assert payload.get("human_product_verdict") is False
assert payload.get("status") == "OK"
assert "rolling_24h_waiver" not in payload
quality = payload.get("quality_gate")
assert isinstance(quality, dict) and quality.get("status") in {"GO", "STOP"}
assert quality.get("schema_version") == "pilot50-v5-quality-gate-v1"
counts = payload.get("counts")
assert counts == {"typical": 25, "atypical": 25}
trace = payload.get("trace_coverage")
assert isinstance(trace, dict) and trace.get("found") == 50 and trace.get("total") == 50
budget = payload.get("budget")
assert isinstance(budget, dict) and budget.get("max_rub") == 30
assert isinstance(payload.get("llm_cost_rub"), (int, float))
assert 0 <= payload["llm_cost_rub"] <= 30
for digest in (cases_sha, report_sha, safe_sha):
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
print(quality["status"])
PY
}

write_recovery_receipt() {
  local cases_sha="$1" report_sha="$2" safe_sha="$3" quality_status="$4"
  {
    printf 'schema_version=pilot50-v5-interrupted-recovery-v1\n'
    printf 'tooling_sha=%s\n' "$EXPECTED_TOOLING_SHA"
    printf 'candidate_sha=%s\n' "$SEALED_CANDIDATE_SHA"
    printf 'cases_sha256=%s\n' "$cases_sha"
    printf 'report_sha256=%s\n' "$report_sha"
    printf 'safe_result_sha256=%s\n' "$safe_sha"
    printf 'quality_status=%s\n' "$quality_status"
    printf 'source_run_status=started_report_present\n'
    printf 'new_ask_calls=0\n'
    printf 'network_calls=0\n'
  } | sudo tee "$STAGING_DIR/recovery.receipt" >/dev/null \
    || fail "recovery_receipt_create_failed"
  sudo chown "$(id -u):$(id -g)" "$STAGING_DIR/recovery.receipt" \
    >/dev/null 2>&1 || fail "recovery_receipt_owner_failed"
  sudo chmod 0600 "$STAGING_DIR/recovery.receipt" \
    >/dev/null 2>&1 || fail "recovery_receipt_mode_failed"
}

main() {
  local cases_sha owner_gid owner_uid quality_status raw_safe report_sha safe_sha
  validate_invocation "$@"
  load_tooling
  sudo docker inspect "$CANDIDATE_CONTAINER" >/dev/null 2>&1 \
    && fail "candidate_container_present"
  verify_source_snapshot || fail "source_snapshot_invalid"
  report_sha="$(validate_sealed_interruption)" \
    || fail "sealed_interruption_invalid"
  [[ "$report_sha" =~ ^[0-9a-f]{64}$ ]] || fail "sealed_report_sha_invalid"
  validate_candidate_image || fail "candidate_image_invalid"
  sudo test -d "$RECOVERY_ROOT" || {
    owner_uid="$(id -u)"
    owner_gid="$(id -g)"
    sudo install -d -m 0700 -o "$owner_uid" -g "$owner_gid" \
      "$RECOVERY_ROOT" || fail "recovery_root_create_failed"
  }
  sudo test ! -L "$RECOVERY_ROOT" || fail "recovery_root_invalid"
  [[ "$(sudo readlink -f -- "$RECOVERY_ROOT" 2>/dev/null)" == "$RECOVERY_ROOT" ]] \
    || fail "recovery_root_invalid"
  if sudo test -e "$RECOVERY_DIR" || sudo test -L "$RECOVERY_DIR"; then
    fail "recovery_already_exists"
  fi
  STAGING_DIR="$RECOVERY_ROOT/.staging-${SEALED_CANDIDATE_SHA}-$$"
  sudo install -d -m 0700 -o 10001 -g 10001 "$STAGING_DIR" \
    || fail "recovery_staging_create_failed"
  recover_safe_result || fail "sealed_report_not_summarizable"
  verify_source_snapshot || fail "source_snapshot_changed"
  [[ "$(sudo sha256sum "$EVIDENCE_DIR/pilot50-ask-report.json" 2>/dev/null \
    | cut -d ' ' -f 1)" == "$report_sha" ]] || fail "sealed_report_changed"
  cases_sha="$(sudo sha256sum "$EVIDENCE_DIR/pilot50-cases.json" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "cases_sha_unavailable"
  [[ "$cases_sha" == "$EXPECTED_CASES_SHA256" ]] || fail "cases_sha_mismatch"
  safe_sha="$(sudo sha256sum "$STAGING_DIR/pilot50-safe-result.json" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "safe_result_sha_unavailable"
  raw_safe="$(show_safe_result)" || fail "safe_result_invalid"
  [[ ${#raw_safe} -le 16384 ]] || fail "safe_result_oversized"
  quality_status="$(printf '%s' "$raw_safe" \
    | validate_safe_summary "$cases_sha" "$report_sha" "$safe_sha")" \
    || fail "safe_summary_invalid"
  write_recovery_receipt "$cases_sha" "$report_sha" "$safe_sha" "$quality_status"
  sudo mv -T -- "$STAGING_DIR" "$RECOVERY_DIR" \
    || fail "recovery_publish_failed"
  STAGING_DIR=""
  printf 'pilot50_v5_interrupted_recovery=OK\n'
  printf 'tooling_sha=%s\n' "$EXPECTED_TOOLING_SHA"
  printf 'candidate_sha=%s\n' "$SEALED_CANDIDATE_SHA"
  printf 'cases_sha256=%s\n' "$cases_sha"
  printf 'report_sha256=%s\n' "$report_sha"
  printf 'safe_result_sha256=%s\n' "$safe_sha"
  printf 'new_ask_calls=0\n'
  printf 'network_calls=0\n'
  printf '%s\n' "$raw_safe"
}

main "$@"
