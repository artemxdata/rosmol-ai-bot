from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from src.main import app as fastapi_app


@pytest.mark.asyncio
async def test_ask_rejects_blank_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(api_auth_token="", webhook_auth_token=""),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask", json={"user_id": "u1", "text": "   "})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_ask_requires_token_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(api_auth_token="secret", webhook_auth_token=""),
    )

    async def fake_process_message(message, fastapi_app) -> str:
        return "ok"

    monkeypatch.setattr("src.main.process_message", fake_process_message)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/ask", json={"user_id": "u1", "text": "Привет"})
        provided = await client.post(
            "/ask",
            json={"user_id": "u1", "text": "Привет"},
            headers={"X-API-Key": "secret"},
        )

    assert missing.status_code == 401
    assert provided.status_code == 200
    assert provided.json()["response"] == "ok"


@pytest.mark.asyncio
async def test_webhook_requires_token_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(api_auth_token="", webhook_auth_token="secret"),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/webhook/vk", json={"object": {"message": {"text": "x"}}})

    assert response.status_code == 401
