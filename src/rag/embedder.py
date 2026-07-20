from __future__ import annotations

from threading import Lock, RLock
from typing import Any

import numpy as np

from src.rag.errors import MLDependencyError
from src.rag.model_location import resolve_model_location

BGE_M3_REPOSITORY = "BAAI/bge-m3"
BGE_M3_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


class Embedder:
    _instance: Embedder | None = None
    _lock = Lock()

    def __new__(cls) -> Embedder:
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._model = None
                cls._instance._model_lock = RLock()
        return cls._instance

    def _load_model(self) -> Any:
        with self._model_lock:
            if self._model is None:
                try:
                    from FlagEmbedding import BGEM3FlagModel
                except ImportError as exc:
                    raise MLDependencyError(
                        "FlagEmbedding is not installed. Install project ML extras or rebuild "
                        "Docker with INSTALL_ML=true to enable bge-m3 embeddings."
                    ) from exc

                model_location = resolve_model_location(
                    environment_name="BGE_M3_MODEL_PATH",
                    default_repo_id=BGE_M3_REPOSITORY,
                    expected_revision=BGE_M3_REVISION,
                    expected_target="bge-m3",
                )
                self._model = BGEM3FlagModel(model_location, use_fp16=False)
        return self._model

    def encode(self, text: str) -> tuple[np.ndarray, dict[str, float]]:
        return self.encode_batch([text])[0]

    def encode_batch(self, texts: list[str]) -> list[tuple[np.ndarray, dict[str, float]]]:
        if not texts:
            return []
        with self._model_lock:
            model = self._load_model()
            output = model.encode(texts, return_dense=True, return_sparse=True)

        encoded: list[tuple[np.ndarray, dict[str, float]]] = []
        for dense_raw, sparse_raw in zip(
            output["dense_vecs"],
            output["lexical_weights"],
            strict=True,
        ):
            dense = np.asarray(dense_raw, dtype=np.float32)
            sparse = {str(key): float(value) for key, value in sparse_raw.items()}
            encoded.append((dense, sparse))
        return encoded

    def unload(self) -> None:
        with self._model_lock:
            self._model = None


def sparse_to_indices_values(sparse: dict[str, float]) -> tuple[list[int], list[float]]:
    pairs = sorted((int(token_id), float(weight)) for token_id, weight in sparse.items())
    return [token_id for token_id, _ in pairs], [weight for _, weight in pairs]
