#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly PROJECT_DIR="/opt/rosmol-ai-bot"
readonly APP_CONTAINER="rosmol-app"
readonly ADMIN_CONTAINER="rosmol-app-ml"
readonly CHANNELS_DISABLED_ATTESTATION="HDE_VK_DISABLED"
readonly PYTHON_HELPER="scripts/admin_kb_acceptance.py"

fail() {
  printf 'admin_kb_acceptance_server_local=FAIL reason=%s\n' "$1" >&2
  exit 1
}

[[ "$#" -eq 2 ]] || fail "usage"
readonly EXPECTED_SHA="$1"
readonly OWNER_CHANNELS_ATTESTATION="$2"

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] \
  && [[ "$EXPECTED_SHA" != "0000000000000000000000000000000000000000" ]] \
  || fail "candidate_sha_invalid"
[[ "$OWNER_CHANNELS_ATTESTATION" == "$CHANNELS_DISABLED_ATTESTATION" ]] \
  || fail "channels_disabled_attestation_required"

cd "$PROJECT_DIR" || fail "project_dir_unavailable"
[[ "$(git rev-parse HEAD 2>/dev/null)" == "$EXPECTED_SHA" ]] \
  || fail "candidate_sha_mismatch"
! git symbolic-ref -q HEAD >/dev/null 2>&1 || fail "not_detached_head"
[[ -z "$(git status --porcelain=v1 --untracked-files=all)" ]] \
  || fail "worktree_not_clean"
[[ -f "$PYTHON_HELPER" ]] || fail "acceptance_helper_missing"

for container in "$APP_CONTAINER" "$ADMIN_CONTAINER"; do
  [[ "$(sudo docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" == "true" ]] \
    || fail "runtime_container_not_running"
  [[ "$(sudo docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$container" 2>/dev/null)" == "$EXPECTED_SHA" ]] \
    || fail "runtime_image_sha_mismatch"
done

# The helper is streamed from the exact, clean Git checkout. Runtime credentials remain
# exclusively in the app-ml environment; neither docker arguments nor stdout contains them.
# This gate never forwards EXPECTED_KB_SEED_SHA256 and never starts index-kb. The owner supplies
# the reviewed Preview/current seed hash only to the separate, explicit index-kb invocation.
if ! sudo docker exec -i "$ADMIN_CONTAINER" python - \
  "$EXPECTED_SHA" "$OWNER_CHANNELS_ATTESTATION" < "$PYTHON_HELPER"; then
  fail "admin_api_acceptance_failed"
fi

printf 'admin_kb_acceptance_server_local=OK\n'
printf 'candidate_sha=%s\n' "$EXPECTED_SHA"
printf 'channels_status=%s owner_attested=true\n' "$CHANNELS_DISABLED_ATTESTATION"
