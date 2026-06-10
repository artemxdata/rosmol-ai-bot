from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from src.config import get_settings
from src.models import Channel, Session
from src.session.memory import UserMemory, hash_user_id


class SessionManager:
    def __init__(self, redis: Redis, memory: UserMemory | None = None) -> None:
        self.redis = redis
        self.memory = memory
        self.settings = get_settings()

    async def get_or_create(self, channel: str, user_id: str) -> Session:
        key = self._key(channel, user_id)
        raw = await self.redis.get(key)
        if raw:
            session = Session.model_validate_json(raw)
            await self.redis.expire(key, self.settings.session_ttl_seconds)
            return session

        user_hash = hash_user_id(channel, user_id)
        session = Session(user_id=user_id, channel=Channel(channel), user_id_hash=user_hash)
        if self.memory is not None:
            memory = await self.memory.get(user_hash, channel)
            if memory:
                session.forum_context = memory.last_forum
                session.extracted_entities["last_topics"] = memory.last_topics

        await self._save(session)
        return session

    async def update(self, session: Session, **kwargs: Any) -> Session:
        data = session.model_dump()
        data.update(kwargs)
        updated = Session.model_validate(data)
        await self._save(updated)
        return updated

    async def append_turn(self, session: Session, user_text: str, bot_text: str) -> Session:
        messages = [*session.last_messages, {"user": user_text, "bot": bot_text}][-5:]
        return await self.update(session, last_messages=messages, turn_count=session.turn_count + 1)

    async def _save(self, session: Session) -> None:
        await self.redis.set(
            self._key(session.channel.value, session.user_id),
            session.model_dump_json(),
            ex=self.settings.session_ttl_seconds,
        )

    def _key(self, channel: str, user_id: str) -> str:
        return f"session:{channel}:{user_id}"
