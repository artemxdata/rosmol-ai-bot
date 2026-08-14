#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
exec 2>/dev/null

readonly SERVER_PROJECT_DIR="/opt/rosmol-ai-bot"
readonly PROD_CONTAINER="rosmol-app-ml"
readonly SEALED_RUNTIME_SHA="b37f462f240b65cc1de76bae7fb4ff2a63235458"
readonly SEALED_CASES_SHA256="f2168c9e8721c82e46165b3803bb7adc7f89249f50210d96dc3dcb03d2710aaf"
readonly SEALED_MANIFEST_SHA256="419a6a62671d7dbb03c402ae688f400e5fa1dbe46565e2a477b01cfcb4662068"
readonly SEALED_APPROVAL_ID="owner-chat-20260814-semantic10-b37f462f240b65cc1de76bae7fb4ff2a63235458-f2168c9e8721-cap200"
readonly SEALED_RUN_DIR="/var/lib/rosmol/semantic-recovery10/runs/semantic_recovery10_v1-${SEALED_RUNTIME_SHA}"
readonly SEALED_IMAGE="rosmol-ai-bot-pilot50-candidate:${SEALED_RUNTIME_SHA}"
readonly COST_LEDGER_DIR="/var/lib/rosmol/eval-cost-ledger-v1"
readonly MAX_STDOUT_BYTES="$((32 * 1024 + 1))"

EXPECTED_TOOLING_SHA="${1:-}"
TOOLING_ROOT=""
TOOLING_SOURCE=""
TEMP_ROOT=""
TRACE_ENV_FILE=""
RAW_STDOUT=""
DATA_NETWORK=""
PRODUCTION_SNAPSHOT_BEFORE=""

fail() {
  printf 'semantic_recovery10_failed_diagnostics=FAIL reason=%s\n' "$1"
  exit 1
}

cleanup_on_exit() {
  local cleanup_failed=0 exit_code="$?" resolved=""
  trap - EXIT
  if [[ -n "$TEMP_ROOT" ]]; then
    resolved="$(readlink -f -- "$TEMP_ROOT" 2>/dev/null || true)"
    if [[ "$TEMP_ROOT" == /run/semantic-recovery10-failed-diagnostics.* && \
      "$resolved" == "$TEMP_ROOT" && "$resolved" != "/run" ]]; then
      sudo rm -rf --one-file-system -- "$TEMP_ROOT" >/dev/null 2>&1 \
        || cleanup_failed=1
    else
      cleanup_failed=1
    fi
  fi
  if [[ "$cleanup_failed" -ne 0 ]]; then
    printf 'semantic_recovery10_failed_diagnostics=FAIL reason=temp_cleanup_failed\n'
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

production_snapshot() {
  sudo docker inspect -f \
    '{{.Id}}|{{.Image}}|{{.RestartCount}}|{{.State.OOMKilled}}|{{.State.Running}}|{{.State.StartedAt}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$PROD_CONTAINER" 2>/dev/null
}

load_tooling_and_runtime() {
  local image_id image_revision image_user inspect
  for command in git sudo docker python3 sha256sum awk find sort tar readlink grep \
    mktemp chmod mkdir install; do
    require_command "$command" "${command}_missing"
  done
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
    scripts/diagnose_semantic_recovery10_failed_server_local.sh \
    scripts/diagnose_semantic_recovery10_failed.py \
    scripts/semantic_recovery10.py eval/cost_governance.py \
    >/dev/null 2>&1 || fail "diagnostic_tooling_not_tracked"
  git -C "$TOOLING_ROOT" cat-file -e "${SEALED_RUNTIME_SHA}^{commit}" 2>/dev/null \
    || fail "sealed_source_commit_missing"
  if git -C "$TOOLING_ROOT" ls-files -s 2>/dev/null \
    | awk '$1 == "120000" { found=1 } END { exit(found ? 0 : 1) }' \
      >/dev/null 2>&1; then
    fail "tooling_source_has_symlink"
  fi
  [[ "$(sudo docker inspect -f '{{.State.Running}}' \
    "$PROD_CONTAINER" 2>/dev/null)" == "true" ]] || fail "production_not_running"
  [[ "$(sudo docker inspect \
    -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$PROD_CONTAINER" 2>/dev/null)" == "healthy" ]] || fail "production_not_healthy"
  PRODUCTION_SNAPSHOT_BEFORE="$(production_snapshot)" \
    || fail "production_snapshot_failed"
  DATA_NETWORK="$(network_for_role data)" || fail "data_network_unavailable"
  [[ "$DATA_NETWORK" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] \
    || fail "data_network_invalid"
  inspect="$(sudo docker image inspect \
    -f '{{.Id}}|{{index .Config.Labels "org.opencontainers.image.revision"}}|{{.Config.User}}' \
    "$SEALED_IMAGE" 2>/dev/null)" || fail "sealed_image_missing"
  IFS='|' read -r image_id image_revision image_user <<<"$inspect"
  [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ && \
    "$image_revision" == "$SEALED_RUNTIME_SHA" && "$image_user" == "app" ]] \
    || fail "sealed_image_identity_invalid"
  sudo test -d "$COST_LEDGER_DIR" || fail "cost_ledger_unavailable"
  sudo test ! -L "$COST_LEDGER_DIR" || fail "cost_ledger_invalid"
  [[ "$(sudo stat -c '%u:%g:%a' "$COST_LEDGER_DIR" 2>/dev/null)" == \
    "10001:10001:700" ]] || fail "cost_ledger_mode_mismatch"
}

create_tooling_snapshot() {
  local actual_paths_sha expected_paths_sha owner_gid owner_uid
  owner_uid="$(id -u)"
  owner_gid="$(id -g)"
  TEMP_ROOT="$(sudo mktemp -d \
    /run/semantic-recovery10-failed-diagnostics.XXXXXX 2>/dev/null)" \
    || fail "temp_create_failed"
  [[ "$TEMP_ROOT" == /run/semantic-recovery10-failed-diagnostics.* ]] \
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
  TRACE_ENV_FILE="$TEMP_ROOT/trace.env"
  : >"$RAW_STDOUT"
  : >"$TRACE_ENV_FILE"
  chmod 0600 "$RAW_STDOUT" "$TRACE_ENV_FILE" || fail "temp_permissions_failed"
}

validate_sealed_run() {
  sudo python3 -I -S - "$SEALED_RUN_DIR" "$SEALED_CASES_SHA256" \
    "$SEALED_MANIFEST_SHA256" <<'PY' 2>/dev/null
import hashlib
import os
import stat
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
cases_sha, manifest_sha = sys.argv[2:]
source_dir = run_dir / "source"
evidence_dir = run_dir / "evidence"
preflight = run_dir / "preflight.receipt"
started = run_dir / "run.started"
completed = run_dir / "run.completed"
cases = evidence_dir / "semantic-recovery10-cases.json"
manifest = evidence_dir / "semantic-recovery10-manifest.json"
report = evidence_dir / "semantic-recovery10-ask-report.json"
safe = evidence_dir / "semantic-recovery10-safe-result.json"

def real_dir(path, modes, uid=None, gid=None):
    meta = path.lstat()
    assert stat.S_ISDIR(meta.st_mode) and not path.is_symlink()
    assert path.resolve(strict=True) == path
    mode = stat.S_IMODE(meta.st_mode)
    assert mode in modes and mode & 0o022 == 0
    if uid is not None:
        assert meta.st_uid == uid and meta.st_gid == gid

def regular(path, maximum, uid, gid, exact_mode=None, allow_empty=False):
    meta = path.lstat()
    assert stat.S_ISREG(meta.st_mode) and not path.is_symlink()
    assert meta.st_nlink == 1 and meta.st_size <= maximum
    assert allow_empty or meta.st_size > 0
    assert meta.st_uid == uid and meta.st_gid == gid
    mode = stat.S_IMODE(meta.st_mode)
    assert mode & 0o022 == 0 and mode & 0o400 != 0
    if exact_mode is not None:
        assert mode == exact_mode
    return path.read_bytes()

real_dir(run_dir, {0o700})
real_dir(source_dir, {0o555, 0o755})
real_dir(evidence_dir, {0o700}, 10001, 10001)
root_uid = run_dir.lstat().st_uid
root_gid = run_dir.lstat().st_gid
assert root_uid == 0 and root_gid == 0
preflight_bytes = regular(preflight, 4096, 0, 0, exact_mode=0o600)
started_bytes = regular(started, 4096, 0, 0)
cases_bytes = regular(
    cases, 4 * 1024 * 1024, 10001, 10001, exact_mode=0o600
)
manifest_bytes = regular(
    manifest, 128 * 1024, 10001, 10001, exact_mode=0o600
)
assert hashlib.sha256(cases_bytes).hexdigest() == cases_sha
assert hashlib.sha256(manifest_bytes).hexdigest() == manifest_sha
assert not completed.exists() and not completed.is_symlink()
assert not safe.exists() and not safe.is_symlink()
parts = [
    ("preflight.receipt", preflight_bytes),
    ("run.started", started_bytes),
    (f"evidence/{cases.name}", cases_bytes),
    (f"evidence/{manifest.name}", manifest_bytes),
]
if report.exists() or report.is_symlink():
    report_bytes = regular(
        report, 32 * 1024 * 1024, 10001, 10001, allow_empty=True
    )
    parts.append((f"evidence/{report.name}", report_bytes))
entries = list(evidence_dir.iterdir())
assert 2 <= len(entries) <= 8
known_names = {cases.name, manifest.name, report.name}
for path in entries:
    if path.name in known_names:
        continue
    payload = regular(
        path, 32 * 1024 * 1024, 10001, 10001, allow_empty=True
    )
    parts.append((f"evidence/{path.name}", payload))
assert {path.name for path in run_dir.iterdir()} == {
    "source", "evidence", "preflight.receipt", "run.started"
}
digest = hashlib.sha256()
for name, payload in sorted(parts):
    digest.update(name.encode("ascii") + b"\0")
    digest.update(hashlib.sha256(payload).digest())
sys.stdout.write(digest.hexdigest())
PY
}

validate_sealed_source() {
  if sudo find "$SEALED_RUN_DIR/source" -type l -print -quit 2>/dev/null \
    | grep -q .; then
    return 1
  fi
  sudo git -c core.fileMode=false -c "safe.directory=$TOOLING_ROOT" \
    --git-dir="$TOOLING_ROOT/.git" --work-tree="$SEALED_RUN_DIR/source" \
    diff --quiet "$SEALED_RUNTIME_SHA" -- >/dev/null 2>&1
}

create_receipt_view() {
  local owner_gid owner_uid view="$TEMP_ROOT/sealed-meta"
  owner_uid="$(id -u)"
  owner_gid="$(id -g)"
  mkdir -m 0755 "$view" || fail "receipt_view_create_failed"
  sudo install -p -m 0444 -o "$owner_uid" -g "$owner_gid" \
    "$SEALED_RUN_DIR/preflight.receipt" "$view/preflight.receipt" \
    || fail "receipt_view_create_failed"
  sudo install -p -m 0444 -o "$owner_uid" -g "$owner_gid" \
    "$SEALED_RUN_DIR/run.started" "$view/run.started" \
    || fail "receipt_view_create_failed"
}

create_trace_env() {
  sudo docker inspect "$PROD_CONTAINER" 2>/dev/null \
    | python3 /dev/fd/3 "$TRACE_ENV_FILE" 3<<'PY' 2>/dev/null
import json
import os
import sys

path = sys.argv[1]
item = json.load(sys.stdin)[0]
env = dict(value.split("=", 1) for value in item["Config"]["Env"] if "=" in value)
dsn = str(env.get("POSTGRES_DSN") or "")
assert dsn and "\n" not in dsn and "\r" not in dsn
fd = os.open(path, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
    handle.write(f"ASK_EVAL_POSTGRES_DSN={dsn}\n")
    handle.write("PYTHONDONTWRITEBYTECODE=1\nPYTHONUNBUFFERED=1\n")
    handle.write("HTTP_PROXY=\nHTTPS_PROXY=\nALL_PROXY=\nNO_PROXY=postgres\n")
PY
}

run_diagnostic() {
  if ! sudo docker run --rm --pull never --network "$DATA_NETWORK" \
    --user app --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --cap-drop ALL --security-opt no-new-privileges=true \
    --pids-limit 128 --memory 1g --cpus 1 \
    --env-file "$TRACE_ENV_FILE" \
    --mount "type=bind,src=$TOOLING_SOURCE,dst=/workspace,readonly" \
    --mount "type=bind,src=$SEALED_RUN_DIR/evidence,dst=/sealed-evidence,readonly" \
    --mount "type=bind,src=$TEMP_ROOT/sealed-meta,dst=/sealed-meta,readonly" \
    --mount "type=bind,src=$COST_LEDGER_DIR,dst=/cost-ledger,readonly" \
    -w /workspace --entrypoint python "$SEALED_IMAGE" \
    -E -m scripts.diagnose_semantic_recovery10_failed \
    --evidence-dir /sealed-evidence \
    --preflight-receipt /sealed-meta/preflight.receipt \
    --started-receipt /sealed-meta/run.started \
    --ledger-dir /cost-ledger \
    --expected-runtime-git-sha "$SEALED_RUNTIME_SHA" \
    --expected-cases-sha256 "$SEALED_CASES_SHA256" \
    --expected-manifest-sha256 "$SEALED_MANIFEST_SHA256" \
    --expected-approval-id "$SEALED_APPROVAL_ID" \
    >"$RAW_STDOUT" 2>/dev/null; then
    fail "diagnostic_execution_failed"
  fi
}

validate_stdout() {
  python3 -I -S /dev/fd/3 "$RAW_STDOUT" "$MAX_STDOUT_BYTES" \
    "$SEALED_RUNTIME_SHA" "$SEALED_CASES_SHA256" \
    "$SEALED_MANIFEST_SHA256" "$SEALED_APPROVAL_ID" 3<<'PY' 2>/dev/null
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
maximum = int(sys.argv[2])
runtime_sha, cases_sha, manifest_sha, approval_id = sys.argv[3:]
raw = path.read_bytes()
assert 0 < len(raw) <= maximum and raw.isascii()
assert raw.endswith(b"\n") and raw.count(b"\n") == 1
body = raw[:-1]
payload = json.loads(body.decode("ascii"))
canonical = json.dumps(
    payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
).encode("ascii")
assert body == canonical
assert set(payload) == {
    "schema_version", "status", "bindings", "artifacts", "reservation",
    "trace_aggregate", "failure_stage", "failure_reasons",
    "report_diagnostic",
    "quality_verdict_available", "retry_forbidden",
    "diagnostic_new_ask_calls", "recovered_safe_result",
}
assert payload["schema_version"] == "semantic-recovery10-failed-diagnostics-v1"
assert payload["status"] == "execution_rejected"
assert payload["bindings"] == {
    "candidate_sha": runtime_sha,
    "cases_sha256": cases_sha,
    "manifest_sha256": manifest_sha,
    "approval_id": approval_id,
}
artifacts = payload["artifacts"]
assert set(artifacts) == {
    "run_started", "run_completed", "raw_report_present",
    "raw_report_sha256", "safe_result_present",
}
assert artifacts["run_started"] is True and artifacts["run_completed"] is False
assert artifacts["safe_result_present"] is False
report_sha = artifacts["raw_report_sha256"]
assert (report_sha is None) is (artifacts["raw_report_present"] is False)
assert report_sha is None or re.fullmatch(r"[0-9a-f]{64}", report_sha)
reservation = payload["reservation"]
assert set(reservation) == {
    "status", "matching_records", "approval_consumed_elsewhere",
    "ledger_lock_present", "rolling_24h_routine_reserved_before_rub",
    "requested_cap_rub", "rolling_24h_cap_rub", "requested_would_fit",
}
assert reservation["status"] in {"exact", "missing"}
assert reservation["matching_records"] in {0, 1}
assert reservation["requested_cap_rub"] == 200.0
assert reservation["rolling_24h_cap_rub"] == 300.0
assert type(reservation["requested_would_fit"]) is bool
trace = payload["trace_aggregate"]
assert trace.get("status") in {"ok", "unavailable", "not_bound"}
if trace["status"] == "ok":
    assert set(trace) == {
        "status", "traces_total", "distinct_cases", "null_case_ids",
        "cache_hits", "cache_misses", "errors", "llm_cost_rub",
    }
    assert all(type(trace[key]) is int and trace[key] >= 0 for key in (
        "traces_total", "distinct_cases", "null_case_ids", "cache_hits",
        "cache_misses", "errors",
    ))
    assert type(trace["llm_cost_rub"]) in {int, float} and trace["llm_cost_rub"] >= 0
allowed_stages = {
    "before_cost_reservation", "after_reservation_before_case_trace",
    "case_execution_incomplete", "post_case_pre_report",
    "report_present_invalid", "post_report_cli_gate",
}
allowed_reasons = {
    "approval_replay_rejected", "cost_ledger_locked",
    "llm_budget_stopped", "llm_cost_accounting_incomplete",
    "pre_reservation_failure", "report_validation_failed",
    "rolling_24h_cap_rejected", "runtime_or_case_execution_failed",
    "trace_coverage_below_100_percent",
    "unexplained_nonzero_exit_after_report",
}
assert payload["failure_stage"] in allowed_stages
reasons = payload["failure_reasons"]
assert type(reasons) is list and reasons and reasons == list(dict.fromkeys(reasons))
assert set(reasons) <= allowed_reasons
report_diagnostic = payload["report_diagnostic"]
assert (report_diagnostic is None) is (artifacts["raw_report_present"] is False)
if report_diagnostic is not None:
    assert set(report_diagnostic) == {
        "status", "validation_failures", "cases_total", "results_total",
        "cases_binding_match", "target_match", "result_identity_match",
        "runtime_identity", "cost_control", "result_counts",
        "failure_reason_counts",
    }
    assert report_diagnostic["status"] in {"valid", "invalid", "unreadable"}
    validation_failures = report_diagnostic["validation_failures"]
    allowed_validation_failures = {
        "report_json_unreadable", "manifest_invalid", "cases_invalid",
        "manifest_cases_binding_mismatch", "report_cardinality_mismatch",
        "report_cases_binding_mismatch", "report_target_mismatch",
        "result_identity_mismatch", "runtime_identity_mismatch",
        "pricing_incomplete", "reservation_invalid",
        "reservation_binding_mismatch", "reservation_run_id_mismatch",
        "eval_run_id_mismatch", "llm_cost_invalid", "llm_budget_exceeded",
        "llm_budget_stopped", "llm_pricing_stopped",
    }
    assert type(validation_failures) is list
    assert validation_failures == list(dict.fromkeys(validation_failures))
    assert set(validation_failures) <= allowed_validation_failures
    assert (report_diagnostic["status"] == "valid") is (not validation_failures)
    for key in ("cases_total", "results_total"):
        value = report_diagnostic[key]
        assert value is None or type(value) is int and value >= 0
    for key in ("cases_binding_match", "target_match", "result_identity_match"):
        assert report_diagnostic[key] is None or type(report_diagnostic[key]) is bool
    runtime = report_diagnostic["runtime_identity"]
    if runtime is not None:
        assert set(runtime) == {
            "status", "expected_match", "verified_match", "matched_expected",
        }
        assert runtime["status"] in {
            "verified", "invalid", "observed_unbound", "not_checked",
            "missing", "other",
        }
        for key in ("expected_match", "verified_match", "matched_expected"):
            assert runtime[key] is None or type(runtime[key]) is bool
    cost = report_diagnostic["cost_control"]
    if cost is not None:
        assert set(cost) == {
            "pricing_complete", "reservation_valid", "reservation_binding_match",
            "reservation_run_id_match", "eval_run_id_match", "budget_exceeded",
            "budget_stopped", "pricing_stopped", "llm_cost_rub",
        }
        for key in set(cost) - {"llm_cost_rub"}:
            assert cost[key] is None or type(cost[key]) is bool
        assert cost["llm_cost_rub"] is None or (
            type(cost["llm_cost_rub"]) in {int, float}
            and cost["llm_cost_rub"] >= 0
        )
    result_counts = report_diagnostic["result_counts"]
    if result_counts is not None:
        assert set(result_counts) == {
            "passed", "trace_found", "http_success", "http_error",
            "was_escalated", "semantic_recovery_attempted",
            "semantic_recovery_succeeded",
        }
        assert all(type(value) is int and value >= 0 for value in result_counts.values())
        if report_diagnostic["results_total"] is not None:
            assert all(
                value <= report_diagnostic["results_total"]
                for value in result_counts.values()
            )
    failure_counts = report_diagnostic["failure_reason_counts"]
    assert type(failure_counts) is dict
    assert all(
        re.fullmatch(r"[a-z][a-z0-9_.:-]{0,95}", key)
        and type(value) is int and value >= 0
        for key, value in failure_counts.items()
    )
assert payload["retry_forbidden"] is True
assert payload["diagnostic_new_ask_calls"] == 0
recovered = payload["recovered_safe_result"]
assert payload["quality_verdict_available"] is (recovered is not None)
if recovered is not None:
    assert recovered["candidate_sha"] == runtime_sha
    assert recovered["cases_sha256"] == cases_sha
    assert recovered["manifest_sha256"] == manifest_sha
    assert recovered["approval_id"] == approval_id
    assert recovered["report_sha256"] == report_sha
    assert recovered["human_product_verdict"] is False
    assert recovered["counts"]["total"] == 10
    assert recovered["diagnostic_gate"]["status"] in {"GO", "STOP"}

forbidden = {"query", "response", "message", "chunk_text", "request_id"}
def walk(value):
    if isinstance(value, dict):
        assert not (set(value) & forbidden)
        for child in value.values():
            walk(child)
    elif isinstance(value, list):
        for child in value:
            walk(child)
walk(payload)
sys.stdout.write(canonical.decode("ascii"))
PY
}

main() {
  local before_fingerprint after_fingerprint validated_stdout
  validate_invocation "$@"
  load_tooling_and_runtime
  create_tooling_snapshot
  before_fingerprint="$(validate_sealed_run)" \
    || fail "sealed_evidence_invalid"
  validate_sealed_source || fail "sealed_source_invalid"
  create_receipt_view
  create_trace_env || fail "trace_env_create_failed"
  run_diagnostic
  [[ "$(production_snapshot)" == "$PRODUCTION_SNAPSHOT_BEFORE" ]] \
    || fail "production_changed_during_diagnostic"
  after_fingerprint="$(validate_sealed_run)" \
    || fail "sealed_evidence_changed"
  [[ "$after_fingerprint" == "$before_fingerprint" ]] \
    || fail "sealed_evidence_changed"
  validated_stdout="$(validate_stdout)" || fail "diagnostic_output_invalid"
  printf 'semantic_recovery10_failed_diagnostics=OK\n'
  printf 'tooling_sha=%s\n' "$EXPECTED_TOOLING_SHA"
  printf 'candidate_sha=%s\n' "$SEALED_RUNTIME_SHA"
  printf 'cases_sha256=%s\n' "$SEALED_CASES_SHA256"
  printf 'manifest_sha256=%s\n' "$SEALED_MANIFEST_SHA256"
  printf '%s\n' "$validated_stdout"
}

main "$@"
