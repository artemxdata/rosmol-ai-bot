from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.main import _run_ml_prewarm, ready


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


class FakeEmbedder:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def encode(self, query: str) -> tuple[list[float], dict[str, float]]:
        self.queries.append(query)
        return [0.1], {"1": 0.5}


class FakeReranker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def rerank(self, query: str, chunks: list, top_k: int) -> list:
        self.calls.append((query, len(chunks), top_k))
        return []


class FakePIIMasker:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def mask(self, text: str) -> tuple[str, dict[str, list[str]]]:
        self.texts.append(text)
        return text, {}


def _request(*, redis_fail: bool = False, ml_prewarm: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                redis=FakeRedis(redis_fail),
                pg_pool=FakePGPool(),
                qdrant=FakeQdrant(),
                ml_prewarm=ml_prewarm,
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


@pytest.mark.asyncio
async def test_ready_reports_failed_ml_prewarm() -> None:
    with pytest.raises(HTTPException) as exc:
        await ready(  # type: ignore[arg-type]
            _request(ml_prewarm={"enabled": True, "status": "error", "error": "TimeoutError"})
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["checks"]["ml_prewarm"] == "error: TimeoutError"


@pytest.mark.asyncio
async def test_run_ml_prewarm_loads_embedder_and_reranker() -> None:
    embedder = FakeEmbedder()
    reranker = FakeReranker()
    pii_masker = FakePIIMasker()
    app = SimpleNamespace(
        state=SimpleNamespace(
            embedder=embedder,
            reranker=reranker,
            pii_masker=pii_masker,
        )
    )

    await _run_ml_prewarm(app)  # type: ignore[arg-type]

    assert pii_masker.texts == ["Иван Иванов спрашивает о регистрации на форум."]
    assert embedder.queries == ["регистрация на форум"]
    assert reranker.calls == [("регистрация на форум", 1, 1)]
