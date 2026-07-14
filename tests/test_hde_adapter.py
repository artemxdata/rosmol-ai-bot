from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.channels.hde import HDEAdapter, HDEDeliveryStatus, _build_hde_posts_url
from src.models import Channel


def test_hde_adapter_parses_nested_new_ticket_payload_and_strips_trigger_prefix() -> None:
    payload = {
        "event": "new_message",
        "chat_id": "ticket-123",
        "visitor": {"id": "user-456", "fields": {"name": "Test User"}},
        "message": {
            "id": "message-789",
            "kind": "visitor",
            "text": "slcb373n93f Как зарегистрироваться на форум?",
        },
    }

    message = HDEAdapter(trigger_prefix="slcb373n93f").parse(payload)

    assert message.user_id == "ticket-123"
    assert message.channel == Channel.HDE
    assert message.text == "Как зарегистрироваться на форум?"
    assert message.attachments == []
    assert message.upstream_event_id == "message-789"
    assert message.upstream_event_id_source == "message.id"


def test_hde_adapter_parses_nested_new_reply_payload_without_prefix() -> None:
    payload = {
        "event": "new_message",
        "chat_id": "ticket-123",
        "visitor": {"id": "user-456", "fields": {"name": "Test User"}},
        "message": {
            "kind": "visitor",
            "text": "Не пришло письмо по форуму, что делать?",
        },
    }

    message = HDEAdapter(trigger_prefix="slcb373n93f").parse(payload)

    assert message.user_id == "ticket-123"
    assert message.channel == Channel.HDE
    assert message.text == "Не пришло письмо по форуму, что делать?"


def test_hde_adapter_parses_explicit_forum_context() -> None:
    payload = {
        "chat_id": "ticket-123",
        "forum_context": "День молодёжи",
        "message": {"kind": "visitor", "text": "Где мой билет?"},
    }

    message = HDEAdapter().parse(payload)

    assert message.forum_context == "День молодёжи"


def test_hde_adapter_keeps_legacy_flat_payload_support() -> None:
    payload = {
        "ticket_id": "legacy-ticket",
        "text": "Передайте оператору",
        "attachments": {"id": "file-1"},
    }

    message = HDEAdapter().parse(payload)

    assert message.user_id == "legacy-ticket"
    assert message.channel == Channel.HDE
    assert message.text == "Передайте оператору"
    assert message.attachments == [{"id": "file-1"}]
    assert message.upstream_event_id == str(message.request_id)
    assert message.upstream_event_id_source == "request_id_fallback"


def test_hde_adapter_rejects_payload_without_ticket_id() -> None:
    with pytest.raises(ValueError, match="ticket_id_required"):
        HDEAdapter().parse(
            {
                "visitor": {"id": "visitor-must-not-be-used-as-ticket"},
                "message": {"text": "Где мой билет?"},
            }
        )


@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        ({"chat_id": "x" * 256, "message": {"text": "Вопрос"}}, "ticket_id_too_long"),
        (
            {"chat_id": "ticket-1", "message": {"text": "x" * 4001}},
            "message_text_too_long",
        ),
        (
            {"chat_id": "ticket-1", "message": {"id": "x" * 256, "text": "Вопрос"}},
            "upstream_event_id_too_long",
        ),
    ),
)
def test_hde_adapter_rejects_oversized_identifiers_and_text(
    payload: dict[str, Any],
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        HDEAdapter().parse(payload)


def test_build_hde_posts_url_accepts_domain_or_api_base() -> None:
    assert (
        _build_hde_posts_url("https://rosmolodezh.helpdeskeddy.com", "123")
        == "https://rosmolodezh.helpdeskeddy.com/api/v2/tickets/123/posts/"
    )
    assert (
        _build_hde_posts_url("https://rosmolodezh.helpdeskeddy.com/api/v2/", "ABC 123")
        == "https://rosmolodezh.helpdeskeddy.com/api/v2/tickets/ABC%20123/posts/"
    )


@pytest.mark.asyncio
async def test_hde_send_posts_public_reply_with_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        status_code = 201

        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str], auth: object) -> FakeResponse:
            captured["url"] = url
            captured["data"] = data
            captured["auth_type"] = type(auth).__name__
            return FakeResponse()

    monkeypatch.setattr("src.channels.hde.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        "src.channels.hde.get_settings",
        lambda: SimpleNamespace(
            hde_base_url="https://rosmolodezh.helpdeskeddy.com",
            hde_api_email="bot@example.com",
            hde_api_key="secret",
            hde_bot_user_id="42",
            hde_request_timeout_seconds=7,
        ),
    )

    result = await HDEAdapter().send("123", "Ответ бота")

    assert captured["url"] == "https://rosmolodezh.helpdeskeddy.com/api/v2/tickets/123/posts/"
    assert captured["data"] == {"text": "Ответ бота", "user_id": "42"}
    assert captured["auth_type"] == "BasicAuth"
    assert captured["timeout"] == 7
    assert result.status == HDEDeliveryStatus.DELIVERED
    assert result.status_code == 201


@pytest.mark.asyncio
async def test_hde_send_skips_when_local_rate_limit_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRateLimiter:
        async def try_acquire(self, *, rpm: int) -> tuple[bool, str, float]:
            assert rpm == 250
            return False, "hde_local_rpm_limit_reached", 12.5

    class FailingAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("HTTP client must not be created")

    monkeypatch.setattr("src.channels.hde.httpx.AsyncClient", FailingAsyncClient)
    monkeypatch.setattr(
        "src.channels.hde.get_settings",
        lambda: SimpleNamespace(
            hde_base_url="https://rosmolodezh.helpdeskeddy.com",
            hde_api_email="bot@example.com",
            hde_api_key="secret",
            hde_bot_user_id="",
            hde_request_timeout_seconds=7,
            hde_rate_limit_rpm=250,
            hde_rate_limit_remaining_reserve=30,
            hde_rate_limit_ban_seconds=1200,
        ),
    )

    result = await HDEAdapter(rate_limiter=FakeRateLimiter()).send("123", "Ответ")

    assert result.status == HDEDeliveryStatus.RATE_LIMITED
    assert result.attempted is False
    assert result.retry_after_seconds == 12.5


@pytest.mark.asyncio
async def test_hde_send_handles_hde_ban_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeRateLimiter:
        async def try_acquire(self, *, rpm: int) -> tuple[bool, None, None]:
            captured["rpm"] = rpm
            return True, None, None

        async def block_for(self, seconds: float) -> None:
            captured["blocked_for"] = seconds

    class FakeResponse:
        status_code = 401
        headers = {"X-Rate-Limit": "300", "X-Rate-Limit-Remaining": "0"}

        def json(self) -> dict[str, Any]:
            return {
                "errors": [
                    {
                        "code": "e-401",
                        "title": "Ban",
                        "details": "Ban for 20 min. API limit reached (300 request per minute).",
                    }
                ]
            }

        def raise_for_status(self) -> None:
            raise AssertionError("rate limit response must not raise")

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str], auth: object) -> FakeResponse:
            captured["url"] = url
            return FakeResponse()

    monkeypatch.setattr("src.channels.hde.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        "src.channels.hde.get_settings",
        lambda: SimpleNamespace(
            hde_base_url="https://rosmolodezh.helpdeskeddy.com",
            hde_api_email="bot@example.com",
            hde_api_key="secret",
            hde_bot_user_id="",
            hde_request_timeout_seconds=7,
            hde_rate_limit_rpm=250,
            hde_rate_limit_remaining_reserve=30,
            hde_rate_limit_ban_seconds=1200,
        ),
    )

    result = await HDEAdapter(rate_limiter=FakeRateLimiter()).send("123", "Ответ")

    assert captured["rpm"] == 250
    assert captured["blocked_for"] == 1200
    assert captured["url"] == "https://rosmolodezh.helpdeskeddy.com/api/v2/tickets/123/posts/"
    assert result.status == HDEDeliveryStatus.RATE_LIMITED
    assert result.attempted is True
    assert result.status_code == 401


@pytest.mark.asyncio
async def test_hde_send_blocks_future_sends_when_remaining_is_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeRateLimiter:
        async def try_acquire(self, *, rpm: int) -> tuple[bool, None, None]:
            return True, None, None

        async def block_for(self, seconds: float) -> None:
            captured["blocked_for"] = seconds

    class FakeResponse:
        status_code = 201
        headers = {"X-Rate-Limit": "300", "X-Rate-Limit-Remaining": "20"}

        def json(self) -> dict[str, Any]:
            return {}

        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str], auth: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("src.channels.hde.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        "src.channels.hde.get_settings",
        lambda: SimpleNamespace(
            hde_base_url="https://rosmolodezh.helpdeskeddy.com",
            hde_api_email="bot@example.com",
            hde_api_key="secret",
            hde_bot_user_id="",
            hde_request_timeout_seconds=7,
            hde_rate_limit_rpm=250,
            hde_rate_limit_remaining_reserve=30,
            hde_rate_limit_ban_seconds=1200,
        ),
    )

    await HDEAdapter(rate_limiter=FakeRateLimiter()).send("123", "РћС‚РІРµС‚")

    assert captured["blocked_for"] == 60.0


@pytest.mark.asyncio
async def test_hde_send_skips_when_api_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("HTTP client must not be created")

    monkeypatch.setattr("src.channels.hde.httpx.AsyncClient", FailingAsyncClient)
    monkeypatch.setattr(
        "src.channels.hde.get_settings",
        lambda: SimpleNamespace(
            hde_base_url="",
            hde_api_email="",
            hde_api_key="",
            hde_bot_user_id="",
            hde_request_timeout_seconds=7,
        ),
    )

    result = await HDEAdapter().send("123", "Ответ бота")

    assert result.status == HDEDeliveryStatus.NOT_CONFIGURED
    assert result.attempted is False


@pytest.mark.asyncio
async def test_hde_send_returns_timeout_delivery_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **_kwargs: object) -> object:
            raise httpx.ReadTimeout("timeout", request=httpx.Request("POST", url))

    monkeypatch.setattr("src.channels.hde.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        "src.channels.hde.get_settings",
        lambda: SimpleNamespace(
            hde_base_url="https://rosmolodezh.helpdeskeddy.com",
            hde_api_email="bot@example.com",
            hde_api_key="secret",
            hde_bot_user_id="",
            hde_request_timeout_seconds=7,
            hde_rate_limit_rpm=250,
            hde_rate_limit_remaining_reserve=30,
            hde_rate_limit_ban_seconds=1200,
        ),
    )

    result = await HDEAdapter().send("123", "Ответ")

    assert result.status == HDEDeliveryStatus.TIMEOUT
    assert result.attempted is True
    assert result.error_code == "hde_timeout"
