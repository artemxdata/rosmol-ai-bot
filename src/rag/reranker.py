from __future__ import annotations

from threading import RLock
from typing import Any

from src.models import Chunk, ScoredChunk
from src.rag.errors import MLDependencyError
from src.rag.model_location import resolve_model_location

RerankGroup = tuple[str, list[Chunk], int]
BGE_RERANKER_REPOSITORY = "BAAI/bge-reranker-v2-m3"
BGE_RERANKER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"


class Reranker:
    def __init__(self) -> None:
        self._model: Any | None = None
        self._model_lock = RLock()

    def _load_model(self) -> Any:
        with self._model_lock:
            if self._model is None:
                try:
                    from FlagEmbedding import FlagReranker
                except ImportError as exc:
                    raise MLDependencyError(
                        "FlagEmbedding is not installed. Install project ML extras or rebuild "
                        "Docker with INSTALL_ML=true to enable bge-reranker-v2-m3."
                    ) from exc

                model_location = resolve_model_location(
                    environment_name="BGE_RERANKER_MODEL_PATH",
                    default_repo_id=BGE_RERANKER_REPOSITORY,
                    expected_revision=BGE_RERANKER_REVISION,
                    expected_target="bge-reranker-v2-m3",
                )
                self._model = FlagReranker(model_location, use_fp16=False)
        return self._model

    def rerank(self, query: str, chunks: list[Chunk], top_k: int = 4) -> list[ScoredChunk]:
        if not chunks:
            return []

        return self.rerank_groups([(query, chunks, top_k)])[0]

    def rerank_groups(self, groups: list[RerankGroup]) -> list[list[ScoredChunk]]:
        prepared = [(query, chunks, top_k) for query, chunks, top_k in groups if chunks]
        if not prepared:
            return [[] for _ in groups]

        flattened_pairs = [
            [query, chunk.text]
            for query, chunks, _ in prepared
            for chunk in chunks
        ]
        scores = self._compute_scores(flattened_pairs)

        output_by_group: list[list[ScoredChunk]] = []
        score_offset = 0
        prepared_offset = 0
        for _, chunks, top_k in groups:
            if not chunks:
                output_by_group.append([])
                continue
            _, prepared_chunks, _ = prepared[prepared_offset]
            if chunks is not prepared_chunks:
                raise RuntimeError("rerank group order mismatch")
            prepared_offset += 1

            group_scores = scores[score_offset : score_offset + len(chunks)]
            score_offset += len(chunks)
            output_by_group.append(_rank_scored_chunks(chunks, group_scores, top_k))

        return output_by_group

    def _compute_scores(self, pairs: list[list[str]]) -> list[float]:
        with self._model_lock:
            model = self._load_model()
            raw_scores = model.compute_score(pairs, normalize=True)
        if isinstance(raw_scores, float):
            return [raw_scores]
        return [float(score) for score in raw_scores]

    def unload(self) -> None:
        with self._model_lock:
            self._model = None


def _rank_scored_chunks(
    chunks: list[Chunk],
    scores: list[float],
    top_k: int,
) -> list[ScoredChunk]:
    ranked = sorted(zip(chunks, scores, strict=True), key=lambda item: item[1], reverse=True)
    return [
        ScoredChunk(
            **chunk.model_dump(exclude={"score"}),
            score=chunk.score,
            reranker_score=score,
        )
        for chunk, score in ranked[:top_k]
    ]
