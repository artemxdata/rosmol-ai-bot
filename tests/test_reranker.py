from __future__ import annotations

from src.rag.reranker import Reranker


class DummyModel:
    def compute_score(self, pairs, normalize=True):
        return [0.2, 0.9]


def test_reranker_orders_by_score(sample_chunks) -> None:
    reranker = Reranker()
    reranker._model = DummyModel()

    result = reranker.rerank("Кто платит за дорогу?", sample_chunks, top_k=2)

    assert [chunk.chunk_id for chunk in result] == ["ctx_2", "ctx_1"]
    assert result[0].reranker_score == 0.9
