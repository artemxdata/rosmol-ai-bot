from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace

import pytest

from src.main import _validate_runtime_security
from src.session.memory import hash_user_id


def test_hash_user_id_uses_keyed_hmac_when_secret_is_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.session.memory.get_settings",
        lambda: SimpleNamespace(
            user_hash_secret="dedicated-secret",
            webhook_auth_token="",
            api_auth_token="",
            admin_auth_token="",
            hde_api_key="",
        ),
    )

    actual = hash_user_id("hde", "predictable-user-42")
    expected = hmac.new(
        b"dedicated-secret",
        b"hde:predictable-user-42",
        hashlib.sha256,
    ).hexdigest()

    assert actual == expected
    assert actual != hashlib.sha256(b"hde:predictable-user-42").hexdigest()


def test_hash_user_id_does_not_reuse_operational_tokens(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.session.memory.get_settings",
        lambda: SimpleNamespace(
            user_hash_secret="",
            webhook_auth_token="existing-webhook-secret",
            api_auth_token="",
            admin_auth_token="",
            hde_api_key="",
        ),
    )

    first = hash_user_id("hde", "user")
    second = hash_user_id("hde", "user")

    assert first == second
    assert first == hashlib.sha256(b"hde:user").hexdigest()
    assert first != hmac.new(
        b"existing-webhook-secret", b"hde:user", hashlib.sha256
    ).hexdigest()


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_non_local_runtime_requires_dedicated_user_hash_secret(app_env: str) -> None:
    with pytest.raises(RuntimeError, match="USER_HASH_SECRET"):
        _validate_runtime_security(
            SimpleNamespace(app_env=app_env, user_hash_secret="")
        )


@pytest.mark.parametrize("app_env", ["local", "test", "staging", "production"])
def test_runtime_accepts_supported_secret_configuration(app_env: str) -> None:
    settings = SimpleNamespace(app_env=app_env, user_hash_secret="dedicated-secret")
    if app_env == "production":
        settings = _production_settings()

    _validate_runtime_security(settings)


def test_production_runtime_rejects_template_placeholders() -> None:
    settings = _production_settings()
    _enable_ml_transport(settings)
    settings.hde_api_key = "replace-with-a-real-hde-key-that-is-long-enough"

    with pytest.raises(RuntimeError, match="placeholder value"):
        _validate_runtime_security(settings)


def test_production_runtime_requires_independent_internal_secrets() -> None:
    settings = _production_settings()
    settings.webhook_auth_token = settings.api_auth_token

    with pytest.raises(RuntimeError, match="must be independent values"):
        _validate_runtime_security(settings)


def test_production_runtime_requires_all_public_endpoint_credentials() -> None:
    settings = _production_settings()
    settings.webhook_auth_token = ""

    with pytest.raises(RuntimeError, match="WEBHOOK_AUTH_TOKEN is required"):
        _validate_runtime_security(settings)


@pytest.mark.parametrize(
    ("attribute", "value", "expected"),
    [
        ("qdrant_api_key", "", "QDRANT_API_KEY"),
        ("redis_url", "redis://redis:6379/0", "REDIS_URL"),
        ("redis_url", "redis://:short@redis:6379/0", "REDIS_URL"),
    ],
)
def test_production_runtime_requires_authenticated_internal_datastores(
    attribute: str,
    value: str,
    expected: str,
) -> None:
    settings = _production_settings()
    setattr(settings, attribute, value)

    with pytest.raises(RuntimeError, match=expected):
        _validate_runtime_security(settings)


@pytest.mark.parametrize(
    ("attribute", "env_name"),
    [
        ("cloud_ru_chat_completions_url", "CLOUD_RU_CHAT_COMPLETIONS_URL"),
        ("hde_base_url", "HDE_BASE_URL"),
        ("yonote_base_url", "YONOTE_BASE_URL"),
    ],
)
def test_production_runtime_rejects_plaintext_active_external_urls(
    attribute: str,
    env_name: str,
) -> None:
    settings = _production_settings()
    if attribute in {"cloud_ru_chat_completions_url", "hde_base_url"}:
        _enable_ml_transport(settings)
    setattr(settings, attribute, "http://external.vendor.ru/api")
    if attribute == "yonote_base_url":
        settings.yonote_sync_enabled = True

    with pytest.raises(RuntimeError, match=env_name):
        _validate_runtime_security(settings)


def test_production_ml_runtime_requires_prewarm() -> None:
    settings = _production_settings()
    _enable_ml_transport(settings)
    settings.ml_prewarm_on_startup = False

    with pytest.raises(RuntimeError, match="ML_PREWARM_ON_STARTUP"):
        _validate_runtime_security(settings)


def test_production_ml_runtime_requires_isolated_egress_proxy() -> None:
    settings = _production_settings()
    _enable_ml_transport(settings)
    settings.https_proxy = "https://unrestricted-proxy.example.org"

    with pytest.raises(RuntimeError, match="HTTPS_PROXY"):
        _validate_runtime_security(settings)


def test_production_api_runtime_does_not_require_provider_or_transport_secrets() -> None:
    settings = _production_settings()
    settings.cloud_ru_api_key = ""
    settings.hde_trigger_prefix = ""
    settings.hde_base_url = ""
    settings.hde_api_email = ""
    settings.hde_api_key = ""

    _validate_runtime_security(settings)


def test_production_runtime_requires_read_only_admin() -> None:
    settings = _production_settings()
    settings.admin_read_only = False

    with pytest.raises(RuntimeError, match="ADMIN_READ_ONLY"):
        _validate_runtime_security(settings)


@pytest.mark.parametrize("value", ["", "0" * 40, "A" * 40, "abc123"])
def test_production_runtime_requires_trusted_release_git_sha(value: str) -> None:
    settings = _production_settings()
    settings.release_git_sha = value

    with pytest.raises(RuntimeError, match="RELEASE_GIT_SHA"):
        _validate_runtime_security(settings)


def test_enabled_transport_rejects_unsafe_lease_window() -> None:
    settings = _production_settings()
    _enable_ml_transport(settings)
    settings.request_timeout_seconds = 150
    settings.hde_transport_lease_timeout_seconds = 240

    with pytest.raises(RuntimeError, match="LEASE_TIMEOUT_SECONDS"):
        _validate_runtime_security(settings)


def test_production_ml_requires_enabled_distinct_transport_secrets() -> None:
    disabled = _production_settings()
    disabled.runtime_role = "ml"
    disabled.ml_prewarm_on_startup = True

    with pytest.raises(RuntimeError, match="HDE_TRANSPORT_ENABLED"):
        _validate_runtime_security(disabled)

    reused = _production_settings()
    _enable_ml_transport(reused)
    reused.hde_transport_encryption_key = reused.hde_transport_event_key_secret

    with pytest.raises(RuntimeError, match="must be independent"):
        _validate_runtime_security(reused)


def test_production_api_rejects_provider_or_transport_secrets() -> None:
    settings = _production_settings()
    settings.cloud_ru_api_key = "provider-secret-must-not-reach-edge"

    with pytest.raises(RuntimeError, match="must not be configured in the API runtime"):
        _validate_runtime_security(settings)


@pytest.mark.parametrize(
    ("attribute", "value", "expected"),
    [
        ("hde_transport_recovery_interval_seconds", 200, "RECOVERY_INTERVAL_SECONDS"),
        ("hde_transport_shutdown_timeout_seconds", 100, "SHUTDOWN_TIMEOUT_SECONDS"),
    ],
)
def test_enabled_transport_rejects_unsafe_recovery_or_shutdown_window(
    attribute: str,
    value: float,
    expected: str,
) -> None:
    settings = _production_settings()
    _enable_ml_transport(settings)
    setattr(settings, attribute, value)

    with pytest.raises(RuntimeError, match=expected):
        _validate_runtime_security(settings)


def _production_settings() -> SimpleNamespace:
    return SimpleNamespace(
        app_env="production",
        runtime_role="api",
        release_git_sha="a" * 40,
        admin_read_only=True,
        api_auth_token="a" * 32,
        webhook_auth_token="w" * 32,
        admin_auth_token="m" * 32,
        user_hash_secret="u" * 32,
        redis_url="redis://:" + "r" * 48 + "@redis:6379/0",
        qdrant_api_key="q" * 48,
        cloud_ru_api_key="",
        cloud_ru_chat_completions_url=(
            "https://foundation-models.api.cloud.ru/v1/chat/completions"
        ),
        hde_trigger_prefix="",
        hde_base_url="",
        hde_api_email="",
        hde_api_key="",
        postgres_dsn="postgresql://bot:strong-password@postgres:5432/bot",
        ml_prewarm_on_startup=False,
        yonote_sync_enabled=False,
        yonote_api_token="",
        yonote_base_url="https://yonote.vendor.ru",
        hde_transport_enabled=False,
        hde_transport_event_key_secret="",
        hde_transport_encryption_key="",
        hde_transport_lease_timeout_seconds=420,
        hde_transport_recovery_interval_seconds=30,
        hde_transport_shutdown_timeout_seconds=420,
        request_timeout_seconds=45,
        hde_request_timeout_seconds=20,
    )


def _enable_ml_transport(settings: SimpleNamespace) -> None:
    settings.runtime_role = "ml"
    settings.ml_prewarm_on_startup = True
    settings.hde_transport_enabled = True
    settings.hde_transport_event_key_secret = "e" * 48
    settings.hde_transport_encryption_key = "k" * 48
    settings.cloud_ru_api_key = "provider-cloud-key"
    settings.hde_trigger_prefix = "independent-prefix"
    settings.hde_base_url = "https://rosmolodezh.helpdeskeddy.com"
    settings.hde_api_email = "bot@vendor.ru"
    settings.hde_api_key = "provider-hde-key"
    settings.https_proxy = "http://runtime-egress-proxy:3128"
