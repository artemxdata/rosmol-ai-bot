from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.main import _run_ml_prewarm, ready
from src.security.pii_masker import PIIMaskingUnavailable


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
    def __init__(self, count: int = 1) -> None:
        self._count = count

    async def count(self, *, collection_name: str, exact: bool) -> SimpleNamespace:
        assert collection_name == "knowledge_base"
        assert exact is True
        return SimpleNamespace(count=self._count)


class FakeEmbedder:
    def __init__(self, events: list[str] | None = None) -> None:
        self.queries: list[str] = []
        self.unload_calls = 0
        self.events = events

    def encode(self, query: str) -> tuple[list[float], dict[str, float]]:
        self.queries.append(query)
        if self.events is not None:
            self.events.append("embedder_probe")
        return [0.1], {"1": 0.5}

    def unload(self) -> None:
        self.unload_calls += 1
        if self.events is not None:
            self.events.append("embedder_unload")


class FakeReranker:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[str, int, int]] = []
        self.unload_calls = 0
        self.events = events

    def rerank(self, query: str, chunks: list, top_k: int) -> list:
        self.calls.append((query, len(chunks), top_k))
        if self.events is not None:
            self.events.append("reranker_probe")
        return []

    def unload(self) -> None:
        self.unload_calls += 1
        if self.events is not None:
            self.events.append("reranker_unload")


class FakePIIMasker:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def mask(self, text: str) -> tuple[str, dict[str, list[str]]]:
        self.texts.append(text)
        return "[ИМЯ] спрашивает о регистрации на форум.", {
            "name": ["Иван Иванов"]
        }


class FakeTransportWorker:
    def __init__(self, running: bool = True) -> None:
        self.is_running = running


class FakeTransportRepository:
    def __init__(
        self,
        *,
        inbox_dead: int = 0,
        outbox_dead: int = 0,
        fail: bool = False,
        inbox_ready_age: float | None = 5.0,
        inbox_processing_age: float | None = 10.0,
        outbox_ready_age: float | None = 7.0,
        outbox_sending_age: float | None = None,
    ) -> None:
        self.inbox_dead = inbox_dead
        self.outbox_dead = outbox_dead
        self.fail = fail
        self.inbox_ready_age = inbox_ready_age
        self.inbox_processing_age = inbox_processing_age
        self.outbox_ready_age = outbox_ready_age
        self.outbox_sending_age = outbox_sending_age

    async def get_queue_counts(self) -> SimpleNamespace:
        if self.fail:
            raise ConnectionError("queue query failed")
        values = {
            "inbox_backlog": 2,
            "inbox_processing": 1,
            "inbox_dead_letter": self.inbox_dead,
            "outbox_backlog": 3,
            "outbox_sending": 0,
            "outbox_dead_letter": self.outbox_dead,
            "inbox_oldest_ready_age_seconds": self.inbox_ready_age,
            "inbox_oldest_processing_age_seconds": self.inbox_processing_age,
            "outbox_oldest_ready_age_seconds": self.outbox_ready_age,
            "outbox_oldest_sending_age_seconds": self.outbox_sending_age,
        }
        return SimpleNamespace(**values, as_dict=lambda: dict(values))


def _request(
    *,
    redis_fail: bool = False,
    qdrant_count: int = 1,
    expected_kb_count: int = 1,
    ml_prewarm: dict | None = None,
    runtime_role: str = "api",
    transport_enabled: bool = False,
    transport_worker: FakeTransportWorker | None = None,
    transport_repository: FakeTransportRepository | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                redis=FakeRedis(redis_fail),
                pg_pool=FakePGPool(),
                qdrant=FakeQdrant(qdrant_count),
                runtime_settings=SimpleNamespace(
                    app_env="test",
                    runtime_role=runtime_role,
                    release_git_sha="",
                    hde_transport_enabled=transport_enabled,
                    hde_transport_event_key_secret="e" * 48,
                    hde_transport_encryption_key="k" * 48,
                    hde_transport_lease_timeout_seconds=420,
                    hde_transport_recovery_interval_seconds=30,
                    hde_transport_shutdown_timeout_seconds=420,
                    hde_transport_queue_stale_after_seconds=900,
                    request_timeout_seconds=45,
                    hde_request_timeout_seconds=20,
                    qdrant_knowledge_collection="knowledge_base",
                ),
                runtime_config={"status": "ok", "runtime_role": runtime_role},
                kb_manifest={"status": "ok", "published_records": expected_kb_count},
                ml_prewarm=ml_prewarm,
                hde_transport_worker=transport_worker,
                hde_transport_repository=transport_repository,
            )
        )
    )


@pytest.mark.asyncio
async def test_ready_returns_dependency_checks() -> None:
    response = await ready(_request())  # type: ignore[arg-type]

    assert response == {
        "status": "ready",
        "release_git_sha": None,
        "checks": {
            "config": "ok",
            "redis": "ok",
            "postgres": "ok",
            "knowledge_base": "ok",
        },
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
async def test_ready_rejects_incomplete_knowledge_index() -> None:
    with pytest.raises(HTTPException) as exc:
        await ready(_request(qdrant_count=2, expected_kb_count=3))  # type: ignore[arg-type]

    assert exc.value.status_code == 503
    assert exc.value.detail["checks"]["knowledge_base"] == "error: indexed=2, expected=3"


@pytest.mark.asyncio
async def test_ready_requires_prewarm_for_ml_runtime() -> None:
    with pytest.raises(HTTPException) as exc:
        await ready(_request(runtime_role="ml"))  # type: ignore[arg-type]

    assert exc.value.status_code == 503
    assert exc.value.detail["checks"]["ml_prewarm"] == "error: disabled for ML runtime"


@pytest.mark.asyncio
async def test_ready_reports_healthy_transport_and_safe_aggregate_counts() -> None:
    response = await ready(  # type: ignore[arg-type]
        _request(
            runtime_role="ml",
            ml_prewarm={"enabled": True, "status": "ok"},
            transport_enabled=True,
            transport_worker=FakeTransportWorker(),
            transport_repository=FakeTransportRepository(),
        )
    )

    assert response["checks"]["hde_transport"] == "ok"
    assert response["hde_transport_counts"] == {
        "inbox_backlog": 2,
        "inbox_processing": 1,
        "inbox_dead_letter": 0,
        "outbox_backlog": 3,
        "outbox_sending": 0,
        "outbox_dead_letter": 0,
        "inbox_oldest_ready_age_seconds": 5.0,
        "inbox_oldest_processing_age_seconds": 10.0,
        "outbox_oldest_ready_age_seconds": 7.0,
        "outbox_oldest_sending_age_seconds": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker", "repository", "expected"),
    [
        (None, FakeTransportRepository(), "workers not running"),
        (FakeTransportWorker(False), FakeTransportRepository(), "workers not running"),
        (FakeTransportWorker(), FakeTransportRepository(inbox_dead=1), "dead_letter/HOL"),
        (
            FakeTransportWorker(),
            FakeTransportRepository(inbox_ready_age=901),
            "stale queue/HOL inbox_ready",
        ),
        (
            FakeTransportWorker(),
            FakeTransportRepository(outbox_sending_age=421),
            "stale queue/HOL outbox_sending",
        ),
        (FakeTransportWorker(), FakeTransportRepository(fail=True), "ConnectionError"),
    ],
)
async def test_ready_fails_closed_for_unhealthy_durable_transport(
    worker: FakeTransportWorker | None,
    repository: FakeTransportRepository,
    expected: str,
) -> None:
    with pytest.raises(HTTPException) as exc:
        await ready(  # type: ignore[arg-type]
            _request(
                runtime_role="ml",
                ml_prewarm={"enabled": True, "status": "ok"},
                transport_enabled=True,
                transport_worker=worker,
                transport_repository=repository,
            )
        )

    assert exc.value.status_code == 503
    assert expected in exc.value.detail["checks"]["hde_transport"]


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

    settings = SimpleNamespace(
        ml_unload_after_use=False,
        ml_unload_embedder_after_use=False,
        ml_unload_reranker_after_use=False,
    )

    await _run_ml_prewarm(app, settings)  # type: ignore[arg-type]

    assert pii_masker.texts == ["Иван Иванов спрашивает о регистрации на форум."]
    assert embedder.queries == ["регистрация на форум"]
    assert reranker.calls == [("регистрация на форум", 1, 1)]
    assert embedder.unload_calls == 0
    assert reranker.unload_calls == 0


@pytest.mark.asyncio
async def test_run_ml_prewarm_unloads_models_in_low_memory_probe_order() -> None:
    events: list[str] = []
    embedder = FakeEmbedder(events)
    reranker = FakeReranker(events)
    app = SimpleNamespace(
        state=SimpleNamespace(
            embedder=embedder,
            reranker=reranker,
            pii_masker=FakePIIMasker(),
        )
    )
    settings = SimpleNamespace(
        ml_unload_after_use=False,
        ml_unload_embedder_after_use=True,
        ml_unload_reranker_after_use=True,
    )

    await _run_ml_prewarm(app, settings)  # type: ignore[arg-type]

    assert events == [
        "embedder_probe",
        "embedder_unload",
        "reranker_probe",
        "reranker_unload",
    ]


@pytest.mark.asyncio
async def test_run_ml_prewarm_rejects_fail_open_name_masking() -> None:
    class FailOpenPIIMasker:
        def mask(self, text: str) -> tuple[str, dict[str, list[str]]]:
            return text, {}

    embedder = FakeEmbedder()
    app = SimpleNamespace(
        state=SimpleNamespace(
            embedder=embedder,
            reranker=FakeReranker(),
            pii_masker=FailOpenPIIMasker(),
        )
    )

    with pytest.raises(PIIMaskingUnavailable, match="pii_ner_prewarm_probe_failed"):
        await _run_ml_prewarm(
            app,
            SimpleNamespace(
                ml_unload_after_use=False,
                ml_unload_embedder_after_use=False,
                ml_unload_reranker_after_use=False,
            ),
        )  # type: ignore[arg-type]

    assert embedder.queries == []
