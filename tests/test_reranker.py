from __future__ import annotations

from src.rag.reranker import Reranker


class DummyModel:
    def __init__(self) -> None:
        self.calls = 0
        self.pairs = []

    def compute_score(self, pairs, normalize=True):
        self.calls += 1
        self.pairs.append(pairs)
        return [0.2, 0.9, 0.7, 0.1][: len(pairs)]


def test_reranker_orders_by_score(sample_chunks) -> None:
    reranker = Reranker()
    model = DummyModel()
    reranker._model = model

    result = reranker.rerank("Кто платит за дорогу?", sample_chunks, top_k=2)

    assert [chunk.chunk_id for chunk in result] == ["ctx_2", "ctx_1"]
    assert result[0].reranker_score == 0.9
    assert model.calls == 1


def test_reranker_batches_group_scores(sample_chunks) -> None:
    reranker = Reranker()
    model = DummyModel()
    reranker._model = model

    result = reranker.rerank_groups(
        [
            ("Кто платит за дорогу?", sample_chunks, 1),
            ("Как зарегистрироваться?", sample_chunks, 2),
        ]
    )

    assert [[chunk.chunk_id for chunk in group] for group in result] == [
        ["ctx_2"],
        ["ctx_1", "ctx_2"],
    ]
    assert model.calls == 1
    assert len(model.pairs[0]) == 4
