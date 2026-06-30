from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.graph.graph import build_graph
from src.graph.nodes.clarify import OFFTOPIC_SCOPE_NOTE
from src.main import app as fastapi_app
from src.main import process_message
from src.models import Channel, Chunk, IncomingMessage, ScoredChunk, Session
from src.session.memory import hash_user_id


class FakeRateLimiter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    async def check(self, user_id: str, channel: str) -> bool:
        return self.allowed


class FakePIIMasker:
    def __init__(
        self,
        masked_text: str | None = None,
        mapping: dict[str, Any] | None = None,
    ) -> None:
        self.masked_text = masked_text
        self.mapping = mapping or {}

    def mask(self, text: str) -> tuple[str, dict[str, str]]:
        return self.masked_text or text, self.mapping


class FakeRedis:
    def __init__(self) -> None:
        self.set_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def set(self, *args: Any, **kwargs: Any) -> None:
        self.set_calls.append((args, kwargs))
        return None


class FakeSessions:
    def __init__(self, session: Session | None = None) -> None:
        self.appended: list[tuple[str, str]] = []
        self.session = session

    async def get_or_create(self, channel: str, user_id: str) -> Session:
        if self.session is not None:
            return self.session
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
        self.check_calls: list[tuple[str, str | None]] = []
        self.save_calls: list[tuple[str, str | None, str]] = []

    async def check(self, query: str, forum: str | None) -> str | None:
        self.check_calls.append((query, forum))
        return self.response

    async def save(self, query: str, forum: str | None, response: str) -> None:
        self.save_calls.append((query, forum, response))
        return None


class FakeMemory:
    async def upsert(self, **kwargs: Any) -> None:
        return None


class FailingGraph:
    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("graph must not be called in this test")


class CapturingGraph:
    def __init__(self, response: str = "Ответ из graph") -> None:
        self.response = response
        self.seen_state: dict[str, Any] | None = None

    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        self.seen_state = state
        return {**state, "final_response": self.response}


class HangingGraph:
    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {**state, "final_response": "too late"}


class FakeAnalyzerLLM:
    async def generate(self, **kwargs: Any) -> str:
        return (
            '{"forum":"Машук","forum_normalized":"Машук",'
            '"questions":[{"text":"Кто платит за дорогу?","topic":"оплата_проезда",'
            '"category":"форумы","forum_normalized":"Машук"}],'
            '"topics":["оплата_проезда"],"category":"форумы","complexity":"simple"}'
        )


class FakeRetriever:
    async def retrieve(self, query: str, filters: dict[str, Any], top_k: int) -> list[Chunk]:
        return [
            Chunk(
                chunk_id="ctx_travel",
                text="Проезд участник оплачивает самостоятельно.",
                metadata={
                    "chunk_id": "ctx_travel",
                    "category": "форумы",
                    "forum_normalized": "Машук",
                    "topic": "oplata_proezda",
                    "source_type": "xlsx",
                },
                score=0.9,
            )
        ]


class UnscopedFakeRetriever:
    async def retrieve(self, query: str, filters: dict[str, Any], top_k: int) -> list[Chunk]:
        return [
            Chunk(
                chunk_id="ctx_travel",
                text="Проезд участник оплачивает самостоятельно.",
                metadata={"chunk_id": "ctx_travel"},
                score=0.9,
            )
        ]


class LowConfidenceReranker:
    def rerank(self, query: str, chunks: list[Chunk], top_k: int) -> list[ScoredChunk]:
        chunk = chunks[0]
        return [
            ScoredChunk(
                **chunk.model_dump(exclude={"score"}),
                score=chunk.score,
                reranker_score=0.2,
            )
        ]


class HighConfidenceReranker:
    def rerank(self, query: str, chunks: list[Chunk], top_k: int) -> list[ScoredChunk]:
        chunk = chunks[0]
        return [
            ScoredChunk(
                **chunk.model_dump(exclude={"score"}),
                score=chunk.score,
                reranker_score=0.91,
            )
        ]


def _app(
    *,
    allowed: bool = True,
    cached_response: str | None = None,
    masked_text: str | None = None,
    pii_mapping: dict[str, Any] | None = None,
    graph: Any | None = None,
    llm_client: Any | None = None,
    retriever: Any | None = None,
    reranker: Any | None = None,
    session: Session | None = None,
) -> SimpleNamespace:
    state = SimpleNamespace(
        rate_limiter=FakeRateLimiter(allowed),
        pii_masker=FakePIIMasker(masked_text, pii_mapping),
        redis=FakeRedis(),
        sessions=FakeSessions(session),
        semantic_cache=FakeSemanticCache(cached_response),
        graph=graph or FailingGraph(),
        llm_client=llm_client or object(),
        retriever=retriever or object(),
        reranker=reranker or object(),
        memory=FakeMemory(),
    )
    return SimpleNamespace(state=state)


@pytest.fixture
def no_llm_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        session_ttl_seconds=1800,
        cloud_ru_api_key="",
    )
    monkeypatch.setattr("src.main.get_settings", lambda: settings)


@pytest.fixture
def configured_llm_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        session_ttl_seconds=1800,
        cloud_ru_api_key="configured",
        reranker_threshold_low=0.4,
        reranker_threshold_high=0.7,
    )
    monkeypatch.setattr("src.main.get_settings", lambda: settings)
    monkeypatch.setattr("src.graph.edges.get_settings", lambda: settings)
    monkeypatch.setattr("src.graph.nodes.rerank.get_settings", lambda: settings)


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
async def test_process_message_safety_escalates_before_graph(
    no_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    app = _app(masked_text="Меня травят в чате форума")
    message = IncomingMessage(
        user_id="u1",
        channel=Channel.API,
        text="Меня травят в чате форума",
    )

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert "Передаю обращение специалисту" in response
    assert captured_logs[0]["should_escalate"] is True
    assert captured_logs[0]["escalation_reason"] == "safety_bullying"
    assert captured_logs[0]["message_masked"] == "Меня травят в чате форума"
    assert app.state.sessions.appended == []
    assert app.state.semantic_cache.check_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "На площадке ко мне применили силу, помоги решить вопрос",
        "Ко мне пристают на площадке, я не чувствую себя в безопасности",
    ],
)
async def test_process_message_abuse_safety_escalates_before_graph(
    no_llm_settings: None,
    captured_logs: list[dict[str, Any]],
    text: str,
) -> None:
    app = _app()
    message = IncomingMessage(user_id="u1", channel=Channel.API, text=text)

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert response == "Передаю обращение специалисту."
    assert captured_logs[0]["should_escalate"] is True
    assert captured_logs[0]["escalation_reason"] == "safety_abuse"
    assert captured_logs[0]["message_masked"] == text
    assert app.state.sessions.appended == []
    assert app.state.semantic_cache.check_calls == []


@pytest.mark.asyncio
async def test_process_message_operator_request_escalates_before_cache_and_graph(
    configured_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    app = _app(cached_response="Ответ из кэша")
    message = IncomingMessage(user_id="u1", channel=Channel.HDE, text="Позови оператора")

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert response == "Передаю обращение специалисту."
    assert app.state.semantic_cache.check_calls == []
    assert app.state.semantic_cache.save_calls == []
    assert app.state.sessions.appended == [("Позови оператора", response)]
    assert captured_logs[0]["should_escalate"] is True
    assert captured_logs[0]["escalation_reason"] == "operator_requested"


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
async def test_process_message_skips_cache_for_contextual_followup(
    configured_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    session = Session(
        user_id="u1",
        channel=Channel.API,
        user_id_hash=hash_user_id(Channel.API.value, "u1"),
        last_messages=[
            {
                "user": "Подскажи по форуму Амур: как подать заявку?",
                "bot": "Регистрация на форум «Амур» закрыта.",
            }
        ],
    )
    graph = CapturingGraph("Свежий ответ из graph")
    app = _app(
        cached_response="Старый ответ из кэша про проезд",
        graph=graph,
        session=session,
    )
    message = IncomingMessage(
        user_id="u1",
        channel=Channel.API,
        text="А что делать, если я уже подтвердил участие, но теперь не могу поехать?",
    )

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert response == "Свежий ответ из graph"
    assert app.state.semantic_cache.check_calls == []
    assert app.state.semantic_cache.save_calls == []
    assert graph.seen_state is not None
    assert graph.seen_state["cache_allowed"] is False
    assert captured_logs[0]["cache_hit"] is False


@pytest.mark.asyncio
async def test_process_message_skips_cache_for_grant_followup(
    configured_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    session = Session(
        user_id="u1",
        channel=Channel.API,
        user_id_hash=hash_user_id(Channel.API.value, "u1"),
        last_messages=[
            {
                "user": "Как вернуть грантовые средства?",
                "bot": "Для возврата грантовых средств напиши на reportgrant2024@fadm.gov.ru.",
            }
        ],
    )
    graph = CapturingGraph("Свежий ответ по почте")
    app = _app(cached_response="Старый ответ из кэша", graph=graph, session=session)
    message = IncomingMessage(user_id="u1", channel=Channel.API, text="А куда именно писать?")

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert response == "Свежий ответ по почте"
    assert app.state.semantic_cache.check_calls == []
    assert graph.seen_state is not None
    assert graph.seen_state["cache_allowed"] is False


@pytest.mark.asyncio
async def test_process_message_bypass_cache_runs_graph_without_cache_read_or_write(
    configured_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    graph = CapturingGraph("Ответ из graph")
    app = _app(cached_response="Ответ из кэша", graph=graph)
    message = IncomingMessage(user_id="u1", channel=Channel.API, text="Кто платит за дорогу?")

    response = await process_message(message, app, bypass_cache=True)  # type: ignore[arg-type]

    assert response == "Ответ из graph"
    assert app.state.semantic_cache.check_calls == []
    assert app.state.semantic_cache.save_calls == []
    assert captured_logs[0]["cache_hit"] is False


@pytest.mark.asyncio
async def test_process_message_scopes_cache_by_detected_forum_before_graph(
    no_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    app = _app(cached_response="Ответ из кэша", masked_text="[ИМЯ] Вышлите положение")
    message = IncomingMessage(
        user_id="u1",
        channel=Channel.API,
        text="Амур Вышлите положение",
    )

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert response == "Ответ из кэша"
    assert app.state.semantic_cache.check_calls == [
        ("[ИМЯ] Вышлите положение", "Амур")
    ]


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


@pytest.mark.asyncio
async def test_process_message_masks_pii_before_graph_and_logs_trace(
    configured_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    graph = CapturingGraph("Ответ по найденным источникам")
    app = _app(
        graph=graph,
        masked_text="Меня зовут [ИМЯ], хочу на Машук",
        pii_mapping={"names": ["Иван Иванов"]},
    )
    message = IncomingMessage(
        user_id="u1",
        channel=Channel.API,
        text="Меня зовут Иван Иванов, хочу на Машук",
    )

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert response == "Ответ по найденным источникам"
    assert graph.seen_state is not None
    assert graph.seen_state["message_masked"] == "Меня зовут [ИМЯ], хочу на Машук"
    assert app.state.sessions.appended == [
        ("Меня зовут [ИМЯ], хочу на Машук", "Ответ по найденным источникам")
    ]
    assert app.state.redis.set_calls
    assert captured_logs[0]["final_response"] == "Ответ по найденным источникам"


@pytest.mark.asyncio
async def test_process_message_adds_routing_hint_to_graph_state(
    configured_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    graph = CapturingGraph("Ответ про регистрацию")
    app = _app(graph=graph)
    message = IncomingMessage(user_id="u1", channel=Channel.API, text="Регистрация на форум")

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert response == "Ответ про регистрацию"
    assert graph.seen_state is not None
    assert graph.seen_state["routing_hint"]["complexity"] == "simple"
    assert graph.seen_state["routing_hint"]["reason"] == "registration_faq"


@pytest.mark.asyncio
async def test_http_ask_uses_mocked_app_state(
    configured_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    graph = CapturingGraph("HTTP ответ")
    fake = _app(graph=graph, masked_text="[ИМЯ] спрашивает про Машук")
    for name, value in vars(fake.state).items():
        setattr(fastapi_app.state, name, value)

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/ask",
            json={"user_id": "u1", "channel": "api", "text": "Иван спрашивает про Машук"},
        )

    assert response.status_code == 200
    assert response.json()["response"] == "HTTP ответ"
    assert graph.seen_state is not None
    assert graph.seen_state["message_masked"] == "[ИМЯ] спрашивает про Машук"
    assert captured_logs[0]["final_response"] == "HTTP ответ"


@pytest.mark.asyncio
async def test_process_message_low_confidence_graph_path_escalates(
    configured_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    app = _app(
        graph=build_graph(),
        llm_client=FakeAnalyzerLLM(),
        retriever=UnscopedFakeRetriever(),
        reranker=LowConfidenceReranker(),
    )
    message = IncomingMessage(user_id="u1", channel=Channel.API, text="Кто платит за дорогу?")

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert "Передаю обращение специалисту" in response
    assert captured_logs[0]["should_escalate"] is True
    assert captured_logs[0]["escalation_reason"] == "low_confidence"


@pytest.mark.asyncio
async def test_process_message_safe_offtopic_returns_scope_note_without_escalation(
    configured_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    app = _app(graph=build_graph())
    message = IncomingMessage(
        user_id="u1",
        channel=Channel.HDE,
        text="Какая погода завтра в Москве?",
    )

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert response == OFFTOPIC_SCOPE_NOTE
    assert app.state.sessions.appended == [("Какая погода завтра в Москве?", response)]
    assert captured_logs[0]["analysis"].category == "offtopic"
    assert captured_logs[0]["analysis"].is_offtopic is True
    assert captured_logs[0]["should_escalate"] is False
    assert captured_logs[0]["escalation_reason"] is None


@pytest.mark.asyncio
async def test_process_message_high_confidence_returns_source_chunk(
    configured_llm_settings: None,
    captured_logs: list[dict[str, Any]],
) -> None:
    app = _app(
        graph=build_graph(),
        llm_client=FakeAnalyzerLLM(),
        retriever=FakeRetriever(),
        reranker=HighConfidenceReranker(),
    )
    message = IncomingMessage(user_id="u1", channel=Channel.API, text="Кто платит за дорогу?")

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert response == "Проезд участник оплачивает самостоятельно."
    assert app.state.sessions.appended == [
        ("Кто платит за дорогу?", "Проезд участник оплачивает самостоятельно.")
    ]
    assert captured_logs[0]["generator_model"] == "source_chunk"
    assert captured_logs[0]["cited_sources"] == ["ctx_travel"]


@pytest.mark.asyncio
async def test_process_message_escalates_on_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[dict[str, Any]],
) -> None:
    settings = SimpleNamespace(
        session_ttl_seconds=1800,
        cloud_ru_api_key="configured",
        request_timeout_seconds=0.01,
    )
    monkeypatch.setattr("src.main.get_settings", lambda: settings)
    app = _app(graph=HangingGraph())
    message = IncomingMessage(user_id="u1", channel=Channel.API, text="Регистрация на форум")

    response = await process_message(message, app)  # type: ignore[arg-type]

    assert "Передаю обращение специалисту" in response
    assert app.state.sessions.appended
    assert captured_logs[0]["should_escalate"] is True
    assert captured_logs[0]["escalation_reason"] == "request_timeout"
