from __future__ import annotations

from src.llm.client import GigaChatClient


def test_gigachat_client_uses_access_token_for_jwt_like_value() -> None:
    client = GigaChatClient(api_key="header.payload.signature")
    client.access_token = ""

    assert client._auth_kwargs() == {"access_token": "header.payload.signature"}


def test_gigachat_client_strips_bearer_prefix() -> None:
    client = GigaChatClient(api_key="Bearer header.payload.signature")
    client.access_token = ""

    assert client._auth_kwargs() == {"access_token": "header.payload.signature"}
