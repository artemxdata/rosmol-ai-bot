#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
exec 2>/dev/null

readonly SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"
readonly ENV_FILE="$SERVER_PROJECT_DIR/.env.production"
readonly RUNTIME_CONTAINER="rosmol-app-ml"
readonly DATASET_ID="pilot50_balanced_v5"
readonly MANIFEST_REL="eval/cases/pilot50_balanced_v5.json"
readonly EXPECTED_MANIFEST_SHA256="12747d62190cc5e70d70490e9a649d91596ec69a316b5c2de3843ac3df6f85b4"
readonly EXPECTED_CASES_SHA256="9d53114722191330214f5917ee3baf4ccfcf4eb644be34a0253c60531b225529"
readonly TARGET="http://app-ml:8000/ask"
readonly COST_CAP_RUB="200"
readonly BASE_DIR="/var/lib/rosmol/balanced50-runtime-v1"
readonly COST_LEDGER_DIR="/var/lib/rosmol/eval-cost-ledger-v1"
readonly APP_UID="10001"
readonly APP_GID="10001"
readonly SIMPLE_PRICE="12.2"
readonly COMPLEX_PRICE="569.34"

MODE="${1:-}"
EXPECTED_RUNTIME_SHA="${2:-}"
APPROVAL_ID="${3:-}"
CHANNEL_ATTESTATION="${4:-}"
TOOLING_ROOT=""
TOOLING_SHA=""
SOURCE_DIR=""
STAGING_DIR=""
RUN_DIR=""
EVIDENCE_DIR=""
PROVENANCE_DIR=""
compose=()

fail() {
  printf 'balanced50_runtime_server_local=FAIL reason=%s\n' "$1"
  exit 1
}

cleanup() {
  if [[ -n "$STAGING_DIR" && "$STAGING_DIR" == "$BASE_DIR/tooling/.staging-"* ]]; then
    sudo rm -rf --one-file-system -- "$STAGING_DIR" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT
trap 'fail unexpected_error' ERR

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "$2"
}

validate_inputs() {
  [[ "$#" -eq 4 && "$MODE" == "run" ]] || fail "usage"
  [[ "$EXPECTED_RUNTIME_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "runtime_sha_invalid"
  [[ "$APPROVAL_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$ ]] \
    || fail "approval_id_invalid"
  [[ "$CHANNEL_ATTESTATION" == "HDE_VK_DISABLED" ]] \
    || fail "channels_not_attested_disabled"
}

verify_tooling_delta() {
  local changed_path
  git -C "$TOOLING_ROOT" merge-base --is-ancestor \
    "$EXPECTED_RUNTIME_SHA" "$TOOLING_SHA" >/dev/null 2>&1 \
    || fail "tooling_not_descended_from_runtime"
  while IFS= read -r changed_path; do
    case "$changed_path" in
      scripts/analyze_balanced50_runtime.py | \
      scripts/run_balanced50_runtime_server_local.sh | \
      tests/test_analyze_balanced50_runtime.py | \
      tests/test_run_balanced50_runtime_server_local.py | \
      docs/CURRENT_STATE.md)
        ;;
      *) fail "tooling_contains_runtime_change" ;;
    esac
  done < <(
    git -C "$TOOLING_ROOT" diff --name-only \
      "$EXPECTED_RUNTIME_SHA..$TOOLING_SHA" --
  )
}

verify_runtime_ready() {
  [[ "$(sudo docker inspect -f '{{.State.Running}}' \
    "$RUNTIME_CONTAINER" 2>/dev/null)" == "true" ]] || return 1
  [[ "$(sudo docker inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$RUNTIME_CONTAINER" 2>/dev/null)" == "$EXPECTED_RUNTIME_SHA" ]] || return 1
  [[ "$(sudo docker inspect -f '{{.Image}}' "$RUNTIME_CONTAINER" 2>/dev/null)" == \
    "$(sudo docker image inspect -f '{{.Id}}' \
      "rosmol-ai-bot-ml:$EXPECTED_RUNTIME_SHA" 2>/dev/null)" ]] || return 1
  [[ -z "$(sudo docker port "$RUNTIME_CONTAINER" 2>/dev/null)" ]] || return 1
  sudo docker exec -i "$RUNTIME_CONTAINER" python - "$EXPECTED_RUNTIME_SHA" \
    >/dev/null 2>&1 <<'PY'
import json
import sys
import urllib.request

expected = sys.argv[1]
with urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=30) as response:
    payload = json.load(response)
assert payload.get("status") == "ready"
assert payload.get("release_git_sha") == expected
checks = payload.get("checks")
assert isinstance(checks, dict) and checks
assert all(value == "ok" for value in checks.values())
PY
}

load_state() {
  require_command git "git_missing"
  require_command sudo "sudo_missing"
  require_command docker "docker_missing"
  require_command tar "tar_missing"
  require_command python3 "python_missing"
  require_command sha256sum "sha256sum_missing"
  require_command find "find_missing"
  require_command sort "sort_missing"
  require_command readlink "readlink_missing"

  sudo -v >/dev/null 2>&1 || fail "sudo_auth_failed"
  TOOLING_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || fail "not_in_git_worktree"
  [[ "$TOOLING_ROOT" == "$SERVER_PROJECT_DIR" ]] || fail "wrong_server_project"
  [[ "$(readlink -f -- "$TOOLING_ROOT")" == "$TOOLING_ROOT" ]] \
    || fail "server_project_not_regular"
  TOOLING_SHA="$(git -C "$TOOLING_ROOT" rev-parse HEAD 2>/dev/null)" \
    || fail "tooling_sha_unavailable"
  [[ "$TOOLING_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "tooling_sha_invalid"
  ! git -C "$TOOLING_ROOT" symbolic-ref -q HEAD >/dev/null 2>&1 \
    || fail "tooling_not_detached"
  [[ -z "$(git -C "$TOOLING_ROOT" status --porcelain=v1 --untracked-files=all)" ]] \
    || fail "tooling_worktree_not_clean"
  verify_tooling_delta

  [[ "$(sha256sum "$TOOLING_ROOT/$MANIFEST_REL" | cut -d ' ' -f 1)" == \
    "$EXPECTED_MANIFEST_SHA256" ]] || fail "manifest_sha_mismatch"
  sudo test -f "$ENV_FILE" || fail "server_env_missing"
  sudo test ! -L "$ENV_FILE" || fail "server_env_not_regular"
  sudo docker image inspect "rosmol-ai-bot-ml:$EXPECTED_RUNTIME_SHA" \
    >/dev/null 2>&1 || fail "runtime_image_missing"
  verify_runtime_ready || fail "runtime_not_ready"

  SOURCE_DIR="$BASE_DIR/tooling/$TOOLING_SHA"
  RUN_DIR="$BASE_DIR/runs/$DATASET_ID-$EXPECTED_RUNTIME_SHA"
  EVIDENCE_DIR="$RUN_DIR/evidence"
  PROVENANCE_DIR="$RUN_DIR/provenance"
}

source_paths_sha() {
  git -C "$TOOLING_ROOT" ls-tree -r -t -z --name-only "$TOOLING_SHA" \
    | LC_ALL=C sort -z | sha256sum | cut -d ' ' -f 1
}

verify_source_snapshot() {
  local actual_paths_sha expected_paths_sha
  sudo test -d "$SOURCE_DIR" || fail "source_snapshot_missing"
  sudo test ! -L "$SOURCE_DIR" || fail "source_snapshot_not_regular"
  sudo test ! -e "$SOURCE_DIR/.git" || fail "source_snapshot_contains_git"
  sudo test ! -e "$SOURCE_DIR/.env" || fail "source_snapshot_contains_env"
  sudo test ! -e "$SOURCE_DIR/.env.production" \
    || fail "source_snapshot_contains_env"
  sudo git -c core.fileMode=false -c "safe.directory=$TOOLING_ROOT" \
    --git-dir="$TOOLING_ROOT/.git" --work-tree="$SOURCE_DIR" \
    diff --quiet "$TOOLING_SHA" -- >/dev/null 2>&1 \
    || fail "source_snapshot_mismatch"
  expected_paths_sha="$(source_paths_sha)"
  actual_paths_sha="$(sudo find "$SOURCE_DIR" -mindepth 1 -printf '%P\0' \
    | LC_ALL=C sort -z | sha256sum | cut -d ' ' -f 1)"
  [[ "$actual_paths_sha" == "$expected_paths_sha" ]] \
    || fail "source_snapshot_paths_mismatch"
  [[ -z "$(sudo find "$SOURCE_DIR" ! -type d ! -type f -print -quit)" ]] \
    || fail "source_snapshot_unsupported_path"
  [[ -z "$(sudo find "$SOURCE_DIR" -perm /222 -print -quit)" ]] \
    || fail "source_snapshot_writable"
}

prepare_source_snapshot() {
  sudo install -d -m 0750 -o "$APP_UID" -g "$APP_GID" \
    "$BASE_DIR" "$BASE_DIR/tooling" "$BASE_DIR/runs" >/dev/null 2>&1 \
    || fail "base_directory_unavailable"
  if sudo test -e "$SOURCE_DIR" || sudo test -L "$SOURCE_DIR"; then
    verify_source_snapshot
    return
  fi
  STAGING_DIR="$BASE_DIR/tooling/.staging-$TOOLING_SHA-$$"
  sudo install -d -m 0750 -o "$APP_UID" -g "$APP_GID" "$STAGING_DIR" \
    >/dev/null 2>&1 || fail "source_staging_create_failed"
  if ! (git -C "$TOOLING_ROOT" archive --format=tar "$TOOLING_SHA" \
    | sudo tar -xf - -C "$STAGING_DIR" --no-same-owner) >/dev/null 2>&1; then
    fail "source_snapshot_create_failed"
  fi
  sudo chown -R "$APP_UID:$APP_GID" "$STAGING_DIR" >/dev/null 2>&1 \
    || fail "source_snapshot_owner_failed"
  sudo find "$STAGING_DIR" -type d -exec chmod 0550 {} + >/dev/null 2>&1 \
    || fail "source_snapshot_mode_failed"
  sudo find "$STAGING_DIR" -type f -exec chmod 0440 {} + >/dev/null 2>&1 \
    || fail "source_snapshot_mode_failed"
  sudo mv -T -- "$STAGING_DIR" "$SOURCE_DIR" >/dev/null 2>&1 \
    || fail "source_snapshot_publish_failed"
  STAGING_DIR=""
  verify_source_snapshot
}

prepare_private_directories() {
  sudo install -d -m 0700 -o "$APP_UID" -g "$APP_GID" \
    "$RUN_DIR" "$EVIDENCE_DIR" "$PROVENANCE_DIR" >/dev/null 2>&1 \
    || fail "run_directory_unavailable"
  if sudo test -e "$COST_LEDGER_DIR" || sudo test -L "$COST_LEDGER_DIR"; then
    sudo test -d "$COST_LEDGER_DIR" || fail "cost_ledger_not_directory"
    sudo test ! -L "$COST_LEDGER_DIR" || fail "cost_ledger_not_regular"
  else
    sudo install -d -m 0700 -o "$APP_UID" -g "$APP_GID" "$COST_LEDGER_DIR" \
      >/dev/null 2>&1 || fail "cost_ledger_create_failed"
  fi
  normalize_cost_ledger_access
}

normalize_cost_ledger_access() {
  local access_status
  [[ "$(sudo readlink -f -- "$COST_LEDGER_DIR")" == "$COST_LEDGER_DIR" ]] \
    || fail "cost_ledger_not_regular"
  sudo python3 - "$COST_LEDGER_DIR" \
    >/dev/null 2>&1 <<'PY' || fail "cost_ledger_contents_unsafe"
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
root_device = root.stat().st_dev
for current, directories, files in os.walk(root, followlinks=False):
    current_path = Path(current)
    for path in [current_path, *(current_path / name for name in directories + files)]:
        value = path.lstat()
        assert value.st_dev == root_device
        assert stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)
PY
  if sudo python3 - "$COST_LEDGER_DIR" "$APP_UID" "$APP_GID" \
    >/dev/null 2>&1 <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
app_uid = int(sys.argv[2])
app_gid = int(sys.argv[3])
for current, directories, files in os.walk(root, followlinks=False):
    current_path = Path(current)
    directory_paths = [current_path, *(current_path / name for name in directories)]
    for path in directory_paths:
        value = path.stat()
        assert value.st_uid == app_uid and value.st_gid == app_gid
        assert stat.S_IMODE(value.st_mode) & 0o700 == 0o700
    for name in files:
        value = (current_path / name).stat()
        assert value.st_uid == app_uid and value.st_gid == app_gid
        assert stat.S_IMODE(value.st_mode) & 0o400 == 0o400
PY
  then
    access_status="unchanged"
  else
    sudo chown -R --no-dereference "$APP_UID:$APP_GID" "$COST_LEDGER_DIR" \
      >/dev/null 2>&1 || fail "cost_ledger_owner_repair_failed"
    sudo find "$COST_LEDGER_DIR" -xdev -type d -exec chmod 0700 {} + \
      >/dev/null 2>&1 || fail "cost_ledger_mode_repair_failed"
    sudo find "$COST_LEDGER_DIR" -xdev -type f -exec chmod 0600 {} + \
      >/dev/null 2>&1 || fail "cost_ledger_mode_repair_failed"
    access_status="repaired"
  fi
  printf 'balanced50_cost_ledger_access=%s\n' "$access_status"
}

build_compose_command() {
  compose=(
    sudo env
    "RELEASE_GIT_SHA=$EXPECTED_RUNTIME_SHA"
    "CLOUD_RU_MODEL_SIMPLE_INPUT_PRICE_RUB_PER_MILLION=$SIMPLE_PRICE"
    "CLOUD_RU_MODEL_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION=$SIMPLE_PRICE"
    "CLOUD_RU_MODEL_COMPLEX_INPUT_PRICE_RUB_PER_MILLION=$COMPLEX_PRICE"
    "CLOUD_RU_MODEL_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION=$COMPLEX_PRICE"
    "ACCEPTANCE_SOURCE_DIR=$SOURCE_DIR"
    "ACCEPTANCE_OUTPUT_DIR=$EVIDENCE_DIR"
    "ACCEPTANCE_PROVENANCE_DIR=$PROVENANCE_DIR"
    "ACCEPTANCE_COST_LEDGER_DIR=$COST_LEDGER_DIR"
    "PHASE0_RUNTIME_GIT_SHA=$EXPECTED_RUNTIME_SHA"
    "PHASE0_RUNNER_SOURCE_DIR=$SOURCE_DIR"
    "PHASE0_BUILDER_SOURCE_DIR=$PROVENANCE_DIR"
    "PHASE0_PRIVATE_DIR=$EVIDENCE_DIR"
    "PHASE0_COST_LEDGER_DIR=$COST_LEDGER_DIR"
    docker compose
    --env-file "$ENV_FILE"
    --project-directory "$SERVER_PROJECT_DIR"
    -f "$SOURCE_DIR/docker-compose.yml"
    -f "$SOURCE_DIR/docker-compose.ml.yml"
    -f "$SOURCE_DIR/docker-compose.prod.yml"
    -f "$SOURCE_DIR/docker-compose.acceptance.yml"
    --profile ml
    --profile acceptance
  )
}

verify_cost_ledger_container_access() {
  "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance -c \
    'import os,tempfile; descriptor,path=tempfile.mkstemp(prefix=".balanced50-access-",dir="/cost-ledger"); os.close(descriptor); os.unlink(path)' \
    >/dev/null || fail "cost_ledger_container_access_failed"
}

write_or_validate_binding() {
  sudo python3 - "$EVIDENCE_DIR/run.binding.json" "$TOOLING_SHA" \
    "$EXPECTED_RUNTIME_SHA" "$APPROVAL_ID" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected = {
    "schema_version": "balanced50-runtime-binding-v1",
    "tooling_sha": sys.argv[2],
    "runtime_sha": sys.argv[3],
    "approval_id": sys.argv[4],
    "dataset_id": "pilot50_balanced_v5",
    "cases_sha256": "9d53114722191330214f5917ee3baf4ccfcf4eb644be34a0253c60531b225529",
    "cost_cap_rub": 200,
    "channels_status": "HDE_VK_DISABLED",
}
payload = (json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n").encode()
if path.exists():
    assert not path.is_symlink() and path.is_file() and path.read_bytes() == payload
else:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
PY
}

validate_cases() {
  local cases_path="$EVIDENCE_DIR/cases.json"
  sudo test -f "$cases_path" || fail "cases_missing"
  sudo test ! -L "$cases_path" || fail "cases_not_regular"
  [[ "$(sudo sha256sum "$cases_path" | cut -d ' ' -f 1)" == \
    "$EXPECTED_CASES_SHA256" ]] || fail "cases_sha_mismatch"
  sudo python3 - "$cases_path" >/dev/null 2>&1 <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

rows = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert isinstance(rows, list) and len(rows) == 50
assert len({row["id"] for row in rows}) == 50
groups = Counter(row.get("pilot50_group") for row in rows)
assert groups == {"typical": 25, "atypical": 25}
assert all(row.get("expected_behavior") == "answer" for row in rows)
assert all(row.get("expected_escalated") is False for row in rows)
PY
}

prepare_cases_if_needed() {
  if sudo test -e "$EVIDENCE_DIR/cases.json" || \
    sudo test -L "$EVIDENCE_DIR/cases.json"; then
    validate_cases
    return
  fi
  "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -m scripts.pilot50 prepare \
    --manifest "/workspace/$MANIFEST_REL" \
    --output /evidence/cases.json >/dev/null \
    || fail "cases_prepare_failed"
  validate_cases
}

write_marker_once() {
  local path="$1"
  local value="$2"
  sudo python3 - "$path" "$value" >/dev/null 2>&1 <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = (sys.argv[2] + "\n").encode("ascii")
if path.exists():
    assert not path.is_symlink() and path.is_file() and path.read_bytes() == payload
else:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
PY
}

analyze_and_print() {
  "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    /workspace/scripts/analyze_balanced50_runtime.py \
    --report /evidence/report.json \
    --output /evidence/global-summary.json \
    --expected-runtime-git-sha "$EXPECTED_RUNTIME_SHA" \
    || fail "global_analysis_failed"
}

run_eval_once() {
  local report_path="$EVIDENCE_DIR/report.json"
  local started_path="$EVIDENCE_DIR/run.started"
  local completed_path="$EVIDENCE_DIR/run.completed"

  if sudo test -e "$started_path" || sudo test -L "$started_path"; then
    sudo test -f "$report_path" || fail "interrupted_after_start_no_retry"
    analyze_and_print
    write_marker_once "$completed_path" "$EXPECTED_RUNTIME_SHA"
    return
  fi
  sudo test ! -e "$report_path" || fail "unbound_report_exists"
  sudo test ! -e "$completed_path" || fail "unbound_completed_marker"
  write_marker_once "$started_path" "$EXPECTED_RUNTIME_SHA"

  if ! "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -m eval.run_ask \
    --cases /evidence/cases.json \
    --output /evidence/report.json \
    --no-markdown \
    --target "$TARGET" \
    --concurrency 2 \
    --timeout 180 \
    --max-llm-cost-rub "$COST_CAP_RUB" \
    --large-run-threshold 10 \
    --high-cost-approval-id "$APPROVAL_ID" \
    --user-prefix "balanced50-${EXPECTED_RUNTIME_SHA:0:12}" \
    --kb-seed /workspace/data/knowledge_base_seed.json \
    --bypass-cache \
    --require-complete-traces >/dev/null; then
    fail "ask_eval_failed_no_retry"
  fi
  sudo test -f "$report_path" || fail "ask_report_missing"
  verify_runtime_ready || fail "runtime_changed_during_run"
  analyze_and_print
  write_marker_once "$completed_path" "$EXPECTED_RUNTIME_SHA"
}

main() {
  validate_inputs "$@"
  load_state
  prepare_source_snapshot
  prepare_private_directories
  build_compose_command
  "${compose[@]}" config --quiet >/dev/null || fail "compose_config_failed"
  verify_cost_ledger_container_access
  write_or_validate_binding || fail "run_binding_conflict"
  prepare_cases_if_needed
  printf 'balanced50_runtime_run=START cases=50 typical=25 atypical=25 cost_cap_rub=200 channels=HDE_VK_DISABLED\n'
  run_eval_once
  verify_runtime_ready || fail "runtime_not_ready_after_analysis"
  printf 'balanced50_runtime_server_local=OK runtime_sha=%s tooling_sha=%s channels=HDE_VK_DISABLED\n' \
    "$EXPECTED_RUNTIME_SHA" "$TOOLING_SHA"
}

main "$@"
