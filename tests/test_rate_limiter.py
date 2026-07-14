from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.security.rate_limiter import RateLimiter


class FakeRedis:
    def __init__(self) -> None:
        self.key: str | None = None
        self.expired: tuple[str, int] | None = None

    async def incr(self, key: str) -> int:
        self.key = key
        return 1

    async def expire(self, key: str, ttl: int) -> None:
        self.expired = (key, ttl)


@pytest.mark.asyncio
async def test_rate_limiter_uses_hmac_pseudonym_in_redis_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        rate_limit_window_seconds=300,
        rate_limit_messages=20,
        user_hash_secret="dedicated-secret",
        webhook_auth_token="",
        api_auth_token="",
        admin_auth_token="",
        hde_api_key="",
    )
    monkeypatch.setattr("src.security.rate_limiter.get_settings", lambda: settings)
    monkeypatch.setattr("src.session.memory.get_settings", lambda: settings)
    redis = FakeRedis()

    allowed = await RateLimiter(redis).check("raw-ticket-id", "hde")  # type: ignore[arg-type]

    assert allowed is True
    assert redis.key is not None
    assert redis.key.startswith("rate:hde:")
    assert "raw-ticket-id" not in redis.key
    assert redis.expired == (redis.key, 300)
