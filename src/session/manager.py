from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from math import ceil
from typing import Any

from loguru import logger
from redis.asyncio import Redis
from redis.exceptions import LockError, RedisError

from src.config import get_settings
from src.models import Channel, Session
from src.session.memory import UserMemory, hash_user_id

RECENT_SESSION_TURNS = 20
SUMMARY_MAX_CHARS = 6000
CLARIFICATION_HISTORY_LIMIT = 50
SESSION_LOCK_MIN_TTL_SECONDS = 120
SESSION_LOCK_MIN_WAIT_SECONDS = 90
HDE_TURN_LOCK_MIN_TTL_SECONDS = 180
HDE_TURN_LOCK_MIN_WAIT_SECONDS = 150
LOCK_SAFETY_MARGIN_SECONDS = 30


class SessionManager:
    def __init__(self, redis: Redis, memory: UserMemory | None = None) -> None:
        self.redis = redis
        self.memory = memory
        self.settings = get_settings()

    @asynccontextmanager
    async def serialized(self, channel: str, user_id: str) -> AsyncIterator[None]:
        """Serialize a full dialog turn across workers for one channel/user pair."""
        turn_window = _timeout_window(
            getattr(self.settings, "request_timeout_seconds", 45.0),
            margin_seconds=LOCK_SAFETY_MARGIN_SECONDS,
        )
        async with self._distributed_lock(
            "session-lock",
            channel,
            user_id,
            ttl_seconds=max(SESSION_LOCK_MIN_TTL_SECONDS, turn_window),
            wait_seconds=max(SESSION_LOCK_MIN_WAIT_SECONDS, turn_window),
        ):
            yield

    @asynccontextmanager
    async def serialized_hde_turn(self, user_id: str) -> AsyncIterator[None]:
        """Keep HDE generation and delivery ordered for one ticket."""
        turn_window = _timeout_window(
            getattr(self.settings, "request_timeout_seconds", 45.0),
            getattr(self.settings, "hde_request_timeout_seconds", 20.0),
            margin_seconds=LOCK_SAFETY_MARGIN_SECONDS,
        )
        async with self._distributed_lock(
            "hde-turn-lock",
            Channel.HDE.value,
            user_id,
            ttl_seconds=max(HDE_TURN_LOCK_MIN_TTL_SECONDS, turn_window),
            wait_seconds=max(HDE_TURN_LOCK_MIN_WAIT_SECONDS, turn_window),
        ):
            yield

    @asynccontextmanager
    async def _distributed_lock(
        self,
        prefix: str,
        channel: str,
        user_id: str,
        *,
        ttl_seconds: int,
        wait_seconds: int,
    ) -> AsyncIterator[None]:
        user_hash = hash_user_id(channel, user_id)
        lock = self.redis.lock(
            f"{prefix}:{channel}:{user_hash}",
            timeout=ttl_seconds,
            blocking_timeout=wait_seconds,
        )
        acquired = await lock.acquire()
        if not acquired:
            raise TimeoutError("session_lock_timeout")
        try:
            yield
        finally:
            try:
                await lock.release()
            except (LockError, RedisError) as exc:
                logger.warning(
                    "session_lock_release_failed",
                    lock_prefix=prefix,
                    channel=channel,
                    user_id_hash=user_hash,
                    error_type=type(exc).__name__,
                )

    async def get_or_create(self, channel: str, user_id: str) -> Session:
        key = self._key(channel, user_id)
        raw = await self.redis.get(key)
        if raw:
            session = Session.model_validate_json(raw)
            await self.redis.expire(key, self.settings.session_ttl_seconds)
            return session

        user_hash = hash_user_id(channel, user_id)
        session = Session(user_id=user_hash, channel=Channel(channel), user_id_hash=user_hash)
        if self.memory is not None:
            memory = await self.memory.get(user_hash, channel)
            if memory:
                context = memory.structured_context
                stored_forum = context.get("forum_context")
                session.forum_context = memory.last_forum or (
                    str(stored_forum) if stored_forum else None
                )
                entities = context.get("entities")
                if isinstance(entities, dict):
                    session.extracted_entities.update(entities)
                if memory.last_topics:
                    session.extracted_entities["last_topics"] = memory.last_topics
                history = context.get("clarification_history")
                if isinstance(history, list):
                    session.clarification_history = [
                        str(item) for item in history[-CLARIFICATION_HISTORY_LIMIT:] if item
                    ]
                pending = context.get("pending_clarification")
                session.pending_clarification = str(pending) if pending else None
                session.clarification_attempts = int(
                    context.get("clarification_attempts") or 0
                )
                session.conversation_summary = memory.turn_summary
                session.turn_count = memory.interaction_count
                session.last_messages = await self.memory.get_recent_turns(
                    user_hash,
                    channel,
                    limit=RECENT_SESSION_TURNS,
                )

        await self._save(session)
        return session

    async def update(self, session: Session, **kwargs: Any) -> Session:
        data = session.model_dump()
        data.update(kwargs)
        updated = Session.model_validate(data)
        await self._save(updated)
        return updated

    async def append_turn(self, session: Session, user_text: str, bot_text: str) -> Session:
        all_messages = [*session.last_messages, {"user": user_text, "bot": bot_text}]
        evicted_messages = all_messages[:-RECENT_SESSION_TURNS]
        messages = all_messages[-RECENT_SESSION_TURNS:]
        turn_index = session.turn_count + 1
        summary = session.conversation_summary
        for evicted in evicted_messages:
            summary = _append_summary(
                summary,
                str(evicted.get("user") or ""),
                str(evicted.get("bot") or ""),
            )
        updated = await self.update(
            session,
            last_messages=messages,
            turn_count=turn_index,
            conversation_summary=summary,
        )
        if self.memory is not None:
            try:
                await self.memory.append_turn(
                    user_id_hash=session.user_id_hash,
                    channel=session.channel.value,
                    turn_index=turn_index,
                    user_text_masked=user_text,
                    bot_text=bot_text,
                    summary=summary,
                    structured_context=_structured_context(updated),
                )
            except Exception as exc:
                logger.warning("conversation_turn_persist_failed", error=str(exc))
        return updated

    async def _save(self, session: Session) -> None:
        await self.redis.set(
            self._key_from_hash(session.channel.value, session.user_id_hash),
            session.model_dump_json(),
            ex=self.settings.session_ttl_seconds,
        )

    def _key(self, channel: str, user_id: str) -> str:
        return self._key_from_hash(channel, hash_user_id(channel, user_id))

    @staticmethod
    def _key_from_hash(channel: str, user_id_hash: str) -> str:
        return f"session:{channel}:{user_id_hash}"


def _timeout_window(*timeouts: float, margin_seconds: int) -> int:
    total = sum(max(0.0, float(timeout or 0.0)) for timeout in timeouts)
    return ceil(total + margin_seconds)


def _append_summary(previous: str | None, user_text: str, bot_text: str) -> str:
    turn = f"Пользователь: {user_text.strip()}\nБот: {bot_text.strip()}"
    summary = f"{previous.strip()}\n{turn}" if previous else turn
    return summary[-SUMMARY_MAX_CHARS:]


def _structured_context(session: Session) -> dict[str, Any]:
    return {
        "forum_context": session.forum_context,
        "entities": session.extracted_entities,
        "clarification_history": session.clarification_history[
            -CLARIFICATION_HISTORY_LIMIT:
        ],
        "pending_clarification": session.pending_clarification,
        "clarification_attempts": session.clarification_attempts,
    }
