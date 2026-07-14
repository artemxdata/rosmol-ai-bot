from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.models import Channel, MemoryRecord, Session
from src.session.manager import RECENT_SESSION_TURNS, SessionManager


class FakeRedis:
    def __init__(self, raw: str | None = None) -> None:
        self.raw = raw
        self.saved: str | None = None
        self.saved_key: str | None = None
        self.get_keys: list[str] = []
        self.expired: list[tuple[str, int]] = []

    async def get(self, key: str) -> str | None:
        self.get_keys.append(key)
        return self.raw

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.saved_key = key
        self.saved = value

    async def expire(self, key: str, ttl: int) -> None:
        self.expired.append((key, ttl))


class FakeMemory:
    def __init__(self, record: MemoryRecord | None = None) -> None:
        self.record = record
        self.appended: list[dict[str, Any]] = []
        self.recent_turns = [
            {"user": "Где мой билет?", "bot": "Уточни мероприятие."},
            {"user": "День молодёжи", "bot": "Проверь папку Спам."},
        ]

    async def get(self, user_id_hash: str, channel: str) -> MemoryRecord | None:
        return self.record

    async def get_recent_turns(
        self,
        user_id_hash: str,
        channel: str,
        *,
        limit: int,
    ) -> list[dict[str, str]]:
        return self.recent_turns[-limit:]

    async def append_turn(self, **kwargs: Any) -> None:
        self.appended.append(kwargs)


class FakeRedisLock:
    def __init__(self) -> None:
        self.acquired = False
        self.released = False

    async def acquire(self) -> bool:
        self.acquired = True
        return True

    async def release(self) -> None:
        self.released = True


class FakeLockingRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.created: tuple[str, int, int] | None = None
        self.lock_instance = FakeRedisLock()

    def lock(self, key: str, *, timeout: int, blocking_timeout: int) -> FakeRedisLock:
        self.created = (key, timeout, blocking_timeout)
        return self.lock_instance


@pytest.mark.asyncio
async def test_restores_structured_context_and_recent_turns_from_postgres() -> None:
    memory = FakeMemory(
        MemoryRecord(
            user_id_hash="hash",
            channel=Channel.API,
            last_forum="День молодёжи",
            last_topics=["bilet"],
            turn_summary="Пользователь: Старый вопрос\nБот: Старый ответ",
            structured_context={
                "entities": {"last_category": "форумы", "city": "Томск"},
                "clarification_history": ["Уточни мероприятие."],
                "pending_clarification": "Где мой билет?",
                "clarification_attempts": 2,
            },
            interaction_count=22,
        )
    )
    manager = SessionManager(FakeRedis(), memory)  # type: ignore[arg-type]

    session = await manager.get_or_create(Channel.API.value, "user-1")

    assert session.forum_context == "День молодёжи"
    assert session.extracted_entities == {
        "last_category": "форумы",
        "city": "Томск",
        "last_topics": ["bilet"],
    }
    assert session.pending_clarification == "Где мой билет?"
    assert session.clarification_attempts == 2
    assert session.clarification_history == ["Уточни мероприятие."]
    assert session.conversation_summary == (
        "Пользователь: Старый вопрос\nБот: Старый ответ"
    )
    assert session.last_messages == memory.recent_turns
    assert session.turn_count == 22


@pytest.mark.asyncio
async def test_session_redis_key_and_payload_do_not_store_raw_external_id() -> None:
    redis = FakeRedis()
    manager = SessionManager(redis, None)  # type: ignore[arg-type]

    session = await manager.get_or_create(Channel.HDE.value, "raw-ticket-id")

    assert redis.get_keys[0].startswith("session:hde:")
    assert "raw-ticket-id" not in redis.get_keys[0]
    assert redis.saved_key == redis.get_keys[0]
    assert redis.saved is not None
    assert "raw-ticket-id" not in redis.saved
    assert session.user_id == session.user_id_hash


@pytest.mark.asyncio
async def test_keeps_recent_window_summarizes_evicted_turn_and_persists_full_turn() -> None:
    redis = FakeRedis()
    memory = FakeMemory()
    manager = SessionManager(redis, memory)  # type: ignore[arg-type]
    session = Session(
        user_id="user-1",
        channel=Channel.HDE,
        user_id_hash="hash",
        forum_context="Амур",
        extracted_entities={"last_category": "форумы", "last_topics": ["proezd"]},
        pending_clarification="Где жить?",
        clarification_attempts=7,
        clarification_history=["Какое мероприятие?", "Где жить?"],
        last_messages=[
            {"user": f"Вопрос {index}", "bot": f"Ответ {index}"}
            for index in range(1, RECENT_SESSION_TURNS + 1)
        ],
        turn_count=20,
    )

    updated = await manager.append_turn(session, "Новый вопрос", "Новый ответ")

    assert len(updated.last_messages) == RECENT_SESSION_TURNS
    assert updated.last_messages[0]["user"] == "Вопрос 2"
    assert updated.last_messages[-1] == {
        "user": "Новый вопрос",
        "bot": "Новый ответ",
    }
    assert updated.conversation_summary == (
        "Пользователь: Вопрос 1\nБот: Ответ 1"
    )
    assert updated.turn_count == 21
    assert redis.saved is not None
    assert len(memory.appended) == 1
    persisted = memory.appended[0]
    assert persisted["turn_index"] == 21
    assert persisted["user_text_masked"] == "Новый вопрос"
    assert persisted["bot_text"] == "Новый ответ"
    assert persisted["structured_context"] == {
        "forum_context": "Амур",
        "entities": {"last_category": "форумы", "last_topics": ["proezd"]},
        "clarification_history": ["Какое мероприятие?", "Где жить?"],
        "pending_clarification": "Где жить?",
        "clarification_attempts": 7,
    }


@pytest.mark.asyncio
async def test_session_turn_lock_uses_hashed_distributed_redis_key() -> None:
    redis = FakeLockingRedis()
    manager = SessionManager(redis, None)  # type: ignore[arg-type]

    async with manager.serialized(Channel.HDE.value, "raw-ticket-id"):
        assert redis.lock_instance.acquired is True

    assert redis.created is not None
    key, timeout, blocking_timeout = redis.created
    assert key.startswith("session-lock:hde:")
    assert "raw-ticket-id" not in key
    assert timeout == 120
    assert blocking_timeout == 90
    assert redis.lock_instance.released is True


@pytest.mark.asyncio
async def test_hde_turn_lock_covers_longer_generation_and_delivery_window() -> None:
    redis = FakeLockingRedis()
    manager = SessionManager(redis, None)  # type: ignore[arg-type]

    async with manager.serialized_hde_turn("raw-ticket-id"):
        assert redis.lock_instance.acquired is True

    assert redis.created is not None
    key, timeout, blocking_timeout = redis.created
    assert key.startswith("hde-turn-lock:hde:")
    assert "raw-ticket-id" not in key
    assert timeout == 180
    assert blocking_timeout == 150
    assert redis.lock_instance.released is True


@pytest.mark.asyncio
async def test_lock_windows_cover_configured_ml_generation_and_hde_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.session.manager.get_settings",
        lambda: SimpleNamespace(
            request_timeout_seconds=150.0,
            hde_request_timeout_seconds=20.0,
        ),
    )
    redis = FakeLockingRedis()
    manager = SessionManager(redis, None)  # type: ignore[arg-type]

    async with manager.serialized(Channel.HDE.value, "raw-ticket-id"):
        pass

    assert redis.created is not None
    _, timeout, blocking_timeout = redis.created
    assert timeout == 180
    assert blocking_timeout == 180

    async with manager.serialized_hde_turn("raw-ticket-id"):
        pass

    assert redis.created is not None
    _, timeout, blocking_timeout = redis.created
    assert timeout == 200
    assert blocking_timeout == 200
