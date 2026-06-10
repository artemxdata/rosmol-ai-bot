from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.main import process_message
from src.models import Channel, IncomingMessage, Session
from src.session.memory import hash_user_id


class FakeRateLimiter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    async def check(self, user_id: str, channel: str) -> bool:
        return self.allowed


class FakePIIMasker:
    def mask(self, text: str) -> tuple[str, dict[str, str]]:
        return text, {}


class FakeRedis:
    async def set(self, *args: Any, **kwargs: Any) -> None:
        return None


class FakeSessions:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str]] = []

    async def get_or_create(self, channel: str, user_id: str) -> Session:
        return Session(
            user_id=user_id,
            channel=Channel(channel),
            user_id_hash=hash_user_id(channel, user_id),
        )

    async def append_turn(self, session: Session, user_text: str, bot_text: str) -> Session:
        self.appended.append((user_text, bot_text))
        return session


class FakeSemanticCache:
    def __init__(self, response: str | None = None) -> None:
        self.response = response

    async def check(self, query: str, forum: str | None) -> str | None:
        return self.response

    async def save(self, query: str, forum: str | None, response: str) -> None:
        return None


class FailingGraph:
    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("graph must not be called in this test")


def _app(*, allowed: bool = True, cached_response: str | None = None) -> SimpleNamespace:
    state = SimpleNamespace(
        rate_limiter=FakeRateLimiter(allowed),
        pii_masker=FakePIIMasker(),
        redis=FakeRedis(),
        sessions=FakeSessions(),
        semantic_cache=FakeSemanticCache(cached_response),
        graph=FailingGraph(),
        llm_client=object(),
        retriever=object(),
        reranker=object(),
    )
    return SimpleNamespace(state=state)


@pytest.fixture
def no_llm_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        session_ttl_seconds=1800,
        gigachat_api_key="",
        gigachat_access_token="",
    )
    monkeypatch.setattr("src.main.get_settings", lambda: settings)


@pytest.fixture
def captured_logs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []

    async def fake_safe_log(fastapi_app: Any, state: dict[str, Any]) -> None:
        logs.append(state)

    monkeypatch.setattr("src.main._safe_log", fake_safe_log)
    return logs


@pytest.mark.asyncio
async def test_process_message_rate_limit_logs_without_graph(
    no_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    app = _app(allowed=False)
    message = IncomingMessage(user_id="u1", channel=Channel.API, text="Привет")

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert "Слишком много сообщений" in response
    assert captured_logs[0]["escalation_reason"] == "rate_limited"


@pytest.mark.asyncio
async def test_process_message_returns_semantic_cache_hit(
    no_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    app = _app(cached_response="Ответ из кэша")
    message = IncomingMessage(user_id="u1", channel=Channel.API, text="Кто платит за дорогу?")

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert response == "Ответ из кэша"
    assert app.state.sessions.appended == [("Кто платит за дорогу?", "Ответ из кэша")]
    assert captured_logs[0]["cache_hit"] is True


@pytest.mark.asyncio
async def test_process_message_escalates_when_llm_is_not_configured(
    no_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    app = _app()
    message = IncomingMessage(user_id="u1", channel=Channel.API, text="Расскажи про Машук")

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert "LLM-доступ ещё не настроен" in response
    assert captured_logs[0]["should_escalate"] is True
    assert captured_logs[0]["escalation_reason"] == "llm_not_configured"
