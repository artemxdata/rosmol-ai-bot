from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ADMIN_REPORT = ROOT / "reports/presentation_quality/presentation_quality_report.json"


def test_docker_context_excludes_private_and_local_artifacts() -> None:
    rules = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
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
    } <= rules
    assert {
        rule
        for rule in rules
        if rule.startswith("!reports/") and not rule.endswith("/")
    } == {"!reports/presentation_quality/presentation_quality_report.json"}


def test_runtime_image_explicitly_copies_only_reviewed_admin_report() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY reports/presentation_quality/presentation_quality_report.json" in dockerfile
    assert ADMIN_REPORT.is_file()


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
