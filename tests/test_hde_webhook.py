from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.main import app as fastapi_app
from src.models import Channel, IncomingMessage


@pytest.mark.asyncio
async def test_hde_webhook_processes_message_in_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakeHDEAdapter:
        def parse(self, payload: dict[str, Any]) -> IncomingMessage:
            calls["payload"] = payload
            return IncomingMessage(
                user_id="ticket-123",
                channel=Channel.HDE,
                text="Как зарегистрироваться на форум?",
            )

        async def send(self, user_id: str, text: str) -> None:
            calls["sent"] = {"user_id": user_id, "text": text}

    async def fake_process_message(message: IncomingMessage, fastapi_app_arg: Any) -> str:
        calls["processed"] = {
            "user_id": message.user_id,
            "channel": message.channel.value,
            "text": message.text,
            "app": fastapi_app_arg,
        }
        return "Ответ бота"

    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(webhook_auth_token="secret"),
    )
    monkeypatch.setattr("src.main.hde_adapter", FakeHDEAdapter())
    monkeypatch.setattr("src.main.process_message", fake_process_message)

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhook/hde",
            json={"event": "new_message"},
            headers={"X-Webhook-Secret": "secret"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert calls["payload"] == {"event": "new_message"}
    assert calls["processed"]["user_id"] == "ticket-123"
    assert calls["processed"]["channel"] == "hde"
    assert calls["sent"] == {"user_id": "ticket-123", "text": "Ответ бота"}
