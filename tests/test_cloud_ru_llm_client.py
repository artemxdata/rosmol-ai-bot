from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from src.llm.client import CloudRuLLMClient
from src.llm.usage import reset_llm_usage_collection, start_llm_usage_collection


def _settings(**overrides):
    values = {
        "cloud_ru_api_key": "",
        "cloud_ru_chat_completions_url": (
            "https://foundation-models.api.cloud.ru/v1/chat/completions"
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_cloud_ru_client_strips_bearer_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.llm.client.get_settings", lambda: _settings())

    client = CloudRuLLMClient(api_key="Bearer cloud-key")

    assert client.api_key == "cloud-key"


def test_cloud_ru_client_rejects_url_without_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.llm.client.get_settings",
        lambda: _settings(cloud_ru_chat_completions_url="foundation-models.api.cloud.ru/v1"),
    )

    with pytest.raises(ValueError, match="Cloud.ru chat completions URL"):
        CloudRuLLMClient(api_key="cloud-key")


@pytest.mark.asyncio
async def test_cloud_ru_client_posts_openai_compatible_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["payload"] = request.read()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}], "usage": {"total_tokens": 1}},
    )

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            self._client = original_async_client(transport=transport)

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, exc_type, exc, tb):
            await self._client.aclose()

    monkeypatch.setattr("src.llm.client.get_settings", lambda: _settings())
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    client = CloudRuLLMClient(api_key="cloud-key")
    usage_events, token = start_llm_usage_collection()
    try:
        answer = await client.generate(
            model="ai-sage/GigaChat3-10B-A1.8B",
            system="system",
            user="user",
            response_format="json",
            max_tokens=50,
        )
    finally:
        reset_llm_usage_collection(token)

    assert answer == "OK"
    assert usage_events[0]["model"] == "ai-sage/GigaChat3-10B-A1.8B"
    assert usage_events[0]["total_tokens"] == 1
    headers = captured["headers"]
    assert isinstance(headers, httpx.Headers)
    assert headers["Authorization"] == "Bearer cloud-key"
    assert b'"model":"ai-sage/GigaChat3-10B-A1.8B"' in captured["payload"]
    assert b'"response_format":{"type":"json_object"}' in captured["payload"]


def test_cloud_ru_client_summarizes_unauthorized() -> None:
    request = httpx.Request("POST", "https://foundation-models.api.cloud.ru/v1/chat/completions")
    response = httpx.Response(401, request=request, text="Unauthorized")
    exc = httpx.HTTPStatusError("unauthorized", request=request, response=response)

    assert CloudRuLLMClient._summarize_exception(exc) == "Unauthorized"
    assert CloudRuLLMClient._is_retryable(exc) is False


def test_cloud_ru_client_uses_latency_guardrail_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.llm.client.get_settings",
        lambda: _settings(
            cloud_ru_request_timeout_seconds=7,
            cloud_ru_max_retries=1,
        ),
    )

    client = CloudRuLLMClient(api_key="cloud-key")

    assert client.timeout == 7
    assert client.max_retries == 1


@pytest.mark.asyncio
async def test_cloud_ru_client_respects_retry_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    request = httpx.Request("POST", "https://foundation-models.api.cloud.ru/v1/chat/completions")

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, *args, **kwargs) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, request=request, text="temporary failure")

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(
        "src.llm.client.get_settings",
        lambda: _settings(
            cloud_ru_request_timeout_seconds=7,
            cloud_ru_max_retries=2,
        ),
    )
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("src.llm.client.asyncio.sleep", fake_sleep)

    client = CloudRuLLMClient(api_key="cloud-key")

    with pytest.raises(RuntimeError, match="Cloud.ru LLM request failed after retries"):
        await client.generate(model="model", system="system", user="user")

    assert calls == 2
