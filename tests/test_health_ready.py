from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.main import ready


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def ping(self) -> bool:
        if self.fail:
            raise RuntimeError("redis down")
        return True


class FakePGPool:
    async def fetchval(self, query: str) -> int:
        return 1


class FakeQdrant:
    async def get_collections(self) -> list[str]:
        return []


def _request(*, redis_fail: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                redis=FakeRedis(redis_fail),
                pg_pool=FakePGPool(),
                qdrant=FakeQdrant(),
            )
        )
    )


@pytest.mark.asyncio
async def test_ready_returns_dependency_checks() -> None:
    response = await ready(_request())  # type: ignore[arg-type]

    assert response == {
        "status": "ready",
        "checks": {"redis": "ok", "postgres": "ok", "qdrant": "ok"},
    }


@pytest.mark.asyncio
async def test_ready_returns_503_when_dependency_fails() -> None:
    with pytest.raises(HTTPException) as exc:
        await ready(_request(redis_fail=True))  # type: ignore[arg-type]

    assert exc.value.status_code == 503
    assert exc.value.detail["checks"]["redis"] == "error: RuntimeError"
