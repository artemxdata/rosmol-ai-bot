from __future__ import annotations

from typing import Any

from src.models import Chunk, ScoredChunk
from src.rag.errors import MLDependencyError


class Reranker:
    def __init__(self) -> None:
        self._model: Any | None = None

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from FlagEmbedding import FlagReranker
            except ImportError as exc:
                raise MLDependencyError(
                    "FlagEmbedding is not installed. Install project ML extras or rebuild "
                    "Docker with INSTALL_ML=true to enable bge-reranker-v2-m3."
                ) from exc

            self._model = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=False)
        return self._model

    def rerank(self, query: str, chunks: list[Chunk], top_k: int = 4) -> list[ScoredChunk]:
        if not chunks:
            return []

        model = self._load_model()
        pairs = [[query, chunk.text] for chunk in chunks]
        raw_scores = model.compute_score(pairs, normalize=True)
        if isinstance(raw_scores, float):
            scores = [raw_scores]
        else:
            scores = [float(score) for score in raw_scores]

        ranked = sorted(zip(chunks, scores, strict=True), key=lambda item: item[1], reverse=True)
        return [
            ScoredChunk(
                **chunk.model_dump(exclude={"score"}),
                score=chunk.score,
                reranker_score=score,
            )
            for chunk, score in ranked[:top_k]
        ]

    def unload(self) -> None:
        self._model = None
