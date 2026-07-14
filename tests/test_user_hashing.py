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
    _validate_runtime_security(
        SimpleNamespace(app_env=app_env, user_hash_secret="dedicated-secret")
    )
