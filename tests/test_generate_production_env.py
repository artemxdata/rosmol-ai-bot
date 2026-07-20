from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from scripts import generate_production_env

TEMPLATE = """\
# private production env
APP_ENV=production
RELEASE_GIT_SHA=replace-with-trusted-git-sha
POSTGRES_USER=rosmol
POSTGRES_PASSWORD=replace-with-password
POSTGRES_DB=rosmol_ai_bot
POSTGRES_DSN=postgresql://rosmol:replace-with-password@postgres:5432/rosmol_ai_bot
REDIS_PASSWORD=replace-with-redis-password
REDIS_URL=redis://:replace-with-redis-password@redis:6379/0
QDRANT_API_KEY=replace-with-qdrant-key
API_AUTH_TOKEN=replace-with-api-token
WEBHOOK_AUTH_TOKEN=replace-with-webhook-token
ADMIN_AUTH_TOKEN=replace-with-admin-token
USER_HASH_SECRET=replace-with-user-secret
HDE_TRIGGER_PREFIX=replace-with-trigger
HDE_TRANSPORT_EVENT_KEY_SECRET=replace-with-event-secret
HDE_TRANSPORT_ENCRYPTION_KEY=replace-with-encryption-key
HDE_BASE_URL=https://replace-with-hde.helpdeskeddy.com
HDE_API_EMAIL=replace-with-email
HDE_API_KEY=replace-with-key
CLOUD_RU_API_KEY=replace-with-key
CLOUD_RU_CHAT_COMPLETIONS_URL=https://foundation-models.api.cloud.ru/v1/chat/completions
ADMIN_PUBLIC_HOST=bot.example.test
CERTBOT_EMAIL=operations@example.test
"""


def _write_template(path: Path) -> None:
    path.write_text(TEMPLATE, encoding="utf-8")


def _complete_provider_fields(path: Path) -> None:
    content = path.read_text(encoding="utf-8")
    replacements = {
        "replace-with-trusted-git-sha": "a" * 40,
        "https://replace-with-hde.helpdeskeddy.com": "https://rosmolodezh.helpdeskeddy.com",
        "replace-with-email": "bot-api@example.org",
        "HDE_API_KEY=replace-with-key": "HDE_API_KEY=" + "h" * 48,
        "CLOUD_RU_API_KEY=replace-with-key": "CLOUD_RU_API_KEY=" + "c" * 48,
        "bot.example.test": "bot.example.org",
        "operations@example.test": "operations@example.org",
    }
    for old, new in replacements.items():
        content = content.replace(old, new)
    path.write_text(content, encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)


def test_init_generates_distinct_values_without_printing_them(
    tmp_path: Path,
    capsys,
) -> None:
    template = tmp_path / "template.env"
    output = tmp_path / ".env.production"
    _write_template(template)

    assert (
        generate_production_env.main(
            ["init", "--template", str(template), "--output", str(output)]
        )
        == 0
    )

    values, errors = generate_production_env._parse_env(output.read_text(encoding="utf-8"))
    assert errors == []
    generated = [values[key] for key in generate_production_env.GENERATED_SECRET_KEYS]
    assert all(len(value) >= generate_production_env.MIN_SECRET_LENGTH for value in generated)
    assert len(generated) == len(set(generated))
    assert values["POSTGRES_PASSWORD"] in values["POSTGRES_DSN"]
    assert values["REDIS_PASSWORD"] in values["REDIS_URL"]
    captured = capsys.readouterr()
    assert not any(value in captured.out or value in captured.err for value in generated)
    if os.name == "posix":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_init_refuses_to_overwrite_existing_file(tmp_path: Path) -> None:
    template = tmp_path / "template.env"
    output = tmp_path / ".env.production"
    _write_template(template)
    output.write_text("sentinel", encoding="utf-8")

    result = generate_production_env.main(
        ["init", "--template", str(template), "--output", str(output)]
    )

    assert result == 2
    assert output.read_text(encoding="utf-8") == "sentinel"


def test_validate_rejects_unresolved_provider_placeholders(tmp_path: Path) -> None:
    template = tmp_path / "template.env"
    output = tmp_path / ".env.production"
    _write_template(template)
    generate_production_env.initialize_env(template, output)

    errors = generate_production_env.validate_env(output)

    assert "HDE_API_KEY: missing_or_placeholder" in errors
    assert "RELEASE_GIT_SHA: missing_or_placeholder" in errors
    assert "ADMIN_PUBLIC_HOST: missing_or_placeholder" in errors
    assert "CERTBOT_EMAIL: missing_or_placeholder" in errors


def test_validate_accepts_complete_private_config(tmp_path: Path) -> None:
    template = tmp_path / "template.env"
    output = tmp_path / ".env.production"
    _write_template(template)
    generate_production_env.initialize_env(template, output)
    _complete_provider_fields(output)

    assert generate_production_env.validate_env(output) == []


def test_set_release_sha_atomically_preserves_all_secrets(
    tmp_path: Path,
    capsys,
) -> None:
    template = tmp_path / "template.env"
    output = tmp_path / ".env.production"
    _write_template(template)
    generate_production_env.initialize_env(template, output)
    _complete_provider_fields(output)
    before, _ = generate_production_env._parse_env(output.read_text(encoding="utf-8"))

    result = generate_production_env.main(
        [
            "set-release-sha",
            "--env-file",
            str(output),
            "--git-sha",
            "b" * 40,
        ]
    )

    assert result == 0
    after, errors = generate_production_env._parse_env(output.read_text(encoding="utf-8"))
    assert errors == []
    assert after["RELEASE_GIT_SHA"] == "b" * 40
    assert {**after, "RELEASE_GIT_SHA": before["RELEASE_GIT_SHA"]} == before
    captured = capsys.readouterr()
    output_text = captured.out + captured.err
    assert not any(value in output_text for value in before.values() if len(value) >= 32)
    if os.name == "posix":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_set_release_sha_refuses_stale_or_invalid_identity(tmp_path: Path) -> None:
    template = tmp_path / "template.env"
    output = tmp_path / ".env.production"
    _write_template(template)
    generate_production_env.initialize_env(template, output)
    _complete_provider_fields(output)
    original = output.read_bytes()

    assert (
        generate_production_env.main(
            [
                "set-release-sha",
                "--env-file",
                str(output),
                "--git-sha",
                "a" * 40,
            ]
        )
        == 2
    )
    assert (
        generate_production_env.main(
            [
                "set-release-sha",
                "--env-file",
                str(output),
                "--git-sha",
                "not-a-sha",
            ]
        )
        == 2
    )
    assert output.read_bytes() == original


def test_render_egress_proxy_uses_only_validated_endpoint_hosts(
    tmp_path: Path,
    capsys,
) -> None:
    template = tmp_path / "template.env"
    env_file = tmp_path / ".env.production"
    proxy_config = tmp_path / "runtime-egress" / "squid.conf"
    _write_template(template)
    generate_production_env.initialize_env(template, env_file)
    _complete_provider_fields(env_file)
    values, _ = generate_production_env._parse_env(
        env_file.read_text(encoding="utf-8")
    )

    assert (
        generate_production_env.main(
            [
                "render-egress-proxy",
                "--env-file",
                str(env_file),
                "--output",
                str(proxy_config),
            ]
        )
        == 0
    )

    config = proxy_config.read_text(encoding="utf-8")
    assert (
        "acl runtime_destinations dstdomain foundation-models.api.cloud.ru "
        "rosmolodezh.helpdeskeddy.com"
    ) in config
    assert "http_access allow CONNECT runtime_destinations" in config
    assert "http_access deny !CONNECT" in config
    assert "http_access deny all" in config
    assert values["HDE_API_KEY"] not in config
    assert values["CLOUD_RU_API_KEY"] not in config
    captured = capsys.readouterr()
    assert values["HDE_API_KEY"] not in captured.out + captured.err
    assert values["CLOUD_RU_API_KEY"] not in captured.out + captured.err
    if os.name == "posix":
        assert stat.S_IMODE(proxy_config.stat().st_mode) == 0o600

    assert (
        generate_production_env.main(
            [
                "render-egress-proxy",
                "--env-file",
                str(env_file),
                "--output",
                str(proxy_config),
            ]
        )
        == 2
    )


def test_validate_rejects_proxy_bypass_endpoint_forms(tmp_path: Path) -> None:
    template = tmp_path / "template.env"
    output = tmp_path / ".env.production"
    _write_template(template)
    generate_production_env.initialize_env(template, output)
    _complete_provider_fields(output)
    content = output.read_text(encoding="utf-8").replace(
        "https://rosmolodezh.helpdeskeddy.com",
        "https://192.0.2.10:8443",
    )
    output.write_text(content, encoding="utf-8")
    if os.name == "posix":
        output.chmod(0o600)

    errors = generate_production_env.validate_env(output)

    assert "HDE_BASE_URL: must_be_https_url_without_credentials" in errors


@pytest.mark.parametrize(
    ("source", "replacement", "expected"),
    (
        (
            "https://foundation-models.api.cloud.ru/v1/chat/completions",
            "https://rosmolodezh.helpdeskeddy.com/v1/chat/completions",
            "CLOUD_RU_CHAT_COMPLETIONS_URL: must_match_reviewed_cloud_ru_endpoint",
        ),
        (
            "https://rosmolodezh.helpdeskeddy.com",
            "https://foundation-models.api.cloud.ru",
            "HDE_BASE_URL: must_match_reviewed_hde_tenant_endpoint",
        ),
    ),
)
def test_validate_rejects_cross_provider_endpoint_substitution(
    tmp_path: Path,
    source: str,
    replacement: str,
    expected: str,
) -> None:
    template = tmp_path / "template.env"
    output = tmp_path / ".env.production"
    _write_template(template)
    generate_production_env.initialize_env(template, output)
    _complete_provider_fields(output)
    output.write_text(
        output.read_text(encoding="utf-8").replace(source, replacement),
        encoding="utf-8",
    )
    if os.name == "posix":
        output.chmod(0o600)

    assert expected in generate_production_env.validate_env(output)


def test_validate_rejects_reused_secret_and_external_database(tmp_path: Path) -> None:
    template = tmp_path / "template.env"
    output = tmp_path / ".env.production"
    _write_template(template)
    generate_production_env.initialize_env(template, output)
    _complete_provider_fields(output)
    content = output.read_text(encoding="utf-8")
    values, _ = generate_production_env._parse_env(content)
    content = content.replace(
        f"WEBHOOK_AUTH_TOKEN={values['WEBHOOK_AUTH_TOKEN']}",
        f"WEBHOOK_AUTH_TOKEN={values['API_AUTH_TOKEN']}",
    )
    content = content.replace("@postgres:5432/", "@public-db.example.org:5432/")
    output.write_text(content, encoding="utf-8")
    if os.name == "posix":
        output.chmod(0o600)

    errors = generate_production_env.validate_env(output)

    assert "generated_secrets: values_must_be_distinct" in errors
    assert "POSTGRES_DSN: must_match_internal_postgres_credentials" in errors


def test_validate_rejects_redis_url_without_matching_generated_password(
    tmp_path: Path,
) -> None:
    template = tmp_path / "template.env"
    output = tmp_path / ".env.production"
    _write_template(template)
    generate_production_env.initialize_env(template, output)
    _complete_provider_fields(output)
    content = output.read_text(encoding="utf-8").replace(
        "@redis:6379/0",
        "@external-redis.example.org:6379/0",
    )
    output.write_text(content, encoding="utf-8")
    if os.name == "posix":
        output.chmod(0o600)

    assert (
        "REDIS_URL: must_match_internal_redis_credentials"
        in generate_production_env.validate_env(output)
    )
