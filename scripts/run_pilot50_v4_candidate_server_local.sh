#!/usr/bin/env bash
set -Eeuo pipefail

readonly PILOT50_RUNNER_GENERATION="v4"
readonly PILOT50_CANDIDATE_DATASET_ID="pilot50_balanced_v4"
readonly PILOT50_CANDIDATE_PROMPT_VERSION="pilot50-quality-v4"
export PILOT50_RUNNER_GENERATION PILOT50_CANDIDATE_DATASET_ID
export PILOT50_CANDIDATE_PROMPT_VERSION

wrapper_dir="${0%/*}"
[[ "$wrapper_dir" != "$0" ]] || wrapper_dir="."
readonly wrapper_dir
readonly shared_runner="$wrapper_dir/run_pilot50_candidate_server_local.sh"
[[ -f "$shared_runner" && ! -L "$shared_runner" ]] || {
  printf 'pilot50_candidate_server_local=FAIL reason=shared_runner_invalid\n'
  exit 1
}
# shellcheck source=run_pilot50_candidate_server_local.sh
source "$shared_runner" "$@"
