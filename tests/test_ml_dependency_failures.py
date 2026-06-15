from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.graph.nodes.rerank import rerank
from src.graph.nodes.retrieve import retrieve
from src.models import QueryAnalysis, Question, ScoredChunk
from src.rag.embedder import Embedder
from src.rag.errors import MLDependencyError
from src.rag.reranker import Reranker


class FailingRetriever:
    async def retrieve(self, query: str, filters: dict[str, Any], top_k: int):
        raise MLDependencyError("FlagEmbedding is not installed")


class FailingReranker:
    def rerank(self, query: str, chunks: list[Any], top_k: int):
        raise MLDependencyError("FlagEmbedding is not installed")


class UnloadableEmbedder:
    def __init__(self) -> None:
        self.unloaded = False

    def unload(self) -> None:
        self.unloaded = True


class UnloadableReranker:
    def __init__(self) -> None:
        self.unloaded = False

    def rerank(self, query: str, chunks: list[Any], top_k: int):
        chunk = chunks[0]
        return [
            ScoredChunk(
                **chunk.model_dump(exclude={"score"}),
                score=chunk.score,
                reranker_score=0.9,
            )
        ]

    def unload(self) -> None:
        self.unloaded = True


def test_embedder_reports_missing_flag_embedding() -> None:
    embedder = Embedder()
    embedder._model = None

    with pytest.raises(MLDependencyError, match="FlagEmbedding is not installed"):
        embedder._load_model()


def test_reranker_reports_missing_flag_embedding() -> None:
    reranker = Reranker()

    with pytest.raises(MLDependencyError, match="FlagEmbedding is not installed"):
        reranker._load_model()


@pytest.mark.asyncio
async def test_retrieve_escalates_when_ml_dependency_is_missing() -> None:
    state = {
        "analysis": QueryAnalysis(
            questions=[Question(text="Кто платит за дорогу?", topic="оплата_проезда")],
            forum_normalized="Машук",
            category="форумы",
        ),
        "retriever": FailingRetriever(),
    }

    result = await retrieve(state)

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "ml_dependency_missing"
    assert "FlagEmbedding is not installed" in result["error"]


@pytest.mark.asyncio
async def test_rerank_unloads_ml_models_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    sample_chunks,
) -> None:
    settings = SimpleNamespace(ml_unload_after_use=True, reranker_threshold_low=0.4)
    monkeypatch.setattr("src.graph.nodes.rerank.get_settings", lambda: settings)
    embedder = UnloadableEmbedder()
    reranker = UnloadableReranker()
    state = {
        "message_masked": "Кто платит за дорогу?",
        "retrieved_chunks": sample_chunks,
        "embedder": embedder,
        "reranker": reranker,
    }

    result = await rerank(state)

    assert result["max_confidence"] == 0.9
    assert embedder.unloaded is True
    assert reranker.unloaded is True


@pytest.mark.asyncio
async def test_rerank_can_keep_reranker_warm_with_split_unload_policy(
    monkeypatch: pytest.MonkeyPatch,
    sample_chunks,
) -> None:
    settings = SimpleNamespace(
        ml_unload_after_use=True,
        ml_unload_embedder_after_use=True,
        ml_unload_reranker_after_use=False,
        reranker_threshold_low=0.4,
    )
    monkeypatch.setattr("src.graph.nodes.rerank.get_settings", lambda: settings)
    embedder = UnloadableEmbedder()
    reranker = UnloadableReranker()
    state = {
        "message_masked": "Кто платит за дорогу?",
        "retrieved_chunks": sample_chunks,
        "embedder": embedder,
        "reranker": reranker,
    }

    result = await rerank(state)

    assert result["max_confidence"] == 0.9
    assert embedder.unloaded is True
    assert reranker.unloaded is False


@pytest.mark.asyncio
async def test_rerank_escalates_when_ml_dependency_is_missing(sample_chunks) -> None:
    state = {
        "message_masked": "Кто платит за дорогу?",
        "retrieved_chunks": sample_chunks,
        "reranker": FailingReranker(),
    }

    result = await rerank(state)

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "ml_dependency_missing"
    assert "FlagEmbedding is not installed" in result["error"]
