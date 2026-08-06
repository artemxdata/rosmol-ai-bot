#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly PHASE0_RUNTIME_SHA="7d244e4fdee21a36a609e6f1cd0012e198746376"
readonly PHASE0_CASES_SHA="aff198bbc98d07894a3e1676e3457891e3a38f674315051505b681641fe9d02d"
readonly PHASE0_MANIFEST_SHA="8cf9959aaf9caf8728b386214ebba826f7bb0eb349f27fd2737e2830eb353264"
readonly PHASE0_APPROVAL_ID="RAG-PHASE0-30-20260805"
readonly PHASE0_INPUT_DIR="/dev/shm/rosmol-phase0-30-20260805"
readonly PHASE0_BUILDER_SOURCE="/var/lib/rosmol/release-source/${PHASE0_RUNTIME_SHA}"
readonly PHASE0_ENV_FILE="/opt/rosmol-ai-bot/.env.production"
readonly PHASE0_RUNTIME_CONTAINER="rosmol-phase0-ml"
readonly PHASE0_EVIDENCE_DIR="/var/lib/rosmol/phase0/phase0-20260805/evidence"
readonly PHASE0_LEDGER_DIR="/var/lib/rosmol/phase0/phase0-20260805/cost-ledger"

fail() {
  printf 'phase0_server_local=FAIL reason=%s\n' "$1" >&2
  exit 1
}

command -v git >/dev/null 2>&1 || fail "git_missing"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum_missing"
command -v findmnt >/dev/null 2>&1 || fail "findmnt_missing"
command -v sudo >/dev/null 2>&1 || fail "sudo_missing"

RUNNER_SOURCE="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not_in_git_worktree"
RUNNER_SHA="$(git -C "$RUNNER_SOURCE" rev-parse HEAD 2>/dev/null)" || fail "runner_sha_unavailable"
[[ "$RUNNER_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "runner_sha_invalid"
[[ -z "$(git -C "$RUNNER_SOURCE" status --porcelain=v1 --untracked-files=all)" ]] \
  || fail "runner_source_not_clean"
[[ ! -e "$RUNNER_SOURCE/.env.production" ]] || fail "runner_source_contains_env"

sudo test -r "$PHASE0_INPUT_DIR/phase0-cases.json" || fail "cases_missing"
sudo test -r "$PHASE0_INPUT_DIR/phase0-manifest.json" || fail "manifest_missing"
[[ "$(sudo findmnt -n -o FSTYPE --target "$PHASE0_INPUT_DIR")" == "tmpfs" ]] \
  || fail "private_input_not_in_tmpfs"
[[ "$(sudo sha256sum "$PHASE0_INPUT_DIR/phase0-cases.json" | cut -d ' ' -f 1)" == "$PHASE0_CASES_SHA" ]] \
  || fail "cases_sha_mismatch"
[[ "$(sudo sha256sum "$PHASE0_INPUT_DIR/phase0-manifest.json" | cut -d ' ' -f 1)" == "$PHASE0_MANIFEST_SHA" ]] \
  || fail "manifest_sha_mismatch"

[[ "$(git -C "$PHASE0_BUILDER_SOURCE" rev-parse HEAD 2>/dev/null)" == "$PHASE0_RUNTIME_SHA" ]] \
  || fail "builder_source_sha_mismatch"
[[ -z "$(git -C "$PHASE0_BUILDER_SOURCE" status --porcelain=v1 --untracked-files=all)" ]] \
  || fail "builder_source_not_clean"
[[ ! -e "$PHASE0_BUILDER_SOURCE/.env.production" ]] || fail "builder_source_contains_env"
sudo test -r "$PHASE0_ENV_FILE" || fail "server_env_unreadable"

[[ "$(sudo docker inspect -f '{{.State.Running}}' "$PHASE0_RUNTIME_CONTAINER" 2>/dev/null)" == "true" ]] \
  || fail "phase0_runtime_not_running"
[[ "$(sudo docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$PHASE0_RUNTIME_CONTAINER")" == "$PHASE0_RUNTIME_SHA" ]] \
  || fail "phase0_runtime_image_sha_mismatch"
sudo docker exec "$PHASE0_RUNTIME_CONTAINER" python -c '
import json, urllib.request
p = json.load(urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=30))
assert p.get("status") == "ready"
assert p.get("release_git_sha") == "7d244e4fdee21a36a609e6f1cd0012e198746376"
assert p.get("checks") and all(value == "ok" for value in p["checks"].values())
' || fail "phase0_runtime_not_ready"
sudo docker exec "$PHASE0_RUNTIME_CONTAINER" sh -lc '
model="ai-sage/GigaChat3-10B-A1.8B"
test "$CLOUD_RU_MODEL" = "$model"
test "$CLOUD_RU_MODEL_SIMPLE" = "$model"
test "$CLOUD_RU_MODEL_COMPLEX" = "$model"
test "$CLOUD_RU_MODEL_ANALYZER" = "$model"
test "$CLOUD_RU_MODEL_JUDGE" = "$model"
case "$CLOUD_RU_MODEL_SIMPLE_INPUT_PRICE_RUB_PER_MILLION" in 12.2|12.20|12.200) ;; *) exit 1 ;; esac
case "$CLOUD_RU_MODEL_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION" in 12.2|12.20|12.200) ;; *) exit 1 ;; esac
case "$CLOUD_RU_MODEL_COMPLEX_INPUT_PRICE_RUB_PER_MILLION" in 12.2|12.20|12.200) ;; *) exit 1 ;; esac
case "$CLOUD_RU_MODEL_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION" in 12.2|12.20|12.200) ;; *) exit 1 ;; esac
' || fail "phase0_runtime_model_or_price_mismatch"
sudo docker exec "$PHASE0_RUNTIME_CONTAINER" sh -lc '
test "$APP_ENV" = "local"
test "$RUNTIME_ROLE" = "ml"
test "$HDE_TRANSPORT_ENABLED" = "false"
test -z "$HDE_BASE_URL"
test -z "$HDE_API_KEY"
test -z "$WEBHOOK_AUTH_TOKEN"
test -z "$ADMIN_AUTH_TOKEN"
test "$ADMIN_READ_ONLY" = "true"
test "$ADMIN_MUTATIONS_ENABLED" = "false"
test "$YONOTE_SYNC_ENABLED" = "false"
test -z "$YONOTE_API_TOKEN"
' || fail "phase0_runtime_isolation_mismatch"
[[ -z "$(sudo docker port "$PHASE0_RUNTIME_CONTAINER")" ]] \
  || fail "phase0_runtime_has_published_ports"

sudo install -d -m 0750 -o rosmolops -g rosmolops "$PHASE0_EVIDENCE_DIR"
sudo install -d -m 0700 -o 10001 -g 10001 "$PHASE0_LEDGER_DIR"
install -d -m 0750 "$RUNNER_SOURCE/data/private"
sudo install -d -m 0700 -o 10001 -g 10001 \
  "$PHASE0_INPUT_DIR/phase0-social-30-cost-ledger-v1"
[[ ! -e "$PHASE0_EVIDENCE_DIR/phase0-safe-metrics.json" ]] \
  || fail "safe_report_already_exists"
[[ -z "$(sudo find "$PHASE0_LEDGER_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] \
  || fail "cost_ledger_not_empty"

sudo chown -R 10001:10001 "$PHASE0_INPUT_DIR"
sudo chmod 0700 "$PHASE0_INPUT_DIR"

compose=(
  sudo env
  "RELEASE_GIT_SHA=$PHASE0_RUNTIME_SHA"
  "ACCEPTANCE_SOURCE_DIR=$RUNNER_SOURCE"
  "ACCEPTANCE_OUTPUT_DIR=$PHASE0_INPUT_DIR"
  "ACCEPTANCE_PROVENANCE_DIR=$PHASE0_BUILDER_SOURCE"
  "ACCEPTANCE_COST_LEDGER_DIR=$PHASE0_LEDGER_DIR"
  "PHASE0_RUNTIME_GIT_SHA=$PHASE0_RUNTIME_SHA"
  "PHASE0_RUNNER_SOURCE_DIR=$RUNNER_SOURCE"
  "PHASE0_BUILDER_SOURCE_DIR=$PHASE0_BUILDER_SOURCE"
  "PHASE0_PRIVATE_DIR=$PHASE0_INPUT_DIR"
  "PHASE0_COST_LEDGER_DIR=$PHASE0_LEDGER_DIR"
  docker compose
  --env-file "$PHASE0_ENV_FILE"
  --project-directory "$RUNNER_SOURCE"
  -f "$RUNNER_SOURCE/docker-compose.yml"
  -f "$RUNNER_SOURCE/docker-compose.ml.yml"
  -f "$RUNNER_SOURCE/docker-compose.prod.yml"
  -f "$RUNNER_SOURCE/docker-compose.acceptance.yml"
  --profile ml
  --profile phase0
)

"${compose[@]}" run --rm --no-deps phase0-acceptance \
  --cases /workspace/data/private/phase0-cases.json \
  --output /workspace/data/private/phase0-ask-report.json \
  --no-markdown \
  --target http://rosmol-phase0-ml:8000/ask \
  --concurrency 1 \
  --timeout 180 \
  --max-llm-cost-rub 200 \
  --high-cost-approval-id "$PHASE0_APPROVAL_ID" \
  --kb-seed /workspace/data/knowledge_base_seed.json \
  --bypass-cache \
  --allow-source-observed-diagnostic \
  --phase0-manifest /workspace/data/private/phase0-manifest.json \
  --expected-cases-file-sha256 "$PHASE0_CASES_SHA" \
  --expected-runtime-git-sha "$PHASE0_RUNTIME_SHA" \
  --require-complete-traces \
  --phase0-server-local \
  --phase0-builder-source /builder-source

sudo docker run --rm \
  --network none \
  --read-only \
  --user app \
  --workdir /workspace \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777 \
  --volume "$RUNNER_SOURCE:/workspace:ro" \
  --volume "$PHASE0_INPUT_DIR:/workspace/data/private" \
  --entrypoint python \
  "rosmol-ai-bot-ml:$PHASE0_RUNTIME_SHA" \
  scripts/build_phase0_safe_metrics.py \
  --manifest /workspace/data/private/phase0-manifest.json \
  --ask-report /workspace/data/private/phase0-ask-report.json \
  --output /workspace/data/private/phase0-safe-metrics.json

sudo install -m 0600 -o rosmolops -g rosmolops \
  "$PHASE0_INPUT_DIR/phase0-safe-metrics.json" \
  "$PHASE0_EVIDENCE_DIR/phase0-safe-metrics.json"
sudo rm -f \
  "$PHASE0_INPUT_DIR/phase0-cases.json" \
  "$PHASE0_INPUT_DIR/phase0-manifest.json" \
  "$PHASE0_INPUT_DIR/phase0-ask-report.json" \
  "$PHASE0_INPUT_DIR/phase0-safe-metrics.json"

printf 'phase0_server_local=OK\n'
printf 'runner_sha=%s\n' "$RUNNER_SHA"
printf 'runtime_sha=%s\n' "$PHASE0_RUNTIME_SHA"
printf 'safe_report=%s\n' "$PHASE0_EVIDENCE_DIR/phase0-safe-metrics.json"
sha256sum "$PHASE0_EVIDENCE_DIR/phase0-safe-metrics.json"
python3 -m json.tool "$PHASE0_EVIDENCE_DIR/phase0-safe-metrics.json"
