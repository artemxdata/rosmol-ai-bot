#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
exec 2>/dev/null

readonly DATASET_ID="semantic_recovery10_v1"
readonly TARGET="http://pilot50-candidate-ml:8000/ask"
readonly COST_CAP_RUB="99"
readonly CASES_TOTAL="10"
readonly SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"
readonly SERVER_ENV_FILE="/opt/rosmol-ai-bot/.env.production"
readonly PROD_CONTAINER="rosmol-app-ml"
readonly CANDIDATE_CONTAINER="rosmol-pilot50-candidate-ml"
readonly CANDIDATE_IMAGE_PREFIX="rosmol-ai-bot-pilot50-candidate"
readonly CANDIDATE_PROMPT_VERSION="semrec10-v1"
readonly BASE_DIR="/var/lib/rosmol/semantic-recovery10"
readonly COST_LEDGER_DIR="/var/lib/rosmol/eval-cost-ledger-v1"
readonly PRIOR_RUN_DIR="/var/lib/rosmol/pilot50-candidate/runs/pilot50_balanced_v4-d5cf413492a079c396c56017f51acaa3ebbacb3c"
readonly PRIOR_CASES_SHA256="c88a52225f6eec3b21a5837a94f12670f5a8ff1006818f559cb81e438d52fab8"
readonly PRIOR_REPORT_SHA256="2defcace63de2a2184b162fcae5fa8f4d50ed8317042ae64aabbb49181076a8d"
readonly COMPOSE_REL="docker-compose.pilot50-candidate.yml"
readonly SIMPLE_INPUT_PRICE_RUB_PER_MILLION="12.2"
readonly SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION="12.2"
readonly COMPLEX_INPUT_PRICE_RUB_PER_MILLION="569.34"
readonly COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION="569.34"

MODE="${1:-}"
EXPECTED_SHA="${2:-}"
TOOLING_ROOT=""
RUN_DIR=""
SOURCE_DIR=""
EVIDENCE_DIR=""
DATA_NETWORK=""
RUNTIME_EGRESS_NETWORK=""
HF_CACHE_VOLUME=""
TORCH_CACHE_VOLUME=""
MODEL_CACHE_VOLUME=""
KB_SEED_PATH=""
ADMIN_KB_DIR=""
PROD_RUNTIME_SHA=""
EPHEMERAL_ENV_FILE=""
RUNNER_ENV_FILE=""
CANDIDATE_ID=""
STAGING_DIR=""
compose=()
runner=()

fail() {
  printf 'semantic_recovery10_server_local=FAIL reason=%s\n' "$1"
  exit 1
}

cleanup_temp() {
  local failed=0
  if [[ -n "$EPHEMERAL_ENV_FILE" ]]; then
    if [[ "$EPHEMERAL_ENV_FILE" == /run/semantic-recovery10-env.* ]] && \
      sudo rm -f -- "$EPHEMERAL_ENV_FILE" >/dev/null 2>&1; then
      EPHEMERAL_ENV_FILE=""
    else
      failed=1
    fi
  fi
  if [[ -n "$RUNNER_ENV_FILE" ]]; then
    if [[ "$RUNNER_ENV_FILE" == /run/semantic-recovery10-runner-env.* ]] && \
      sudo rm -f -- "$RUNNER_ENV_FILE" >/dev/null 2>&1; then
      RUNNER_ENV_FILE=""
    else
      failed=1
    fi
  fi
  if [[ -n "$STAGING_DIR" ]]; then
    if [[ "$STAGING_DIR" == "$BASE_DIR/runs/.staging-$EXPECTED_SHA-"* ]] && \
      sudo test -d "$STAGING_DIR" && \
      sudo rm -rf --one-file-system -- "$STAGING_DIR" >/dev/null 2>&1; then
      STAGING_DIR=""
    else
      failed=1
    fi
  fi
  return "$failed"
}

candidate_owned() {
  local dataset image_id name purpose sha
  CANDIDATE_ID="$(sudo docker inspect -f '{{.Id}}' "$CANDIDATE_CONTAINER" 2>/dev/null)" \
    || return 1
  name="$(sudo docker inspect -f '{{.Name}}' "$CANDIDATE_ID" 2>/dev/null)" \
    || return 1
  purpose="$(sudo docker inspect \
    -f '{{index .Config.Labels "com.rosmol.purpose"}}' "$CANDIDATE_ID" 2>/dev/null)" \
    || return 1
  dataset="$(sudo docker inspect \
    -f '{{index .Config.Labels "com.rosmol.dataset"}}' "$CANDIDATE_ID" 2>/dev/null)" \
    || return 1
  sha="$(sudo docker inspect \
    -f '{{index .Config.Labels "com.rosmol.candidate-git-sha"}}' \
    "$CANDIDATE_ID" 2>/dev/null)" || return 1
  image_id="$(sudo docker inspect -f '{{.Image}}' "$CANDIDATE_ID" 2>/dev/null)" \
    || return 1
  [[ "$name" == "/$CANDIDATE_CONTAINER" ]] || return 1
  [[ "$purpose" == "pilot50-candidate" ]] || return 1
  [[ "$dataset" == "$DATASET_ID" ]] || return 1
  [[ "$sha" == "$EXPECTED_SHA" ]] || return 1
  [[ "$(sudo docker image inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$image_id" 2>/dev/null)" == "$EXPECTED_SHA" ]] || return 1
}

remove_owned_candidate() {
  candidate_owned || return 1
  if [[ "$(sudo docker inspect -f '{{.State.Running}}' "$CANDIDATE_ID" 2>/dev/null)" == \
    "true" ]]; then
    sudo docker stop --time 45 "$CANDIDATE_ID" >/dev/null 2>&1 || return 1
  fi
  sudo docker rm "$CANDIDATE_ID" >/dev/null 2>&1 || return 1
  CANDIDATE_ID=""
}

cleanup_on_exit() {
  local exit_code="$?" failed=0
  trap - EXIT
  if [[ -n "$CANDIDATE_ID" ]] && ! remove_owned_candidate; then
    failed=1
  fi
  cleanup_temp || failed=1
  if [[ "$failed" -ne 0 ]]; then
    printf 'semantic_recovery10_exit_cleanup=FAIL\n'
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
  [[ "$#" -eq 2 ]] || fail "usage"
  case "$MODE" in
    preflight | run | review | cleanup) ;;
    *) fail "usage" ;;
  esac
  [[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "candidate_sha_invalid"
  [[ "$EXPECTED_SHA" != "0000000000000000000000000000000000000000" ]] \
    || fail "candidate_sha_invalid"
}

require_base_commands() {
  require_command git "git_missing"
  require_command sudo "sudo_missing"
  require_command docker "docker_missing"
  require_command python3 "python_missing"
  require_command sha256sum "sha256sum_missing"
  require_command awk "awk_missing"
  require_command cut "cut_missing"
  require_command readlink "readlink_missing"
  require_command openssl "openssl_missing"
  require_command tar "tar_missing"
  require_command find "find_missing"
  sudo -v >/dev/null 2>&1 || fail "sudo_auth_failed"
}

network_for_role() {
  local candidate label network_name="" role="$1"
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    label="$(sudo docker network inspect \
      -f '{{index .Labels "com.docker.compose.network"}}' "$candidate" 2>/dev/null)" \
      || continue
    if [[ "$label" == "$role" ]]; then
      [[ -z "$network_name" ]] || return 1
      network_name="$candidate"
    fi
  done < <(sudo docker inspect \
    -f '{{range $name, $value := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}' \
    "$PROD_CONTAINER" 2>/dev/null)
  [[ -n "$network_name" ]] || return 1
  printf '%s\n' "$network_name"
}

mount_source_for_target() {
  sudo docker inspect -f \
    "{{range .Mounts}}{{if eq .Destination \"$1\"}}{{.Source}}{{end}}{{end}}" \
    "$PROD_CONTAINER" 2>/dev/null
}

mount_volume_for_target() {
  sudo docker inspect -f \
    "{{range .Mounts}}{{if eq .Destination \"$1\"}}{{.Name}}{{end}}{{end}}" \
    "$PROD_CONTAINER" 2>/dev/null
}

verify_network_service() {
  local container_id label network="$1" service="$2"
  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    label="$(sudo docker inspect \
      -f '{{index .Config.Labels "com.docker.compose.service"}}' \
      "$container_id" 2>/dev/null)" || continue
    if [[ "$label" == "$service" ]]; then
      [[ "$(sudo docker inspect -f '{{.State.Running}}' \
        "$container_id" 2>/dev/null)" == "true" ]] || return 1
      return 0
    fi
  done < <(sudo docker network inspect \
    -f '{{range $id, $value := .Containers}}{{$id}}{{"\n"}}{{end}}' \
    "$network" 2>/dev/null)
  return 1
}

load_common_state() {
  local value
  require_base_commands
  TOOLING_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || fail "not_in_git_worktree"
  [[ "$TOOLING_ROOT" == "$SERVER_PROJECT_DIR" ]] || fail "wrong_server_project"
  [[ "$(readlink -f -- "$TOOLING_ROOT" 2>/dev/null)" == "$TOOLING_ROOT" ]] \
    || fail "server_project_not_regular"
  [[ "$(git -C "$TOOLING_ROOT" rev-parse HEAD 2>/dev/null)" == "$EXPECTED_SHA" ]] \
    || fail "candidate_sha_mismatch"
  ! git -C "$TOOLING_ROOT" symbolic-ref -q HEAD >/dev/null 2>&1 \
    || fail "candidate_checkout_not_detached"
  [[ -z "$(git -C "$TOOLING_ROOT" status \
    --porcelain=v1 --untracked-files=all 2>/dev/null)" ]] \
    || fail "candidate_source_not_clean"
  git -C "$TOOLING_ROOT" ls-files --error-unmatch \
    "$COMPOSE_REL" scripts/semantic_recovery10.py eval/run_ask.py \
    >/dev/null 2>&1 || fail "candidate_tooling_not_tracked"
  if git -C "$TOOLING_ROOT" ls-files -s 2>/dev/null \
    | awk '$1 == "120000" { found=1 } END { exit(found ? 0 : 1) }' \
      >/dev/null 2>&1; then
    fail "candidate_source_has_symlink"
  fi
  sudo test -f "$SERVER_ENV_FILE" || fail "server_env_unreadable"
  sudo test ! -L "$SERVER_ENV_FILE" || fail "server_env_not_regular"
  sudo test -r "$SERVER_ENV_FILE" || fail "server_env_unreadable"
  [[ "$(sudo docker inspect -f '{{.State.Running}}' \
    "$PROD_CONTAINER" 2>/dev/null)" == "true" ]] || fail "production_not_running"
  [[ "$(sudo docker inspect \
    -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$PROD_CONTAINER" 2>/dev/null)" == "healthy" ]] || fail "production_not_healthy"
  PROD_RUNTIME_SHA="$(sudo docker inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$PROD_CONTAINER" 2>/dev/null)" || fail "production_sha_unavailable"
  [[ "$PROD_RUNTIME_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "production_sha_invalid"
  [[ -z "$(sudo docker port "$PROD_CONTAINER" 2>/dev/null)" ]] \
    || fail "production_has_published_ports"

  DATA_NETWORK="$(network_for_role data)" || fail "data_network_unavailable"
  RUNTIME_EGRESS_NETWORK="$(network_for_role runtime_egress)" \
    || fail "runtime_egress_network_unavailable"
  for value in postgres redis qdrant; do
    verify_network_service "$DATA_NETWORK" "$value" || fail "data_service_unavailable"
  done
  verify_network_service "$RUNTIME_EGRESS_NETWORK" runtime-egress-proxy \
    || fail "runtime_egress_proxy_unavailable"

  KB_SEED_PATH="$(mount_source_for_target /app/data/knowledge_base_seed.json)" \
    || fail "kb_seed_mount_unavailable"
  ADMIN_KB_DIR="$(mount_source_for_target /app/data/private/admin-kb)" \
    || fail "admin_kb_mount_unavailable"
  HF_CACHE_VOLUME="$(mount_volume_for_target /home/app/.cache/huggingface)" \
    || fail "hf_cache_volume_unavailable"
  TORCH_CACHE_VOLUME="$(mount_volume_for_target /home/app/.cache/torch)" \
    || fail "torch_cache_volume_unavailable"
  MODEL_CACHE_VOLUME="$(mount_volume_for_target /opt/models)" \
    || fail "model_cache_volume_unavailable"
  sudo test -f "$KB_SEED_PATH" || fail "kb_seed_unavailable"
  sudo test ! -L "$KB_SEED_PATH" || fail "kb_seed_not_regular"
  sudo test -d "$ADMIN_KB_DIR" || fail "admin_kb_unavailable"
  sudo test ! -L "$ADMIN_KB_DIR" || fail "admin_kb_not_regular"
  for value in \
    "$DATA_NETWORK" "$RUNTIME_EGRESS_NETWORK" "$HF_CACHE_VOLUME" \
    "$TORCH_CACHE_VOLUME" "$MODEL_CACHE_VOLUME"; do
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] \
      || fail "docker_resource_name_invalid"
  done

  RUN_DIR="$BASE_DIR/runs/${DATASET_ID}-${EXPECTED_SHA}"
  SOURCE_DIR="$RUN_DIR/source"
  EVIDENCE_DIR="$RUN_DIR/evidence"
}

production_snapshot() {
  sudo docker inspect -f \
    '{{.Id}}|{{.Image}}|{{.RestartCount}}|{{.State.OOMKilled}}|{{.State.Running}}|{{.State.StartedAt}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$PROD_CONTAINER" 2>/dev/null
}

capacity_snapshot() {
  local docker_root
  docker_root="$(sudo docker info -f '{{.DockerRootDir}}' 2>/dev/null)" \
    || fail "docker_root_unavailable"
  [[ "$docker_root" == /* ]] || fail "docker_root_invalid"
  sudo python3 - "$docker_root" 2>/dev/null <<'PY'
import os
import shutil
import sys

meminfo = {}
with open("/proc/meminfo", encoding="ascii") as handle:
    for line in handle:
        key, raw = line.split(":", 1)
        meminfo[key] = int(raw.strip().split()[0])
mem = meminfo.get("MemAvailable", 0)
swap = meminfo.get("SwapFree", 0)
with open("/proc/loadavg", encoding="ascii") as handle:
    load = float(handle.read().split()[0])
cpus = os.cpu_count() or 1
disk = shutil.disk_usage(sys.argv[1]).free
status = "GO" if mem >= 7 * 1024 * 1024 and swap >= 6 * 1024 * 1024 and load <= .75 * cpus and disk >= 5 * 1024**3 else "STOP"
print(f"capacity_status={status}")
print(f"mem_available_mib={mem // 1024}")
print(f"swap_free_mib={swap // 1024}")
print(f"load1={load:.2f}")
print(f"nproc={cpus}")
print(f"docker_free_gib={disk / 1024**3:.2f}")
PY
}

create_ephemeral_env() {
  local api_token build_source user_hash_secret
  api_token="$(openssl rand -hex 32 2>/dev/null)" || fail "ephemeral_secret_failed"
  user_hash_secret="$(openssl rand -hex 32 2>/dev/null)" \
    || fail "ephemeral_secret_failed"
  EPHEMERAL_ENV_FILE="$(sudo mktemp /run/semantic-recovery10-env.XXXXXX 2>/dev/null)" \
    || fail "ephemeral_env_create_failed"
  sudo chmod 0600 "$EPHEMERAL_ENV_FILE" || fail "ephemeral_env_mode_failed"
  build_source="$SOURCE_DIR"
  {
    printf 'PILOT50_CANDIDATE_GIT_SHA=%s\n' "$EXPECTED_SHA"
    printf 'PILOT50_CANDIDATE_SOURCE_DIR=%s\n' "$build_source"
    printf 'PILOT50_CANDIDATE_DATASET_ID=%s\n' "$DATASET_ID"
    printf 'PILOT50_CANDIDATE_PROMPT_VERSION=%s\n' "$CANDIDATE_PROMPT_VERSION"
    printf 'PILOT50_CANDIDATE_API_AUTH_TOKEN=%s\n' "$api_token"
    printf 'PILOT50_CANDIDATE_USER_HASH_SECRET=%s\n' "$user_hash_secret"
    printf 'API_AUTH_TOKEN=%s\nUSER_HASH_SECRET=%s\n' "$api_token" "$user_hash_secret"
    printf 'WEBHOOK_AUTH_TOKEN=\nADMIN_AUTH_TOKEN=\n'
    printf 'HDE_TRIGGER_PREFIX=\nHDE_BASE_URL=\nHDE_API_EMAIL=\nHDE_API_KEY=\n'
    printf 'HDE_BOT_USER_ID=\nHDE_TRANSPORT_EVENT_KEY_SECRET=\n'
    printf 'HDE_TRANSPORT_ENCRYPTION_KEY=\nYONOTE_API_TOKEN=\n'
    printf 'VK_API_TOKEN=\nVK_GROUP_TOKEN=\nVK_CONFIRMATION_CODE=\n'
    printf 'VK_SECRET=\nVK_CALLBACK_SECRET=\n'
    printf 'PILOT50_SIMPLE_INPUT_PRICE_RUB_PER_MILLION=%s\n' \
      "$SIMPLE_INPUT_PRICE_RUB_PER_MILLION"
    printf 'PILOT50_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION=%s\n' \
      "$SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION"
    printf 'PILOT50_COMPLEX_INPUT_PRICE_RUB_PER_MILLION=%s\n' \
      "$COMPLEX_INPUT_PRICE_RUB_PER_MILLION"
    printf 'PILOT50_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION=%s\n' \
      "$COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION"
    printf 'PILOT50_DATA_NETWORK=%s\n' "$DATA_NETWORK"
    printf 'PILOT50_RUNTIME_EGRESS_NETWORK=%s\n' "$RUNTIME_EGRESS_NETWORK"
    printf 'PILOT50_HF_CACHE_VOLUME=%s\n' "$HF_CACHE_VOLUME"
    printf 'PILOT50_TORCH_CACHE_VOLUME=%s\n' "$TORCH_CACHE_VOLUME"
    printf 'PILOT50_MODEL_CACHE_VOLUME=%s\n' "$MODEL_CACHE_VOLUME"
    printf 'PILOT50_KB_SEED_PATH=%s\n' "$KB_SEED_PATH"
    printf 'PILOT50_ADMIN_KB_DIR=%s\n' "$ADMIN_KB_DIR"
  } | sudo tee "$EPHEMERAL_ENV_FILE" >/dev/null || fail "ephemeral_env_write_failed"
}

build_compose_command() {
  compose=(
    sudo docker compose
    --env-file "$SERVER_ENV_FILE"
    --env-file "$EPHEMERAL_ENV_FILE"
    --project-name rosmol-semantic-recovery10
    --project-directory "$SOURCE_DIR"
    -f "$SOURCE_DIR/$COMPOSE_REL"
  )
}

validate_effective_compose() {
  "${compose[@]}" config --format json 2>/dev/null \
    | python3 /dev/fd/3 "$EXPECTED_SHA" "$DATASET_ID" \
      "$CANDIDATE_PROMPT_VERSION" 3<<'PY' 2>/dev/null
import json
import sys

sha, dataset, prompt_version = sys.argv[1:]
payload = json.load(sys.stdin)
assert set(payload.get("services") or {}) == {"pilot50-candidate-ml"}
service = payload["services"]["pilot50-candidate-ml"]
assert service.get("container_name") == "rosmol-pilot50-candidate-ml"
assert service.get("ports") in (None, [])
assert service.get("read_only") is True
assert service.get("restart") == "no"
assert service.get("user") == "app"
assert set(service.get("cap_drop") or []) == {"ALL"}
assert service.get("security_opt") == ["no-new-privileges=true"]
assert service.get("labels", {}).get("com.rosmol.dataset") == dataset
assert service.get("labels", {}).get("com.rosmol.candidate-git-sha") == sha
env = service.get("environment") or {}
assert env["RELEASE_GIT_SHA"] == sha
assert env["HDE_TRANSPORT_ENABLED"] == "false"
assert env["YONOTE_SYNC_ENABLED"] == "false"
assert env["SEMANTIC_RECOVERY_ENABLED"] == "true"
assert env["SEMANTIC_RECOVERY_MAX_QUESTIONS"] == "6"
assert env["PROMPT_VERSION"] == prompt_version
assert 1 <= len(prompt_version) <= 20
for key in (
    "WEBHOOK_AUTH_TOKEN", "ADMIN_AUTH_TOKEN", "HDE_TRIGGER_PREFIX",
    "HDE_BASE_URL", "HDE_API_EMAIL", "HDE_API_KEY", "HDE_BOT_USER_ID",
    "HDE_TRANSPORT_EVENT_KEY_SECRET", "HDE_TRANSPORT_ENCRYPTION_KEY",
    "VK_API_TOKEN", "VK_GROUP_TOKEN", "VK_CONFIRMATION_CODE", "VK_SECRET",
    "VK_CALLBACK_SECRET", "YONOTE_API_TOKEN",
):
    assert env[key] == ""
assert env["API_AUTH_TOKEN"] and env["USER_HASH_SECRET"]
mounts = service.get("volumes") or []
assert mounts and all(item.get("read_only") is True for item in mounts)
assert set(service.get("networks") or {}) == {"data", "runtime_egress"}
PY
}

create_runner_env() {
  RUNNER_ENV_FILE="$(sudo mktemp /run/semantic-recovery10-runner-env.XXXXXX 2>/dev/null)" \
    || fail "runner_env_create_failed"
  sudo chown "$(id -u):$(id -g)" "$RUNNER_ENV_FILE" \
    || fail "runner_env_create_failed"
  chmod 0600 "$RUNNER_ENV_FILE" || fail "runner_env_mode_failed"
  "${compose[@]}" config --format json 2>/dev/null \
    | python3 /dev/fd/3 "$RUNNER_ENV_FILE" "$EXPECTED_SHA" 3<<'PY' 2>/dev/null
import json
import os
import sys

path, sha = sys.argv[1:]
env = json.load(sys.stdin)["services"]["pilot50-candidate-ml"]["environment"]
selected = {
    "API_AUTH_TOKEN": env["API_AUTH_TOKEN"],
    "ASK_EVAL_POSTGRES_DSN": env["POSTGRES_DSN"],
    "RELEASE_GIT_SHA": sha,
    "EVAL_COST_LEDGER_DIR": "/cost-ledger",
    "CLOUD_RU_MODEL": "",
    "CLOUD_RU_MODEL_SIMPLE": env["CLOUD_RU_MODEL_SIMPLE"],
    "CLOUD_RU_MODEL_COMPLEX": env["CLOUD_RU_MODEL_COMPLEX"],
    "CLOUD_RU_MODEL_ANALYZER": "",
    "CLOUD_RU_MODEL_JUDGE": "",
    "CLOUD_RU_MODEL_SIMPLE_INPUT_PRICE_RUB_PER_MILLION": env["CLOUD_RU_MODEL_SIMPLE_INPUT_PRICE_RUB_PER_MILLION"],
    "CLOUD_RU_MODEL_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION": env["CLOUD_RU_MODEL_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION"],
    "CLOUD_RU_MODEL_COMPLEX_INPUT_PRICE_RUB_PER_MILLION": env["CLOUD_RU_MODEL_COMPLEX_INPUT_PRICE_RUB_PER_MILLION"],
    "CLOUD_RU_MODEL_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION": env["CLOUD_RU_MODEL_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION"],
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
    "ALL_PROXY": "",
    "NO_PROXY": "pilot50-candidate-ml,postgres,127.0.0.1,localhost",
}
assert len(selected["API_AUTH_TOKEN"]) >= 32
fd = os.open(path, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
    for key, value in selected.items():
        text = str(value)
        assert "\n" not in text and "\r" not in text
        handle.write(f"{key}={text}\n")
PY
}

validate_candidate_runtime() {
  candidate_owned || return 1
  sudo docker inspect "$CANDIDATE_ID" 2>/dev/null \
    | python3 /dev/fd/3 "$EXPECTED_SHA" "$DATASET_ID" 3<<'PY' 2>/dev/null
import json
import sys

sha, dataset = sys.argv[1:]
item = json.load(sys.stdin)[0]
assert item["State"]["Running"] is True
assert item["State"]["OOMKilled"] is False
assert item["HostConfig"]["ReadonlyRootfs"] is True
assert item["HostConfig"]["Privileged"] is False
assert item["HostConfig"]["PortBindings"] in (None, {})
assert item["Config"]["User"] == "app"
assert item["Config"]["Labels"]["com.rosmol.dataset"] == dataset
assert item["Config"]["Labels"]["com.rosmol.candidate-git-sha"] == sha
env = dict(value.split("=", 1) for value in item["Config"]["Env"] if "=" in value)
assert env["RELEASE_GIT_SHA"] == sha
assert env["HDE_TRANSPORT_ENABLED"] == "false"
assert env["YONOTE_SYNC_ENABLED"] == "false"
assert env["SEMANTIC_RECOVERY_ENABLED"] == "true"
for key in ("HDE_API_KEY", "VK_API_TOKEN", "VK_GROUP_TOKEN", "YONOTE_API_TOKEN"):
    assert env[key] == ""
PY
}

wait_candidate_ready() {
  local attempt
  for ((attempt = 1; attempt <= 42; attempt += 1)); do
    if sudo docker exec -i "$CANDIDATE_ID" python - "$EXPECTED_SHA" \
      >/dev/null 2>&1 <<'PY'
import json
import sys
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=15) as response:
    payload = json.load(response)
assert payload.get("status") == "ready"
assert payload.get("release_git_sha") == sys.argv[1]
checks = payload.get("checks")
assert isinstance(checks, dict) and checks and all(value == "ok" for value in checks.values())
PY
    then
      return 0
    fi
    [[ "$(sudo docker inspect -f '{{.State.Running}}' "$CANDIDATE_ID" 2>/dev/null)" == \
      "true" ]] || return 1
    [[ "$(sudo docker inspect -f '{{.State.OOMKilled}}' "$CANDIDATE_ID" 2>/dev/null)" == \
      "false" ]] || return 1
    sleep 10
  done
  return 1
}

start_candidate() {
  if sudo docker inspect "$CANDIDATE_CONTAINER" >/dev/null 2>&1; then
    fail "candidate_container_already_exists"
  fi
  create_ephemeral_env
  build_compose_command
  "${compose[@]}" config --quiet >/dev/null 2>&1 || fail "compose_config_failed"
  validate_effective_compose || fail "compose_isolation_invalid"
  "${compose[@]}" build --pull=false pilot50-candidate-ml \
    >/dev/null 2>&1 || fail "candidate_image_build_failed"
  [[ "$(sudo docker image inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$CANDIDATE_IMAGE_PREFIX:$EXPECTED_SHA" 2>/dev/null)" == "$EXPECTED_SHA" ]] \
    || fail "candidate_image_sha_mismatch"
  CANDIDATE_ID="$("${compose[@]}" run -d --no-deps --use-aliases \
    --name "$CANDIDATE_CONTAINER" pilot50-candidate-ml 2>/dev/null)" \
    || fail "candidate_start_failed"
  [[ "$CANDIDATE_ID" =~ ^[0-9a-f]{64}$ ]] || fail "candidate_start_failed"
  validate_candidate_runtime || fail "candidate_isolation_invalid"
  wait_candidate_ready || fail "candidate_not_ready"
  validate_candidate_runtime || fail "candidate_isolation_invalid"
}

receipt_value() {
  sudo awk -F= -v key="$1" '$1 == key {print substr($0, length(key) + 2)}' "$2"
}

runner_command() {
  runner=(
    sudo docker run --rm --pull never
    --network "$DATA_NETWORK"
    --user app
    --read-only
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m
    --cap-drop ALL
    --security-opt no-new-privileges=true
    --pids-limit 256
    --memory 2g
    --cpus 2
    --env-file "$RUNNER_ENV_FILE"
    -v "$SOURCE_DIR:/workspace:ro"
    -v "$EVIDENCE_DIR:/evidence"
    -v "$COST_LEDGER_DIR:/cost-ledger"
    -w /workspace
    --entrypoint python
    "$CANDIDATE_IMAGE_PREFIX:$EXPECTED_SHA"
  )
}

cost_capacity_snapshot() {
  sudo docker run --rm \
    --network none \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --pids-limit 64 \
    --memory 256m \
    --cpus 1 \
    --user 10001:10001 \
    --env PYTHONDONTWRITEBYTECODE=1 \
    --mount "type=bind,src=$SOURCE_DIR,dst=/workspace,readonly" \
    --mount "type=bind,src=$COST_LEDGER_DIR,dst=/cost-ledger,readonly" \
    --workdir /workspace \
    --entrypoint python \
    "$CANDIDATE_IMAGE_PREFIX:$EXPECTED_SHA" \
    -m scripts.semantic_recovery10 cost-preflight \
    --ledger-dir /cost-ledger \
    --requested-cap-rub "$COST_CAP_RUB" 2>/dev/null
}

cost_capacity_value() {
  local key="$1" payload="$2"
  printf '%s\n' "$payload" \
    | awk -F= -v expected="$key" '$1 == expected {print substr($0, index($0, "=") + 1)}'
}

prepare_source_and_cases() {
  local cases_path manifest_path
  sudo install -d -m 0700 -o root -g root "$BASE_DIR" "$BASE_DIR/runs" \
    || fail "base_directory_create_failed"
  sudo test ! -e "$RUN_DIR" || fail "preflight_already_exists"
  STAGING_DIR="$(sudo mktemp -d \
    "$BASE_DIR/runs/.staging-$EXPECTED_SHA-XXXXXX" 2>/dev/null)" \
    || fail "staging_create_failed"
  sudo install -d -m 0755 -o root -g root "$STAGING_DIR/source" \
    || fail "staging_source_create_failed"
  sudo install -d -m 0700 -o 10001 -g 10001 "$STAGING_DIR/evidence" \
    || fail "staging_evidence_create_failed"
  git -C "$TOOLING_ROOT" archive --format=tar "$EXPECTED_SHA" \
    | sudo tar -xf - -C "$STAGING_DIR/source" \
    || fail "source_snapshot_create_failed"
  if sudo find "$STAGING_DIR/source" -type l -print -quit | grep -q .; then
    fail "source_snapshot_has_symlink"
  fi
  cases_path="$STAGING_DIR/evidence/semantic-recovery10-cases.json"
  manifest_path="$STAGING_DIR/evidence/semantic-recovery10-manifest.json"
  sudo env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGING_DIR/source" \
    python3 "$STAGING_DIR/source/scripts/semantic_recovery10.py" prepare \
    --prior-cases "$PRIOR_RUN_DIR/evidence/pilot50-cases.json" \
    --prior-report "$PRIOR_RUN_DIR/evidence/pilot50-ask-report.json" \
    --output-cases "$cases_path" \
    --output-manifest "$manifest_path" \
    --cost-cap-rub "$COST_CAP_RUB" >/dev/null 2>&1 \
    || fail "recovery10_prepare_failed"
  sudo chown 10001:10001 "$cases_path" "$manifest_path" \
    || fail "recovery10_artifact_owner_failed"
  sudo chmod 0600 "$cases_path" "$manifest_path" \
    || fail "recovery10_artifact_mode_failed"
  SOURCE_DIR="$STAGING_DIR/source"
  EVIDENCE_DIR="$STAGING_DIR/evidence"
}

validate_frozen_preflight() {
  local cases_sha manifest_sha receipt="$RUN_DIR/preflight.receipt"
  sudo test -f "$receipt" || fail "preflight_receipt_missing"
  sudo test ! -L "$receipt" || fail "preflight_receipt_invalid"
  [[ "$(receipt_value schema_version "$receipt")" == \
    "semantic-recovery10-preflight-v2" ]] || fail "preflight_receipt_mismatch"
  cases_sha="$(sudo sha256sum "$EVIDENCE_DIR/semantic-recovery10-cases.json" \
    2>/dev/null | cut -d ' ' -f 1)" || fail "cases_sha_unavailable"
  manifest_sha="$(sudo sha256sum "$EVIDENCE_DIR/semantic-recovery10-manifest.json" \
    2>/dev/null | cut -d ' ' -f 1)" || fail "manifest_sha_unavailable"
  [[ "$(receipt_value candidate_sha "$receipt")" == "$EXPECTED_SHA" ]] \
    || fail "preflight_receipt_mismatch"
  [[ "$(receipt_value cases_sha256 "$receipt")" == "$cases_sha" ]] \
    || fail "preflight_receipt_mismatch"
  [[ "$(receipt_value manifest_sha256 "$receipt")" == "$manifest_sha" ]] \
    || fail "preflight_receipt_mismatch"
  [[ "$(receipt_value cost_cap_rub "$receipt")" == "$COST_CAP_RUB" ]] \
    || fail "preflight_receipt_mismatch"
  [[ "$(sudo sha256sum "$KB_SEED_PATH" 2>/dev/null | cut -d ' ' -f 1)" == \
    "$(receipt_value kb_seed_sha256 "$receipt")" ]] || fail "kb_seed_changed"
}

preflight_mode() {
  local capacity cases_sha cost_capacity cost_fingerprint kb_sha manifest_sha
  local prod_snapshot prod_snapshot_sha
  load_common_state
  [[ "$(sudo sha256sum "$PRIOR_RUN_DIR/evidence/pilot50-cases.json" \
    2>/dev/null | cut -d ' ' -f 1)" == "$PRIOR_CASES_SHA256" ]] \
    || fail "prior_cases_mismatch"
  [[ "$(sudo sha256sum "$PRIOR_RUN_DIR/evidence/pilot50-ask-report.json" \
    2>/dev/null | cut -d ' ' -f 1)" == "$PRIOR_REPORT_SHA256" ]] \
    || fail "prior_report_mismatch"
  sudo test -d "$COST_LEDGER_DIR" || fail "cost_ledger_unavailable"
  sudo test ! -L "$COST_LEDGER_DIR" || fail "cost_ledger_invalid"
  [[ "$(sudo stat -c '%u:%g:%a' "$COST_LEDGER_DIR" 2>/dev/null)" == \
    "10001:10001:700" ]] || fail "cost_ledger_mode_mismatch"
  capacity="$(capacity_snapshot)" || fail "capacity_check_failed"
  [[ "$(printf '%s\n' "$capacity" | awk -F= '$1 == "capacity_status" {print $2}')" == \
    "GO" ]] || fail "capacity"
  prod_snapshot="$(production_snapshot)" || fail "production_snapshot_failed"
  prod_snapshot_sha="$(printf '%s' "$prod_snapshot" | sha256sum | cut -d ' ' -f 1)"
  prepare_source_and_cases
  cases_sha="$(sudo sha256sum "$EVIDENCE_DIR/semantic-recovery10-cases.json" \
    2>/dev/null | cut -d ' ' -f 1)"
  manifest_sha="$(sudo sha256sum "$EVIDENCE_DIR/semantic-recovery10-manifest.json" \
    2>/dev/null | cut -d ' ' -f 1)"
  kb_sha="$(sudo sha256sum "$KB_SEED_PATH" 2>/dev/null | cut -d ' ' -f 1)"
  start_candidate
  cost_capacity="$(cost_capacity_snapshot)" \
    || fail "rolling_24h_cap_rejected"
  [[ "$(cost_capacity_value cost_capacity_status "$cost_capacity")" == "GO" ]] \
    || fail "rolling_24h_cap_rejected"
  [[ "$(cost_capacity_value requested_cap_rub "$cost_capacity")" == \
    "$COST_CAP_RUB" ]] || fail "cost_capacity_invalid"
  cost_fingerprint="$(cost_capacity_value \
    cost_ledger_fingerprint_sha256 "$cost_capacity")"
  [[ "$cost_fingerprint" =~ ^[0-9a-f]{64}$ ]] \
    || fail "cost_capacity_invalid"
  [[ "$(production_snapshot)" == "$prod_snapshot" ]] \
    || fail "production_changed_during_preflight"
  remove_owned_candidate || fail "candidate_cleanup_failed"
  {
    printf 'schema_version=semantic-recovery10-preflight-v2\n'
    printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
    printf 'production_runtime_sha=%s\n' "$PROD_RUNTIME_SHA"
    printf 'production_snapshot_sha256=%s\n' "$prod_snapshot_sha"
    printf 'cases_sha256=%s\n' "$cases_sha"
    printf 'manifest_sha256=%s\n' "$manifest_sha"
    printf 'kb_seed_sha256=%s\n' "$kb_sha"
    printf 'cases_total=%s\n' "$CASES_TOTAL"
    printf 'cost_cap_rub=%s\n' "$COST_CAP_RUB"
    printf 'cost_ledger_fingerprint_sha256=%s\n' "$cost_fingerprint"
    printf 'channels_status=HDE_VK_DISABLED\n'
    printf '%s\n' "$capacity"
  } | sudo tee "$STAGING_DIR/preflight.receipt" >/dev/null \
    || fail "preflight_receipt_create_failed"
  sudo chmod 0600 "$STAGING_DIR/preflight.receipt" || fail "preflight_receipt_mode_failed"
  sudo mv -T -- "$STAGING_DIR" "$RUN_DIR" || fail "preflight_publish_failed"
  STAGING_DIR=""
  SOURCE_DIR="$RUN_DIR/source"
  EVIDENCE_DIR="$RUN_DIR/evidence"
  cleanup_temp || fail "preflight_temp_cleanup_failed"
  printf 'semantic_recovery10_preflight=GO\n'
  printf 'candidate_runtime_smoke=OK\n'
  printf 'channels_status=HDE_VK_DISABLED\n'
  printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
  printf 'production_runtime_sha=%s\n' "$PROD_RUNTIME_SHA"
  printf 'cases_total=%s\n' "$CASES_TOTAL"
  printf 'cases_sha256=%s\n' "$cases_sha"
  printf 'manifest_sha256=%s\n' "$manifest_sha"
  printf 'cost_cap_rub=%s\n' "$COST_CAP_RUB"
  printf 'approval_id=owner-chat-20260814-semantic10-%s-%s-cap%s\n' \
    "$EXPECTED_SHA" "${cases_sha:0:12}" "$COST_CAP_RUB"
  printf '%s\n' "$cost_capacity"
  printf '%s\n' "$capacity"
}

run_mode() {
  local approval_id ask_exit="0" cases_sha completed cost_capacity cost_fingerprint
  local expected_approval_id manifest_sha
  local post_prod pre_prod pre_prod_sha raw_report report_sha safe_result safe_sha safe_stdout
  load_common_state
  sudo test -d "$RUN_DIR" || fail "preflight_not_found"
  sudo test ! -L "$RUN_DIR" || fail "run_directory_invalid"
  SOURCE_DIR="$RUN_DIR/source"
  EVIDENCE_DIR="$RUN_DIR/evidence"
  validate_frozen_preflight
  cases_sha="$(receipt_value cases_sha256 "$RUN_DIR/preflight.receipt")"
  manifest_sha="$(receipt_value manifest_sha256 "$RUN_DIR/preflight.receipt")"
  expected_approval_id="owner-chat-20260814-semantic10-${EXPECTED_SHA}-${cases_sha:0:12}-cap${COST_CAP_RUB}"
  approval_id="${HIGH_COST_APPROVAL_ID:-}"
  [[ "$approval_id" == "$expected_approval_id" ]] \
    || fail "approval_id_missing_or_invalid"
  [[ ! -e "$RUN_DIR/run.started" && ! -e "$RUN_DIR/run.completed" ]] \
    || fail "candidate_run_replay_refused"
  raw_report="$EVIDENCE_DIR/semantic-recovery10-ask-report.json"
  safe_result="$EVIDENCE_DIR/semantic-recovery10-safe-result.json"
  [[ ! -e "$raw_report" && ! -e "$safe_result" ]] \
    || fail "candidate_run_artifact_exists"
  pre_prod="$(production_snapshot)" || fail "production_snapshot_failed"
  pre_prod_sha="$(printf '%s' "$pre_prod" | sha256sum | cut -d ' ' -f 1)"
  [[ "$pre_prod_sha" == \
    "$(receipt_value production_snapshot_sha256 "$RUN_DIR/preflight.receipt")" ]] \
    || fail "production_changed_since_preflight"
  cost_capacity="$(cost_capacity_snapshot)" \
    || fail "rolling_24h_cap_rejected"
  [[ "$(cost_capacity_value cost_capacity_status "$cost_capacity")" == "GO" ]] \
    || fail "rolling_24h_cap_rejected"
  cost_fingerprint="$(cost_capacity_value \
    cost_ledger_fingerprint_sha256 "$cost_capacity")"
  [[ "$cost_fingerprint" == "$(receipt_value \
    cost_ledger_fingerprint_sha256 "$RUN_DIR/preflight.receipt")" ]] \
    || fail "cost_ledger_changed_since_preflight"
  start_candidate
  create_runner_env
  runner_command
  (set -o noclobber; {
    printf 'schema_version=semantic-recovery10-run-started-v1\n'
    printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
    printf 'cases_sha256=%s\n' "$cases_sha"
    printf 'manifest_sha256=%s\n' "$manifest_sha"
    printf 'approval_id=%s\n' "$approval_id"
    printf 'cost_cap_rub=%s\n' "$COST_CAP_RUB"
  } | sudo tee "$RUN_DIR/run.started" >/dev/null) 2>/dev/null \
    || fail "candidate_run_replay_refused"
  "${runner[@]}" -m eval.run_ask \
    --cases /evidence/semantic-recovery10-cases.json \
    --output /evidence/semantic-recovery10-ask-report.json \
    --no-markdown \
    --target "$TARGET" \
    --concurrency 1 \
    --timeout 180 \
    --max-llm-cost-rub "$COST_CAP_RUB" \
    --expected-runtime-git-sha "$EXPECTED_SHA" \
    --high-cost-approval-id "$approval_id" \
    --kb-seed /workspace/data/knowledge_base_seed.json \
    --bypass-cache \
    --require-complete-traces \
    >/dev/null 2>&1 || ask_exit="$?"
  if [[ "$ask_exit" -eq 1 ]]; then
    sudo test -f "$raw_report" || fail "candidate_ask_eval_failed"
  elif [[ "$ask_exit" -eq 2 ]]; then
    fail "candidate_ask_eval_cost_stop"
  elif [[ "$ask_exit" -ne 0 ]]; then
    fail "candidate_ask_eval_failed"
  fi
  sudo test -f "$raw_report" || fail "candidate_raw_report_missing"
  if ! "${runner[@]}" -m scripts.semantic_recovery10 summarize \
    --manifest /evidence/semantic-recovery10-manifest.json \
    --cases /evidence/semantic-recovery10-cases.json \
    --report /evidence/semantic-recovery10-ask-report.json \
    --output /evidence/semantic-recovery10-safe-result.json \
    --expected-runtime-git-sha "$EXPECTED_SHA" \
    --expected-approval-id "$approval_id" \
    --expected-cost-cap-rub "$COST_CAP_RUB" \
    >/dev/null 2>&1; then
    fail "candidate_summarize_failed"
  fi
  sudo test -f "$safe_result" || fail "candidate_safe_result_missing"
  report_sha="$(sudo sha256sum "$raw_report" 2>/dev/null | cut -d ' ' -f 1)"
  safe_sha="$(sudo sha256sum "$safe_result" 2>/dev/null | cut -d ' ' -f 1)"
  safe_stdout="$("${runner[@]}" -m scripts.semantic_recovery10 show-safe \
    --input /evidence/semantic-recovery10-safe-result.json 2>/dev/null)" \
    || fail "candidate_safe_output_failed"
  [[ ${#safe_stdout} -le 16384 ]] || fail "candidate_safe_output_oversized"
  wait_candidate_ready || fail "candidate_not_ready_after_run"
  validate_candidate_runtime || fail "candidate_isolation_invalid"
  post_prod="$(production_snapshot)" || fail "production_snapshot_failed"
  [[ "$post_prod" == "$pre_prod" ]] || fail "production_changed_during_run"
  remove_owned_candidate || fail "candidate_cleanup_failed"
  completed="$RUN_DIR/run.completed"
  (set -o noclobber; {
    printf 'schema_version=semantic-recovery10-run-completed-v1\n'
    printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
    printf 'cases_sha256=%s\n' "$cases_sha"
    printf 'manifest_sha256=%s\n' "$manifest_sha"
    printf 'report_sha256=%s\n' "$report_sha"
    printf 'safe_result_sha256=%s\n' "$safe_sha"
    printf 'approval_id=%s\n' "$approval_id"
    printf 'production_snapshot_sha256=%s\n' "$pre_prod_sha"
  } | sudo tee "$completed" >/dev/null) 2>/dev/null \
    || fail "candidate_completion_marker_failed"
  cleanup_temp || fail "run_temp_cleanup_failed"
  printf 'semantic_recovery10_server_local=OK\n'
  printf 'channels_status=HDE_VK_DISABLED\n'
  printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
  printf 'cases_sha256=%s\n' "$cases_sha"
  printf 'report_sha256=%s\n' "$report_sha"
  printf 'safe_result_sha256=%s\n' "$safe_sha"
  printf '%s\n' "$safe_stdout"
}

review_mode() {
  local safe_result
  load_common_state
  sudo test -d "$RUN_DIR" || fail "candidate_run_not_found"
  SOURCE_DIR="$RUN_DIR/source"
  EVIDENCE_DIR="$RUN_DIR/evidence"
  safe_result="$EVIDENCE_DIR/semantic-recovery10-safe-result.json"
  sudo test -f "$RUN_DIR/run.completed" || fail "candidate_run_not_completed"
  sudo test -f "$safe_result" || fail "candidate_safe_result_missing"
  sudo env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SOURCE_DIR" \
    python3 "$SOURCE_DIR/scripts/semantic_recovery10.py" show-safe \
    --input "$safe_result" 2>/dev/null
}

cleanup_mode() {
  require_base_commands
  if sudo docker inspect "$CANDIDATE_CONTAINER" >/dev/null 2>&1; then
    candidate_owned || fail "candidate_not_owned"
    remove_owned_candidate || fail "candidate_cleanup_failed"
  fi
  printf 'semantic_recovery10_cleanup=OK\n'
}

validate_invocation "$@"
case "$MODE" in
  preflight) preflight_mode ;;
  run) run_mode ;;
  review) review_mode ;;
  cleanup) cleanup_mode ;;
esac
