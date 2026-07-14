from __future__ import annotations

from redis.asyncio import Redis

from src.config import get_settings
from src.session.memory import hash_user_id


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.settings = get_settings()

    async def check(self, user_id: str, channel: str) -> bool:
        key = f"rate:{channel}:{hash_user_id(channel, user_id)}"
        count = await self.redis.incr(key)
        if count == 1:
            await self.redis.expire(key, self.settings.rate_limit_window_seconds)
        return int(count) <= self.settings.rate_limit_messages
