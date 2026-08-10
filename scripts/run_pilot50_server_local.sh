#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
exec 2>/dev/null

readonly PILOT50_DATASET_ID="pilot50_balanced_v1"
readonly PILOT50_MANIFEST_REL="eval/cases/pilot50_balanced_v1.json"
readonly PILOT50_TARGET="http://app-ml:8000/ask"
readonly PILOT50_CASES_TOTAL="50"
readonly PILOT50_TYPICAL_TOTAL="25"
readonly PILOT50_ATYPICAL_TOTAL="25"
readonly PILOT50_FORECAST_COST_RUB="10"
readonly PILOT50_COST_CAP_RUB="20"
readonly PILOT50_SIMPLE_INPUT_PRICE_RUB_PER_MILLION="12.2"
readonly PILOT50_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION="12.2"
readonly PILOT50_COMPLEX_OFFICIAL_PRICE_RUB_PER_MILLION="569.3374"
readonly PILOT50_COMPLEX_INPUT_PRICE_RUB_PER_MILLION="569.34"
readonly PILOT50_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION="569.34"
readonly PILOT50_REPRICING_CONTRACT_ID="pilot50-c38-pricing-v1"
readonly PILOT50_REPRICING_RATE_CARD_SHA256="3aebb12db82391bad23ec9256781e3439f2692ad63814070e4341bd28ea27bd6"
readonly PILOT50_SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"
readonly PILOT50_ENV_FILE="/opt/rosmol-ai-bot/.env.production"
readonly PILOT50_RUNTIME_CONTAINER="rosmol-app-ml"
readonly PILOT50_BASE_DIR="/var/lib/rosmol/pilot50"
readonly PILOT50_LEDGER_DIR="/var/lib/rosmol/eval-cost-ledger-v1"
readonly PILOT50_PRE_REQUEST_FAILED_TOOLING_SHA="36d0f0e5e4739a0264516cc46c3524beaa6fd934"
readonly PILOT50_STARTED_MARKER_SHA256="eff64b343dcb2b1dc113648e7089b9ce9f8a7f6c7808a03a2cffb4ad7302f606"
readonly PILOT50_RECOVERY_RUNTIME_SHA="c38f0e055630fae2af50720fae81acee20ff4f6a"
readonly PILOT50_RECOVERY_CASES_SHA256="65da11ebc790b37e0b8e5dff2601f6cc2cd3956d17652f7d74ab95eb1c21c6ed"
readonly PILOT50_RECOVERY_MANIFEST_SHA256="d6f38ee2a7c95c6b558c55d0a6e5f67bd3fa92fac6b954bb0c2b23a88d322ca7"

MODE="${1:-}"
TOOLING_ROOT=""
TOOLING_SHA=""
SOURCE_SHA=""
RUNTIME_SHA=""
RUN_DIR=""
SOURCE_DIR=""
RUNNER_SOURCE_DIR=""
EVIDENCE_DIR=""
PROVENANCE_DIR=""
STAGING_DIR=""
RECOVERY_STAGING_DIR=""
RECOVER_PRE_REQUEST="0"
compose=()

fail() {
  printf 'pilot50_server_local=FAIL reason=%s\n' "$1"
  exit 1
}

cleanup_staging() {
  if [[ -n "$STAGING_DIR" && "$STAGING_DIR" == "$PILOT50_BASE_DIR/runs/.staging-"* ]]; then
    sudo rm -rf --one-file-system -- "$STAGING_DIR" >/dev/null 2>&1 || true
  fi
  if [[ -n "$RECOVERY_STAGING_DIR" && \
    "$RECOVERY_STAGING_DIR" == "$RUN_DIR/.recovery-source-staging-"* ]]; then
    sudo rm -rf --one-file-system -- "$RECOVERY_STAGING_DIR" \
      >/dev/null 2>&1 || true
  fi
}

trap cleanup_staging EXIT

unexpected_error() {
  trap - ERR
  fail "unexpected_error"
}

trap unexpected_error ERR

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "$2"
}

validate_mode() {
  [[ "$#" -eq 1 ]] || fail "usage"
  case "$MODE" in
    preflight | run | recover-pre-request | review) ;;
    *) fail "usage" ;;
  esac
}

load_common_state() {
  require_command git "git_missing"
  require_command sudo "sudo_missing"
  require_command docker "docker_missing"
  require_command tar "tar_missing"
  require_command sha256sum "sha256sum_missing"
  require_command python3 "python_missing"
  require_command awk "awk_missing"
  require_command cut "cut_missing"
  require_command find "find_missing"
  require_command readlink "readlink_missing"
  require_command sort "sort_missing"
  require_command stat "stat_missing"

  sudo -v >/dev/null 2>&1 || fail "sudo_auth_failed"

  TOOLING_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || fail "not_in_git_worktree"
  [[ "$TOOLING_ROOT" == "$PILOT50_SERVER_PROJECT_DIR" ]] \
    || fail "wrong_server_project"
  [[ "$(readlink -f -- "$TOOLING_ROOT" 2>/dev/null)" == "$TOOLING_ROOT" ]] \
    || fail "server_project_not_regular"
  TOOLING_SHA="$(git -C "$TOOLING_ROOT" rev-parse HEAD 2>/dev/null)" \
    || fail "tooling_sha_unavailable"
  [[ "$TOOLING_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "tooling_sha_invalid"
  SOURCE_SHA="$TOOLING_SHA"
  [[ -z "$(git -C "$TOOLING_ROOT" status --porcelain=v1 --untracked-files=all 2>/dev/null)" ]] \
    || fail "tooling_source_not_clean"

  [[ -f "$TOOLING_ROOT/$PILOT50_MANIFEST_REL" ]] \
    || fail "manifest_missing"
  [[ ! -L "$TOOLING_ROOT/$PILOT50_MANIFEST_REL" ]] \
    || fail "manifest_not_regular"
  git -C "$TOOLING_ROOT" ls-files --error-unmatch "$PILOT50_MANIFEST_REL" \
    >/dev/null 2>&1 || fail "manifest_not_tracked"
  if git -C "$TOOLING_ROOT" ls-files -s 2>/dev/null \
    | awk '$1 == "120000" { found=1 } END { exit(found ? 0 : 1) }' \
      >/dev/null 2>&1; then
    fail "tooling_source_has_symlink"
  fi
  if git -C "$TOOLING_ROOT" ls-files --error-unmatch .env.production \
    >/dev/null 2>&1; then
    fail "server_env_is_tracked"
  fi
  sudo test -f "$PILOT50_ENV_FILE" || fail "server_env_unreadable"
  sudo test ! -L "$PILOT50_ENV_FILE" || fail "server_env_not_regular"
  sudo test -r "$PILOT50_ENV_FILE" || fail "server_env_unreadable"

  [[ "$(sudo docker inspect -f '{{.State.Running}}' "$PILOT50_RUNTIME_CONTAINER" 2>/dev/null)" == "true" ]] \
    || fail "runtime_not_running"
  RUNTIME_SHA="$(sudo docker inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$PILOT50_RUNTIME_CONTAINER" 2>/dev/null)" \
    || fail "runtime_sha_unavailable"
  [[ "$RUNTIME_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "runtime_sha_invalid"
  sudo docker image inspect "rosmol-ai-bot-ml:$RUNTIME_SHA" \
    >/dev/null 2>&1 || fail "acceptance_image_missing"
  [[ "$(sudo docker image inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "rosmol-ai-bot-ml:$RUNTIME_SHA" 2>/dev/null)" == "$RUNTIME_SHA" ]] \
    || fail "acceptance_image_sha_mismatch"
  [[ "$(sudo docker inspect -f '{{.Image}}' \
    "$PILOT50_RUNTIME_CONTAINER" 2>/dev/null)" == \
    "$(sudo docker image inspect -f '{{.Id}}' \
    "rosmol-ai-bot-ml:$RUNTIME_SHA" 2>/dev/null)" ]] \
    || fail "acceptance_image_identity_mismatch"
  [[ -z "$(sudo docker port "$PILOT50_RUNTIME_CONTAINER" 2>/dev/null)" ]] \
    || fail "runtime_has_published_ports"

  verify_runtime_ready || fail "runtime_not_ready"

  RUN_DIR="$PILOT50_BASE_DIR/runs/${PILOT50_DATASET_ID}-${RUNTIME_SHA}"
  SOURCE_DIR="$RUN_DIR/source"
  RUNNER_SOURCE_DIR="$SOURCE_DIR"
  EVIDENCE_DIR="$RUN_DIR/evidence"
  PROVENANCE_DIR="$RUN_DIR/provenance"
}

verify_runtime_ready() {
  [[ "$(sudo docker inspect -f '{{.State.Running}}' \
    "$PILOT50_RUNTIME_CONTAINER" 2>/dev/null)" == "true" ]] || return 1
  [[ "$(sudo docker inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$PILOT50_RUNTIME_CONTAINER" 2>/dev/null)" == "$RUNTIME_SHA" ]] || return 1
  [[ "$(sudo docker inspect -f '{{.Image}}' \
    "$PILOT50_RUNTIME_CONTAINER" 2>/dev/null)" == \
    "$(sudo docker image inspect -f '{{.Id}}' \
    "rosmol-ai-bot-ml:$RUNTIME_SHA" 2>/dev/null)" ]] || return 1
  [[ -z "$(sudo docker port "$PILOT50_RUNTIME_CONTAINER" 2>/dev/null)" ]] \
    || return 1
  sudo docker exec -i "$PILOT50_RUNTIME_CONTAINER" \
    python - "$RUNTIME_SHA" >/dev/null 2>&1 <<'PY'
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

build_compose_command() {
  compose=(
    sudo env
    "RELEASE_GIT_SHA=$RUNTIME_SHA"
    "CLOUD_RU_MODEL_SIMPLE_INPUT_PRICE_RUB_PER_MILLION=$PILOT50_SIMPLE_INPUT_PRICE_RUB_PER_MILLION"
    "CLOUD_RU_MODEL_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION=$PILOT50_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION"
    "CLOUD_RU_MODEL_COMPLEX_INPUT_PRICE_RUB_PER_MILLION=$PILOT50_COMPLEX_INPUT_PRICE_RUB_PER_MILLION"
    "CLOUD_RU_MODEL_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION=$PILOT50_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION"
    "ACCEPTANCE_SOURCE_DIR=$RUNNER_SOURCE_DIR"
    "ACCEPTANCE_OUTPUT_DIR=$EVIDENCE_DIR"
    "ACCEPTANCE_PROVENANCE_DIR=$PROVENANCE_DIR"
    "ACCEPTANCE_COST_LEDGER_DIR=$PILOT50_LEDGER_DIR"
    "PHASE0_RUNTIME_GIT_SHA=$RUNTIME_SHA"
    "PHASE0_RUNNER_SOURCE_DIR=$RUNNER_SOURCE_DIR"
    "PHASE0_BUILDER_SOURCE_DIR=$PROVENANCE_DIR"
    "PHASE0_PRIVATE_DIR=$EVIDENCE_DIR"
    "PHASE0_COST_LEDGER_DIR=$PILOT50_LEDGER_DIR"
    docker compose
    --env-file "$PILOT50_ENV_FILE"
    --project-directory "$TOOLING_ROOT"
    -f "$RUNNER_SOURCE_DIR/docker-compose.yml"
    -f "$RUNNER_SOURCE_DIR/docker-compose.ml.yml"
    -f "$RUNNER_SOURCE_DIR/docker-compose.prod.yml"
    -f "$RUNNER_SOURCE_DIR/docker-compose.acceptance.yml"
    --profile ml
    --profile acceptance
  )
}

verify_source_snapshot() {
  local actual_paths_sha candidate expected_paths_sha expected_sha

  candidate="${1:-$SOURCE_DIR}"
  expected_sha="${2:-$SOURCE_SHA}"
  sudo test -d "$candidate" || fail "source_snapshot_missing"
  sudo test ! -L "$candidate" || fail "source_snapshot_not_regular"
  sudo test ! -e "$candidate/.git" || fail "source_snapshot_contains_git"
  sudo test ! -e "$candidate/.env" || fail "source_snapshot_contains_env"
  sudo test ! -e "$candidate/.env.production" \
    || fail "source_snapshot_contains_env"
  sudo git -c core.fileMode=false -c "safe.directory=$TOOLING_ROOT" \
    --git-dir="$TOOLING_ROOT/.git" --work-tree="$candidate" \
    diff --quiet "$expected_sha" -- >/dev/null 2>&1 \
    || fail "source_snapshot_mismatch"
  expected_paths_sha="$(source_paths_sha "$expected_sha")" \
    || fail "source_snapshot_paths_unavailable"
  actual_paths_sha="$(sudo find "$candidate" -mindepth 1 -printf '%P\0' \
    2>/dev/null | LC_ALL=C sort -z | sha256sum | cut -d ' ' -f 1)" \
    || fail "source_snapshot_paths_unavailable"
  [[ "$expected_paths_sha" =~ ^[0-9a-f]{64}$ ]] \
    || fail "source_snapshot_paths_unavailable"
  [[ "$actual_paths_sha" =~ ^[0-9a-f]{64}$ ]] \
    || fail "source_snapshot_paths_unavailable"
  [[ "$actual_paths_sha" == "$expected_paths_sha" ]] \
    || fail "source_snapshot_extra_or_missing_path"
  [[ -z "$(sudo find "$candidate" ! -type d ! -type f -print -quit 2>/dev/null)" ]] \
    || fail "source_snapshot_unsupported_path"
  [[ -z "$(sudo find "$candidate" -perm /222 -print -quit 2>/dev/null)" ]] \
    || fail "source_snapshot_writable"
}

source_paths_sha() {
  local expected_sha="$1"
  sudo git -c "safe.directory=$TOOLING_ROOT" \
    --git-dir="$TOOLING_ROOT/.git" ls-tree -r -t -z --name-only \
    "$expected_sha" 2>/dev/null | LC_ALL=C sort -z | sha256sum \
    | cut -d ' ' -f 1
}

prepare_recovery_runner_snapshot() {
  local final_dir

  final_dir="$RUN_DIR/recovery-source-$TOOLING_SHA"
  if sudo test -e "$final_dir" || sudo test -L "$final_dir"; then
    RUNNER_SOURCE_DIR="$final_dir"
    verify_source_snapshot "$RUNNER_SOURCE_DIR" "$TOOLING_SHA"
    return
  fi
  RECOVERY_STAGING_DIR="$RUN_DIR/.recovery-source-staging-$$"
  sudo test ! -e "$RECOVERY_STAGING_DIR" \
    || fail "recovery_source_staging_exists"
  sudo install -d -m 0550 -o 10001 -g 10001 "$RECOVERY_STAGING_DIR" \
    >/dev/null 2>&1 || fail "recovery_source_create_failed"
  if ! (git -C "$TOOLING_ROOT" archive --format=tar "$TOOLING_SHA" \
    | sudo tar -xf - -C "$RECOVERY_STAGING_DIR" --no-same-owner) \
    >/dev/null 2>&1; then
    fail "recovery_source_snapshot_failed"
  fi
  sudo chown -R 10001:10001 "$RECOVERY_STAGING_DIR" \
    >/dev/null 2>&1 || fail "recovery_source_owner_failed"
  sudo find "$RECOVERY_STAGING_DIR" -type d -exec chmod 0550 {} + \
    >/dev/null 2>&1 || fail "recovery_source_mode_failed"
  sudo find "$RECOVERY_STAGING_DIR" -type f -exec chmod 0440 {} + \
    >/dev/null 2>&1 || fail "recovery_source_mode_failed"
  verify_source_snapshot "$RECOVERY_STAGING_DIR" "$TOOLING_SHA"
  sudo mv -T -- "$RECOVERY_STAGING_DIR" "$final_dir" \
    >/dev/null 2>&1 || fail "recovery_source_publish_failed"
  RECOVERY_STAGING_DIR=""
  RUNNER_SOURCE_DIR="$final_dir"
  verify_source_snapshot "$RUNNER_SOURCE_DIR" "$TOOLING_SHA"
}

validate_prepare_receipt() {
  local receipt="$1"
  local manifest_sha="$2"
  local cases_sha="$3"
  python3 - "$receipt" "$manifest_sha" "$cases_sha" \
    >/dev/null 2>&1 <<'PY'
import json
import re
import sys
from pathlib import Path

path, manifest_sha, cases_sha = sys.argv[1:]
payload = json.loads(Path(path).read_text(encoding="utf-8"))
assert payload == {
    "status": "OK",
    "operation": "prepare",
    "dataset_id": "pilot50_balanced_v1",
    "cases_total": 50,
    "type_counts": {"typical": 25, "atypical": 25},
    "expected_behavior": "answer",
    "expected_escalated": False,
    "manifest_sha256": manifest_sha,
    "cases_sha256": cases_sha,
}
assert re.fullmatch(r"[0-9a-f]{64}", manifest_sha)
assert re.fullmatch(r"[0-9a-f]{64}", cases_sha)
PY
}

validate_preflight_receipt() {
  local receipt="$1"
  local tooling_sha="$2"
  local runtime_sha="$3"
  local manifest_sha="$4"
  local cases_sha="$5"
  python3 - "$receipt" "$tooling_sha" "$runtime_sha" "$manifest_sha" \
    "$cases_sha" >/dev/null 2>&1 <<'PY'
import re
import sys
from pathlib import Path

path, tooling_sha, runtime_sha, manifest_sha, cases_sha = sys.argv[1:]
payload = Path(path).read_bytes()
assert 0 < len(payload) <= 4096 and payload.endswith(b"\n")
assert re.fullmatch(r"[0-9a-f]{40}", tooling_sha)
assert re.fullmatch(r"[0-9a-f]{40}", runtime_sha)
assert re.fullmatch(r"[0-9a-f]{64}", manifest_sha)
assert re.fullmatch(r"[0-9a-f]{64}", cases_sha)
assert payload.decode("ascii").splitlines() == [
    "dataset_id=pilot50_balanced_v1",
    f"tooling_sha={tooling_sha}",
    f"runtime_sha={runtime_sha}",
    f"manifest_sha256={manifest_sha}",
    f"cases_sha256={cases_sha}",
    "cases_total=50",
    "typical_total=25",
    "atypical_total=25",
    "forecast_llm_cost_rub=10",
    "max_llm_cost_rub=20",
]
PY
}

validate_completed_receipt() {
  local receipt="$1"
  local tooling_sha="$2"
  local source_tooling_sha="$3"
  local runtime_sha="$4"
  local cases_sha="$5"
  local report_sha="$6"
  local safe_result_sha="$7"
  local recovery_receipt_sha="$8"
  local runner_source_paths_sha="$9"
  python3 - "$receipt" "$tooling_sha" "$source_tooling_sha" "$runtime_sha" \
    "$cases_sha" "$report_sha" "$safe_result_sha" "$recovery_receipt_sha" \
    "$runner_source_paths_sha" \
    >/dev/null 2>&1 <<'PY'
import re
import sys
from pathlib import Path

(
    path,
    tooling_sha,
    source_tooling_sha,
    runtime_sha,
    cases_sha,
    report_sha,
    safe_result_sha,
    recovery_receipt_sha,
    runner_source_paths_sha,
) = sys.argv[1:]
payload = Path(path).read_bytes()
assert 0 < len(payload) <= 4096 and payload.endswith(b"\n")
for value in (tooling_sha, source_tooling_sha):
    assert re.fullmatch(r"[0-9a-f]{40}", value)
assert re.fullmatch(r"[0-9a-f]{40}", runtime_sha)
for value in (cases_sha, report_sha, safe_result_sha, runner_source_paths_sha):
    assert re.fullmatch(r"[0-9a-f]{64}", value)
assert recovery_receipt_sha == "none" or re.fullmatch(
    r"[0-9a-f]{64}", recovery_receipt_sha
)
assert payload.decode("ascii").splitlines() == [
    "dataset_id=pilot50_balanced_v1",
    f"tooling_sha={tooling_sha}",
    f"source_tooling_sha={source_tooling_sha}",
    f"runtime_sha={runtime_sha}",
    f"cases_sha256={cases_sha}",
    f"report_sha256={report_sha}",
    f"safe_result_sha256={safe_result_sha}",
    f"recovery_receipt_sha256={recovery_receipt_sha}",
    f"runner_source_paths_sha256={runner_source_paths_sha}",
]
PY
}

preflight_source_sha() {
  local receipt="$1"
  python3 - "$receipt" 2>/dev/null <<'PY'
import re
import sys
from pathlib import Path

payload = Path(sys.argv[1]).read_bytes()
assert 0 < len(payload) <= 4096 and payload.endswith(b"\n")
lines = payload.decode("ascii").splitlines()
assert len(lines) == 10
assert lines[0] == "dataset_id=pilot50_balanced_v1"
prefix = "tooling_sha="
assert lines[1].startswith(prefix)
value = lines[1].removeprefix(prefix)
assert re.fullmatch(r"[0-9a-f]{40}", value)
sys.stdout.write(value)
PY
}

validate_recovery_receipt() {
  local receipt="$1"
  local recovery_tooling_sha="$2"
  local source_tooling_sha="$3"
  local runtime_sha="$4"
  local cases_sha="$5"
  local started_sha="$6"
  local approval_id="$7"
  local runner_source_paths_sha="$8"
  python3 - "$receipt" "$recovery_tooling_sha" "$source_tooling_sha" \
    "$runtime_sha" "$cases_sha" "$started_sha" "$approval_id" \
    "$runner_source_paths_sha" \
    >/dev/null 2>&1 <<'PY'
import re
import sys
from pathlib import Path

(
    path,
    recovery_tooling_sha,
    source_tooling_sha,
    runtime_sha,
    cases_sha,
    started_sha,
    approval_id,
    runner_source_paths_sha,
) = sys.argv[1:]
payload = Path(path).read_bytes()
assert 0 < len(payload) <= 4096 and payload.endswith(b"\n")
for value in (recovery_tooling_sha, source_tooling_sha, runtime_sha):
    assert re.fullmatch(r"[0-9a-f]{40}", value)
for value in (cases_sha, started_sha, runner_source_paths_sha):
    assert re.fullmatch(r"[0-9a-f]{64}", value)
assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}", approval_id)
assert payload.decode("ascii").splitlines() == [
    "schema_version=pilot50-pre-request-recovery-v1",
    "dataset_id=pilot50_balanced_v1",
    f"recovery_tooling_sha={recovery_tooling_sha}",
    f"source_tooling_sha={source_tooling_sha}",
    f"runtime_sha={runtime_sha}",
    f"cases_sha256={cases_sha}",
    f"started_sha256={started_sha}",
    f"approval_id={approval_id}",
    f"runner_source_paths_sha256={runner_source_paths_sha}",
    "phase0_billing_status=unreconciled",
    "owner_risk_accepted_for_pilot50_only=true",
    "provider_residual_risk_ceiling_rub=100",
    "runner_projected_stop_limit_rub=20",
    "prior_stage=pricing_preflight_failed",
    "prior_reservation=absent",
    "prior_report=absent",
    "prior_ask_count=0",
    "prior_runner_cost_rub=0",
    "simple_input_price_rub_per_million=12.2",
    "simple_output_price_rub_per_million=12.2",
    "complex_official_price_rub_per_million=569.3374",
    "complex_input_effective_price_rub_per_million=569.34",
    "complex_output_effective_price_rub_per_million=569.34",
    "complex_price_policy=conservative_round_up",
    "pricing_source=eval_repriced",
    "pricing_contract_id=pilot50-c38-pricing-v1",
    "pricing_rate_card_sha256=3aebb12db82391bad23ec9256781e3439f2692ad63814070e4341bd28ea27bd6",
    "pre_request_checks=target_models,acceptance_pricing,repricing_contract,trace_db,signed_ready",
]
PY
}

safe_result_approval_id() {
  local safe_result="$1"
  sudo python3 - "$safe_result" 2>/dev/null <<'PY'
import json
import re
import sys
from pathlib import Path

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result
        result[key] = value
    return result

payload = Path(sys.argv[1]).read_bytes()
assert 0 < len(payload) <= 128 * 1024
value = json.loads(payload, object_pairs_hook=unique_object)
assert isinstance(value, dict)
approval_id = value.get("approval_id")
assert isinstance(approval_id, str)
assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}", approval_id)
sys.stdout.write(approval_id)
PY
}

verify_target_models_and_reported_pricing() {
  sudo docker exec -i "$PILOT50_RUNTIME_CONTAINER" python - \
    >/dev/null 2>&1 <<'PY'
from decimal import Decimal

from src.config import get_settings
from src.llm.cascade import (
    select_analyzer_model,
    select_generator_model,
    select_judge_model,
)
from src.models import Complexity

settings = get_settings()
assert settings.cloud_ru_model_simple == "ai-sage/GigaChat3-10B-A1.8B"
assert settings.cloud_ru_model_complex == "GigaChat/GigaChat-2-Max"
reported_prices = (
    Decimal(str(settings.cloud_ru_model_simple_input_price_rub_per_million)),
    Decimal(str(settings.cloud_ru_model_simple_output_price_rub_per_million)),
    Decimal(str(settings.cloud_ru_model_complex_input_price_rub_per_million)),
    Decimal(str(settings.cloud_ru_model_complex_output_price_rub_per_million)),
)
assert reported_prices == (
    Decimal("0"),
    Decimal("0"),
    Decimal("569.34"),
    Decimal("569.34"),
)
priced_models = {settings.cloud_ru_model_simple, settings.cloud_ru_model_complex}
selected_models = {
    select_analyzer_model(Complexity.SIMPLE),
    select_analyzer_model(Complexity.COMPLEX),
    select_generator_model(Complexity.SIMPLE),
    select_generator_model(Complexity.COMPLEX),
    select_judge_model(),
}
assert selected_models <= priced_models
PY
}

verify_repricing_contract_preflight() {
  "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -c '
import os
from eval.run_ask import (
    PILOT50_REPRICING_CASES_SHA256,
    PILOT50_REPRICING_CONTRACT_ID,
    PILOT50_REPRICING_RUNTIME_SHA,
    PILOT50_REPRICING_TARGET,
    _validated_pilot50_repricing_contract,
)

assert os.environ.get("RELEASE_GIT_SHA") == PILOT50_REPRICING_RUNTIME_SHA
contract = _validated_pilot50_repricing_contract(
    PILOT50_REPRICING_CONTRACT_ID,
    cases=[{"id": str(index)} for index in range(50)],
    cases_file_sha256=PILOT50_REPRICING_CASES_SHA256,
    target=PILOT50_REPRICING_TARGET,
    concurrency=1,
    trace_lookup=True,
    bypass_cache=True,
    max_llm_cost_rub=20.0,
    max_cases=None,
    auto_smoke_cases=False,
    generated_user_prefix=None,
    private_contract_run=False,
    source_diagnostic_cases=False,
    phase0_contract=None,
    strict_live=True,
    high_cost_approval_id="preflight-only-valid-reference",
    expected_runtime_git_sha=PILOT50_REPRICING_RUNTIME_SHA,
)
assert contract is not None and contract["source"] == "eval_repriced"
' >/dev/null 2>&1
}

verify_acceptance_pricing() {
  "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -c '
import sys
from decimal import Decimal

from eval.run_ask import _local_llm_pricing_preflight_failure
from src.config import get_settings

expected = tuple(Decimal(value) for value in sys.argv[1:])
settings = get_settings()
assert settings.cloud_ru_model_simple == "ai-sage/GigaChat3-10B-A1.8B"
assert settings.cloud_ru_model_complex == "GigaChat/GigaChat-2-Max"
actual = (
    Decimal(str(settings.cloud_ru_model_simple_input_price_rub_per_million)),
    Decimal(str(settings.cloud_ru_model_simple_output_price_rub_per_million)),
    Decimal(str(settings.cloud_ru_model_complex_input_price_rub_per_million)),
    Decimal(str(settings.cloud_ru_model_complex_output_price_rub_per_million)),
)
assert actual == expected
assert _local_llm_pricing_preflight_failure() is None
' \
    "$PILOT50_SIMPLE_INPUT_PRICE_RUB_PER_MILLION" \
    "$PILOT50_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION" \
    "$PILOT50_COMPLEX_INPUT_PRICE_RUB_PER_MILLION" \
    "$PILOT50_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION" \
    >/dev/null 2>&1
}

verify_trace_db_preflight() {
  "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -c '
import asyncio

import asyncpg

from eval.run_ask import _trace_dsn_candidates

async def main():
    pool = None
    for candidate in _trace_dsn_candidates(None):
        try:
            pool = await asyncpg.create_pool(
                candidate,
                min_size=1,
                max_size=1,
                command_timeout=30,
            )
            break
        except Exception:
            continue
    assert pool is not None
    try:
        async with pool.acquire() as connection:
            async with connection.transaction(readonly=True):
                value = await connection.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM request_traces
                    WHERE eval_run_id = $1
                      AND eval_case_id = $2
                      AND cache_hit IS NOT NULL
                    """,
                    "pilot50-pre-request-probe",
                    "pilot50-pre-request-probe",
                )
                assert value == 0
    finally:
        await pool.close()

asyncio.run(main())
' >/dev/null 2>&1
}

verify_signed_ready_preflight() {
  "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -c '
import asyncio
import sys
from uuid import uuid4

import httpx

from eval.run_ask import _auth_headers, _verify_cache_bypass_runtime

async def main():
    expected_runtime_sha, target = sys.argv[1:]
    headers = _auth_headers("API_AUTH_TOKEN")
    secret = headers.get("X-API-Key", "").strip()
    assert secret
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        observed = await _verify_cache_bypass_runtime(
            client=client,
            target=target,
            headers=headers,
            expected_git_sha=expected_runtime_sha,
            eval_run_id=f"ask-eval-preflight-{uuid4()}",
            cache_bypass_secret=secret,
        )
    assert observed == expected_runtime_sha

asyncio.run(main())
' "$RUNTIME_SHA" "$PILOT50_TARGET" >/dev/null 2>&1
}

verify_cost_governance_preflight() {
  local approval_id="$1"
  "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -c '
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from eval.cost_governance import (
    _enforce_approval_once,
    _enforce_rolling_limits,
    _scan_records,
    _validated_approval_id,
    approval_required,
)

runtime_sha, approval_id = sys.argv[1:]
ledger = Path("/cost-ledger")
assert ledger.is_dir() and not ledger.is_symlink()
assert not os.path.lexists(ledger / ".cost-governance.lock")
validated_approval = _validated_approval_id(approval_id)
assert validated_approval == approval_id
assert approval_required(case_count=50, budget_rub=20.0, private_full=False)
now = datetime.now(UTC)
records = _scan_records(ledger, now=now)
_enforce_approval_once(records, approval_id=validated_approval)
_enforce_rolling_limits(
    records,
    now=now,
    requested_cap=20.0,
    private_full=False,
    requested_runtime_git_sha=runtime_sha,
)
' "$RUNTIME_SHA" "$approval_id" >/dev/null 2>&1
}

verify_free_live_preflight() {
  local approval_id="$1"
  verify_cost_governance_preflight "$approval_id" \
    || fail "cost_governance_preflight_failed"
  verify_target_models_and_reported_pricing \
    || fail "target_model_or_reported_pricing_invalid"
  verify_acceptance_pricing || fail "acceptance_pricing_invalid"
  verify_repricing_contract_preflight || fail "repricing_contract_invalid"
  verify_trace_db_preflight || fail "trace_db_preflight_failed"
  verify_signed_ready_preflight || fail "signed_ready_preflight_failed"
  verify_cost_governance_preflight "$approval_id" \
    || fail "cost_governance_preflight_failed"
}

verify_recovery_ledger_unused() {
  local approval_id="$1"
  "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -c '
import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from eval.cost_governance import _scan_records

runtime_sha, approval_id = sys.argv[1:]
ledger = Path("/cost-ledger")
assert ledger.is_dir() and not ledger.is_symlink()
assert not os.path.lexists(ledger / ".cost-governance.lock")
cases_path = Path("/evidence/pilot50-cases.json")
cases_sha = hashlib.sha256(cases_path.read_bytes()).hexdigest()
records = _scan_records(ledger, now=datetime.now(UTC))
matching = [
    record
    for record in records
    if record.get("scope") == "ask-eval"
    and record.get("runtime_git_sha") == runtime_sha
    and record.get("manifest_sha256") == cases_sha
    and record.get("case_count") == 50
    and float(record.get("approved_cap_rub")) == 20.0
    and record.get("private_full") is False
    and record.get("approval_required") is True
]
approval_uses = [
    record for record in records
    if record.get("high_cost_approval_id") == approval_id
]
assert not matching and not approval_uses
' "$RUNTIME_SHA" "$approval_id" >/dev/null 2>&1
}

validate_started_marker() {
  local marker="$1"
  local owner_uid="$2"
  local owner_gid="$3"
  [[ -f "$marker" && ! -L "$marker" ]] || return 1
  [[ "$(stat -c '%u:%g:%a:%s' "$marker" 2>/dev/null)" == \
    "$owner_uid:$owner_gid:600:8" ]] || return 1
  python3 - "$marker" >/dev/null 2>&1 <<'PY'
import sys
from pathlib import Path

assert Path(sys.argv[1]).read_bytes() == b"started\n"
PY
}

preflight_mode() {
  local owner_uid owner_gid marker prepare_receipt manifest_sha cases_sha
  owner_uid="$(id -u)"
  owner_gid="$(id -g)"

  if sudo test -e "$RUN_DIR" || sudo test -L "$RUN_DIR"; then
    fail "run_already_prepared_or_executed"
  fi
  sudo install -d -m 0750 -o "$owner_uid" -g "$owner_gid" \
    "$PILOT50_BASE_DIR" "$PILOT50_BASE_DIR/runs" \
    >/dev/null 2>&1 || fail "pilot50_base_unavailable"
  sudo test -d "$PILOT50_BASE_DIR" || fail "pilot50_base_unavailable"
  sudo test ! -L "$PILOT50_BASE_DIR" || fail "pilot50_base_not_regular"
  [[ "$(sudo readlink -f -- "$PILOT50_BASE_DIR" 2>/dev/null)" == \
    "$PILOT50_BASE_DIR" ]] || fail "pilot50_base_not_regular"
  sudo test -d "$PILOT50_BASE_DIR/runs" || fail "pilot50_base_unavailable"
  sudo test ! -L "$PILOT50_BASE_DIR/runs" || fail "pilot50_base_not_regular"
  [[ "$(sudo readlink -f -- "$PILOT50_BASE_DIR/runs" 2>/dev/null)" == \
    "$PILOT50_BASE_DIR/runs" ]] || fail "pilot50_base_not_regular"
  sudo install -d -m 0700 -o 10001 -g 10001 "$PILOT50_LEDGER_DIR" \
    >/dev/null 2>&1 || fail "cost_ledger_unavailable"
  sudo test -d "$PILOT50_LEDGER_DIR" || fail "cost_ledger_unavailable"
  sudo test ! -L "$PILOT50_LEDGER_DIR" || fail "cost_ledger_not_regular"
  [[ "$(sudo readlink -f -- "$PILOT50_LEDGER_DIR" 2>/dev/null)" == \
    "$PILOT50_LEDGER_DIR" ]] || fail "cost_ledger_not_regular"

  STAGING_DIR="$PILOT50_BASE_DIR/runs/.staging-${PILOT50_DATASET_ID}-${RUNTIME_SHA}-$$"
  if sudo test -e "$STAGING_DIR" || sudo test -L "$STAGING_DIR"; then
    fail "staging_exists"
  fi
  sudo install -d -m 0700 -o "$owner_uid" -g "$owner_gid" "$STAGING_DIR" \
    >/dev/null 2>&1 || fail "staging_create_failed"
  SOURCE_DIR="$STAGING_DIR/source"
  RUNNER_SOURCE_DIR="$SOURCE_DIR"
  EVIDENCE_DIR="$STAGING_DIR/evidence"
  PROVENANCE_DIR="$STAGING_DIR/provenance"
  sudo install -d -m 0550 -o 10001 -g 10001 "$SOURCE_DIR" "$PROVENANCE_DIR" \
    >/dev/null 2>&1 || fail "snapshot_directory_failed"
  sudo install -d -m 0700 -o 10001 -g 10001 "$EVIDENCE_DIR" \
    >/dev/null 2>&1 || fail "evidence_directory_failed"

  if ! (git -C "$TOOLING_ROOT" archive --format=tar "$TOOLING_SHA" \
    | sudo tar -xf - -C "$SOURCE_DIR" --no-same-owner) >/dev/null 2>&1; then
    fail "source_snapshot_failed"
  fi
  sudo chown -R 10001:10001 "$SOURCE_DIR" "$PROVENANCE_DIR" \
    >/dev/null 2>&1 || fail "snapshot_owner_failed"
  sudo find "$SOURCE_DIR" -type d -exec chmod 0550 {} + \
    >/dev/null 2>&1 || fail "snapshot_mode_failed"
  sudo find "$SOURCE_DIR" -type f -exec chmod 0440 {} + \
    >/dev/null 2>&1 || fail "snapshot_mode_failed"
  verify_source_snapshot

  build_compose_command
  "${compose[@]}" config --quiet >/dev/null 2>&1 \
    || fail "compose_config_failed"

  prepare_receipt="$STAGING_DIR/prepare-safe.json"
  if ! "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -m scripts.pilot50 prepare \
    --manifest "/workspace/$PILOT50_MANIFEST_REL" \
    --output /evidence/pilot50-cases.json \
    >"$prepare_receipt" 2>/dev/null; then
    fail "prepare_failed"
  fi

  sudo test -f "$EVIDENCE_DIR/pilot50-cases.json" \
    || fail "cases_missing_after_prepare"
  sudo test ! -L "$EVIDENCE_DIR/pilot50-cases.json" \
    || fail "cases_not_regular"
  manifest_sha="$(sudo sha256sum "$SOURCE_DIR/$PILOT50_MANIFEST_REL" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "manifest_sha_unavailable"
  cases_sha="$(sudo sha256sum "$EVIDENCE_DIR/pilot50-cases.json" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "cases_sha_unavailable"
  [[ "$manifest_sha" =~ ^[0-9a-f]{64}$ ]] || fail "manifest_sha_invalid"
  [[ "$cases_sha" =~ ^[0-9a-f]{64}$ ]] || fail "cases_sha_invalid"
  validate_prepare_receipt "$prepare_receipt" "$manifest_sha" "$cases_sha" \
    || fail "prepare_receipt_invalid"

  marker="$STAGING_DIR/preflight.receipt"
  printf '%s\n' \
    "dataset_id=$PILOT50_DATASET_ID" \
    "tooling_sha=$TOOLING_SHA" \
    "runtime_sha=$RUNTIME_SHA" \
    "manifest_sha256=$manifest_sha" \
    "cases_sha256=$cases_sha" \
    "cases_total=$PILOT50_CASES_TOTAL" \
    "typical_total=$PILOT50_TYPICAL_TOTAL" \
    "atypical_total=$PILOT50_ATYPICAL_TOTAL" \
    "forecast_llm_cost_rub=$PILOT50_FORECAST_COST_RUB" \
    "max_llm_cost_rub=$PILOT50_COST_CAP_RUB" \
    >"$marker" || fail "preflight_receipt_write_failed"
  validate_preflight_receipt \
    "$marker" "$TOOLING_SHA" "$RUNTIME_SHA" "$manifest_sha" "$cases_sha" \
    || fail "preflight_receipt_invalid"
  chmod 0600 "$marker" "$prepare_receipt" \
    || fail "preflight_receipt_mode_failed"
  sudo chmod 0600 "$EVIDENCE_DIR/pilot50-cases.json" \
    >/dev/null 2>&1 || fail "cases_mode_failed"

  SOURCE_DIR="$RUN_DIR/source"
  RUNNER_SOURCE_DIR="$SOURCE_DIR"
  EVIDENCE_DIR="$RUN_DIR/evidence"
  PROVENANCE_DIR="$RUN_DIR/provenance"
  if sudo test -e "$RUN_DIR" || sudo test -L "$RUN_DIR"; then
    fail "run_already_prepared_or_executed"
  fi
  sudo mv -T -- "$STAGING_DIR" "$RUN_DIR" >/dev/null 2>&1 \
    || fail "preflight_publish_failed"
  STAGING_DIR=""

  printf 'pilot50_preflight=OK\n'
  printf 'tooling_sha=%s\n' "$TOOLING_SHA"
  printf 'runtime_sha=%s\n' "$RUNTIME_SHA"
  printf 'manifest_sha256=%s\n' "$manifest_sha"
  printf 'cases_sha256=%s\n' "$cases_sha"
  printf 'cases_total=%s\n' "$PILOT50_CASES_TOTAL"
  printf 'typical=%s\n' "$PILOT50_TYPICAL_TOTAL"
  printf 'atypical=%s\n' "$PILOT50_ATYPICAL_TOTAL"
  printf 'forecast_llm_cost_rub=%s\n' "$PILOT50_FORECAST_COST_RUB"
  printf 'max_llm_cost_rub=%s\n' "$PILOT50_COST_CAP_RUB"
}

validate_safe_stdout() {
  local expected_cases_sha="$1"
  local expected_report_sha="$2"
  local expected_runtime_sha="$3"
  local expected_approval_id="$4"
  python3 -c '
import json
import math
import re
import sys
from datetime import datetime, timedelta
from uuid import UUID

allowed = {
    "schema_version", "dataset_id", "eval_run_id", "runtime_git_sha",
    "approval_id", "run_window_utc", "billing_status",
    "status", "classification",
    "human_product_verdict", "denominator", "counts",
    "mechanical_first_turn_closure", "policy_pass", "trace_coverage",
    "cache_hits", "budget", "pricing", "latency_ms", "llm_cost_rub",
    "cases_sha256", "report_sha256", "disclaimer",
}
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result
        result[key] = value
    return result

expected_cases_sha, expected_report_sha, expected_runtime_sha, expected_approval_id = (
    sys.argv[1:]
)
assert re.fullmatch(r"[0-9a-f]{64}", expected_cases_sha)
assert re.fullmatch(r"[0-9a-f]{64}", expected_report_sha)
assert re.fullmatch(r"[0-9a-f]{40}", expected_runtime_sha)
assert expected_runtime_sha != "0" * 40
assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}", expected_approval_id)
payload = json.load(sys.stdin, object_pairs_hook=unique_object)
assert isinstance(payload, dict) and set(payload) == allowed
assert payload.get("schema_version") == "pilot50-safe-result-v1"
assert payload.get("dataset_id") == "pilot50_balanced_v1"
eval_run_id = payload.get("eval_run_id")
assert isinstance(eval_run_id, str) and re.fullmatch(r"ask-eval-[0-9a-f-]{36}", eval_run_id)
assert eval_run_id == "ask-eval-" + str(UUID(eval_run_id.removeprefix("ask-eval-")))
assert payload.get("runtime_git_sha") == expected_runtime_sha
assert payload.get("approval_id") == expected_approval_id
run_window = payload.get("run_window_utc")
assert isinstance(run_window, dict) and set(run_window) == {"started_at", "completed_at"}
timestamps = []
for field in ("started_at", "completed_at"):
    value = run_window.get(field)
    assert isinstance(value, str) and 0 < len(value) <= 40
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    timestamps.append(parsed)
assert timestamps[0] <= timestamps[1]
assert timestamps[1] - timestamps[0] <= timedelta(hours=4)
assert payload.get("billing_status") == "pending_provider_reconciliation"
assert payload.get("status") == "OK"
assert payload.get("classification") == "calibration_only"
assert payload.get("human_product_verdict") is False
assert payload.get("denominator") == 50
assert payload.get("counts") == {"typical": 25, "atypical": 25}
for field, count_key in (
    ("mechanical_first_turn_closure", "closed"),
    ("policy_pass", "passed"),
):
    table = payload.get(field)
    assert isinstance(table, dict) and set(table) == {"typical", "atypical", "overall"}
    for group, denominator in {"typical": 25, "atypical": 25, "overall": 50}.items():
        row = table.get(group)
        assert isinstance(row, dict) and set(row) == {count_key, "total", "rate"}
        numerator = row.get(count_key)
        assert type(numerator) is int and 0 <= numerator <= denominator
        assert row.get("total") == denominator
        assert row.get("rate") == round(numerator / denominator, 6)
    assert table["overall"][count_key] == (
        table["typical"][count_key] + table["atypical"][count_key]
    )
closure = payload["mechanical_first_turn_closure"]
policy = payload["policy_pass"]
for group in ("typical", "atypical", "overall"):
    assert closure[group]["closed"] == policy[group]["passed"]
assert payload.get("trace_coverage") == {"found": 50, "total": 50, "rate": 1.0}
assert payload.get("cache_hits") == 0
assert payload.get("budget") == {"max_rub": 20, "exceeded": False, "stopped": False}
assert payload.get("pricing") == {
    "complete": True,
    "stopped": False,
    "source": "eval_repriced",
    "contract_id": "pilot50-c38-pricing-v1",
    "rate_card_sha256": "3aebb12db82391bad23ec9256781e3439f2692ad63814070e4341bd28ea27bd6",
    "target_telemetry_preserved": True,
    "target_telemetry_pricing_complete": False,
}
latency = payload.get("latency_ms")
assert isinstance(latency, dict) and set(latency) == {"p50", "p95"}
assert all(type(value) is int and value >= 0 for value in latency.values())
assert latency["p50"] <= latency["p95"]
cost = payload.get("llm_cost_rub")
assert type(cost) in (int, float) and math.isfinite(cost) and 0 <= cost <= 20
for field in ("cases_sha256", "report_sha256"):
    assert isinstance(payload.get(field), str)
    assert re.fullmatch(r"[0-9a-f]{64}", payload[field])
assert payload.get("cases_sha256") == expected_cases_sha
assert payload.get("report_sha256") == expected_report_sha
expected_disclaimer = (
    "Tracked regression calibration only. This is a mechanical first-turn "
    "closure result for the balanced Pilot50 set, not an independent holdout, "
    "a human product verdict, ticket-level conversion, or production traffic conversion."
)
assert payload.get("disclaimer") == expected_disclaimer
print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
' "$expected_cases_sha" "$expected_report_sha" \
  "$expected_runtime_sha" "$expected_approval_id" 2>/dev/null
}

validate_review_stdout() {
  python3 -c '
import json
import re
import sys
from collections import Counter

max_bytes = 32 * 1024 * 1024
sentinel = b"pilot50-review-stream-complete-v1"
allowed = {
    "ordinal", "group", "query", "response", "was_escalated",
    "escalation_reason", "passed", "observed_behavior",
}
behaviors = {"answer", "clarify", "escalate", "scope_note"}
reason_re = re.compile(r"[a-z][a-z0-9_]{0,79}")

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result
        result[key] = value
    return result

payload = sys.stdin.buffer.read(max_bytes + 1)
assert 0 < len(payload) <= max_bytes and payload.endswith(b"\n")
assert all(byte == 10 or (byte >= 32 and byte != 127) for byte in payload)
lines = payload[:-1].split(b"\n")
assert lines and lines[-1] == sentinel
lines.pop()
assert len(lines) == 50 and all(lines)

rows = []
canonical = []
for line in lines:
    row = json.loads(line.decode("utf-8"), object_pairs_hook=unique_object)
    assert isinstance(row, dict) and set(row) == allowed
    ordinal = row.get("ordinal")
    assert type(ordinal) is int and 1 <= ordinal <= 50
    assert row.get("group") in {"typical", "atypical"}
    query = row.get("query")
    response = row.get("response")
    assert isinstance(query, str) and 1 <= len(query) <= 50000
    assert isinstance(response, str) and len(response) <= 50000
    query.encode("utf-8")
    response.encode("utf-8")
    assert type(row.get("was_escalated")) is bool
    escalation_reason = row.get("escalation_reason")
    assert escalation_reason is None or (
        isinstance(escalation_reason, str)
        and reason_re.fullmatch(escalation_reason) is not None
    )
    assert type(row.get("passed")) is bool
    assert row.get("observed_behavior") in behaviors
    rows.append(row)
    canonical.append(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )

assert [row["ordinal"] for row in rows] == list(range(1, 51))
assert Counter(row["group"] for row in rows) == {"typical": 25, "atypical": 25}
sys.stdout.write("\n".join(canonical) + "\n")
' 2>/dev/null
}

run_mode() {
  local marker cases_sha final_cases_sha manifest_sha approval_id
  local report_sha safe_result_sha safe_stdout validated_safe
  local raw_report safe_result started completed recovery_receipt started_sha
  local recovery_receipt_sha
  local runner_source_paths_sha runner_source_sha
  local owner_uid owner_gid

  owner_uid="$(id -u)"
  owner_gid="$(id -g)"

  approval_id="${HIGH_COST_APPROVAL_ID:-}"
  [[ "$approval_id" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$ ]] \
    || fail "approval_id_missing_or_invalid"
  if [[ "$RECOVER_PRE_REQUEST" == "1" ]]; then
    [[ -z "${PHASE0_BILLING_VERDICT+x}" ]] \
      || fail "recovery_phase0_billing_verdict_must_be_unset"
    [[ "${PILOT50_PHASE0_BILLING_RISK_ACCEPTANCE_ID:-}" == "$approval_id" ]] \
      || fail "recovery_risk_acceptance_missing_or_mismatch"
  else
    [[ "${PHASE0_BILLING_VERDICT:-}" == "PASS" ]] \
      || fail "phase0_billing_not_passed"
    [[ -z "${PILOT50_PHASE0_BILLING_RISK_ACCEPTANCE_ID+x}" ]] \
      || fail "unexpected_recovery_risk_acceptance"
  fi

  sudo test -d "$RUN_DIR" || fail "preflight_not_found"
  sudo test ! -L "$RUN_DIR" || fail "run_directory_not_regular"
  [[ "$(sudo readlink -f -- "$RUN_DIR" 2>/dev/null)" == "$RUN_DIR" ]] \
    || fail "run_directory_not_regular"
  sudo test -d "$PILOT50_LEDGER_DIR" || fail "cost_ledger_unavailable"
  sudo test ! -L "$PILOT50_LEDGER_DIR" || fail "cost_ledger_not_regular"
  [[ "$(sudo readlink -f -- "$PILOT50_LEDGER_DIR" 2>/dev/null)" == \
    "$PILOT50_LEDGER_DIR" ]] || fail "cost_ledger_not_regular"
  [[ "$(sudo stat -c '%u:%g:%a' "$PILOT50_LEDGER_DIR" 2>/dev/null)" == \
    "10001:10001:700" ]] || fail "cost_ledger_mode_mismatch"
  marker="$RUN_DIR/preflight.receipt"
  [[ -f "$marker" && ! -L "$marker" ]] || fail "preflight_receipt_missing"
  SOURCE_SHA="$(preflight_source_sha "$marker")" \
    || fail "preflight_source_sha_invalid"
  if [[ "$RECOVER_PRE_REQUEST" == "1" ]]; then
    [[ "$SOURCE_SHA" == "$PILOT50_PRE_REQUEST_FAILED_TOOLING_SHA" ]] \
      || fail "recovery_source_sha_mismatch"
  else
    [[ "$SOURCE_SHA" == "$TOOLING_SHA" ]] \
      || fail "preflight_tooling_sha_mismatch"
  fi

  verify_source_snapshot
  sudo test -d "$EVIDENCE_DIR" || fail "evidence_directory_missing"
  sudo test ! -L "$EVIDENCE_DIR" || fail "evidence_directory_not_regular"
  [[ "$(sudo stat -c '%u:%g:%a' "$EVIDENCE_DIR" 2>/dev/null)" == \
    "10001:10001:700" ]] || fail "evidence_directory_mode_mismatch"
  sudo test -f "$EVIDENCE_DIR/pilot50-cases.json" || fail "cases_missing"
  sudo test ! -L "$EVIDENCE_DIR/pilot50-cases.json" || fail "cases_not_regular"
  cases_sha="$(sudo sha256sum "$EVIDENCE_DIR/pilot50-cases.json" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "cases_sha_unavailable"
  manifest_sha="$(sudo sha256sum "$SOURCE_DIR/$PILOT50_MANIFEST_REL" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "manifest_sha_unavailable"
  [[ "$cases_sha" =~ ^[0-9a-f]{64}$ ]] || fail "cases_sha_unavailable"
  [[ "$manifest_sha" =~ ^[0-9a-f]{64}$ ]] || fail "manifest_sha_unavailable"
  if [[ "$RECOVER_PRE_REQUEST" == "1" ]]; then
    [[ "$RUNTIME_SHA" == "$PILOT50_RECOVERY_RUNTIME_SHA" ]] \
      || fail "recovery_runtime_sha_mismatch"
    [[ "$cases_sha" == "$PILOT50_RECOVERY_CASES_SHA256" ]] \
      || fail "recovery_cases_sha_mismatch"
    [[ "$manifest_sha" == "$PILOT50_RECOVERY_MANIFEST_SHA256" ]] \
      || fail "recovery_manifest_sha_mismatch"
  fi
  validate_preflight_receipt \
    "$marker" "$SOURCE_SHA" "$RUNTIME_SHA" "$manifest_sha" "$cases_sha" \
    || fail "preflight_receipt_mismatch"

  raw_report="$EVIDENCE_DIR/pilot50-ask-report.json"
  safe_result="$EVIDENCE_DIR/pilot50-safe-result.json"
  started="$RUN_DIR/run.started"
  completed="$RUN_DIR/run.completed"
  recovery_receipt="$RUN_DIR/run.recover-pre-request.receipt"
  recovery_receipt_sha="none"
  if sudo test -e "$raw_report" || sudo test -L "$raw_report"; then
    fail "run_artifact_exists"
  fi
  if sudo test -e "$safe_result" || sudo test -L "$safe_result"; then
    fail "run_artifact_exists"
  fi
  [[ ! -e "$completed" && ! -L "$completed" ]] || fail "run_replay_refused"
  if [[ "$RECOVER_PRE_REQUEST" == "1" ]]; then
    validate_started_marker "$started" "$owner_uid" "$owner_gid" \
      || fail "recovery_started_marker_invalid"
    [[ ! -e "$recovery_receipt" && ! -L "$recovery_receipt" ]] \
      || fail "recovery_already_consumed"
    started_sha="$(sha256sum "$started" 2>/dev/null | cut -d ' ' -f 1)" \
      || fail "recovery_started_sha_unavailable"
    [[ "$started_sha" =~ ^[0-9a-f]{64}$ ]] \
      || fail "recovery_started_sha_unavailable"
    [[ "$started_sha" == "$PILOT50_STARTED_MARKER_SHA256" ]] \
      || fail "recovery_started_marker_invalid"
  else
    [[ ! -e "$started" && ! -L "$started" ]] || fail "run_replay_refused"
    [[ ! -e "$recovery_receipt" && ! -L "$recovery_receipt" ]] \
      || fail "unexpected_recovery_receipt"
  fi

  if [[ "$RECOVER_PRE_REQUEST" == "1" ]]; then
    prepare_recovery_runner_snapshot
    runner_source_sha="$TOOLING_SHA"
  else
    runner_source_sha="$SOURCE_SHA"
  fi
  runner_source_paths_sha="$(source_paths_sha "$runner_source_sha")" \
    || fail "runner_source_paths_sha_unavailable"
  [[ "$runner_source_paths_sha" =~ ^[0-9a-f]{64}$ ]] \
    || fail "runner_source_paths_sha_unavailable"
  verify_source_snapshot "$RUNNER_SOURCE_DIR" "$runner_source_sha"
  build_compose_command
  "${compose[@]}" config --quiet >/dev/null 2>&1 \
    || fail "compose_config_failed"
  if [[ "$RECOVER_PRE_REQUEST" == "1" ]]; then
    verify_recovery_ledger_unused "$approval_id" \
      || fail "recovery_reservation_not_absent"
  fi
  verify_free_live_preflight "$approval_id"
  if [[ "$RECOVER_PRE_REQUEST" == "1" ]]; then
    verify_recovery_ledger_unused "$approval_id" \
      || fail "recovery_reservation_not_absent"
  fi
  if sudo test -e "$raw_report" || sudo test -L "$raw_report"; then
    fail "run_artifact_appeared_during_preflight"
  fi
  if sudo test -e "$safe_result" || sudo test -L "$safe_result"; then
    fail "run_artifact_appeared_during_preflight"
  fi
  [[ ! -e "$completed" && ! -L "$completed" ]] \
    || fail "run_artifact_appeared_during_preflight"
  [[ "$(sudo sha256sum "$EVIDENCE_DIR/pilot50-cases.json" 2>/dev/null \
    | cut -d ' ' -f 1)" == "$cases_sha" ]] \
    || fail "cases_changed_during_preflight"
  if [[ "$RECOVER_PRE_REQUEST" == "1" ]]; then
    [[ "$(sha256sum "$started" 2>/dev/null | cut -d ' ' -f 1)" == \
      "$started_sha" ]] || fail "recovery_started_marker_changed"
    (set -o noclobber; printf '%s\n' \
      "schema_version=pilot50-pre-request-recovery-v1" \
      "dataset_id=$PILOT50_DATASET_ID" \
      "recovery_tooling_sha=$TOOLING_SHA" \
      "source_tooling_sha=$SOURCE_SHA" \
      "runtime_sha=$RUNTIME_SHA" \
      "cases_sha256=$cases_sha" \
      "started_sha256=$started_sha" \
      "approval_id=$approval_id" \
      "runner_source_paths_sha256=$runner_source_paths_sha" \
      "phase0_billing_status=unreconciled" \
      "owner_risk_accepted_for_pilot50_only=true" \
      "provider_residual_risk_ceiling_rub=100" \
      "runner_projected_stop_limit_rub=$PILOT50_COST_CAP_RUB" \
      "prior_stage=pricing_preflight_failed" \
      "prior_reservation=absent" \
      "prior_report=absent" \
      "prior_ask_count=0" \
      "prior_runner_cost_rub=0" \
      "simple_input_price_rub_per_million=$PILOT50_SIMPLE_INPUT_PRICE_RUB_PER_MILLION" \
      "simple_output_price_rub_per_million=$PILOT50_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION" \
      "complex_official_price_rub_per_million=$PILOT50_COMPLEX_OFFICIAL_PRICE_RUB_PER_MILLION" \
      "complex_input_effective_price_rub_per_million=$PILOT50_COMPLEX_INPUT_PRICE_RUB_PER_MILLION" \
      "complex_output_effective_price_rub_per_million=$PILOT50_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION" \
      "complex_price_policy=conservative_round_up" \
      "pricing_source=eval_repriced" \
      "pricing_contract_id=$PILOT50_REPRICING_CONTRACT_ID" \
      "pricing_rate_card_sha256=$PILOT50_REPRICING_RATE_CARD_SHA256" \
      "pre_request_checks=target_models,acceptance_pricing,repricing_contract,trace_db,signed_ready" \
      >"$recovery_receipt") 2>/dev/null \
      || fail "recovery_receipt_create_failed"
    chmod 0600 "$recovery_receipt" || fail "recovery_receipt_mode_failed"
    validate_recovery_receipt \
      "$recovery_receipt" "$TOOLING_SHA" "$SOURCE_SHA" "$RUNTIME_SHA" \
      "$cases_sha" "$started_sha" "$approval_id" "$runner_source_paths_sha" \
      || fail "recovery_receipt_invalid"
    recovery_receipt_sha="$(sha256sum "$recovery_receipt" 2>/dev/null \
      | cut -d ' ' -f 1)" || fail "recovery_receipt_sha_unavailable"
    [[ "$recovery_receipt_sha" =~ ^[0-9a-f]{64}$ ]] \
      || fail "recovery_receipt_sha_unavailable"
  else
    (set -o noclobber; printf 'started\n' >"$started") 2>/dev/null \
      || fail "run_replay_refused"
    chmod 0600 "$started" || fail "run_marker_mode_failed"
  fi

  if ! "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -m eval.run_ask \
    --cases /evidence/pilot50-cases.json \
    --output /evidence/pilot50-ask-report.json \
    --no-markdown \
    --target "$PILOT50_TARGET" \
    --concurrency 1 \
    --timeout 180 \
    --max-llm-cost-rub "$PILOT50_COST_CAP_RUB" \
    --llm-cost-repricing-contract "$PILOT50_REPRICING_CONTRACT_ID" \
    --expected-runtime-git-sha "$RUNTIME_SHA" \
    --high-cost-approval-id "$approval_id" \
    --kb-seed /workspace/data/knowledge_base_seed.json \
    --bypass-cache \
    --require-complete-traces \
    >/dev/null 2>&1; then
    fail "ask_eval_failed"
  fi
  sudo test -f "$raw_report" || fail "raw_report_missing"
  sudo test ! -L "$raw_report" || fail "raw_report_not_regular"

  if ! "${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -m scripts.pilot50 summarize \
    --manifest "/workspace/$PILOT50_MANIFEST_REL" \
    --cases /evidence/pilot50-cases.json \
    --report /evidence/pilot50-ask-report.json \
    --output /evidence/pilot50-safe-result.json \
    --expected-runtime-git-sha "$RUNTIME_SHA" \
    --expected-approval-id "$approval_id" \
    >/dev/null 2>&1; then
    fail "summarize_failed"
  fi
  sudo test -f "$safe_result" || fail "safe_result_missing"
  sudo test ! -L "$safe_result" || fail "safe_result_not_regular"
  sudo chmod 0600 \
    "$EVIDENCE_DIR/pilot50-cases.json" "$raw_report" "$safe_result" \
    >/dev/null 2>&1 || fail "artifact_mode_failed"

  final_cases_sha="$(sudo sha256sum "$EVIDENCE_DIR/pilot50-cases.json" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "cases_sha_unavailable"
  report_sha="$(sudo sha256sum "$raw_report" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "report_sha_unavailable"
  safe_result_sha="$(sudo sha256sum "$safe_result" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "safe_result_sha_unavailable"
  [[ "$final_cases_sha" == "$cases_sha" ]] || fail "cases_changed_during_run"
  [[ "$report_sha" =~ ^[0-9a-f]{64}$ ]] || fail "report_sha_unavailable"
  [[ "$safe_result_sha" =~ ^[0-9a-f]{64}$ ]] \
    || fail "safe_result_sha_unavailable"

  if ! safe_stdout="$("${compose[@]}" run --rm --no-deps --pull never \
    --entrypoint python quality-acceptance \
    -m scripts.pilot50 show-safe \
    --input /evidence/pilot50-safe-result.json \
    2>/dev/null)"; then
    fail "safe_output_failed"
  fi
  [[ ${#safe_stdout} -le 16384 ]] || fail "safe_output_oversized"
  if ! validated_safe="$(printf '%s' "$safe_stdout" \
    | validate_safe_stdout \
      "$final_cases_sha" "$report_sha" "$RUNTIME_SHA" "$approval_id")"; then
    fail "safe_output_invalid"
  fi

  [[ "$(sudo sha256sum "$EVIDENCE_DIR/pilot50-cases.json" 2>/dev/null \
    | cut -d ' ' -f 1)" == "$final_cases_sha" ]] || fail "cases_changed_after_summary"
  [[ "$(sudo sha256sum "$raw_report" 2>/dev/null | cut -d ' ' -f 1)" == \
    "$report_sha" ]] || fail "report_changed_after_summary"
  [[ "$(sudo sha256sum "$safe_result" 2>/dev/null | cut -d ' ' -f 1)" == \
    "$safe_result_sha" ]] || fail "safe_result_changed_after_summary"

  verify_runtime_ready || fail "runtime_not_ready_after_run"

  if [[ "$RECOVER_PRE_REQUEST" == "1" ]]; then
    validate_started_marker "$started" "$owner_uid" "$owner_gid" \
      || fail "recovery_started_marker_changed"
    [[ "$(sha256sum "$started" 2>/dev/null | cut -d ' ' -f 1)" == \
      "$started_sha" ]] || fail "recovery_started_marker_changed"
    validate_recovery_receipt \
      "$recovery_receipt" "$TOOLING_SHA" "$SOURCE_SHA" "$RUNTIME_SHA" \
      "$final_cases_sha" "$started_sha" "$approval_id" \
      "$runner_source_paths_sha" \
      || fail "recovery_receipt_changed"
    [[ "$(sha256sum "$recovery_receipt" 2>/dev/null | cut -d ' ' -f 1)" == \
      "$recovery_receipt_sha" ]] || fail "recovery_receipt_changed"
  fi

  (set -o noclobber; printf '%s\n' \
    "dataset_id=$PILOT50_DATASET_ID" \
    "tooling_sha=$TOOLING_SHA" \
    "source_tooling_sha=$SOURCE_SHA" \
    "runtime_sha=$RUNTIME_SHA" \
    "cases_sha256=$final_cases_sha" \
    "report_sha256=$report_sha" \
    "safe_result_sha256=$safe_result_sha" \
    "recovery_receipt_sha256=$recovery_receipt_sha" \
    "runner_source_paths_sha256=$runner_source_paths_sha" \
    >"$completed") 2>/dev/null \
    || fail "completion_marker_failed"
  chmod 0600 "$completed" || fail "completion_marker_mode_failed"
  validate_completed_receipt \
    "$completed" "$TOOLING_SHA" "$SOURCE_SHA" "$RUNTIME_SHA" \
    "$final_cases_sha" "$report_sha" "$safe_result_sha" \
    "$recovery_receipt_sha" "$runner_source_paths_sha" \
    || fail "completion_marker_invalid"

  printf 'pilot50_server_local=OK\n'
  printf 'safe_result_sha256=%s\n' "$safe_result_sha"
  printf '%s\n' "$validated_safe"
}

recover_pre_request_mode() {
  RECOVER_PRE_REQUEST="1"
  run_mode
}

review_mode() {
  local marker completed raw_report safe_result recovery_receipt
  local manifest_sha cases_sha report_sha safe_result_sha
  local recovery_approval_id recovery_receipt_sha runner_source_paths_sha
  local runner_source_sha started_sha recovered

  [[ -t 1 ]] || fail "review_requires_owner_terminal"
  sudo test -d "$RUN_DIR" || fail "completed_run_not_found"
  sudo test ! -L "$RUN_DIR" || fail "run_directory_not_regular"
  [[ "$(sudo readlink -f -- "$RUN_DIR" 2>/dev/null)" == "$RUN_DIR" ]] \
    || fail "run_directory_not_regular"
  marker="$RUN_DIR/preflight.receipt"
  [[ -f "$marker" && ! -L "$marker" ]] || fail "completed_artifact_missing"
  SOURCE_SHA="$(preflight_source_sha "$marker")" \
    || fail "preflight_source_sha_invalid"
  recovered="0"
  if [[ "$SOURCE_SHA" != "$TOOLING_SHA" ]]; then
    [[ "$SOURCE_SHA" == "$PILOT50_PRE_REQUEST_FAILED_TOOLING_SHA" ]] \
      || fail "review_source_sha_mismatch"
    recovered="1"
  fi
  verify_source_snapshot
  if [[ "$recovered" == "1" ]]; then
    prepare_recovery_runner_snapshot
    runner_source_sha="$TOOLING_SHA"
  else
    RUNNER_SOURCE_DIR="$SOURCE_DIR"
    runner_source_sha="$SOURCE_SHA"
  fi
  runner_source_paths_sha="$(source_paths_sha "$runner_source_sha")" \
    || fail "runner_source_paths_sha_unavailable"
  [[ "$runner_source_paths_sha" =~ ^[0-9a-f]{64}$ ]] \
    || fail "runner_source_paths_sha_unavailable"
  verify_source_snapshot "$RUNNER_SOURCE_DIR" "$runner_source_sha"

  sudo test -d "$EVIDENCE_DIR" || fail "evidence_directory_missing"
  sudo test ! -L "$EVIDENCE_DIR" || fail "evidence_directory_not_regular"
  [[ "$(sudo readlink -f -- "$EVIDENCE_DIR" 2>/dev/null)" == "$EVIDENCE_DIR" ]] \
    || fail "evidence_directory_not_regular"
  [[ "$(sudo stat -c '%u:%g:%a' "$EVIDENCE_DIR" 2>/dev/null)" == \
    "10001:10001:700" ]] || fail "evidence_directory_mode_mismatch"

  completed="$RUN_DIR/run.completed"
  recovery_receipt="$RUN_DIR/run.recover-pre-request.receipt"
  raw_report="$EVIDENCE_DIR/pilot50-ask-report.json"
  safe_result="$EVIDENCE_DIR/pilot50-safe-result.json"
  for path in \
    "$marker" \
    "$RUN_DIR/run.started" \
    "$completed" \
    "$EVIDENCE_DIR/pilot50-cases.json" \
    "$raw_report" \
    "$safe_result"; do
    sudo test -f "$path" || fail "completed_artifact_missing"
    sudo test ! -L "$path" || fail "completed_artifact_not_regular"
  done
  if [[ "$recovered" == "1" ]]; then
    sudo test -f "$recovery_receipt" || fail "recovery_receipt_missing"
    sudo test ! -L "$recovery_receipt" || fail "recovery_receipt_not_regular"
  else
    if sudo test -e "$recovery_receipt" || sudo test -L "$recovery_receipt"; then
      fail "unexpected_recovery_receipt"
    fi
  fi

  manifest_sha="$(sudo sha256sum "$SOURCE_DIR/$PILOT50_MANIFEST_REL" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "manifest_sha_unavailable"
  cases_sha="$(sudo sha256sum "$EVIDENCE_DIR/pilot50-cases.json" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "cases_sha_unavailable"
  report_sha="$(sudo sha256sum "$raw_report" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "report_sha_unavailable"
  safe_result_sha="$(sudo sha256sum "$safe_result" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "safe_result_sha_unavailable"
  [[ "$manifest_sha" =~ ^[0-9a-f]{64}$ ]] || fail "manifest_sha_unavailable"
  [[ "$cases_sha" =~ ^[0-9a-f]{64}$ ]] || fail "cases_sha_unavailable"
  [[ "$report_sha" =~ ^[0-9a-f]{64}$ ]] || fail "report_sha_unavailable"
  [[ "$safe_result_sha" =~ ^[0-9a-f]{64}$ ]] \
    || fail "safe_result_sha_unavailable"
  validate_preflight_receipt \
    "$marker" "$SOURCE_SHA" "$RUNTIME_SHA" "$manifest_sha" "$cases_sha" \
    || fail "preflight_receipt_mismatch"
  recovery_receipt_sha="none"
  if [[ "$recovered" == "1" ]]; then
    validate_started_marker "$RUN_DIR/run.started" "$(id -u)" "$(id -g)" \
      || fail "recovery_started_marker_invalid"
    started_sha="$(sha256sum "$RUN_DIR/run.started" 2>/dev/null \
      | cut -d ' ' -f 1)" || fail "recovery_started_sha_unavailable"
    [[ "$started_sha" == "$PILOT50_STARTED_MARKER_SHA256" ]] \
      || fail "recovery_started_marker_invalid"
    recovery_approval_id="$(safe_result_approval_id "$safe_result")" \
      || fail "safe_result_approval_id_invalid"
    validate_recovery_receipt \
      "$recovery_receipt" "$TOOLING_SHA" "$SOURCE_SHA" "$RUNTIME_SHA" \
      "$cases_sha" "$started_sha" "$recovery_approval_id" \
      "$runner_source_paths_sha" \
      || fail "recovery_receipt_invalid"
    recovery_receipt_sha="$(sha256sum "$recovery_receipt" 2>/dev/null \
      | cut -d ' ' -f 1)" || fail "recovery_receipt_sha_unavailable"
    [[ "$recovery_receipt_sha" =~ ^[0-9a-f]{64}$ ]] \
      || fail "recovery_receipt_sha_unavailable"
  fi
  validate_completed_receipt \
    "$completed" "$TOOLING_SHA" "$SOURCE_SHA" "$RUNTIME_SHA" \
    "$cases_sha" "$report_sha" "$safe_result_sha" \
    "$recovery_receipt_sha" "$runner_source_paths_sha" \
    || fail "completion_marker_mismatch"

  build_compose_command
  "${compose[@]}" config --quiet >/dev/null 2>&1 \
    || fail "compose_config_failed"
  if ! {
    "${compose[@]}" run --rm --no-deps --pull never \
      --entrypoint python quality-acceptance \
      -m scripts.pilot50 show-review \
      --manifest "/workspace/$PILOT50_MANIFEST_REL" \
      --cases /evidence/pilot50-cases.json \
      --report /evidence/pilot50-ask-report.json \
      --safe-result /evidence/pilot50-safe-result.json \
      --expected-runtime-git-sha "$RUNTIME_SHA" \
      2>/dev/null \
      && printf 'pilot50-review-stream-complete-v1\n'
  } | validate_review_stdout; then
    fail "review_output_invalid"
  fi
}

validate_mode "$@"
if [[ "$MODE" == "review" ]]; then
  [[ -t 1 ]] || fail "review_requires_owner_terminal"
fi
load_common_state

case "$MODE" in
  preflight) preflight_mode ;;
  run) run_mode ;;
  recover-pre-request) recover_pre_request_mode ;;
  review) review_mode ;;
esac
