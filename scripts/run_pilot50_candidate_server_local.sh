#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
exec 2>/dev/null

readonly DATASET_ID="pilot50_balanced_v3"
readonly CANDIDATE_CONTRACT_ID="pilot50-v3-candidate-v1"
readonly MANIFEST_REL="eval/cases/pilot50_balanced_v3.json"
readonly TARGET="http://pilot50-candidate-ml:8000/ask"
readonly CASES_TOTAL="50"
readonly TYPICAL_TOTAL="25"
readonly ATYPICAL_TOTAL="25"
readonly COST_CAP_RUB="30"
readonly COMPARISON_WAIVER_DECISION_ID="D-041"
readonly COMPARISON_PROVIDER_RISK_CEILING_RUB="500"
readonly PRIOR_CANDIDATE_SCOPE="pilot50-v2-candidate"
readonly PRIOR_CANDIDATE_SHA="64cc182d37a3c060439ed7a55f5cc875a27d786d"
readonly PRIOR_CASES_SHA256="b027e469e062682b6dc341b2dd4c87440edffb1955c2111f38e6c44a92a3a14d"
readonly SIMPLE_INPUT_PRICE_RUB_PER_MILLION="12.2"
readonly SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION="12.2"
readonly COMPLEX_INPUT_PRICE_RUB_PER_MILLION="569.34"
readonly COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION="569.34"
readonly SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"
readonly SERVER_ENV_FILE="/opt/rosmol-ai-bot/.env.production"
readonly PROD_CONTAINER="rosmol-app-ml"
readonly CANDIDATE_CONTAINER="rosmol-pilot50-candidate-ml"
readonly BASE_DIR="/var/lib/rosmol/pilot50-candidate"
readonly COST_LEDGER_DIR="/var/lib/rosmol/eval-cost-ledger-v1"
readonly COMPOSE_REL="docker-compose.pilot50-candidate.yml"
readonly MIN_MEM_AVAILABLE_KIB="$((7 * 1024 * 1024))"
readonly MIN_SWAP_FREE_KIB="$((6 * 1024 * 1024))"
readonly MIN_DOCKER_HEADROOM_BYTES="$((5 * 1024 * 1024 * 1024))"

MODE="${1:-}"
EXPECTED_SHA="${2:-}"
TOOLING_ROOT=""
RUN_DIR=""
SOURCE_DIR=""
EVIDENCE_DIR=""
PROD_RUNTIME_SHA=""
PROD_IMAGE=""
PROD_IMAGE_ID=""
DATA_NETWORK=""
RUNTIME_EGRESS_NETWORK=""
HF_CACHE_VOLUME=""
TORCH_CACHE_VOLUME=""
MODEL_CACHE_VOLUME=""
KB_SEED_PATH=""
ADMIN_KB_DIR=""
STAGING_DIR=""
EPHEMERAL_ENV_FILE=""
RUNNER_ENV_FILE=""
CANDIDATE_ID=""
compose=()

fail() {
  printf 'pilot50_candidate_server_local=FAIL reason=%s\n' "$1"
  exit 1
}

stop_capacity() {
  local snapshot="$1"
  printf 'pilot50_candidate_preflight=STOP reason=capacity\n'
  printf '%s\n' "$snapshot"
  exit 10
}

cleanup_temp() {
  local cleanup_failed=0
  if [[ -n "$EPHEMERAL_ENV_FILE" ]]; then
    if [[ "$EPHEMERAL_ENV_FILE" == /run/pilot50-candidate-env.* ]] && \
      sudo rm -f -- "$EPHEMERAL_ENV_FILE" >/dev/null 2>&1; then
      EPHEMERAL_ENV_FILE=""
    else
      cleanup_failed=1
    fi
  fi
  if [[ -n "$RUNNER_ENV_FILE" ]]; then
    if [[ "$RUNNER_ENV_FILE" == /run/pilot50-candidate-runner-env.* ]] && \
      sudo rm -f -- "$RUNNER_ENV_FILE" >/dev/null 2>&1; then
      RUNNER_ENV_FILE=""
    else
      cleanup_failed=1
    fi
  fi
  if [[ -n "$STAGING_DIR" ]]; then
    if [[ "$STAGING_DIR" == "$BASE_DIR/runs/.staging-$EXPECTED_SHA-"* ]] && \
      sudo rm -rf --one-file-system -- "$STAGING_DIR" >/dev/null 2>&1; then
      STAGING_DIR=""
    else
      cleanup_failed=1
    fi
  fi
  return "$cleanup_failed"
}

candidate_owned() {
  local candidate_id image_id label_dataset label_purpose label_sha name
  candidate_id="$(sudo docker inspect -f '{{.Id}}' "$CANDIDATE_CONTAINER" 2>/dev/null)" \
    || return 1
  name="$(sudo docker inspect -f '{{.Name}}' "$candidate_id" 2>/dev/null)" \
    || return 1
  label_purpose="$(sudo docker inspect \
    -f '{{index .Config.Labels "com.rosmol.purpose"}}' "$candidate_id" 2>/dev/null)" \
    || return 1
  label_dataset="$(sudo docker inspect \
    -f '{{index .Config.Labels "com.rosmol.dataset"}}' "$candidate_id" 2>/dev/null)" \
    || return 1
  label_sha="$(sudo docker inspect \
    -f '{{index .Config.Labels "com.rosmol.candidate-git-sha"}}' "$candidate_id" 2>/dev/null)" \
    || return 1
  image_id="$(sudo docker inspect -f '{{.Image}}' "$candidate_id" 2>/dev/null)" \
    || return 1
  [[ "$name" == "/$CANDIDATE_CONTAINER" ]] || return 1
  [[ "$label_purpose" == "pilot50-candidate" ]] || return 1
  [[ "$label_dataset" == "$DATASET_ID" ]] || return 1
  [[ "$label_sha" == "$EXPECTED_SHA" ]] || return 1
  [[ "$(sudo docker image inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$image_id" 2>/dev/null)" == "$EXPECTED_SHA" ]] || return 1
  [[ -z "$CANDIDATE_ID" || "$CANDIDATE_ID" == "$candidate_id" ]] || return 1
  CANDIDATE_ID="$candidate_id"
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
  local candidate_cleanup_failed=0 exit_code="$?" temp_cleanup_failed=0
  trap - EXIT
  if [[ -n "$CANDIDATE_ID" ]] && ! remove_owned_candidate; then
    candidate_cleanup_failed=1
  fi
  cleanup_temp || temp_cleanup_failed=1
  if [[ "$candidate_cleanup_failed" -ne 0 ]]; then
    printf 'pilot50_candidate_exit_cleanup=FAIL reason=candidate_cleanup_failed\n'
  fi
  if [[ "$temp_cleanup_failed" -ne 0 ]]; then
    printf 'pilot50_candidate_exit_cleanup=FAIL reason=temp_cleanup_failed\n'
  fi
  if [[ "$candidate_cleanup_failed" -ne 0 || "$temp_cleanup_failed" -ne 0 ]]; then
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
  if [[ "$MODE" == "review" ]]; then
    [[ -t 1 ]] || fail "review_requires_owner_terminal"
  fi
}

require_base_commands() {
  require_command git "git_missing"
  require_command sudo "sudo_missing"
  require_command docker "docker_missing"
  require_command python3 "python_missing"
  require_command sha256sum "sha256sum_missing"
  require_command awk "awk_missing"
  require_command cut "cut_missing"
  require_command grep "grep_missing"
  require_command tar "tar_missing"
  require_command find "find_missing"
  require_command sort "sort_missing"
  require_command readlink "readlink_missing"
  require_command openssl "openssl_missing"
  sudo -v >/dev/null 2>&1 || fail "sudo_auth_failed"
}

network_for_role() {
  local candidate label network_name role="$1"
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    label="$(sudo docker network inspect \
      -f '{{index .Labels "com.docker.compose.network"}}' "$candidate" 2>/dev/null)" \
      || continue
    if [[ "$label" == "$role" ]]; then
      [[ -z "${network_name:-}" ]] || return 1
      network_name="$candidate"
    fi
  done < <(sudo docker inspect \
    -f '{{range $name, $value := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}' \
    "$PROD_CONTAINER" 2>/dev/null)
  [[ -n "${network_name:-}" ]] || return 1
  printf '%s\n' "$network_name"
}

mount_source_for_target() {
  local target="$1"
  sudo docker inspect -f \
    "{{range .Mounts}}{{if eq .Destination \"$target\"}}{{.Source}}{{end}}{{end}}" \
    "$PROD_CONTAINER" 2>/dev/null
}

mount_volume_for_target() {
  local target="$1"
  sudo docker inspect -f \
    "{{range .Mounts}}{{if eq .Destination \"$target\"}}{{.Name}}{{end}}{{end}}" \
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
  require_base_commands
  TOOLING_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || fail "not_in_git_worktree"
  [[ "$TOOLING_ROOT" == "$SERVER_PROJECT_DIR" ]] || fail "wrong_server_project"
  [[ "$(readlink -f -- "$TOOLING_ROOT" 2>/dev/null)" == "$TOOLING_ROOT" ]] \
    || fail "server_project_not_regular"
  [[ "$(git -C "$TOOLING_ROOT" rev-parse HEAD 2>/dev/null)" == "$EXPECTED_SHA" ]] \
    || fail "candidate_sha_mismatch"
  if git -C "$TOOLING_ROOT" symbolic-ref -q HEAD >/dev/null 2>&1; then
    fail "candidate_checkout_not_detached"
  fi
  [[ -z "$(git -C "$TOOLING_ROOT" status \
    --porcelain=v1 --untracked-files=all 2>/dev/null)" ]] \
    || fail "candidate_source_not_clean"
  [[ -f "$TOOLING_ROOT/$MANIFEST_REL" && ! -L "$TOOLING_ROOT/$MANIFEST_REL" ]] \
    || fail "manifest_missing_or_not_regular"
  git -C "$TOOLING_ROOT" ls-files --error-unmatch "$MANIFEST_REL" \
    >/dev/null 2>&1 || fail "manifest_not_tracked"
  git -C "$TOOLING_ROOT" ls-files --error-unmatch "$COMPOSE_REL" \
    >/dev/null 2>&1 || fail "candidate_compose_not_tracked"
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
  [[ "$(sudo docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$PROD_CONTAINER" 2>/dev/null)" == "healthy" ]] || fail "production_not_healthy"
  PROD_RUNTIME_SHA="$(sudo docker inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$PROD_CONTAINER" 2>/dev/null)" || fail "production_sha_unavailable"
  [[ "$PROD_RUNTIME_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "production_sha_invalid"
  PROD_IMAGE="rosmol-ai-bot-ml:$PROD_RUNTIME_SHA"
  PROD_IMAGE_ID="$(sudo docker image inspect -f '{{.Id}}' "$PROD_IMAGE" 2>/dev/null)" \
    || fail "production_image_missing"
  [[ "$(sudo docker inspect -f '{{.Image}}' "$PROD_CONTAINER" 2>/dev/null)" == \
    "$PROD_IMAGE_ID" ]] || fail "production_image_identity_mismatch"
  [[ "$(sudo docker image inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$PROD_IMAGE" 2>/dev/null)" == "$PROD_RUNTIME_SHA" ]] \
    || fail "production_image_sha_mismatch"
  [[ -z "$(sudo docker port "$PROD_CONTAINER" 2>/dev/null)" ]] \
    || fail "production_has_published_ports"

  DATA_NETWORK="$(network_for_role data)" || fail "data_network_unavailable"
  RUNTIME_EGRESS_NETWORK="$(network_for_role runtime_egress)" \
    || fail "runtime_egress_network_unavailable"
  for service in postgres redis qdrant; do
    verify_network_service "$DATA_NETWORK" "$service" \
      || fail "data_service_unavailable"
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

qdrant_snapshot() {
  sudo docker exec -i "$PROD_CONTAINER" python - 2>/dev/null <<'PY'
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from scripts.index_kb import KBSeedRecord, build_embedding_text
from src.rag.filter_keys import build_filter_key_payload

base = os.environ["QDRANT_URL"].rstrip("/")
collection = os.environ.get("QDRANT_KNOWLEDGE_COLLECTION", "knowledge_base")
headers = {"Content-Type": "application/json"}
api_key = os.environ.get("QDRANT_API_KEY", "")
if api_key:
    headers["api-key"] = api_key

def post(path, payload):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    assert value.get("status") == "ok"
    return value["result"]

seed_bytes = Path("/app/data/knowledge_base_seed.json").read_bytes()
seed_sha = hashlib.sha256(seed_bytes).hexdigest()
seed = json.loads(seed_bytes)
expected = []
for raw_record in seed:
    record = KBSeedRecord.model_validate(raw_record)
    if record.status != "published":
        continue
    dumped = record.model_dump()
    payload = {
        **dumped,
        **build_filter_key_payload(dumped),
        "text": record.content,
        "embedding_text": build_embedding_text(record),
        "status": record.status,
    }
    expected.append(
        {
            "id": str(uuid5(NAMESPACE_URL, record.chunk_id)),
            "payload": payload,
        }
    )

count = int(post(f"/collections/{collection}/points/count", {"exact": True})["count"])
rows = []
offset = None
while True:
    payload = {
        "limit": 256,
        "with_payload": True,
        "with_vector": True,
    }
    if offset is not None:
        payload["offset"] = offset
    result = post(f"/collections/{collection}/points/scroll", payload)
    for point in result.get("points") or []:
        point_payload = point.get("payload") or {}
        chunk_id = str(point_payload.get("chunk_id") or "")
        assert chunk_id
        vector = point.get("vector")
        assert isinstance(vector, dict) and vector
        rows.append(
            {"id": str(point.get("id")), "payload": point_payload, "vector": vector}
        )
    assert len(rows) <= 100000
    offset = result.get("next_page_offset")
    if offset is None:
        break
assert count == len(rows) and count > 0
canonical = lambda value: json.dumps(
    value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
)
actual_rows = sorted(
    ({"id": row["id"], "payload": row["payload"]} for row in rows),
    key=lambda row: row["payload"]["chunk_id"],
)
expected_rows = sorted(expected, key=lambda row: row["payload"]["chunk_id"])
actual_digest = hashlib.sha256(canonical(actual_rows).encode()).hexdigest()
expected_digest = hashlib.sha256(canonical(expected_rows).encode()).hexdigest()
assert count == len(expected_rows) and actual_digest == expected_digest
vector_rows = sorted(
    (
        {
            "id": row["id"],
            "chunk_id": row["payload"]["chunk_id"],
            "vector": row["vector"],
        }
        for row in rows
    ),
    key=lambda row: row["chunk_id"],
)
vector_digest = hashlib.sha256(canonical(vector_rows).encode()).hexdigest()
fingerprint = hashlib.sha256(
    canonical({"payload": actual_digest, "vectors": vector_digest}).encode()
).hexdigest()
print(f"{count}|{fingerprint}|{seed_sha}")
PY
}

capacity_snapshot() {
  local docker_root image_size
  docker_root="$(sudo docker info -f '{{.DockerRootDir}}' 2>/dev/null)" \
    || fail "docker_root_unavailable"
  [[ "$docker_root" == /* ]] || fail "docker_root_invalid"
  image_size="$(sudo docker image inspect -f '{{.Size}}' "$PROD_IMAGE" 2>/dev/null)" \
    || fail "production_image_size_unavailable"
  sudo python3 - "$docker_root" "$image_size" \
    "$MIN_MEM_AVAILABLE_KIB" "$MIN_SWAP_FREE_KIB" \
    "$MIN_DOCKER_HEADROOM_BYTES" 2>/dev/null <<'PY'
import os
import shutil
import sys

docker_root, image_size, min_mem, min_swap, headroom = sys.argv[1:]
image_size = int(image_size)
min_mem = int(min_mem)
min_swap = int(min_swap)
headroom = int(headroom)
meminfo = {}
with open("/proc/meminfo", encoding="ascii") as handle:
    for line in handle:
        key, raw = line.split(":", 1)
        meminfo[key] = int(raw.strip().split()[0])
mem_available = meminfo.get("MemAvailable", 0)
swap_free = meminfo.get("SwapFree", 0)
with open("/proc/loadavg", encoding="ascii") as handle:
    load1 = float(handle.read().split()[0])
nproc = os.cpu_count() or 1
disk_free = shutil.disk_usage(docker_root).free
disk_required = image_size + headroom
status = (
    "GO"
    if mem_available >= min_mem
    and swap_free >= min_swap
    and load1 <= 0.75 * nproc
    and disk_free >= disk_required
    else "STOP"
)
print(
    "capacity_status=" + status,
    f"mem_available_mib={mem_available // 1024}",
    f"swap_free_mib={swap_free // 1024}",
    f"load1={load1:.2f}",
    f"nproc={nproc}",
    f"docker_free_gib={disk_free / 1024**3:.2f}",
    f"docker_required_gib={disk_required / 1024**3:.2f}",
    sep="\n",
)
PY
}

verify_runner_contract_support() {
  # Integration contract: these flags must be implemented by the candidate
  # commit before a paid request. Older Pilot50 tooling is intentionally
  # rejected; the shell never substitutes historical c38 repricing.
  grep -Fq -- "--pilot50-candidate-contract" "$TOOLING_ROOT/eval/run_ask.py" \
    || fail "candidate_runner_contract_unavailable"
  grep -Fq -- "--expected-runtime-git-sha" "$TOOLING_ROOT/eval/run_ask.py" \
    || fail "candidate_runner_contract_unavailable"
  grep -Fq -- "--candidate-contract" "$TOOLING_ROOT/scripts/pilot50.py" \
    || fail "candidate_summary_contract_unavailable"
  grep -Fq -- "--rolling-24h-comparison-waiver-id" \
    "$TOOLING_ROOT/eval/run_ask.py" \
    || fail "candidate_runner_contract_unavailable"
  grep -Fq -- "--rolling-24h-comparison-waiver-id" \
    "$TOOLING_ROOT/scripts/pilot50.py" \
    || fail "candidate_summary_contract_unavailable"
  grep -Fq -- "$CANDIDATE_CONTRACT_ID" "$TOOLING_ROOT/eval/run_ask.py" \
    || fail "candidate_runner_contract_unavailable"
  grep -Fq -- "$CANDIDATE_CONTRACT_ID" "$TOOLING_ROOT/scripts/pilot50.py" \
    || fail "candidate_summary_contract_unavailable"
  grep -Fq -- "target_reported" "$TOOLING_ROOT/scripts/pilot50.py" \
    || fail "candidate_summary_contract_unavailable"
}

create_ephemeral_env() {
  local api_token build_source user_hash_secret
  api_token="$(openssl rand -hex 32 2>/dev/null)" || fail "ephemeral_secret_failed"
  user_hash_secret="$(openssl rand -hex 32 2>/dev/null)" \
    || fail "ephemeral_secret_failed"
  [[ "$api_token" =~ ^[0-9a-f]{64}$ && "$user_hash_secret" =~ ^[0-9a-f]{64}$ ]] \
    || fail "ephemeral_secret_failed"
  EPHEMERAL_ENV_FILE="$(sudo mktemp /run/pilot50-candidate-env.XXXXXX 2>/dev/null)" \
    || fail "ephemeral_env_create_failed"
  sudo chmod 0600 "$EPHEMERAL_ENV_FILE" || fail "ephemeral_env_mode_failed"
  build_source="$TOOLING_ROOT"
  if [[ -n "$SOURCE_DIR" ]] && sudo test -d "$SOURCE_DIR"; then
    build_source="$SOURCE_DIR"
  fi
  {
    printf 'PILOT50_CANDIDATE_GIT_SHA=%s\n' "$EXPECTED_SHA"
    printf 'PILOT50_CANDIDATE_SOURCE_DIR=%s\n' "$build_source"
    printf 'PILOT50_CANDIDATE_API_AUTH_TOKEN=%s\n' "$api_token"
    printf 'PILOT50_CANDIDATE_USER_HASH_SECRET=%s\n' "$user_hash_secret"
    printf 'API_AUTH_TOKEN=%s\n' "$api_token"
    printf 'USER_HASH_SECRET=%s\n' "$user_hash_secret"
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
  } | sudo tee "$EPHEMERAL_ENV_FILE" >/dev/null \
    || fail "ephemeral_env_write_failed"
}

build_compose_command() {
  local compose_root="$TOOLING_ROOT"
  if [[ -n "$SOURCE_DIR" && -f "$SOURCE_DIR/$COMPOSE_REL" ]]; then
    compose_root="$SOURCE_DIR"
  fi
  compose=(
    sudo docker compose
    --env-file "$SERVER_ENV_FILE"
    --env-file "$EPHEMERAL_ENV_FILE"
    --project-name rosmol-pilot50-candidate
    --project-directory "$compose_root"
    -f "$compose_root/$COMPOSE_REL"
  )
}

validate_effective_compose() {
  "${compose[@]}" config --format json 2>/dev/null \
    | python3 /dev/fd/3 "$EXPECTED_SHA" "$DATA_NETWORK" \
      "$RUNTIME_EGRESS_NETWORK" "$HF_CACHE_VOLUME" "$TORCH_CACHE_VOLUME" \
      "$MODEL_CACHE_VOLUME" "$KB_SEED_PATH" "$ADMIN_KB_DIR" \
      "$(if [[ -d "$SOURCE_DIR" ]]; then printf '%s' "$SOURCE_DIR"; else printf '%s' "$TOOLING_ROOT"; fi)" \
      3<<'PY' 2>/dev/null
import json
import sys

(
    sha,
    data_network,
    egress_network,
    hf_volume,
    torch_volume,
    model_volume,
    seed_path,
    admin_dir,
    build_source,
) = sys.argv[1:]
payload = json.load(sys.stdin)
assert set(payload.get("services") or {}) == {"pilot50-candidate-ml"}
service = payload["services"]["pilot50-candidate-ml"]
assert service.get("image") == f"rosmol-ai-bot-pilot50-candidate:{sha}"
assert service.get("build", {}).get("context") == build_source
assert service.get("container_name") == "rosmol-pilot50-candidate-ml"
assert service.get("labels") == {
    "com.rosmol.purpose": "pilot50-candidate",
    "com.rosmol.dataset": "pilot50_balanced_v3",
    "com.rosmol.candidate-git-sha": sha,
}
assert service.get("user") == "app"
assert service.get("read_only") is True
assert service.get("restart") == "no"
assert service.get("ports") in (None, [])
assert service.get("command") == [
    "python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"
]
assert set(service.get("cap_drop") or []) == {"ALL"}
assert service.get("security_opt") == ["no-new-privileges=true"]
assert int(service.get("mem_limit")) == 6 * 1024**3
assert float(service.get("cpus")) == 2.0
assert int(service.get("pids_limit")) == 256
assert "depends_on" not in service
networks = service.get("networks") or {}
assert set(networks) == {"data", "runtime_egress"}
assert "pilot50-candidate-ml" in (networks["data"].get("aliases") or [])
environment = service.get("environment") or {}
assert environment["APP_ENV"] == "staging"
assert environment["RUNTIME_ROLE"] == "ml"
assert environment["RELEASE_GIT_SHA"] == sha
assert environment["PROMPT_VERSION"] == "pilot50-quality-v3"
assert environment["ML_PREWARM_ON_STARTUP"] == "true"
assert environment["ML_UNLOAD_AFTER_USE"] == "true"
assert environment["ML_UNLOAD_EMBEDDER_AFTER_USE"] == "true"
assert environment["ML_UNLOAD_RERANKER_AFTER_USE"] == "true"
assert environment["ADMIN_READ_ONLY"] == "true"
assert environment["ADMIN_MUTATIONS_ENABLED"] == "false"
assert environment["HDE_TRANSPORT_ENABLED"] == "false"
assert environment["YONOTE_SYNC_ENABLED"] == "false"
for key in (
    "WEBHOOK_AUTH_TOKEN", "ADMIN_AUTH_TOKEN", "HDE_TRIGGER_PREFIX",
    "HDE_BASE_URL", "HDE_API_EMAIL", "HDE_API_KEY", "HDE_BOT_USER_ID",
    "HDE_TRANSPORT_EVENT_KEY_SECRET", "HDE_TRANSPORT_ENCRYPTION_KEY",
    "VK_API_TOKEN", "VK_GROUP_TOKEN", "VK_CONFIRMATION_CODE", "VK_SECRET",
    "VK_CALLBACK_SECRET", "YONOTE_API_TOKEN",
):
    assert environment[key] == ""
assert environment["API_AUTH_TOKEN"] and environment["USER_HASH_SECRET"]
assert environment["CLOUD_RU_MODEL_SIMPLE"] == "ai-sage/GigaChat3-10B-A1.8B"
assert environment["CLOUD_RU_MODEL_COMPLEX"] == "GigaChat/GigaChat-2-Max"
for key in (
    "CLOUD_RU_MODEL_SIMPLE_INPUT_PRICE_RUB_PER_MILLION",
    "CLOUD_RU_MODEL_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION",
    "CLOUD_RU_MODEL_COMPLEX_INPUT_PRICE_RUB_PER_MILLION",
    "CLOUD_RU_MODEL_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION",
):
    assert float(environment[key]) > 0
mounts = {item["target"]: item for item in service.get("volumes") or []}
assert set(mounts) == {
    "/app/data/knowledge_base_seed.json", "/app/data/private/admin-kb",
    "/home/app/.cache/huggingface", "/home/app/.cache/torch", "/opt/models",
}
for mount in mounts.values():
    assert mount.get("read_only") is True
assert mounts["/app/data/knowledge_base_seed.json"]["source"] == seed_path
assert mounts["/app/data/private/admin-kb"]["source"] == admin_dir
assert mounts["/home/app/.cache/huggingface"]["source"] == "hf_cache"
assert mounts["/home/app/.cache/torch"]["source"] == "torch_cache"
assert mounts["/opt/models"]["source"] == "model_cache"
assert payload["networks"]["data"]["name"] == data_network
assert payload["networks"]["data"]["external"] is True
assert payload["networks"]["runtime_egress"]["name"] == egress_network
assert payload["networks"]["runtime_egress"]["external"] is True
assert payload["volumes"]["hf_cache"] == {"name": hf_volume, "external": True}
assert payload["volumes"]["torch_cache"] == {"name": torch_volume, "external": True}
assert payload["volumes"]["model_cache"] == {"name": model_volume, "external": True}
PY
}

source_paths_sha() {
  git -C "$TOOLING_ROOT" ls-tree -r -t -z --name-only "$EXPECTED_SHA" \
    | sha256sum | cut -d ' ' -f 1
}

verify_source_snapshot() {
  local actual bad_permissions expected
  sudo test -d "$SOURCE_DIR" || return 1
  sudo test ! -L "$SOURCE_DIR" || return 1
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
    diff --quiet "$EXPECTED_SHA" -- >/dev/null 2>&1 || return 1
}

prepare_cases() {
  local prepare_stdout
  if ! prepare_stdout="$(sudo docker run --rm --pull never --network none \
    --user app --read-only --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --cap-drop ALL --security-opt no-new-privileges=true \
    -v "$STAGING_DIR/source:/workspace:ro" \
    -v "$STAGING_DIR/evidence:/evidence" \
    -w /workspace --entrypoint python "$PROD_IMAGE" \
    -m scripts.pilot50 prepare \
    --manifest "/workspace/$MANIFEST_REL" \
    --output /evidence/pilot50-cases.json 2>/dev/null)"; then
    fail "v2_prepare_failed"
  fi
  printf '%s' "$prepare_stdout" | python3 /dev/fd/3 3<<'PY' 2>/dev/null
import json
import sys

value = json.load(sys.stdin)
assert value.get("status") == "OK"
assert value.get("operation") == "prepare"
assert value.get("dataset_id") == "pilot50_balanced_v3"
assert value.get("cases_total") == 50
assert value.get("type_counts") == {"typical": 25, "atypical": 25}
assert value.get("expected_behavior") == "answer"
assert value.get("expected_escalated") is False
PY
}

receipt_value() {
  local key="$1" receipt="$2"
  awk -F= -v expected="$key" '$1 == expected {print substr($0, length($1) + 2)}' \
    "$receipt"
}

validate_receipt() {
  local receipt="$1" manifest_sha="$2" cases_sha="$3"
  [[ -f "$receipt" && ! -L "$receipt" ]] || return 1
  [[ "$(receipt_value schema_version "$receipt")" == \
    "pilot50-candidate-preflight-v1" ]] || return 1
  [[ "$(receipt_value dataset_id "$receipt")" == "$DATASET_ID" ]] || return 1
  [[ "$(receipt_value candidate_sha "$receipt")" == "$EXPECTED_SHA" ]] || return 1
  [[ "$(receipt_value manifest_sha256 "$receipt")" == "$manifest_sha" ]] || return 1
  [[ "$(receipt_value cases_sha256 "$receipt")" == "$cases_sha" ]] || return 1
  [[ "$(receipt_value cases_total "$receipt")" == "$CASES_TOTAL" ]] || return 1
  [[ "$(receipt_value typical "$receipt")" == "$TYPICAL_TOTAL" ]] || return 1
  [[ "$(receipt_value atypical "$receipt")" == "$ATYPICAL_TOTAL" ]] || return 1
  [[ "$(receipt_value cost_cap_rub "$receipt")" == "$COST_CAP_RUB" ]] || return 1
  [[ "$(receipt_value capacity_status "$receipt")" == "GO" ]] || return 1
  [[ "$(receipt_value runtime_smoke_status "$receipt")" == "OK" ]] || return 1
  [[ "$(receipt_value production_snapshot_sha256 "$receipt")" =~ ^[0-9a-f]{64}$ ]] \
    || return 1
  [[ "$(receipt_value qdrant_fingerprint_sha256 "$receipt")" =~ ^[0-9a-f]{64}$ ]] \
    || return 1
  [[ "$(receipt_value qdrant_seed_sha256 "$receipt")" =~ ^[0-9a-f]{64}$ ]] \
    || return 1
}

preflight_mode() {
  local capacity cases_sha manifest_sha owner_gid owner_uid
  local post_prod post_qdrant prod_snapshot prod_snapshot_sha
  local qdrant qdrant_count qdrant_rest
  local qdrant_seed_sha qdrant_sha
  local source_seed_sha source_sha staging_evidence

  load_common_state
  verify_runner_contract_support
  if sudo docker inspect "$CANDIDATE_CONTAINER" >/dev/null 2>&1; then
    fail "candidate_container_already_exists"
  fi
  create_ephemeral_env
  build_compose_command
  "${compose[@]}" config --quiet >/dev/null 2>&1 || fail "compose_config_failed"
  validate_effective_compose || fail "compose_isolation_invalid"

  prod_snapshot="$(production_snapshot)" || fail "production_snapshot_failed"
  [[ "$prod_snapshot" == *"|false|true|"*"|healthy" ]] \
    || fail "production_snapshot_invalid"
  prod_snapshot_sha="$(printf '%s' "$prod_snapshot" | sha256sum | cut -d ' ' -f 1)"
  qdrant="$(qdrant_snapshot)" || fail "qdrant_snapshot_failed"
  qdrant_count="${qdrant%%|*}"
  qdrant_rest="${qdrant#*|}"
  qdrant_sha="${qdrant_rest%%|*}"
  qdrant_seed_sha="${qdrant_rest#*|}"
  [[ "$qdrant_count" =~ ^[1-9][0-9]*$ && "$qdrant_sha" =~ ^[0-9a-f]{64}$ && \
    "$qdrant_seed_sha" =~ ^[0-9a-f]{64}$ ]] \
    || fail "qdrant_snapshot_invalid"

  capacity="$(capacity_snapshot)" || fail "capacity_check_failed"
  [[ "$capacity" == capacity_status=* ]] || fail "capacity_check_failed"
  if [[ "$(printf '%s\n' "$capacity" | awk -F= '$1 == "capacity_status" {print $2}')" \
    != "GO" ]]; then
    stop_capacity "$capacity"
  fi

  sudo test -d "$COST_LEDGER_DIR" || fail "cost_ledger_unavailable"
  sudo test ! -L "$COST_LEDGER_DIR" || fail "cost_ledger_not_regular"
  [[ "$(sudo readlink -f -- "$COST_LEDGER_DIR" 2>/dev/null)" == \
    "$COST_LEDGER_DIR" ]] || fail "cost_ledger_not_regular"
  [[ "$(sudo stat -c '%u:%g:%a' "$COST_LEDGER_DIR" 2>/dev/null)" == \
    "10001:10001:700" ]] || fail "cost_ledger_mode_mismatch"
  if sudo test -e "$RUN_DIR" || sudo test -L "$RUN_DIR"; then
    fail "candidate_run_already_prepared_or_executed"
  fi

  owner_uid="$(id -u)"
  owner_gid="$(id -g)"
  sudo install -d -m 0700 -o "$owner_uid" -g "$owner_gid" "$BASE_DIR" \
    "$BASE_DIR/runs" || fail "pilot50_base_create_failed"
  [[ "$(sudo readlink -f -- "$BASE_DIR" 2>/dev/null)" == "$BASE_DIR" ]] \
    || fail "pilot50_base_not_regular"
  STAGING_DIR="$BASE_DIR/runs/.staging-$EXPECTED_SHA-$$"
  sudo install -d -m 0700 -o "$owner_uid" -g "$owner_gid" \
    "$STAGING_DIR" || fail "staging_create_failed"
  sudo install -d -m 0755 -o "$owner_uid" -g "$owner_gid" \
    "$STAGING_DIR/source" || fail "staging_create_failed"
  sudo install -d -m 0700 -o 10001 -g 10001 "$STAGING_DIR/evidence" \
    || fail "evidence_create_failed"
  git -C "$TOOLING_ROOT" archive --format=tar "$EXPECTED_SHA" 2>/dev/null \
    | sudo tar -xf - -C "$STAGING_DIR/source" >/dev/null 2>&1 \
    || fail "source_snapshot_create_failed"
  sudo find "$STAGING_DIR/source" -type d -exec chmod 0555 {} + \
    >/dev/null 2>&1 || fail "source_snapshot_permissions_failed"
  sudo find "$STAGING_DIR/source" -type f -exec chmod 0444 {} + \
    >/dev/null 2>&1 || fail "source_snapshot_permissions_failed"
  SOURCE_DIR="$STAGING_DIR/source"
  verify_source_snapshot || fail "source_snapshot_invalid"
  source_seed_sha="$(sudo sha256sum \
    "$STAGING_DIR/source/data/knowledge_base_seed.json" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "source_seed_sha_unavailable"
  [[ "$source_seed_sha" == "$qdrant_seed_sha" ]] \
    || fail "candidate_seed_differs_from_runtime"
  prepare_cases
  staging_evidence="$STAGING_DIR/evidence"
  sudo test -f "$staging_evidence/pilot50-cases.json" \
    || fail "prepared_cases_missing"
  sudo test ! -L "$staging_evidence/pilot50-cases.json" \
    || fail "prepared_cases_not_regular"
  sudo chmod 0600 "$staging_evidence/pilot50-cases.json" \
    || fail "prepared_cases_mode_failed"
  manifest_sha="$(sudo sha256sum "$STAGING_DIR/source/$MANIFEST_REL" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "manifest_sha_unavailable"
  cases_sha="$(sudo sha256sum "$staging_evidence/pilot50-cases.json" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "cases_sha_unavailable"
  source_sha="$(source_paths_sha)" || fail "source_paths_sha_unavailable"
  [[ "$manifest_sha" =~ ^[0-9a-f]{64}$ && "$cases_sha" =~ ^[0-9a-f]{64}$ && \
    "$source_sha" =~ ^[0-9a-f]{64}$ ]] || fail "preflight_hash_invalid"

  if [[ "$EPHEMERAL_ENV_FILE" != /run/pilot50-candidate-env.* ]] || \
    ! sudo rm -f -- "$EPHEMERAL_ENV_FILE" >/dev/null 2>&1; then
    fail "ephemeral_env_cleanup_failed"
  fi
  EPHEMERAL_ENV_FILE=""
  create_ephemeral_env
  build_compose_command
  "${compose[@]}" config --quiet >/dev/null 2>&1 || fail "compose_config_failed"
  validate_effective_compose || fail "compose_isolation_invalid"
  "${compose[@]}" build --pull=false pilot50-candidate-ml \
    >/dev/null 2>&1 || fail "candidate_image_build_failed"
  verify_source_snapshot || fail "source_snapshot_changed_during_build"
  [[ "$(sudo docker image inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "rosmol-ai-bot-pilot50-candidate:$EXPECTED_SHA" 2>/dev/null)" == \
    "$EXPECTED_SHA" ]] || fail "candidate_image_sha_mismatch"
  CANDIDATE_ID="$("${compose[@]}" run -d --no-deps --use-aliases \
    --name "$CANDIDATE_CONTAINER" pilot50-candidate-ml 2>/dev/null)" \
    || fail "candidate_start_failed"
  [[ "$CANDIDATE_ID" =~ ^[0-9a-f]{64}$ ]] || fail "candidate_start_failed"
  candidate_owned || fail "candidate_identity_invalid"
  require_candidate_runtime
  wait_candidate_ready || fail "candidate_not_ready"
  require_candidate_runtime
  remove_owned_candidate || fail "candidate_cleanup_failed"
  post_prod="$(production_snapshot)" || fail "production_snapshot_failed"
  [[ "$post_prod" == "$prod_snapshot" ]] \
    || fail "production_changed_during_preflight"
  post_qdrant="$(qdrant_snapshot)" || fail "qdrant_snapshot_failed"
  [[ "$post_qdrant" == "$qdrant" ]] || fail "qdrant_changed_during_preflight"

  {
    printf 'schema_version=pilot50-candidate-preflight-v1\n'
    printf 'dataset_id=%s\n' "$DATASET_ID"
    printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
    printf 'production_runtime_sha=%s\n' "$PROD_RUNTIME_SHA"
    printf 'production_snapshot_sha256=%s\n' "$prod_snapshot_sha"
    printf 'qdrant_count=%s\n' "$qdrant_count"
    printf 'qdrant_fingerprint_sha256=%s\n' "$qdrant_sha"
    printf 'qdrant_seed_sha256=%s\n' "$qdrant_seed_sha"
    printf 'manifest_sha256=%s\n' "$manifest_sha"
    printf 'cases_sha256=%s\n' "$cases_sha"
    printf 'source_paths_sha256=%s\n' "$source_sha"
    printf 'cases_total=%s\ntypical=%s\natypical=%s\n' \
      "$CASES_TOTAL" "$TYPICAL_TOTAL" "$ATYPICAL_TOTAL"
    printf 'target=%s\ncost_cap_rub=%s\n' "$TARGET" "$COST_CAP_RUB"
    printf 'runtime_smoke_status=OK\n'
    printf '%s\n' "$capacity"
  } >"$STAGING_DIR/preflight.receipt" || fail "preflight_receipt_create_failed"
  chmod 0600 "$STAGING_DIR/preflight.receipt" || fail "preflight_receipt_mode_failed"
  validate_receipt "$STAGING_DIR/preflight.receipt" "$manifest_sha" "$cases_sha" \
    || fail "preflight_receipt_invalid"
  sudo mv -T -- "$STAGING_DIR" "$RUN_DIR" || fail "preflight_publish_failed"
  STAGING_DIR=""
  SOURCE_DIR="$RUN_DIR/source"
  cleanup_temp || fail "preflight_temp_cleanup_failed"

  printf 'pilot50_candidate_preflight=GO\n'
  printf 'runtime_smoke_status=OK\n'
  printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
  printf 'production_runtime_sha=%s\n' "$PROD_RUNTIME_SHA"
  printf 'production_snapshot_sha256=%s\n' "$prod_snapshot_sha"
  printf 'manifest_sha256=%s\n' "$manifest_sha"
  printf 'cases_sha256=%s\n' "$cases_sha"
  printf 'qdrant_count=%s\n' "$qdrant_count"
  printf 'qdrant_fingerprint_sha256=%s\n' "$qdrant_sha"
  printf 'qdrant_seed_sha256=%s\n' "$qdrant_seed_sha"
  printf '%s\n' "$capacity"
  printf 'cost_cap_rub=%s\n' "$COST_CAP_RUB"
}

create_runner_env() {
  RUNNER_ENV_FILE="$(sudo mktemp /run/pilot50-candidate-runner-env.XXXXXX 2>/dev/null)" \
    || fail "runner_env_create_failed"
  sudo chown "$(id -u):$(id -g)" "$RUNNER_ENV_FILE" \
    || fail "runner_env_create_failed"
  chmod 0600 "$RUNNER_ENV_FILE" || fail "runner_env_mode_failed"
  "${compose[@]}" config --format json 2>/dev/null \
    | python3 /dev/fd/3 "$RUNNER_ENV_FILE" "$EXPECTED_SHA" "$COST_LEDGER_DIR" \
      3<<'PY' 2>/dev/null
import json
import os
import sys

path, sha, ledger = sys.argv[1:]
payload = json.load(sys.stdin)
env = payload["services"]["pilot50-candidate-ml"]["environment"]
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
    "CLOUD_RU_MODEL_SIMPLE_INPUT_PRICE_RUB_PER_MILLION": env[
        "CLOUD_RU_MODEL_SIMPLE_INPUT_PRICE_RUB_PER_MILLION"
    ],
    "CLOUD_RU_MODEL_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION": env[
        "CLOUD_RU_MODEL_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION"
    ],
    "CLOUD_RU_MODEL_COMPLEX_INPUT_PRICE_RUB_PER_MILLION": env[
        "CLOUD_RU_MODEL_COMPLEX_INPUT_PRICE_RUB_PER_MILLION"
    ],
    "CLOUD_RU_MODEL_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION": env[
        "CLOUD_RU_MODEL_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION"
    ],
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
    "ALL_PROXY": "",
    "NO_PROXY": "pilot50-candidate-ml,postgres,127.0.0.1,localhost",
}
assert ledger == "/var/lib/rosmol/eval-cost-ledger-v1"
assert len(selected["API_AUTH_TOKEN"]) >= 32
flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(path, flags)
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
    for key, value in selected.items():
        text = str(value)
        assert "\n" not in text and "\r" not in text
        handle.write(f"{key}={text}\n")
PY
}

validate_candidate_runtime() {
  candidate_owned || {
    printf 'identity'
    return 1
  }
  sudo docker inspect "$CANDIDATE_ID" 2>/dev/null \
    | python3 /dev/fd/3 "$EXPECTED_SHA" "$DATA_NETWORK" \
      "$RUNTIME_EGRESS_NETWORK" 3<<'PY' 2>/dev/null
import json
import sys

class RuntimeCheckError(Exception):
    pass


def require(code, condition):
    if condition is not True:
        raise RuntimeCheckError(code)


def no_new_privileges_enabled(values):
    return values in (
        ["no-new-privileges"],
        ["no-new-privileges=true"],
        ["no-new-privileges:true"],
    )


try:
    sha, data_network, egress_network = sys.argv[1:]
    payload = json.load(sys.stdin)
    require("inspect", isinstance(payload, list) and len(payload) == 1)
    value = payload[0]
    require("inspect", isinstance(value, dict))
    host = value.get("HostConfig")
    config = value.get("Config")
    state = value.get("State")
    networks = value.get("NetworkSettings")
    require(
        "inspect",
        all(isinstance(item, dict) for item in (host, config, state, networks)),
    )
    require("state", state.get("Running") is True and state.get("OOMKilled") is False)
    require("rootfs", host.get("ReadonlyRootfs") is True)
    require("memory", host.get("Memory") == 6 * 1024**3)
    require("cpu", host.get("NanoCpus") == 2_000_000_000)
    require("pids", host.get("PidsLimit") == 256)
    restart = host.get("RestartPolicy")
    require(
        "restart",
        isinstance(restart, dict)
        and restart.get("Name") in {"", "no"}
        and restart.get("MaximumRetryCount") == 0,
    )
    require("cap_drop", host.get("CapDrop") == ["ALL"])
    require("no_new_privileges", no_new_privileges_enabled(host.get("SecurityOpt")))
    exposed_ports = networks.get("Ports") or {}
    require(
        "ports",
        not (host.get("PortBindings") or {})
        and isinstance(exposed_ports, dict)
        and all(bindings is None for bindings in exposed_ports.values()),
    )
    attached_networks = networks.get("Networks")
    data_endpoint = (
        attached_networks.get(data_network)
        if isinstance(attached_networks, dict)
        else None
    )
    require(
        "networks",
        isinstance(attached_networks, dict)
        and set(attached_networks) == {data_network, egress_network}
        and isinstance(data_endpoint, dict)
        and "pilot50-candidate-ml" in (data_endpoint.get("Aliases") or []),
    )
    labels = config.get("Labels")
    require(
        "labels",
        isinstance(labels, dict)
        and labels.get("com.rosmol.purpose") == "pilot50-candidate"
        and labels.get("com.rosmol.dataset") == "pilot50_balanced_v3"
        and labels.get("com.rosmol.candidate-git-sha") == sha,
    )
    raw_env = config.get("Env")
    require(
        "inspect",
        isinstance(raw_env, list)
        and all(isinstance(item, str) and "=" in item for item in raw_env),
    )
    env = dict(item.split("=", 1) for item in raw_env)
    require(
        "runtime_env",
        env.get("APP_ENV") == "staging"
        and env.get("RUNTIME_ROLE") == "ml"
        and env.get("RELEASE_GIT_SHA") == sha,
    )
    require(
        "ml_lifecycle",
        all(
            env.get(key) == "true"
            for key in (
                "ML_PREWARM_ON_STARTUP",
                "ML_UNLOAD_AFTER_USE",
                "ML_UNLOAD_EMBEDDER_AFTER_USE",
                "ML_UNLOAD_RERANKER_AFTER_USE",
            )
        ),
    )
    require(
        "transports",
        env.get("HDE_TRANSPORT_ENABLED") == "false"
        and env.get("YONOTE_SYNC_ENABLED") == "false"
        and env.get("ADMIN_READ_ONLY") == "true"
        and env.get("ADMIN_MUTATIONS_ENABLED") == "false",
    )
    blank_secrets = (
        "WEBHOOK_AUTH_TOKEN", "ADMIN_AUTH_TOKEN", "HDE_TRIGGER_PREFIX",
        "HDE_BASE_URL", "HDE_API_EMAIL", "HDE_API_KEY", "HDE_BOT_USER_ID",
        "HDE_TRANSPORT_EVENT_KEY_SECRET", "HDE_TRANSPORT_ENCRYPTION_KEY",
        "YONOTE_API_TOKEN", "VK_API_TOKEN", "VK_GROUP_TOKEN",
        "VK_CONFIRMATION_CODE", "VK_SECRET", "VK_CALLBACK_SECRET",
    )
    require(
        "secrets",
        all(env.get(key) == "" for key in blank_secrets)
        and bool(env.get("API_AUTH_TOKEN"))
        and bool(env.get("USER_HASH_SECRET")),
    )
    raw_mounts = value.get("Mounts")
    require("inspect", isinstance(raw_mounts, list))
    mounts = {
        item.get("Destination"): item
        for item in raw_mounts
        if isinstance(item, dict) and isinstance(item.get("Destination"), str)
    }
    required_mounts = (
        "/app/data/knowledge_base_seed.json", "/app/data/private/admin-kb",
        "/home/app/.cache/huggingface", "/home/app/.cache/torch", "/opt/models",
    )
    require(
        "mounts",
        len(raw_mounts) == len(required_mounts)
        and set(mounts) == set(required_mounts)
        and all(mounts[target].get("RW") is False for target in required_mounts),
    )
except RuntimeCheckError as exc:
    print(str(exc))
    raise SystemExit(1) from None
except Exception:
    print("inspect")
    raise SystemExit(1) from None
PY
}

require_candidate_runtime() {
  local stage
  if stage="$(validate_candidate_runtime)"; then
    [[ -z "$stage" ]] || fail "candidate_isolation_output_invalid"
    return 0
  fi
  case "$stage" in
    identity | inspect | state | rootfs | memory | cpu | pids | restart | \
      cap_drop | no_new_privileges | ports | networks | labels | runtime_env | \
      ml_lifecycle | transports | secrets | mounts)
      fail "candidate_isolation_$stage"
      ;;
    *) fail "candidate_isolation_inspect" ;;
  esac
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
    "rosmol-ai-bot-pilot50-candidate:$EXPECTED_SHA"
  )
}

cost_governance_preflight() {
  local approval_id="$1" waiver_id="$2" cases_sha="$3"
  sudo docker run --rm --interactive --pull never --network none \
    --user app --read-only --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --cap-drop ALL --security-opt no-new-privileges=true \
    -v "$SOURCE_DIR:/workspace:ro" -v "$COST_LEDGER_DIR:/cost-ledger:ro" \
    -w /workspace --entrypoint python \
    "rosmol-ai-bot-pilot50-candidate:$EXPECTED_SHA" \
    - "$approval_id" "$waiver_id" "$EXPECTED_SHA" "$cases_sha" \
      "$PRIOR_CANDIDATE_SCOPE" "$PRIOR_CANDIDATE_SHA" \
      "$PRIOR_CASES_SHA256" "$COMPARISON_WAIVER_DECISION_ID" \
      "$COMPARISON_PROVIDER_RISK_CEILING_RUB" "$COST_CAP_RUB" \
      2>/dev/null <<'PY'
import sys
from datetime import UTC, datetime
from pathlib import Path

from eval.cost_governance import (
    _enforce_approval_once,
    _enforce_rolling_limits,
    _enforce_waiver_once,
    _reservation_payload_sha256,
    _scan_records,
    _validated_approval_id,
    _validated_private_full_comparison_waiver,
)
from eval.run_ask import (
    PILOT50_V3_CANDIDATE_CASES_SHA256,
    PILOT50_V3_CANDIDATE_CONTRACT_ID,
    _pilot50_candidate_comparison_waiver,
    _pilot50_v3_expected_approval_id,
    _pilot50_v3_expected_waiver_id,
)

approval_id = _validated_approval_id(sys.argv[1])
assert approval_id is not None
waiver_id = _validated_approval_id(sys.argv[2])
assert waiver_id is not None and waiver_id != approval_id
runtime_sha = sys.argv[3]
cases_sha = sys.argv[4]
(
    prior_scope,
    prior_runtime_sha,
    prior_cases_sha,
    decision_id,
    provider_risk_ceiling,
    cost_cap,
) = sys.argv[5:]
provider_risk_ceiling = float(provider_risk_ceiling)
cost_cap = float(cost_cap)
assert cases_sha == PILOT50_V3_CANDIDATE_CASES_SHA256
assert approval_id == _pilot50_v3_expected_approval_id(runtime_sha)
assert waiver_id == _pilot50_v3_expected_waiver_id(runtime_sha)
ledger = Path("/cost-ledger")
assert ledger.is_dir() and not ledger.is_symlink()
lock = ledger / ".cost-governance.lock"
assert not lock.exists() and not lock.is_symlink()
now = datetime.now(UTC)
records = _scan_records(ledger, now=now)
waiver = _pilot50_candidate_comparison_waiver(
    {
        "contract_id": PILOT50_V3_CANDIDATE_CONTRACT_ID,
        "runtime_git_sha": runtime_sha,
    },
    rolling_24h_comparison_waiver_id=waiver_id,
)
assert waiver is not None
assert waiver.decision_id == decision_id
assert waiver.provider_risk_ceiling_rub == provider_risk_ceiling
assert waiver.prior_scope == prior_scope
assert waiver.prior_runtime_git_sha == prior_runtime_sha
assert waiver.prior_manifest_sha256 == prior_cases_sha
assert waiver.prior_case_count == 50
assert waiver.prior_approved_cap_rub == cost_cap
assert waiver.requested_manifest_sha256 == cases_sha
assert waiver.requested_approved_cap_rub == cost_cap
waiver = _validated_private_full_comparison_waiver(
    waiver,
    scope="pilot50-v3-candidate",
    runtime_git_sha=runtime_sha,
    manifest_sha256=cases_sha,
    case_count=50,
    approved_cap_rub=cost_cap,
    private_full=True,
    approval_id=approval_id,
)
assert waiver is not None
_enforce_approval_once(records, approval_id=approval_id)
_enforce_waiver_once(records, waiver=waiver)
prior = _enforce_rolling_limits(
    records,
    now=now,
    requested_cap=cost_cap,
    private_full=True,
    requested_runtime_git_sha=runtime_sha,
    comparison_waiver=waiver,
)
assert prior is not None
print(_reservation_payload_sha256(prior))
PY
}

validate_safe_stdout() {
  local cases_sha="$1" report_sha="$2" approval_id="$3" waiver_id="$4"
  local waived_reservation_sha="$5"
  python3 /dev/fd/3 "$cases_sha" "$report_sha" "$EXPECTED_SHA" \
    "$approval_id" "$waiver_id" "$waived_reservation_sha" \
    "$COST_CAP_RUB" 3<<'PY' 2>/dev/null
import json
import hashlib
import math
import re
import sys

(
    cases_sha,
    report_sha,
    runtime_sha,
    approval_id,
    waiver_id,
    waived_reservation_sha,
    cap,
) = sys.argv[1:]
raw = sys.stdin.buffer.read(16385)
assert len(raw) <= 16384 and raw and all(byte == 10 or 32 <= byte != 127 for byte in raw)
payload = json.loads(raw)
expected_fields = {
    "schema_version", "dataset_id", "eval_run_id", "runtime_git_sha",
    "approval_id", "run_window_utc", "billing_status", "status",
    "classification", "human_product_verdict", "denominator", "counts",
    "mechanical_first_turn_closure", "policy_pass", "trace_coverage",
    "cache_hits", "budget", "pricing", "latency_ms", "llm_cost_rub",
    "cases_sha256", "report_sha256", "disclaimer", "quality_gate",
    "rolling_24h_waiver",
}
assert isinstance(payload, dict) and set(payload) == expected_fields
for forbidden in ("query", "response", "request_id", "trace_events", "error"):
    assert forbidden not in payload
assert payload.get("schema_version") == "pilot50-safe-result-v1"
assert payload.get("dataset_id") == "pilot50_balanced_v3"
assert payload.get("runtime_git_sha") == runtime_sha
assert payload.get("approval_id") == approval_id
waiver = payload.get("rolling_24h_waiver")
assert waiver == {
    "waiver_id": waiver_id,
    "decision_id": "D-041",
    "waived_reservation_sha256": waived_reservation_sha,
    "provider_residual_risk_ceiling_rub": 500,
    "runner_projected_stop_limit_rub": 30,
}
assert re.fullmatch(r"[0-9a-f]{64}", waived_reservation_sha)
assert re.fullmatch(r"ask-eval-[0-9a-f-]{36}", str(payload.get("eval_run_id") or ""))
assert payload.get("status") == "OK"
assert payload.get("classification") == "calibration_only"
assert payload.get("human_product_verdict") is False
assert payload.get("denominator") == 50
assert payload.get("counts") == {"typical": 25, "atypical": 25}
closed = {}
for field, count_key in (
    ("mechanical_first_turn_closure", "closed"),
    ("policy_pass", "passed"),
):
    table = payload.get(field)
    assert isinstance(table, dict) and set(table) == {"typical", "atypical", "overall"}
    observed = {}
    for group, total in (("typical", 25), ("atypical", 25), ("overall", 50)):
        row = table.get(group)
        assert isinstance(row, dict) and set(row) == {count_key, "total", "rate"}
        actual = row[count_key]
        assert type(actual) is int and 0 <= actual <= total
        assert row["total"] == total and row["rate"] == round(actual / total, 6)
        observed[group] = actual
    assert observed["overall"] == observed["typical"] + observed["atypical"]
    closed[field] = observed
assert closed["mechanical_first_turn_closure"] == closed["policy_pass"]
assert payload.get("trace_coverage") == {"found": 50, "total": 50, "rate": 1.0}
assert payload.get("cache_hits") == 0
assert payload.get("budget") == {"max_rub": int(cap), "exceeded": False, "stopped": False}
pricing = payload.get("pricing")
rate_card = {
    "complex_input_price_rub_per_million": "569.34",
    "complex_model": "GigaChat/GigaChat-2-Max",
    "complex_official_price_rub_per_million": "569.3374",
    "complex_output_price_rub_per_million": "569.34",
    "complex_price_policy": "conservative_round_up",
    "simple_input_price_rub_per_million": "12.2",
    "simple_model": "ai-sage/GigaChat3-10B-A1.8B",
    "simple_output_price_rub_per_million": "12.2",
}
rate_card_sha = hashlib.sha256(
    json.dumps(rate_card, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
).hexdigest()
assert pricing == {
    "complete": True,
    "stopped": False,
    "source": "target_reported",
    "contract_id": "pilot50-v3-candidate-v1",
    "rate_card_sha256": rate_card_sha,
    "target_telemetry_preserved": True,
    "target_telemetry_pricing_complete": True,
}
latency = payload.get("latency_ms")
assert isinstance(latency, dict) and set(latency) == {"p50", "p95"}
assert all(type(value) is int and value >= 0 for value in latency.values())
assert latency["p50"] <= latency["p95"]
cost = float(payload.get("llm_cost_rub"))
assert math.isfinite(cost) and 0 <= cost <= float(cap)
assert payload.get("cases_sha256") == cases_sha
assert payload.get("report_sha256") == report_sha
assert payload.get("billing_status") == "pending_provider_reconciliation"
gate = payload.get("quality_gate")
assert isinstance(gate, dict) and set(gate) == {
    "schema_version", "status", "criteria", "failed_criteria",
    "output_contract_reasons", "source_binding_definition",
    "critical_case_definition",
}
criterion_order = (
    "overall_closed", "typical_closed", "atypical_closed",
    "output_contract_escalations", "source_binding_failures",
    "critical_case_failures",
)
criteria = gate["criteria"]
assert isinstance(criteria, dict) and set(criteria) == set(criterion_order)
criterion_specs = {
    "overall_closed": (
        closed["mechanical_first_turn_closure"]["overall"], "minimum", 30, {}
    ),
    "typical_closed": (
        closed["mechanical_first_turn_closure"]["typical"], "minimum", 11, {}
    ),
    "atypical_closed": (
        closed["mechanical_first_turn_closure"]["atypical"], "minimum", 7, {}
    ),
    "output_contract_escalations": (None, "maximum", 6, {}),
    "source_binding_failures": (
        None, "maximum", 0, {"applicable_qrel_cases": 50, "total_cases": 50}
    ),
    "critical_case_failures": (
        None, "maximum", 0, {"applicable_critical_cases": 15, "total_cases": 50}
    ),
}
failed = []
for name in criterion_order:
    expected_actual, bound_key, bound, extra = criterion_specs[name]
    row = criteria[name]
    assert isinstance(row, dict) and set(row) == {
        "actual", bound_key, "passed", *extra
    }
    actual = row["actual"]
    assert type(actual) is int and 0 <= actual <= 50
    if name == "source_binding_failures":
        assert actual <= 50
    if name == "critical_case_failures":
        assert actual <= 15
    if expected_actual is not None:
        assert actual == expected_actual
    assert row[bound_key] == bound
    assert all(row[key] == value for key, value in extra.items())
    passed = actual >= bound if bound_key == "minimum" else actual <= bound
    assert row["passed"] is passed
    if not passed:
        failed.append(name)
assert gate["schema_version"] == "pilot50-v3-quality-gate-v1"
assert gate["failed_criteria"] == failed
assert gate["status"] == ("STOP" if failed else "GO")
assert gate["output_contract_reasons"] == sorted({
    "empty_generated_response", "final_response_empty", "final_response_too_long",
    "final_response_too_many_links", "final_response_unapproved_emoji",
    "llm_response_contract_failed", "llm_response_profile_failed",
    "llm_response_too_long", "llm_source_citation_failed",
    "llm_source_coverage_failed", "llm_source_fact_binding_failed",
    "source_response_contract_failed",
})
assert gate["source_binding_definition"] == (
    "non_escalated_result_with_qrels_failing_any_effective_expected_retrieval_"
    "or_citation_source_check"
)
assert gate["critical_case_definition"] == (
    "result_passed_is_not_true_for_case_tagged_adversarial_or_off_aspect_guard"
)
assert payload.get("disclaimer") == (
    "Tracked regression calibration only. This is a mechanical first-turn closure "
    "result for the balanced Pilot50 set, not an independent holdout, a human "
    "product verdict, ticket-level conversion, or production traffic conversion."
)
sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
}

run_mode() {
  local approval_id capacity cases_sha completed expected_approval_id
  local expected_waiver_id manifest_sha marker source_seed_sha
  local post_prod post_prod_sha post_qdrant pre_prod_sha pre_qdrant
  local pre_qdrant_count pre_qdrant_rest pre_qdrant_seed_sha pre_qdrant_sha
  local quality_status raw_report report_sha safe_result safe_sha safe_stdout
  local validated_safe waiver_id waived_reservation_sha

  load_common_state
  verify_runner_contract_support
  approval_id="${HIGH_COST_APPROVAL_ID:-}"
  waiver_id="${PILOT50_ROLLING_24H_WAIVER_ID:-}"
  expected_approval_id="owner-chat-20260811-pilot50-v3-${EXPECTED_SHA}-cap30"
  expected_waiver_id="owner-chat-20260811-waive-rolling24h-v2-to-v3-${EXPECTED_SHA}-cap30"
  [[ "$approval_id" == "$expected_approval_id" ]] \
    || fail "approval_id_missing_or_invalid"
  [[ "$waiver_id" == "$expected_waiver_id" ]] \
    || fail "rolling_24h_comparison_waiver_id_missing_or_invalid"
  sudo test -d "$RUN_DIR" || fail "preflight_not_found"
  sudo test ! -L "$RUN_DIR" || fail "run_directory_not_regular"
  [[ "$(sudo readlink -f -- "$RUN_DIR" 2>/dev/null)" == "$RUN_DIR" ]] \
    || fail "run_directory_not_regular"
  marker="$RUN_DIR/preflight.receipt"
  manifest_sha="$(sudo sha256sum "$SOURCE_DIR/$MANIFEST_REL" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "manifest_sha_unavailable"
  cases_sha="$(sudo sha256sum "$EVIDENCE_DIR/pilot50-cases.json" 2>/dev/null \
    | cut -d ' ' -f 1)" || fail "cases_sha_unavailable"
  source_seed_sha="$(sudo sha256sum "$SOURCE_DIR/data/knowledge_base_seed.json" \
    2>/dev/null | cut -d ' ' -f 1)" || fail "source_seed_sha_unavailable"
  validate_receipt "$marker" "$manifest_sha" "$cases_sha" \
    || fail "preflight_receipt_mismatch"
  [[ "$source_seed_sha" == "$(receipt_value qdrant_seed_sha256 "$marker")" ]] \
    || fail "candidate_seed_differs_from_runtime"
  verify_source_snapshot || fail "source_snapshot_invalid"
  sudo test -d "$COST_LEDGER_DIR" || fail "cost_ledger_unavailable"
  sudo test ! -L "$COST_LEDGER_DIR" || fail "cost_ledger_not_regular"
  [[ "$(sudo stat -c '%u:%g:%a' "$COST_LEDGER_DIR" 2>/dev/null)" == \
    "10001:10001:700" ]] || fail "cost_ledger_mode_mismatch"
  if sudo docker inspect "$CANDIDATE_CONTAINER" >/dev/null 2>&1; then
    fail "candidate_container_already_exists"
  fi
  [[ ! -e "$RUN_DIR/run.started" && ! -L "$RUN_DIR/run.started" ]] \
    || fail "candidate_run_replay_refused"
  [[ ! -e "$RUN_DIR/run.completed" && ! -L "$RUN_DIR/run.completed" ]] \
    || fail "candidate_run_replay_refused"
  raw_report="$EVIDENCE_DIR/pilot50-ask-report.json"
  safe_result="$EVIDENCE_DIR/pilot50-safe-result.json"
  if sudo test -e "$raw_report" || sudo test -L "$raw_report" \
    || sudo test -e "$safe_result" || sudo test -L "$safe_result"; then
    fail "candidate_run_artifact_exists"
  fi
  pre_prod_sha="$(printf '%s' "$(production_snapshot)" \
    | sha256sum | cut -d ' ' -f 1)" || fail "production_snapshot_failed"
  [[ "$pre_prod_sha" == "$(receipt_value production_snapshot_sha256 "$marker")" ]] \
    || fail "production_changed_since_preflight"
  pre_qdrant="$(qdrant_snapshot)" || fail "qdrant_snapshot_failed"
  pre_qdrant_count="${pre_qdrant%%|*}"
  pre_qdrant_rest="${pre_qdrant#*|}"
  pre_qdrant_sha="${pre_qdrant_rest%%|*}"
  pre_qdrant_seed_sha="${pre_qdrant_rest#*|}"
  [[ "$pre_qdrant_count" == "$(receipt_value qdrant_count "$marker")" && \
    "$pre_qdrant_sha" == "$(receipt_value qdrant_fingerprint_sha256 "$marker")" && \
    "$pre_qdrant_seed_sha" == "$(receipt_value qdrant_seed_sha256 "$marker")" ]] \
    || fail "qdrant_changed_since_preflight"
  capacity="$(capacity_snapshot)" || fail "capacity_check_failed"
  if [[ "$(printf '%s\n' "$capacity" | awk -F= '$1 == "capacity_status" {print $2}')" \
    != "GO" ]]; then
    stop_capacity "$capacity"
  fi

  create_ephemeral_env
  build_compose_command
  "${compose[@]}" config --quiet >/dev/null 2>&1 || fail "compose_config_failed"
  validate_effective_compose || fail "compose_isolation_invalid"
  create_runner_env

  "${compose[@]}" build --pull=false pilot50-candidate-ml \
    >/dev/null 2>&1 || fail "candidate_image_build_failed"
  verify_source_snapshot || fail "source_snapshot_changed_during_build"
  [[ "$(sudo docker image inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "rosmol-ai-bot-pilot50-candidate:$EXPECTED_SHA" 2>/dev/null)" == \
    "$EXPECTED_SHA" ]] || fail "candidate_image_sha_mismatch"
  CANDIDATE_ID="$("${compose[@]}" run -d --no-deps --use-aliases \
    --name "$CANDIDATE_CONTAINER" pilot50-candidate-ml 2>/dev/null)" \
    || fail "candidate_start_failed"
  [[ "$CANDIDATE_ID" =~ ^[0-9a-f]{64}$ ]] || fail "candidate_start_failed"
  candidate_owned || fail "candidate_identity_invalid"
  require_candidate_runtime
  wait_candidate_ready || fail "candidate_not_ready"
  require_candidate_runtime

  runner_command
  if ! waived_reservation_sha="$(cost_governance_preflight \
    "$approval_id" "$waiver_id" "$cases_sha")"; then
    fail "cost_governance_preflight_failed"
  fi
  [[ "$waived_reservation_sha" =~ ^[0-9a-f]{64}$ ]] \
    || fail "cost_governance_preflight_failed"
  (set -o noclobber; {
    printf 'schema_version=pilot50-candidate-run-started-v1\n'
    printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
    printf 'cases_sha256=%s\n' "$cases_sha"
    printf 'approval_id=%s\n' "$approval_id"
    printf 'rolling_24h_waiver_id=%s\n' "$waiver_id"
    printf 'rolling_24h_waiver_decision_id=%s\n' \
      "$COMPARISON_WAIVER_DECISION_ID"
    printf 'waived_reservation_sha256=%s\n' "$waived_reservation_sha"
    printf 'provider_residual_risk_ceiling_rub=%s\n' \
      "$COMPARISON_PROVIDER_RISK_CEILING_RUB"
    printf 'runner_projected_stop_limit_rub=%s\n' "$COST_CAP_RUB"
    printf 'cost_cap_rub=%s\n' "$COST_CAP_RUB"
  } >"$RUN_DIR/run.started") 2>/dev/null || fail "candidate_run_replay_refused"
  if ! "${runner[@]}" -m eval.run_ask \
    --cases /evidence/pilot50-cases.json \
    --output /evidence/pilot50-ask-report.json \
    --no-markdown \
    --target "$TARGET" \
    --concurrency 1 \
    --timeout 180 \
    --max-llm-cost-rub "$COST_CAP_RUB" \
    --pilot50-candidate-contract "$CANDIDATE_CONTRACT_ID" \
    --expected-runtime-git-sha "$EXPECTED_SHA" \
    --high-cost-approval-id "$approval_id" \
    --rolling-24h-comparison-waiver-id "$waiver_id" \
    --kb-seed /workspace/data/knowledge_base_seed.json \
    --bypass-cache \
    --require-complete-traces \
    >/dev/null 2>&1; then
    fail "candidate_ask_eval_failed"
  fi
  sudo test -f "$raw_report" || fail "candidate_raw_report_missing"
  sudo test ! -L "$raw_report" || fail "candidate_raw_report_not_regular"
  if ! "${runner[@]}" -m scripts.pilot50 summarize \
    --manifest "/workspace/$MANIFEST_REL" \
    --cases /evidence/pilot50-cases.json \
    --report /evidence/pilot50-ask-report.json \
    --output /evidence/pilot50-safe-result.json \
    --expected-runtime-git-sha "$EXPECTED_SHA" \
    --expected-approval-id "$approval_id" \
    --rolling-24h-comparison-waiver-id "$waiver_id" \
    --candidate-contract "$CANDIDATE_CONTRACT_ID" \
    >/dev/null 2>&1; then
    fail "candidate_summarize_failed"
  fi
  sudo test -f "$safe_result" || fail "candidate_safe_result_missing"
  sudo test ! -L "$safe_result" || fail "candidate_safe_result_not_regular"
  sudo chmod 0600 "$EVIDENCE_DIR/pilot50-cases.json" "$raw_report" "$safe_result" \
    >/dev/null 2>&1 || fail "candidate_artifact_mode_failed"
  report_sha="$(sudo sha256sum "$raw_report" 2>/dev/null | cut -d ' ' -f 1)" \
    || fail "candidate_report_sha_unavailable"
  safe_sha="$(sudo sha256sum "$safe_result" 2>/dev/null | cut -d ' ' -f 1)" \
    || fail "candidate_safe_sha_unavailable"
  [[ "$report_sha" =~ ^[0-9a-f]{64}$ && "$safe_sha" =~ ^[0-9a-f]{64}$ ]] \
    || fail "candidate_artifact_sha_invalid"
  if ! safe_stdout="$("${runner[@]}" -m scripts.pilot50 show-safe \
    --input /evidence/pilot50-safe-result.json 2>/dev/null)"; then
    fail "candidate_safe_output_failed"
  fi
  [[ ${#safe_stdout} -le 16384 ]] || fail "candidate_safe_output_oversized"
  if ! validated_safe="$(printf '%s' "$safe_stdout" \
    | validate_safe_stdout "$cases_sha" "$report_sha" "$approval_id" \
      "$waiver_id" "$waived_reservation_sha")"; then
    fail "candidate_safe_output_invalid"
  fi
  quality_status="$(python3 -c \
    'import json,sys; print(json.load(sys.stdin)["quality_gate"]["status"])' \
    <<<"$validated_safe" 2>/dev/null)" || fail "candidate_quality_status_invalid"
  [[ "$quality_status" == "GO" || "$quality_status" == "STOP" ]] \
    || fail "candidate_quality_status_invalid"

  wait_candidate_ready || fail "candidate_not_ready_after_run"
  require_candidate_runtime
  post_prod="$(production_snapshot)" || fail "production_snapshot_failed"
  post_prod_sha="$(printf '%s' "$post_prod" | sha256sum | cut -d ' ' -f 1)"
  [[ "$post_prod_sha" == "$pre_prod_sha" ]] || fail "production_changed_during_run"
  post_qdrant="$(qdrant_snapshot)" || fail "qdrant_snapshot_failed"
  [[ "$post_qdrant" == "$pre_qdrant" ]] || fail "qdrant_changed_during_run"
  remove_owned_candidate || fail "candidate_cleanup_failed"
  [[ "$(printf '%s' "$(production_snapshot)" | sha256sum | cut -d ' ' -f 1)" == \
    "$pre_prod_sha" ]] || fail "production_changed_after_cleanup"

  completed="$RUN_DIR/run.completed"
  (set -o noclobber; {
    printf 'schema_version=pilot50-candidate-run-completed-v1\n'
    printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
    printf 'cases_sha256=%s\n' "$cases_sha"
    printf 'report_sha256=%s\n' "$report_sha"
    printf 'safe_result_sha256=%s\n' "$safe_sha"
    printf 'approval_id=%s\n' "$approval_id"
    printf 'rolling_24h_waiver_id=%s\n' "$waiver_id"
    printf 'rolling_24h_waiver_decision_id=%s\n' \
      "$COMPARISON_WAIVER_DECISION_ID"
    printf 'waived_reservation_sha256=%s\n' "$waived_reservation_sha"
    printf 'provider_residual_risk_ceiling_rub=%s\n' \
      "$COMPARISON_PROVIDER_RISK_CEILING_RUB"
    printf 'runner_projected_stop_limit_rub=%s\n' "$COST_CAP_RUB"
    printf 'quality_status=%s\n' "$quality_status"
    printf 'production_snapshot_sha256=%s\n' "$pre_prod_sha"
    printf 'qdrant_count=%s\n' "$pre_qdrant_count"
    printf 'qdrant_fingerprint_sha256=%s\n' "$pre_qdrant_sha"
    printf 'qdrant_seed_sha256=%s\n' "$pre_qdrant_seed_sha"
  } >"$completed") 2>/dev/null || fail "candidate_completion_marker_failed"
  chmod 0600 "$completed" || fail "candidate_completion_marker_mode_failed"
  cleanup_temp || fail "run_temp_cleanup_failed"

  printf 'pilot50_candidate_server_local=OK\n'
  printf 'pilot50_candidate_quality=%s\n' "$quality_status"
  printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
  printf 'cases_sha256=%s\n' "$cases_sha"
  printf 'report_sha256=%s\n' "$report_sha"
  printf 'safe_result_sha256=%s\n' "$safe_sha"
  printf '%s\n' "$validated_safe"
}

review_mode() {
  local approval_id cases_sha completed expected_approval_id expected_waiver_id
  local quality_status report_sha safe_result safe_sha safe_stdout started validated
  local waiver_id waived_reservation_sha
  load_common_state
  verify_source_snapshot || fail "source_snapshot_invalid"
  completed="$RUN_DIR/run.completed"
  started="$RUN_DIR/run.started"
  safe_result="$EVIDENCE_DIR/pilot50-safe-result.json"
  sudo test -f "$completed" || fail "candidate_completed_run_not_found"
  sudo test ! -L "$completed" || fail "candidate_completed_artifact_not_regular"
  sudo test -f "$started" || fail "candidate_started_run_not_found"
  sudo test ! -L "$started" || fail "candidate_completed_artifact_not_regular"
  sudo test -f "$safe_result" || fail "candidate_completed_artifact_missing"
  sudo test ! -L "$safe_result" || fail "candidate_completed_artifact_not_regular"
  cases_sha="$(receipt_value cases_sha256 "$completed")"
  report_sha="$(receipt_value report_sha256 "$completed")"
  safe_sha="$(receipt_value safe_result_sha256 "$completed")"
  quality_status="$(receipt_value quality_status "$completed")"
  approval_id="$(receipt_value approval_id "$completed")"
  waiver_id="$(receipt_value rolling_24h_waiver_id "$completed")"
  waived_reservation_sha="$(receipt_value waived_reservation_sha256 "$completed")"
  expected_approval_id="owner-chat-20260811-pilot50-v3-${EXPECTED_SHA}-cap30"
  expected_waiver_id="owner-chat-20260811-waive-rolling24h-v2-to-v3-${EXPECTED_SHA}-cap30"
  [[ "$(receipt_value schema_version "$completed")" == \
    "pilot50-candidate-run-completed-v1" ]] \
    || fail "candidate_completion_marker_invalid"
  [[ "$(receipt_value candidate_sha "$completed")" == "$EXPECTED_SHA" ]] \
    || fail "candidate_completion_marker_invalid"
  [[ "$approval_id" == "$expected_approval_id" && \
    "$waiver_id" == "$expected_waiver_id" && \
    "$waived_reservation_sha" =~ ^[0-9a-f]{64}$ && \
    "$(receipt_value rolling_24h_waiver_decision_id "$completed")" == \
      "$COMPARISON_WAIVER_DECISION_ID" && \
    "$(receipt_value provider_residual_risk_ceiling_rub "$completed")" == \
      "$COMPARISON_PROVIDER_RISK_CEILING_RUB" && \
    "$(receipt_value runner_projected_stop_limit_rub "$completed")" == \
      "$COST_CAP_RUB" ]] || fail "candidate_completion_marker_invalid"
  [[ "$(receipt_value schema_version "$started")" == \
      "pilot50-candidate-run-started-v1" && \
    "$(receipt_value candidate_sha "$started")" == "$EXPECTED_SHA" && \
    "$(receipt_value cases_sha256 "$started")" == "$cases_sha" && \
    "$(receipt_value approval_id "$started")" == "$approval_id" && \
    "$(receipt_value rolling_24h_waiver_id "$started")" == "$waiver_id" && \
    "$(receipt_value rolling_24h_waiver_decision_id "$started")" == \
      "$COMPARISON_WAIVER_DECISION_ID" && \
    "$(receipt_value waived_reservation_sha256 "$started")" == \
      "$waived_reservation_sha" && \
    "$(receipt_value provider_residual_risk_ceiling_rub "$started")" == \
      "$COMPARISON_PROVIDER_RISK_CEILING_RUB" && \
    "$(receipt_value runner_projected_stop_limit_rub "$started")" == \
      "$COST_CAP_RUB" && \
    "$(receipt_value cost_cap_rub "$started")" == "$COST_CAP_RUB" ]] \
    || fail "candidate_started_marker_invalid"
  [[ "$cases_sha" =~ ^[0-9a-f]{64}$ && "$report_sha" =~ ^[0-9a-f]{64}$ && \
    "$safe_sha" =~ ^[0-9a-f]{64}$ && \
    ( "$quality_status" == "GO" || "$quality_status" == "STOP" ) ]] \
    || fail "candidate_completion_marker_invalid"
  [[ "$(sudo sha256sum "$EVIDENCE_DIR/pilot50-cases.json" 2>/dev/null \
    | cut -d ' ' -f 1)" == "$cases_sha" ]] || fail "candidate_cases_changed"
  [[ "$(sudo sha256sum "$EVIDENCE_DIR/pilot50-ask-report.json" 2>/dev/null \
    | cut -d ' ' -f 1)" == "$report_sha" ]] || fail "candidate_report_changed"
  [[ "$(sudo sha256sum "$safe_result" 2>/dev/null | cut -d ' ' -f 1)" == \
    "$safe_sha" ]] || fail "candidate_safe_result_changed"
  if ! safe_stdout="$(sudo docker run --rm --pull never --network none \
    --user app --read-only --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --cap-drop ALL --security-opt no-new-privileges=true \
    -v "$SOURCE_DIR:/workspace:ro" -v "$EVIDENCE_DIR:/evidence:ro" \
    -w /workspace --entrypoint python \
    "rosmol-ai-bot-pilot50-candidate:$EXPECTED_SHA" \
    -m scripts.pilot50 show-safe --input /evidence/pilot50-safe-result.json \
    2>/dev/null)"; then
    fail "candidate_safe_output_failed"
  fi
  if ! validated="$(printf '%s' "$safe_stdout" \
    | validate_safe_stdout "$cases_sha" "$report_sha" "$approval_id" \
      "$waiver_id" "$waived_reservation_sha")"; then
    fail "candidate_safe_output_invalid"
  fi
  [[ "$(python3 -c \
    'import json,sys; print(json.load(sys.stdin)["quality_gate"]["status"])' \
    <<<"$validated" 2>/dev/null)" == "$quality_status" ]] \
    || fail "candidate_quality_status_mismatch"
  printf 'pilot50_candidate_review=OK\n'
  printf 'pilot50_candidate_quality=%s\n' "$quality_status"
  printf 'safe_result_sha256=%s\n' "$safe_sha"
  printf '%s\n' "$validated"
}

cleanup_mode() {
  require_base_commands
  if ! sudo docker inspect "$CANDIDATE_CONTAINER" >/dev/null 2>&1; then
    printf 'pilot50_candidate_cleanup=OK state=absent\n'
    return
  fi
  candidate_owned || fail "candidate_cleanup_identity_mismatch"
  remove_owned_candidate || fail "candidate_cleanup_failed"
  printf 'pilot50_candidate_cleanup=OK state=removed\n'
}

validate_invocation "$@"
case "$MODE" in
  preflight) preflight_mode ;;
  run) run_mode ;;
  review) review_mode ;;
  cleanup) cleanup_mode ;;
esac
