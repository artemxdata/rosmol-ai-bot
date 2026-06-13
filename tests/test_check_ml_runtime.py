from __future__ import annotations

import pytest

from scripts import check_ml_runtime
from src.models import ScoredChunk
from src.rag.errors import MLDependencyError


class FakeReranker:
    def rerank(self, query: str, chunks: list, top_k: int):
        chunk = chunks[0]
        return [
            ScoredChunk(
                **chunk.model_dump(exclude={"score"}),
                score=chunk.score,
                reranker_score=0.876543,
            )
        ]


class FailingReranker:
    def rerank(self, query: str, chunks: list, top_k: int):
        raise AttributeError("XLMRobertaTokenizer has no attribute prepare_for_model")


@pytest.mark.asyncio
async def test_check_models_runs_reranker_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(check_ml_runtime, "check_imports", lambda: None)
    monkeypatch.setattr(check_ml_runtime, "Reranker", FakeReranker)

    await check_ml_runtime.check_models(load_embedder=False, load_reranker=True)

    output = capsys.readouterr().out
    assert "ml_runtime=ok" in output
    assert "reranker_loaded=true" in output
    assert "reranker_score=0.876543" in output


@pytest.mark.asyncio
async def test_check_models_reports_reranker_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check_ml_runtime, "check_imports", lambda: None)
    monkeypatch.setattr(check_ml_runtime, "Reranker", FailingReranker)

    with pytest.raises(MLDependencyError, match="Reranker runtime smoke test failed") as exc_info:
        await check_ml_runtime.check_models(load_embedder=False, load_reranker=True)

    assert "prepare_for_model" in str(exc_info.value.__cause__)
