from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.channels.hde import HDEDeliveryResult, HDEDeliveryStatus
from src.main import app as fastapi_app
from src.main import process_message
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

        async def send(self, user_id: str, text: str) -> HDEDeliveryResult:
            calls["sent"] = {"user_id": user_id, "text": text}
            return HDEDeliveryResult(HDEDeliveryStatus.DELIVERED, attempted=True)

    async def fake_process_message(message: IncomingMessage, fastapi_app_arg: Any) -> str:
        calls["processed"] = {
            "user_id": message.user_id,
            "channel": message.channel.value,
            "text": message.text,
            "app": fastapi_app_arg,
        }
        return "Ответ бота"

    async def _noop_delivery_update(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(webhook_auth_token="secret"),
    )
    monkeypatch.setattr("src.main.hde_adapter", FakeHDEAdapter())
    monkeypatch.setattr("src.main.process_message", fake_process_message)
    monkeypatch.setattr("src.main.update_delivery_outcome", _noop_delivery_update)

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


@pytest.mark.asyncio
async def test_hde_webhook_deduplicates_stable_upstream_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"processed": 0, "sent": 0}

    class FakeRedis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        async def set(self, key: str, value: str, **kwargs: Any) -> bool:
            assert kwargs == {"nx": True, "ex": 5 * 60}
            if key in self.values:
                return False
            self.values[key] = value
            return True

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def eval(self, script: str, _key_count: int, key: str, *args: Any) -> Any:
            if self.values.get(key) != args[0]:
                return 0
            if "SET" in script:
                self.values[key] = str(args[1])
                return "OK"
            del self.values[key]
            return 1

    class FakeHDEAdapter:
        def parse(self, _payload: dict[str, Any]) -> IncomingMessage:
            return IncomingMessage(
                user_id="ticket-123",
                channel=Channel.HDE,
                text="Где мой билет?",
                upstream_event_id="message-456",
                upstream_event_id_source="message.id",
            )

        async def send(self, _user_id: str, _text: str) -> HDEDeliveryResult:
            calls["sent"] += 1
            return HDEDeliveryResult(HDEDeliveryStatus.DELIVERED, attempted=True)

    async def fake_process_message(_message: IncomingMessage, _app: Any) -> str:
        calls["processed"] += 1
        return "Ответ"

    async def fake_delivery_update(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(webhook_auth_token="secret"),
    )
    monkeypatch.setattr("src.main.hde_adapter", FakeHDEAdapter())
    monkeypatch.setattr("src.main.process_message", fake_process_message)
    monkeypatch.setattr("src.main.update_delivery_outcome", fake_delivery_update)
    monkeypatch.setattr(fastapi_app.state, "redis", FakeRedis(), raising=False)

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/webhook/hde",
            json={"message": {"id": "message-456"}},
            headers={"X-Webhook-Secret": "secret"},
        )
        duplicate = await client.post(
            "/webhook/hde",
            json={"message": {"id": "message-456"}},
            headers={"X-Webhook-Secret": "secret"},
        )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert calls == {"processed": 1, "sent": 1}
    redis = fastapi_app.state.redis
    assert len(redis.values) == 1
    key, value = next(iter(redis.values.items()))
    assert key.startswith("hde-inbox:v2:")
    assert "ticket-123" not in key
    assert "message-456" not in key
    assert value.startswith("done:")


@pytest.mark.asyncio
async def test_hde_webhook_rejects_missing_or_oversized_ticket_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(webhook_auth_token="secret"),
    )
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post(
            "/webhook/hde",
            json={"visitor": {"id": "visitor-1"}, "message": {"text": "Вопрос"}},
            headers={"X-Webhook-Secret": "secret"},
        )
        oversized = await client.post(
            "/webhook/hde",
            json={"chat_id": "x" * 256, "message": {"text": "Вопрос"}},
            headers={"X-Webhook-Secret": "secret"},
        )

    assert missing.status_code == 422
    assert missing.json()["detail"] == "ticket_id_required"
    assert oversized.status_code == 422
    assert oversized.json()["detail"] == "ticket_id_too_long"


@pytest.mark.asyncio
async def test_hde_webhook_returns_503_when_inbox_redis_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRedis:
        async def set(self, *_args: Any, **_kwargs: Any) -> bool:
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(webhook_auth_token="secret"),
    )
    monkeypatch.setattr(fastapi_app.state, "redis", FailingRedis(), raising=False)
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhook/hde",
            json={
                "chat_id": "ticket-123",
                "message": {"id": "message-456", "text": "Где мой билет?"},
            },
            headers={"X-Webhook-Secret": "secret"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "HDE inbox unavailable"


@pytest.mark.asyncio
async def test_hde_webhook_releases_processing_lease_after_known_non_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"processed": 0, "sent": 0}

    class FakeRedis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        async def set(self, key: str, value: str, **_kwargs: Any) -> bool:
            if key in self.values:
                return False
            self.values[key] = value
            return True

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def eval(self, script: str, _key_count: int, key: str, *args: Any) -> int:
            if self.values.get(key) != args[0]:
                return 0
            assert "DEL" in script
            del self.values[key]
            return 1

    class FakeHDEAdapter:
        def parse(self, _payload: dict[str, Any]) -> IncomingMessage:
            return IncomingMessage(
                user_id="ticket-123",
                channel=Channel.HDE,
                text="Где мой билет?",
                upstream_event_id="message-456",
                upstream_event_id_source="message.id",
            )

        async def send(self, _user_id: str, _text: str) -> HDEDeliveryResult:
            calls["sent"] += 1
            return HDEDeliveryResult(HDEDeliveryStatus.NETWORK_ERROR, attempted=True)

    async def fake_process_message(_message: IncomingMessage, _app: Any) -> str:
        calls["processed"] += 1
        return "Ответ"

    async def fake_delivery_update(*_args: Any, **_kwargs: Any) -> None:
        return None

    redis = FakeRedis()
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(webhook_auth_token="secret"),
    )
    monkeypatch.setattr("src.main.hde_adapter", FakeHDEAdapter())
    monkeypatch.setattr("src.main.process_message", fake_process_message)
    monkeypatch.setattr("src.main.update_delivery_outcome", fake_delivery_update)
    monkeypatch.setattr(fastapi_app.state, "redis", redis, raising=False)

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/webhook/hde",
            json={"message": {"id": "message-456"}},
            headers={"X-Webhook-Secret": "secret"},
        )
        second = await client.post(
            "/webhook/hde",
            json={"message": {"id": "message-456"}},
            headers={"X-Webhook-Secret": "secret"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == {"processed": 2, "sent": 2}
    assert redis.values == {}


@pytest.mark.asyncio
async def test_process_message_serializes_same_ticket_but_not_different_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    active_by_ticket: defaultdict[str, int] = defaultdict(int)
    max_active_by_ticket: defaultdict[str, int] = defaultdict(int)
    active_total = 0
    max_active_total = 0

    class FakeSessions:
        @asynccontextmanager
        async def serialized(self, channel: str, user_id: str) -> Any:
            async with locks[f"{channel}:{user_id}"]:
                yield

    async def fake_unlocked(message: IncomingMessage, _app: Any, **_kwargs: Any) -> str:
        nonlocal active_total, max_active_total
        active_by_ticket[message.user_id] += 1
        active_total += 1
        max_active_by_ticket[message.user_id] = max(
            max_active_by_ticket[message.user_id],
            active_by_ticket[message.user_id],
        )
        max_active_total = max(max_active_total, active_total)
        await asyncio.sleep(0.02)
        active_by_ticket[message.user_id] -= 1
        active_total -= 1
        return message.text

    app = SimpleNamespace(state=SimpleNamespace(sessions=FakeSessions()))
    monkeypatch.setattr("src.main._process_message_unlocked", fake_unlocked)
    messages = [
        IncomingMessage(user_id="ticket-a", channel=Channel.HDE, text="1"),
        IncomingMessage(user_id="ticket-a", channel=Channel.HDE, text="2"),
        IncomingMessage(user_id="ticket-b", channel=Channel.HDE, text="3"),
    ]

    await asyncio.gather(*(process_message(message, app) for message in messages))

    assert max_active_by_ticket["ticket-a"] == 1
    assert max_active_total >= 2
