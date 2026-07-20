from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.channels.hde_transport import HDEInboxReceipt, HDEStableEventRequired, InboxStatus
from src.main import app as fastapi_app
from src.main import process_message
from src.models import Channel, IncomingMessage
from src.security.pii_masker import PIIMaskingUnavailable


class FakePIIMasker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def mask(self, text: str) -> tuple[str, dict[str, list[str]]]:
        self.calls.append(text)
        return f"[MASKED:{len(self.calls)}]", {"masked": [text]}


class FakeRepository:
    def __init__(self, *, created: bool = True, error: Exception | None = None) -> None:
        self.created = created
        self.error = error
        self.calls: list[tuple[IncomingMessage, dict[str, Any]]] = []

    async def enqueue_inbox(
        self,
        message: IncomingMessage,
        **kwargs: Any,
    ) -> HDEInboxReceipt:
        self.calls.append((message, kwargs))
        if self.error is not None:
            raise self.error
        return HDEInboxReceipt(
            id=17,
            event_key="e" * 64,
            request_id=message.request_id,
            status=InboxStatus.PENDING,
            created=self.created,
        )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(webhook_auth_token="secret")


def _stable_message() -> IncomingMessage:
    return IncomingMessage(
        user_id="raw-ticket-123",
        channel=Channel.HDE,
        text="Write to ivan@example.test",
        forum_context="Ivan Petrov forum",
        attachments=[{"id": "raw-attachment"}],
        upstream_event_id="raw-message-456",
        upstream_event_id_source="message.id",
    )


@pytest.mark.asyncio
async def test_hde_webhook_masks_then_awaits_durable_enqueue_before_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository()
    masker = FakePIIMasker()

    class FakeAdapter:
        def parse(self, _payload: dict[str, Any]) -> IncomingMessage:
            return _stable_message()

        async def send(self, *_args: Any) -> None:
            raise AssertionError("webhook must not send inline")

    async def forbidden_process(*_args: Any) -> str:
        raise AssertionError("webhook must not process inline")

    monkeypatch.setattr("src.main.get_settings", _settings)
    monkeypatch.setattr("src.main.hde_adapter", FakeAdapter())
    monkeypatch.setattr("src.main.process_message", forbidden_process)
    monkeypatch.setattr(
        fastapi_app.state,
        "hde_transport_repository",
        repository,
        raising=False,
    )
    monkeypatch.setattr(fastapi_app.state, "pii_masker", masker, raising=False)

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhook/hde",
            json={"event": "new_message"},
            headers={"X-Webhook-Secret": "secret"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(repository.calls) == 1
    message, kwargs = repository.calls[0]
    assert message.user_id == "raw-ticket-123"
    assert kwargs == {
        "masked_text": "[MASKED:1]",
        "masked_forum_context": "[MASKED:2]",
    }
    safe_arguments = repr(kwargs)
    assert "ivan@example.test" not in safe_arguments
    assert "raw-message-456" not in safe_arguments
    assert "raw-attachment" not in safe_arguments
    assert masker.calls == ["Write to ivan@example.test", "Ivan Petrov forum"]


@pytest.mark.asyncio
async def test_hde_duplicate_receipt_is_acknowledged_without_inline_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(created=False)
    monkeypatch.setattr("src.main.get_settings", _settings)
    monkeypatch.setattr(fastapi_app.state, "hde_transport_repository", repository, raising=False)
    monkeypatch.setattr(fastapi_app.state, "pii_masker", FakePIIMasker(), raising=False)

    class FakeAdapter:
        def parse(self, _payload: dict[str, Any]) -> IncomingMessage:
            return _stable_message()

    monkeypatch.setattr("src.main.hde_adapter", FakeAdapter())
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhook/hde",
            json={"message": {"id": "raw-message-456"}},
            headers={"X-Webhook-Secret": "secret"},
        )

    assert response.status_code == 200
    assert len(repository.calls) == 1


@pytest.mark.asyncio
async def test_hde_webhook_requires_provider_stable_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(error=HDEStableEventRequired("stable_upstream_event_id_required"))
    monkeypatch.setattr("src.main.get_settings", _settings)
    monkeypatch.setattr(fastapi_app.state, "hde_transport_repository", repository, raising=False)
    monkeypatch.setattr(fastapi_app.state, "pii_masker", FakePIIMasker(), raising=False)

    class FallbackAdapter:
        def parse(self, _payload: dict[str, Any]) -> IncomingMessage:
            message = _stable_message()
            message.upstream_event_id = str(message.request_id)
            message.upstream_event_id_source = "request_id_fallback"
            return message

    monkeypatch.setattr("src.main.hde_adapter", FallbackAdapter())
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhook/hde",
            json={"chat_id": "ticket-123", "message": {"text": "Question"}},
            headers={"X-Webhook-Secret": "secret"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "stable_upstream_event_id_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", [None, FakeRepository(error=ConnectionError("db down"))])
async def test_hde_webhook_returns_503_without_durable_commit(
    monkeypatch: pytest.MonkeyPatch,
    repository: FakeRepository | None,
) -> None:
    monkeypatch.setattr("src.main.get_settings", _settings)
    monkeypatch.setattr(fastapi_app.state, "hde_transport_repository", repository, raising=False)
    monkeypatch.setattr(fastapi_app.state, "pii_masker", FakePIIMasker(), raising=False)

    class FakeAdapter:
        def parse(self, _payload: dict[str, Any]) -> IncomingMessage:
            return _stable_message()

    monkeypatch.setattr("src.main.hde_adapter", FakeAdapter())
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhook/hde",
            json={"message": {"id": "message-456"}},
            headers={"X-Webhook-Secret": "secret"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "HDE transport unavailable"


@pytest.mark.asyncio
async def test_hde_webhook_fails_closed_when_name_masking_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository()

    class FailingPIIMasker:
        def mask(self, _text: str) -> tuple[str, dict[str, list[str]]]:
            raise PIIMaskingUnavailable("pii_ner_unavailable")

    class FakeAdapter:
        def parse(self, _payload: dict[str, Any]) -> IncomingMessage:
            return _stable_message()

    monkeypatch.setattr("src.main.get_settings", _settings)
    monkeypatch.setattr("src.main.hde_adapter", FakeAdapter())
    monkeypatch.setattr(
        fastapi_app.state,
        "hde_transport_repository",
        repository,
        raising=False,
    )
    monkeypatch.setattr(
        fastapi_app.state,
        "pii_masker",
        FailingPIIMasker(),
        raising=False,
    )

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhook/hde",
            json={"message": {"id": "message-456"}},
            headers={"X-Webhook-Secret": "secret"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "PII masking unavailable"
    assert repository.calls == []


@pytest.mark.asyncio
async def test_hde_webhook_auth_and_payload_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.main.get_settings", _settings)
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.post("/webhook/hde", json={})
        missing_ticket = await client.post(
            "/webhook/hde",
            json={"message": {"id": "message-1", "text": "Question"}},
            headers={"X-Webhook-Secret": "secret"},
        )
        oversized = await client.post(
            "/webhook/hde",
            json={"chat_id": "x" * 256, "message": {"id": "message-1"}},
            headers={"X-Webhook-Secret": "secret"},
        )

    assert unauthorized.status_code == 401
    assert missing_ticket.status_code == 422
    assert missing_ticket.json()["detail"] == "ticket_id_required"
    assert oversized.status_code == 422
    assert oversized.json()["detail"] == "ticket_id_too_long"


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
