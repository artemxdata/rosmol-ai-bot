from __future__ import annotations

from types import SimpleNamespace

import pytest
from gigachat.settings import AUTH_URL, BASE_URL

from src.llm.client import GigaChatClient


def _settings(**overrides):
    values = {
        "gigachat_api_key": "",
        "gigachat_access_token": "",
        "gigachat_verify_ssl": False,
        "gigachat_scope": "GIGACHAT_API_CORP",
        "gigachat_base_url": "",
        "gigachat_auth_url": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_gigachat_client_uses_access_token_for_jwt_like_value() -> None:
    client = GigaChatClient(api_key="header.payload.signature")
    client.access_token = ""

    assert client._auth_kwargs() == {"access_token": "header.payload.signature"}


def test_gigachat_client_strips_bearer_prefix() -> None:
    client = GigaChatClient(api_key="Bearer header.payload.signature")
    client.access_token = ""

    assert client._auth_kwargs() == {"access_token": "header.payload.signature"}


def test_gigachat_client_uses_sdk_urls_when_env_urls_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.llm.client.get_settings", lambda: _settings())

    client = GigaChatClient()

    assert client.base_url == BASE_URL
    assert client.auth_url == AUTH_URL


def test_gigachat_client_accepts_custom_http_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.llm.client.get_settings",
        lambda: _settings(
            gigachat_base_url="https://example.test/api/v1",
            gigachat_auth_url="https://example.test/oauth",
        ),
    )

    client = GigaChatClient()

    assert client.base_url == "https://example.test/api/v1"
    assert client.auth_url == "https://example.test/oauth"


def test_gigachat_client_rejects_urls_without_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.llm.client.get_settings",
        lambda: _settings(gigachat_base_url="gigachat.devices.sberbank.ru/api/v1"),
    )

    with pytest.raises(ValueError, match="GigaChat URL"):
        GigaChatClient()
