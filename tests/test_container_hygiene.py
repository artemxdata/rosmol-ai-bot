from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pathspec import GitIgnoreSpec

ROOT = Path(__file__).resolve().parents[1]
ADMIN_REPORT = ROOT / "reports/presentation_quality/presentation_quality_report.json"


def test_docker_context_excludes_private_and_local_artifacts() -> None:
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    rules = {
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".env.*",
        "/data/private/",
        "**/data/private/",
        "reports/**",
        "outputs/",
        "tmp/",
        "**/tmp/",
        ".pytest-tmp-*/",
        "**/.pytest-tmp-*/",
        "*.pdf",
        "**/*.pdf",
        ".idea/",
        ".vscode/",
        "reports/presentation_quality/**",
        "**/.ssh/",
        "**/.gnupg/",
        "**/id_ed25519",
        "**/credentials.json",
        "**/secret.txt",
    } <= rules
    assert {
        rule
        for rule in rules
        if rule.startswith("!reports/") and not rule.endswith("/")
    } == {"!reports/presentation_quality/presentation_quality_report.json"}

    context_rules = GitIgnoreSpec.from_lines(lines)
    assert not context_rules.match_file(
        "reports/presentation_quality/presentation_quality_report.json"
    )
    assert context_rules.match_file(
        "reports/presentation_quality/private_operator_details.json"
    )
    assert context_rules.match_file("credentials.json")
    assert context_rules.match_file("deploy/id_ed25519")


def test_runtime_image_explicitly_copies_only_reviewed_admin_report() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY reports/presentation_quality/presentation_quality_report.json" in dockerfile
    assert ADMIN_REPORT.is_file()


def test_ml_cache_volumes_are_initialized_then_used_by_non_root_services() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.ml.yml").read_text(encoding="utf-8")

    assert "install -d -o app -g app /home/app/.cache" in dockerfile
    assert "/home/app/.cache/torch" in dockerfile
    assert "ml-cache-init:" in compose
    assert "network_mode: none" in compose
    assert 'cap_add: ["CHOWN", "DAC_OVERRIDE"]' in compose
    assert 'entrypoint: ["chown"]' in compose
    assert compose.count("condition: service_completed_successfully") >= 5
    assert compose.count("user: app") >= 4
    app_ml = _compose_service_block(compose, "app-ml")
    assert "HDE_TRANSPORT_ENABLED: ${HDE_TRANSPORT_ENABLED:-false}" in app_ml
    assert "HDE_TRANSPORT_EVENT_KEY_SECRET: ${HDE_TRANSPORT_EVENT_KEY_SECRET:-}" in app_ml
    assert "HDE_TRANSPORT_ENCRYPTION_KEY: ${HDE_TRANSPORT_ENCRYPTION_KEY:-}" in app_ml
    assert "HDE_TRANSPORT_QUEUE_STALE_AFTER_SECONDS:" in app_ml


def test_production_overlay_is_fail_closed_and_uses_minimal_app_mounts() -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    for name in (
        "API_AUTH_TOKEN",
        "WEBHOOK_AUTH_TOKEN",
        "ADMIN_AUTH_TOKEN",
        "USER_HASH_SECRET",
        "HDE_TRANSPORT_EVENT_KEY_SECRET",
        "HDE_TRANSPORT_ENCRYPTION_KEY",
        "HDE_TRIGGER_PREFIX",
        "HDE_BASE_URL",
        "HDE_API_KEY",
        "CLOUD_RU_API_KEY",
        "POSTGRES_DSN",
        "REDIS_PASSWORD",
        "REDIS_URL",
        "QDRANT_API_KEY",
    ):
        assert f"${{{name}:?" in compose

    assert compose.count("volumes: !override") == 2
    assert compose.count(
        "./data/knowledge_base_seed.json:/app/data/knowledge_base_seed.json:ro"
    ) == 2
    assert (
        "./data/private/admin-kb:/app/data/private/admin-kb:ro"
        in _compose_service_block(compose, "app")
    )
    assert (
        "./data/private/admin-kb:/app/data/private/admin-kb"
        in _compose_service_block(compose, "app-ml")
    )
    assert "./data:/app/data" not in compose
    assert "./data/private/runtime:/app/data/private" not in compose
    assert compose.count("read_only: true") >= 4
    assert compose.count("cap_drop: [ALL]") >= 4
    assert compose.count("security_opt: [no-new-privileges:true]") >= 6
    assert "internal: true" in compose
    assert "max-size: \"10m\"" in compose
    assert "RUNTIME_ROLE: ml" in compose
    assert 'HDE_TRANSPORT_ENABLED: "true"' in compose
    assert "stop_grace_period: 450s" in compose
    assert 'ADMIN_READ_ONLY: "true"' in compose
    assert 'ADMIN_MUTATIONS_ENABLED: "false"' in compose
    assert "ADMIN_READ_ONLY: ${ADMIN_READ_ONLY:-true}" in (
        _compose_service_block(compose, "app-ml")
    )
    assert "ADMIN_MUTATIONS_ENABLED: ${ADMIN_MUTATIONS_ENABLED:-false}" in (
        _compose_service_block(compose, "app-ml")
    )
    assert (
        "KB_SEED_PATH: ${ADMIN_KB_SEED_PATH:-/app/data/knowledge_base_seed.json}"
        in compose
    )
    assert "QDRANT__SERVICE__API_KEY:" in compose
    assert 'QDRANT__TELEMETRY_DISABLED: "true"' in compose
    assert "--requirepass" in compose
    assert "REDISCLI_AUTH=$$REDIS_PASSWORD" in compose
    assert 'ML_PREWARM_ON_STARTUP: "true"' in compose
    assert "ubuntu/squid:6.6-24.04_edge@sha256:" in compose
    assert compose.count("urllib.request.urlopen('http://127.0.0.1:8000/ready'") == 2


def test_production_compose_exposes_only_edge_ports() -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    production_env = (ROOT / ".env.production.example").read_text(encoding="utf-8")

    for service in (
        "postgres",
        "redis",
        "qdrant",
        "migrate",
        "init-qdrant",
        "runtime-egress-proxy",
        "edge-relay",
        "app",
        "app-ml",
        "ml-check",
        "index-kb",
        "nginx",
        "ml-cache-init",
        "model-prefetch",
    ):
        assert "pull_policy: never" in _compose_service_block(compose, service)

    for service in ("postgres", "redis", "qdrant", "app", "nginx"):
        block = _compose_service_block(compose, service)
        assert "ports: !override []" in block

    app_ml = _compose_service_block(compose, "app-ml")
    assert "ports: !override []" in app_ml
    assert "APP_ML_HOST_PORT" not in app_ml
    assert "APP_ML_HOST_PORT" not in production_env

    nginx = _compose_service_block(compose, "nginx")
    assert "networks: [edge]" in nginx
    assert "read_only: true" in nginx
    assert "/etc/nginx/conf.d:rw,nosuid,nodev,noexec" in nginx
    assert "cap_drop: [ALL]" in nginx
    assert "cap_add: [CHOWN, DAC_OVERRIDE, SETGID, SETUID, NET_BIND_SERVICE]" in nginx

    assert re.search(r"(?ms)^  data:\n    internal: true$", compose)
    assert re.search(r"(?ms)^  edge:\n    internal: true$", compose)
    app = _compose_service_block(compose, "app")
    assert "networks: [edge, data]" in app
    assert "egress" not in app
    assert 'HDE_TRANSPORT_ENABLED: "false"' in app
    assert 'HDE_TRANSPORT_EVENT_KEY_SECRET: ""' in app
    assert 'HDE_TRANSPORT_ENCRYPTION_KEY: ""' in app
    assert 'CLOUD_RU_API_KEY: ""' in app
    assert "YONOTE_API_TOKEN" not in app
    assert "networks: [edge, data, runtime_egress]" in app_ml
    assert "HTTPS_PROXY: http://runtime-egress-proxy:3128" in app_ml
    assert "YONOTE_SYNC_ENABLED: ${YONOTE_SYNC_ENABLED:-false}" in app_ml
    assert "YONOTE_API_TOKEN: ${YONOTE_API_TOKEN:-}" in app_ml
    assert "YONOTE_BASE_URL: ${YONOTE_BASE_URL:-https://rossmol.yonote.ru}" in app_ml
    assert "condition: service_healthy" in app_ml
    assert "networks: [edge, data, egress]" not in app_ml

    egress_proxy = _compose_service_block(compose, "runtime-egress-proxy")
    assert "networks: [runtime_egress, egress]" in egress_proxy
    assert "./data/private/runtime-egress/squid.conf:/etc/squid/squid.conf:ro" in (
        egress_proxy
    )
    assert "read_only: true" in egress_proxy
    assert "cap_drop: [ALL]" in egress_proxy
    assert "no-new-privileges:true" in egress_proxy
    assert "ports:" not in egress_proxy
    assert "HDE_API_KEY" not in egress_proxy
    assert "CLOUD_RU_API_KEY" not in egress_proxy
    assert "YONOTE_API_TOKEN" not in egress_proxy
    assert "uid=13,gid=13" in egress_proxy
    assert '"</dev/tcp/127.0.0.1/3128"' in egress_proxy
    assert re.search(r"(?ms)^  runtime_egress:\n    internal: true$", compose)

    edge_relay = _compose_service_block(compose, "edge-relay")
    assert "haproxy:3.4.2-alpine@sha256:" in edge_relay
    assert "networks: [ingress, edge]" in edge_relay
    assert "${NGINX_BIND:?NGINX_BIND is required}" in edge_relay
    assert "${NGINX_TLS_BIND:?NGINX_TLS_BIND is required}" in edge_relay
    assert ":8080\"" in edge_relay
    assert ":8443\"" in edge_relay
    assert "read_only: true" in edge_relay
    assert "cap_drop: [ALL]" in edge_relay
    assert "no-new-privileges:true" in edge_relay
    assert "HDE_API_KEY" not in edge_relay
    assert "CLOUD_RU_API_KEY" not in edge_relay
    assert "ADMIN_AUTH_TOKEN" not in edge_relay
    assert not re.search(r"(?ms)^  ingress:\n    internal: true$", compose)

    model_prefetch = _compose_service_block(compose, "model-prefetch")
    assert "network_mode: none" in model_prefetch
    assert "networks: [egress]" not in model_prefetch


def test_quality_acceptance_is_internal_minimal_and_secret_scoped() -> None:
    compose = (ROOT / "docker-compose.acceptance.yml").read_text(encoding="utf-8")
    service = _compose_service_block(compose, "quality-acceptance")

    assert 'profiles: ["acceptance"]' in service
    assert "networks: [data]" in service
    assert "ports:" not in service
    assert "read_only: true" in service
    assert "cap_drop: [ALL]" in service
    assert "no-new-privileges:true" in service
    assert "PRE_PILOT_TRACE_REQUIRED: \"1\"" in service
    assert "API_AUTH_TOKEN:" in service
    assert "ASK_EVAL_POSTGRES_DSN:" in service
    assert "CLOUD_RU_API_KEY" not in service
    assert "HDE_API_KEY" not in service
    assert "WEBHOOK_AUTH_TOKEN" not in service
    assert "ADMIN_AUTH_TOKEN" not in service
    assert "ACCEPTANCE_SOURCE_DIR" in service
    assert "ACCEPTANCE_OUTPUT_DIR" in service
    assert "ACCEPTANCE_PROVENANCE_DIR" in service
    assert "target: /workspace" in service
    assert "target: /evidence" in service
    assert "target: /provenance" in service
    assert 'HTTP_PROXY: ""' in service
    assert 'HTTPS_PROXY: ""' in service
    assert 'ALL_PROXY: ""' in service


def test_production_env_is_ignored_but_reviewed_examples_are_tracked() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    example = (ROOT / ".env.production.example").read_text(encoding="utf-8")

    assert ".env.*" in gitignore
    assert "!.env.example" in gitignore
    assert "!.env.production.example" in gitignore
    assert "APP_ENV=production" in example
    assert "CERTBOT_EMAIL=" in example


def test_runtime_compose_masks_private_data_while_allowing_atomic_seed_updates() -> None:
    compose_text = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in ("docker-compose.yml", "docker-compose.ml.yml")
    )

    assert compose_text.count("./data:/app/data") == 2
    assert compose_text.count("./data/private/runtime:/app/data/private") == 2
    assert "./data/knowledge_base_seed.json:/app/data/knowledge_base_seed.json:ro" in compose_text
    assert "scripts/index_kb.py --path data/knowledge_base_seed.json" in compose_text
    assert "scripts/index_kb.py --path data/knowledge_base_seed.json --prune-stale" not in (
        ROOT / "docker-compose.ml.yml"
    ).read_text(encoding="utf-8")


def test_admin_report_contains_aggregate_fields_only() -> None:
    report = json.loads(ADMIN_REPORT.read_text(encoding="utf-8"))
    forbidden_record_keys = {
        "query",
        "question",
        "message",
        "message_masked",
        "response",
        "response_text",
        "request_id",
        "session_hash",
        "user_id",
        "user_id_hash",
    }

    assert ADMIN_REPORT.stat().st_size < 64 * 1024
    assert not (set(_all_keys(report)) & forbidden_record_keys)


def _all_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.append(str(key))
            keys.extend(_all_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_all_keys(item))
    return keys


def _compose_service_block(compose: str, service: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(service)}:\n"
        rf"(?P<body>.*?)(?=^  [a-z0-9-]+:\n|^networks:\n|\Z)",
        compose,
    )
    assert match is not None, service
    return match.group("body")
