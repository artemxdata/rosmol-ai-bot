#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
exec 2>/dev/null

readonly SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"
readonly PROD_CONTAINER="rosmol-app-ml"
readonly EXPECTED_PRODUCTION_SHA="c38f0e055630fae2af50720fae81acee20ff4f6a"
readonly MANIFEST_REL="eval/cases/pilot50_balanced_v5.json"
readonly EXPECTED_MANIFEST_SHA256="12747d62190cc5e70d70490e9a649d91596ec69a316b5c2de3843ac3df6f85b4"
readonly EXPECTED_CASES_SHA256="9d53114722191330214f5917ee3baf4ccfcf4eb644be34a0253c60531b225529"
readonly EXPECTED_QDRANT_COUNT="2152"
readonly EXPECTED_QDRANT_FINGERPRINT_SHA256="f753b69665f216039b944546886f611410107e1344e52b159ab3f221b60aefa5"
readonly EXPECTED_QDRANT_SEED_SHA256="aead5e930c513d9d5aeaacd3f3d4b8ce99fab536434343e7fcd6e9917de93e8a"
readonly MAX_DIAGNOSTIC_STDOUT_BYTES="$((64 * 1024 + 1))"
readonly MIN_MEM_AVAILABLE_KIB="$((7 * 1024 * 1024))"
readonly MIN_SWAP_FREE_KIB="$((6 * 1024 * 1024))"

EXPECTED_SHA="${1:-}"
TOOLING_ROOT=""
TEMP_ROOT=""
SOURCE_DIR=""
RAW_STDOUT=""
PROXY_ENV_FILE=""
PROD_IMAGE_ID=""
DATA_NETWORK=""
HF_CACHE_VOLUME=""
TORCH_CACHE_VOLUME=""
MODEL_CACHE_VOLUME=""
DIAGNOSTIC_NETWORK=""
PROXY_CONTAINER=""
DIAGNOSTIC_CONTAINER=""
PROXY_ID=""
DIAGNOSTIC_ID=""

fail() {
  printf 'fact_core_qdrant_diagnostic=FAIL reason=%s\n' "$1"
  exit 1
}

owned_container() {
  local container="$1" purpose="$2" candidate label_purpose
  candidate="$(sudo docker inspect \
    -f '{{index .Config.Labels "com.rosmol.candidate-git-sha"}}' \
    "$container" 2>/dev/null)" || return 1
  label_purpose="$(sudo docker inspect \
    -f '{{index .Config.Labels "com.rosmol.purpose"}}' \
    "$container" 2>/dev/null)" || return 1
  [[ "$candidate" == "$EXPECTED_SHA" && "$label_purpose" == "$purpose" ]]
}

owned_network() {
  local candidate purpose
  candidate="$(sudo docker network inspect \
    -f '{{index .Labels "com.rosmol.candidate-git-sha"}}' \
    "$DIAGNOSTIC_NETWORK" 2>/dev/null)" || return 1
  purpose="$(sudo docker network inspect \
    -f '{{index .Labels "com.rosmol.purpose"}}' \
    "$DIAGNOSTIC_NETWORK" 2>/dev/null)" || return 1
  [[ "$candidate" == "$EXPECTED_SHA" && \
    "$purpose" == "fact-core-qdrant-diagnostic" ]]
}

cleanup_on_exit() {
  local cleanup_failed=0 exit_code="$?" resolved=""
  trap - EXIT
  if [[ -n "$DIAGNOSTIC_ID" ]]; then
    if owned_container "$DIAGNOSTIC_ID" "fact-core-qdrant-diagnostic"; then
      sudo docker rm -f "$DIAGNOSTIC_ID" >/dev/null 2>&1 \
        || cleanup_failed=1
    else
      cleanup_failed=1
    fi
    DIAGNOSTIC_ID=""
  fi
  if [[ -n "$PROXY_ID" ]]; then
    if owned_container "$PROXY_ID" "qdrant-readonly-proxy"; then
      sudo docker rm -f "$PROXY_ID" >/dev/null 2>&1 || cleanup_failed=1
    else
      cleanup_failed=1
    fi
    PROXY_ID=""
  fi
  if [[ -n "$DIAGNOSTIC_NETWORK" ]] && \
    sudo docker network inspect "$DIAGNOSTIC_NETWORK" >/dev/null 2>&1; then
    if owned_network; then
      sudo docker network rm "$DIAGNOSTIC_NETWORK" >/dev/null 2>&1 \
        || cleanup_failed=1
    else
      cleanup_failed=1
    fi
  fi
  if [[ -n "$TEMP_ROOT" ]]; then
    resolved="$(readlink -f -- "$TEMP_ROOT" 2>/dev/null || true)"
    if [[ "$TEMP_ROOT" == /run/fact-core-qdrant-diagnostic.* && \
      "$resolved" == "$TEMP_ROOT" && "$resolved" != "/run" ]]; then
      sudo rm -rf --one-file-system -- "$TEMP_ROOT" >/dev/null 2>&1 \
        || cleanup_failed=1
    else
      cleanup_failed=1
    fi
  fi
  if [[ "$cleanup_failed" -ne 0 ]]; then
    printf 'fact_core_qdrant_diagnostic=FAIL reason=cleanup_failed\n'
    exit 1
  fi
  exit "$exit_code"
}

trap cleanup_on_exit EXIT
trap 'fail unexpected_error' ERR
trap 'exit 130' INT TERM

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "$2"
}

validate_invocation() {
  [[ "$#" -eq 1 ]] || fail "usage"
  [[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "candidate_sha_invalid"
  [[ "$EXPECTED_SHA" != "0000000000000000000000000000000000000000" && \
    "$EXPECTED_SHA" != "$EXPECTED_PRODUCTION_SHA" ]] \
    || fail "candidate_sha_invalid"
  local short_sha="${EXPECTED_SHA:0:12}"
  DIAGNOSTIC_NETWORK="rosmol-fact-core-diag-${short_sha}"
  PROXY_CONTAINER="rosmol-qdrant-readonly-${short_sha}"
  DIAGNOSTIC_CONTAINER="rosmol-fact-core-diag-${short_sha}"
}

network_for_role() {
  local candidate label network_name="" role="$1"
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    label="$(sudo docker network inspect \
      -f '{{index .Labels "com.docker.compose.network"}}' \
      "$candidate" 2>/dev/null)" || continue
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

load_state() {
  for command in \
    git sudo docker python3 sha256sum awk grep find sort tar readlink \
    mktemp mkdir chmod id sleep; do
    require_command "$command" "${command}_missing"
  done
  sudo -v >/dev/null 2>&1 || fail "sudo_auth_failed"
  TOOLING_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || fail "not_in_git_worktree"
  [[ "$TOOLING_ROOT" == "$SERVER_PROJECT_DIR" ]] || fail "wrong_server_project"
  [[ "$(readlink -f -- "$TOOLING_ROOT" 2>/dev/null)" == "$TOOLING_ROOT" ]] \
    || fail "server_project_not_regular"
  [[ "$(git -C "$TOOLING_ROOT" rev-parse HEAD 2>/dev/null)" == \
    "$EXPECTED_SHA" ]] || fail "candidate_sha_mismatch"
  if git -C "$TOOLING_ROOT" symbolic-ref -q HEAD >/dev/null 2>&1; then
    fail "candidate_checkout_not_detached"
  fi
  [[ -z "$(git -C "$TOOLING_ROOT" status \
    --porcelain=v1 --untracked-files=all 2>/dev/null)" ]] \
    || fail "candidate_source_not_clean"
  for path in \
    "scripts/check_fact_pipeline_qdrant.py" \
    "scripts/qdrant_readonly_proxy.py" \
    "scripts/run_fact_core_qdrant_diagnostic_server_local.sh" \
    "scripts/pilot50.py" \
    "eval/run_ask.py" \
    "src/graph/nodes/respond.py" \
    "$MANIFEST_REL"; do
    git -C "$TOOLING_ROOT" ls-files --error-unmatch "$path" \
      >/dev/null 2>&1 || fail "diagnostic_source_not_tracked"
  done
  if git -C "$TOOLING_ROOT" ls-files -s 2>/dev/null \
    | awk '$1 == "120000" { found=1 } END { exit(found ? 0 : 1) }' \
      >/dev/null 2>&1; then
    fail "candidate_source_has_symlink"
  fi

  [[ "$(sudo docker inspect -f '{{.State.Running}}' \
    "$PROD_CONTAINER" 2>/dev/null)" == "true" ]] || fail "production_not_running"
  [[ "$(sudo docker inspect \
    -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$PROD_CONTAINER" 2>/dev/null)" == "healthy" ]] \
    || fail "production_not_healthy"
  [[ "$(sudo docker inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$PROD_CONTAINER" 2>/dev/null)" == "$EXPECTED_PRODUCTION_SHA" ]] \
    || fail "production_sha_mismatch"
  PROD_IMAGE_ID="$(sudo docker inspect -f '{{.Image}}' \
    "$PROD_CONTAINER" 2>/dev/null)" || fail "production_image_unavailable"
  [[ "$PROD_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail "production_image_invalid"
  [[ "$(sudo docker image inspect \
    -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$PROD_IMAGE_ID" 2>/dev/null)" == "$EXPECTED_PRODUCTION_SHA" ]] \
    || fail "production_image_sha_mismatch"
  [[ "$(sudo docker image inspect -f '{{.Config.User}}' \
    "$PROD_IMAGE_ID" 2>/dev/null)" == "app" ]] || fail "production_image_user_invalid"

  git -C "$TOOLING_ROOT" cat-file -e \
    "${EXPECTED_PRODUCTION_SHA}^{commit}" 2>/dev/null \
    || fail "production_source_commit_missing"
  git -C "$TOOLING_ROOT" diff --quiet "$EXPECTED_PRODUCTION_SHA" \
    "$EXPECTED_SHA" -- Dockerfile pyproject.toml requirements \
    || fail "candidate_dependency_contract_changed"

  DATA_NETWORK="$(network_for_role data)" || fail "data_network_unavailable"
  [[ "$(sudo docker network inspect -f '{{.Internal}}' \
    "$DATA_NETWORK" 2>/dev/null)" == "true" ]] || fail "data_network_not_internal"
  verify_network_service "$DATA_NETWORK" qdrant || fail "qdrant_unavailable"
  HF_CACHE_VOLUME="$(mount_volume_for_target /home/app/.cache/huggingface)" \
    || fail "hf_cache_volume_unavailable"
  TORCH_CACHE_VOLUME="$(mount_volume_for_target /home/app/.cache/torch)" \
    || fail "torch_cache_volume_unavailable"
  MODEL_CACHE_VOLUME="$(mount_volume_for_target /opt/models)" \
    || fail "model_cache_volume_unavailable"
  for value in \
    "$DATA_NETWORK" "$HF_CACHE_VOLUME" "$TORCH_CACHE_VOLUME" \
    "$MODEL_CACHE_VOLUME" "$DIAGNOSTIC_NETWORK" \
    "$PROXY_CONTAINER" "$DIAGNOSTIC_CONTAINER"; do
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] \
      || fail "docker_resource_name_invalid"
  done
  ! sudo docker inspect "$PROXY_CONTAINER" >/dev/null 2>&1 \
    || fail "proxy_container_exists"
  ! sudo docker inspect "$DIAGNOSTIC_CONTAINER" >/dev/null 2>&1 \
    || fail "diagnostic_container_exists"
  ! sudo docker network inspect "$DIAGNOSTIC_NETWORK" >/dev/null 2>&1 \
    || fail "diagnostic_network_exists"
}

create_tooling_snapshot() {
  local actual_paths_sha expected_paths_sha owner_gid owner_uid
  owner_uid="$(id -u)"
  owner_gid="$(id -g)"
  TEMP_ROOT="$(sudo mktemp -d /run/fact-core-qdrant-diagnostic.XXXXXX \
    2>/dev/null)" || fail "temp_create_failed"
  [[ "$TEMP_ROOT" == /run/fact-core-qdrant-diagnostic.* ]] \
    || fail "temp_create_failed"
  sudo chown "$owner_uid:$owner_gid" "$TEMP_ROOT" \
    || fail "temp_permissions_failed"
  chmod 0755 "$TEMP_ROOT" || fail "temp_permissions_failed"
  SOURCE_DIR="$TEMP_ROOT/source"
  mkdir -m 0755 "$SOURCE_DIR" || fail "temp_create_failed"
  git -C "$TOOLING_ROOT" archive --format=tar "$EXPECTED_SHA" 2>/dev/null \
    | tar --no-same-owner -xf - -C "$SOURCE_DIR" >/dev/null 2>&1 \
    || fail "tooling_snapshot_create_failed"
  find "$SOURCE_DIR" -type d -exec chmod 0555 {} + >/dev/null 2>&1 \
    || fail "tooling_snapshot_permissions_failed"
  find "$SOURCE_DIR" -type f -exec chmod 0444 {} + >/dev/null 2>&1 \
    || fail "tooling_snapshot_permissions_failed"
  [[ ! -e "$SOURCE_DIR/.git" && ! -e "$SOURCE_DIR/.env" && \
    ! -e "$SOURCE_DIR/.env.production" && ! -e "$SOURCE_DIR/data/private" ]] \
    || fail "tooling_snapshot_contains_private_state"
  git -c core.fileMode=false -c "safe.directory=$TOOLING_ROOT" \
    --git-dir="$TOOLING_ROOT/.git" --work-tree="$SOURCE_DIR" \
    diff --quiet "$EXPECTED_SHA" -- >/dev/null 2>&1 \
    || fail "tooling_snapshot_invalid"
  expected_paths_sha="$(git -C "$TOOLING_ROOT" ls-tree -r -t -z \
    --name-only "$EXPECTED_SHA" | sha256sum | awk '{print $1}')" \
    || fail "tooling_snapshot_invalid"
  actual_paths_sha="$(find "$SOURCE_DIR" -mindepth 1 -printf '%P\0' \
    | LC_ALL=C sort -z | sha256sum | awk '{print $1}')" \
    || fail "tooling_snapshot_invalid"
  [[ "$actual_paths_sha" == "$expected_paths_sha" ]] \
    || fail "tooling_snapshot_invalid"
  RAW_STDOUT="$TEMP_ROOT/diagnostic.stdout"
  : >"$RAW_STDOUT" || fail "temp_create_failed"
  chmod 0600 "$RAW_STDOUT" || fail "temp_permissions_failed"
  PROXY_ENV_FILE="$TEMP_ROOT/proxy.env"
  : >"$PROXY_ENV_FILE" || fail "temp_create_failed"
  chmod 0600 "$PROXY_ENV_FILE" || fail "temp_permissions_failed"
}

production_snapshot() {
  sudo docker inspect -f \
    '{{.Id}}|{{.Image}}|{{.RestartCount}}|{{.State.OOMKilled}}|{{.State.Running}}|{{.State.StartedAt}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$PROD_CONTAINER" 2>/dev/null
}

capacity_snapshot() {
  python3 - "$MIN_MEM_AVAILABLE_KIB" "$MIN_SWAP_FREE_KIB" 2>/dev/null <<'PY'
import os
import sys

min_mem, min_swap = (int(value) for value in sys.argv[1:])
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
status = (
    "GO"
    if mem_available >= min_mem
    and swap_free >= min_swap
    and load1 <= 0.75 * nproc
    else "STOP"
)
print(
    "capacity_status=" + status,
    f"mem_available_mib={mem_available // 1024}",
    f"swap_free_mib={swap_free // 1024}",
    f"load1={load1:.2f}",
    f"nproc={nproc}",
    sep="\n",
)
PY
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
    expected.append({"id": str(uuid5(NAMESPACE_URL, record.chunk_id)), "payload": payload})

count = int(post(f"/collections/{collection}/points/count", {"exact": True})["count"])
rows = []
offset = None
while True:
    request_payload = {"limit": 256, "with_payload": True, "with_vector": True}
    if offset is not None:
        request_payload["offset"] = offset
    result = post(f"/collections/{collection}/points/scroll", request_payload)
    for point in result.get("points") or []:
        point_payload = point.get("payload") or {}
        chunk_id = str(point_payload.get("chunk_id") or "")
        assert chunk_id
        vector = point.get("vector")
        assert isinstance(vector, dict) and vector
        rows.append({"id": str(point.get("id")), "payload": point_payload, "vector": vector})
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
        {"id": row["id"], "chunk_id": row["payload"]["chunk_id"], "vector": row["vector"]}
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

create_proxy_env() {
  sudo docker inspect "$PROD_CONTAINER" 2>/dev/null \
    | python3 /dev/fd/3 "$PROXY_ENV_FILE" 3<<'PY' >/dev/null 2>&1
import json
import os
import stat
import sys
from pathlib import Path

payload = json.load(sys.stdin)
assert isinstance(payload, list) and len(payload) == 1
entries = {}
for item in payload[0]["Config"]["Env"]:
    key, separator, value = item.partition("=")
    assert separator and key not in entries
    entries[key] = value
assert entries.get("QDRANT_URL") == "http://qdrant:6333"
assert entries.get("QDRANT_KNOWLEDGE_COLLECTION", "knowledge_base") == "knowledge_base"
api_key = entries.get("QDRANT_API_KEY", "")
assert api_key and len(api_key) <= 4096 and not any(char in api_key for char in "\r\n\0")
path = Path(sys.argv[1])
metadata = path.lstat()
assert stat.S_ISREG(metadata.st_mode) and not path.is_symlink()
assert metadata.st_nlink == 1 and stat.S_IMODE(metadata.st_mode) == 0o600
flags = os.O_WRONLY | os.O_TRUNC
if hasattr(os, "O_CLOEXEC"):
    flags |= os.O_CLOEXEC
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(path, flags)
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
    handle.write("QDRANT_UPSTREAM_URL=http://qdrant:6333\n")
    handle.write(f"QDRANT_UPSTREAM_API_KEY={api_key}\n")
PY
}

create_network_and_proxy() {
  sudo docker network create --internal \
    --label com.rosmol.purpose=fact-core-qdrant-diagnostic \
    --label "com.rosmol.candidate-git-sha=$EXPECTED_SHA" \
    "$DIAGNOSTIC_NETWORK" >/dev/null || fail "diagnostic_network_create_failed"
  owned_network || fail "diagnostic_network_identity_invalid"
  PROXY_ID="$(sudo docker create --name "$PROXY_CONTAINER" --pull never \
    --label com.rosmol.purpose=qdrant-readonly-proxy \
    --label "com.rosmol.candidate-git-sha=$EXPECTED_SHA" \
    --user app --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777 \
    --cap-drop ALL --security-opt no-new-privileges=true \
    --pids-limit 64 --memory 256m --cpus 0.5 \
    --network "$DIAGNOSTIC_NETWORK" --network-alias qdrant-readonly \
    --env-file "$PROXY_ENV_FILE" \
    --mount "type=bind,src=$SOURCE_DIR,dst=/workspace,readonly" \
    --workdir /workspace --entrypoint python "$PROD_IMAGE_ID" \
    -E -B /workspace/scripts/qdrant_readonly_proxy.py 2>/dev/null)" \
    || fail "proxy_container_create_failed"
  [[ "$PROXY_ID" =~ ^[0-9a-f]{64}$ ]] || fail "proxy_container_identity_invalid"
  owned_container "$PROXY_ID" "qdrant-readonly-proxy" \
    || fail "proxy_container_identity_invalid"
  sudo docker network connect "$DATA_NETWORK" "$PROXY_ID" >/dev/null \
    || fail "proxy_data_network_connect_failed"
  sudo docker start "$PROXY_ID" >/dev/null || fail "proxy_start_failed"
  local attempt
  for attempt in {1..30}; do
    if sudo docker exec "$PROXY_ID" python -E -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6333/healthz', timeout=2).read()" \
      >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  fail "proxy_not_ready"
}

create_diagnostic_container() {
  DIAGNOSTIC_ID="$(sudo docker create --name "$DIAGNOSTIC_CONTAINER" --pull never \
    --label com.rosmol.purpose=fact-core-qdrant-diagnostic \
    --label "com.rosmol.candidate-git-sha=$EXPECTED_SHA" \
    --user app --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=1g,mode=1777 \
    --cap-drop ALL --security-opt no-new-privileges=true \
    --pids-limit 256 --memory 6g --cpus 2 \
    --network "$DIAGNOSTIC_NETWORK" \
    -e QDRANT_URL=http://qdrant-readonly:6333 \
    -e QDRANT_API_KEY=read-only-proxy \
    -e QDRANT_KNOWLEDGE_COLLECTION=knowledge_base \
    -e HOME=/home/app \
    -e HF_HOME=/home/app/.cache/huggingface \
    -e TORCH_HOME=/home/app/.cache/torch \
    -e BGE_M3_MODEL_PATH=/opt/models/bge-m3 \
    -e BGE_RERANKER_MODEL_PATH=/opt/models/bge-reranker-v2-m3 \
    -e HF_MODEL_LOCK_PATH=/workspace/deploy/huggingface_models.lock.json \
    -e HF_MODEL_VERIFICATION_RECEIPT=/opt/models/.verified-models.json \
    -e HF_HUB_OFFLINE=1 -e HF_DATASETS_OFFLINE=1 \
    -e TRANSFORMERS_OFFLINE=1 -e HF_HUB_DISABLE_TELEMETRY=1 \
    -e TRANSFORMERS_VERBOSITY=error -e TOKENIZERS_PARALLELISM=false \
    -e HTTP_PROXY= -e HTTPS_PROXY= -e ALL_PROXY= \
    -e NO_PROXY=127.0.0.1,localhost,qdrant-readonly \
    --mount "type=bind,src=$SOURCE_DIR,dst=/workspace,readonly" \
    --mount "type=volume,src=$HF_CACHE_VOLUME,dst=/home/app/.cache/huggingface,readonly" \
    --mount "type=volume,src=$TORCH_CACHE_VOLUME,dst=/home/app/.cache/torch,readonly" \
    --mount "type=volume,src=$MODEL_CACHE_VOLUME,dst=/opt/models,readonly" \
    --workdir /workspace --entrypoint python "$PROD_IMAGE_ID" \
    -E -B /workspace/scripts/check_fact_pipeline_qdrant.py \
    --expected-candidate-sha "$EXPECTED_SHA" \
    --expected-manifest-sha256 "$EXPECTED_MANIFEST_SHA256" \
    --expected-cases-sha256 "$EXPECTED_CASES_SHA256" \
    --manifest "/workspace/$MANIFEST_REL" 2>/dev/null)" \
    || fail "diagnostic_container_create_failed"
  [[ "$DIAGNOSTIC_ID" =~ ^[0-9a-f]{64}$ ]] \
    || fail "diagnostic_container_identity_invalid"
  owned_container "$DIAGNOSTIC_ID" "fact-core-qdrant-diagnostic" \
    || fail "diagnostic_container_identity_invalid"
}

validate_container_boundaries() {
  sudo docker inspect "$PROXY_ID" "$DIAGNOSTIC_ID" 2>/dev/null \
    | python3 /dev/fd/3 "$PROXY_ID" "$DIAGNOSTIC_ID" \
      "$PROD_IMAGE_ID" "$DIAGNOSTIC_NETWORK" "$DATA_NETWORK" \
      "$SOURCE_DIR" "$HF_CACHE_VOLUME" "$TORCH_CACHE_VOLUME" \
      "$MODEL_CACHE_VOLUME" 3<<'PY' >/dev/null 2>&1
import json
import sys

(
    proxy_id,
    diagnostic_id,
    image_id,
    diagnostic_network,
    data_network,
    source_dir,
    hf_volume,
    torch_volume,
    model_volume,
) = sys.argv[1:]
rows = {row["Id"]: row for row in json.load(sys.stdin)}
assert set(rows) == {proxy_id, diagnostic_id}
proxy = rows[proxy_id]
diagnostic = rows[diagnostic_id]

def no_new_privileges_enabled(values):
    return values in (
        ["no-new-privileges"],
        ["no-new-privileges=true"],
        ["no-new-privileges:true"],
    )

for row in (proxy, diagnostic):
    assert row["Image"] == image_id
    assert row["Config"]["User"] == "app"
    assert row["HostConfig"]["ReadonlyRootfs"] is True
    assert set(row["HostConfig"]["CapDrop"]) == {"ALL"}
    assert no_new_privileges_enabled(row["HostConfig"]["SecurityOpt"])
    env = dict(item.split("=", 1) for item in row["Config"]["Env"])
    for forbidden in (
        "POSTGRES_DSN", "REDIS_URL", "CLOUD_RU_API_KEY", "HDE_API_KEY",
        "VK_API_TOKEN", "VK_GROUP_TOKEN", "YONOTE_API_TOKEN",
    ):
        assert forbidden not in env
proxy_networks = set(proxy["NetworkSettings"]["Networks"])
diagnostic_networks = set(diagnostic["NetworkSettings"]["Networks"])
assert proxy_networks == {diagnostic_network, data_network}
assert diagnostic_networks == {diagnostic_network}
proxy_mounts = {(item["Destination"], item["RW"]): item for item in proxy["Mounts"]}
assert ("/workspace", False) in proxy_mounts
assert proxy_mounts[("/workspace", False)]["Source"] == source_dir
diagnostic_mounts = {
    (item["Destination"], item["RW"]): item for item in diagnostic["Mounts"]
}
assert diagnostic_mounts[("/workspace", False)]["Source"] == source_dir
assert diagnostic_mounts[("/home/app/.cache/huggingface", False)]["Name"] == hf_volume
assert diagnostic_mounts[("/home/app/.cache/torch", False)]["Name"] == torch_volume
assert diagnostic_mounts[("/opt/models", False)]["Name"] == model_volume
diagnostic_env = dict(item.split("=", 1) for item in diagnostic["Config"]["Env"])
assert diagnostic_env["QDRANT_URL"] == "http://qdrant-readonly:6333"
assert diagnostic_env["QDRANT_API_KEY"] == "read-only-proxy"
assert "QDRANT_UPSTREAM_API_KEY" not in diagnostic_env
PY
}

validate_diagnostic_stdout() {
  python3 -E /dev/fd/3 "$1" "$MAX_DIAGNOSTIC_STDOUT_BYTES" \
    "$EXPECTED_SHA" "$EXPECTED_MANIFEST_SHA256" "$EXPECTED_CASES_SHA256" \
    3<<'PY' 2>/dev/null
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
maximum = int(sys.argv[2])
candidate_sha, manifest_sha, cases_sha = sys.argv[3:]
raw = path.read_bytes()
assert 0 < len(raw) <= maximum
assert raw.isascii() and raw.endswith(b"\n") and raw.count(b"\n") == 1
body = raw[:-1]
payload = json.loads(body.decode("ascii"))
canonical = json.dumps(
    payload,
    ensure_ascii=True,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode("ascii")
assert body == canonical
assert type(payload) is dict
assert set(payload) == {
    "schema_version", "classification", "disclaimer", "candidate_sha",
    "dataset_id", "manifest_sha256", "cases_sha256", "minimum_passed",
    "counts", "failures", "status",
}
assert payload["schema_version"] == (
    "fact-core-qdrant-postprocess-calibration-v1"
)
assert payload["classification"] == "calibration_only"
assert payload["disclaimer"] == (
    "Mechanical first-turn regression calibration; not an independent holdout, "
    "human product verdict, or production traffic conversion."
)
assert payload["candidate_sha"] == candidate_sha
assert payload["dataset_id"] == "pilot50_balanced_v5"
assert payload["manifest_sha256"] == manifest_sha
assert payload["cases_sha256"] == cases_sha
assert payload["minimum_passed"] == 49
counts = payload["counts"]
assert type(counts) is dict and set(counts) == {
    "total", "passed", "typical_passed", "atypical_passed", "no_operator",
    "typical_no_operator", "atypical_no_operator", "retrieval_complete",
    "citation_complete", "llm_calls",
}
assert all(type(value) is int and 0 <= value <= 50 for value in counts.values())
assert counts["total"] == 50
assert counts["passed"] == counts["typical_passed"] + counts["atypical_passed"]
assert counts["no_operator"] == (
    counts["typical_no_operator"] + counts["atypical_no_operator"]
)
failures = payload["failures"]
assert type(failures) is list and len(failures) == 50 - counts["passed"]
ordinals = []
for row in failures:
    assert type(row) is dict and set(row) == {"ordinal", "reasons"}
    assert type(row["ordinal"]) is int and 1 <= row["ordinal"] <= 50
    ordinals.append(row["ordinal"])
    reasons = row["reasons"]
    assert type(reasons) is list and reasons == sorted(set(reasons))
    assert all(
        type(reason) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason)
        for reason in reasons
    )
assert ordinals == sorted(set(ordinals))
expected_status = (
    "GO"
    if counts["passed"] >= 49
    and counts["retrieval_complete"] == 50
    and counts["llm_calls"] == 0
    else "STOP"
)
assert payload["status"] == expected_status
sys.stdout.write(canonical.decode("ascii"))
PY
}

main() {
  local capacity
  local post_prod post_qdrant pre_prod pre_prod_sha pre_qdrant
  local qdrant_count qdrant_fingerprint qdrant_seed status validated_stdout
  validate_invocation "$@"
  load_state
  capacity="$(capacity_snapshot)" || fail "capacity_snapshot_failed"
  if ! grep -Fxq 'capacity_status=GO' <<<"$capacity"; then
    printf 'fact_core_qdrant_diagnostic=STOP reason=capacity\n'
    printf '%s\n' "$capacity"
    exit 10
  fi
  create_tooling_snapshot
  pre_prod="$(production_snapshot)" || fail "production_snapshot_failed"
  pre_prod_sha="$(printf '%s' "$pre_prod" | sha256sum | awk '{print $1}')" \
    || fail "production_snapshot_failed"
  pre_qdrant="$(qdrant_snapshot)" || fail "qdrant_snapshot_failed"
  IFS='|' read -r qdrant_count qdrant_fingerprint qdrant_seed <<<"$pre_qdrant"
  [[ "$qdrant_count" == "$EXPECTED_QDRANT_COUNT" && \
    "$qdrant_fingerprint" == "$EXPECTED_QDRANT_FINGERPRINT_SHA256" && \
    "$qdrant_seed" == "$EXPECTED_QDRANT_SEED_SHA256" ]] \
    || fail "qdrant_baseline_mismatch"
  create_proxy_env || fail "proxy_env_create_failed"
  create_network_and_proxy
  create_diagnostic_container
  validate_container_boundaries || fail "container_boundary_invalid"
  if ! sudo docker start -a "$DIAGNOSTIC_ID" >"$RAW_STDOUT" 2>/dev/null; then
    fail "diagnostic_execution_failed"
  fi
  validated_stdout="$(validate_diagnostic_stdout "$RAW_STDOUT")" \
    || fail "diagnostic_output_invalid"
  post_prod="$(production_snapshot)" || fail "production_snapshot_failed"
  [[ "$post_prod" == "$pre_prod" ]] || fail "production_changed"
  post_qdrant="$(qdrant_snapshot)" || fail "qdrant_snapshot_failed"
  [[ "$post_qdrant" == "$pre_qdrant" ]] || fail "qdrant_changed"
  status="$(python3 -E -c 'import json,sys; print(json.load(sys.stdin)["status"])' \
    <<<"$validated_stdout")" || fail "diagnostic_output_invalid"
  printf 'fact_core_qdrant_diagnostic=OK\n'
  printf 'fact_core_qdrant_calibration_status=%s\n' "$status"
  printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
  printf 'production_runtime_sha=%s\n' "$EXPECTED_PRODUCTION_SHA"
  printf 'production_snapshot_sha256=%s\n' "$pre_prod_sha"
  printf 'qdrant_count=%s\n' "$qdrant_count"
  printf 'qdrant_fingerprint_sha256=%s\n' "$qdrant_fingerprint"
  printf 'qdrant_seed_sha256=%s\n' "$qdrant_seed"
  printf 'manifest_sha256=%s\n' "$EXPECTED_MANIFEST_SHA256"
  printf 'cases_sha256=%s\n' "$EXPECTED_CASES_SHA256"
  printf '%s\n' "$capacity"
  printf '%s\n' "$validated_stdout"
  [[ "$status" == "GO" ]] || exit 10
}

main "$@"
