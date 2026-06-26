from __future__ import annotations

from threading import Lock, RLock
from typing import Any

import numpy as np

from src.rag.errors import MLDependencyError


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

                self._model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)
        return self._model

    def encode(self, text: str) -> tuple[np.ndarray, dict[str, float]]:
        with self._model_lock:
            model = self._load_model()
            output = model.encode([text], return_dense=True, return_sparse=True)
        dense = np.asarray(output["dense_vecs"][0], dtype=np.float32)
        sparse_raw = output["lexical_weights"][0]
        sparse = {str(key): float(value) for key, value in sparse_raw.items()}
        return dense, sparse

    def unload(self) -> None:
        with self._model_lock:
            self._model = None


def sparse_to_indices_values(sparse: dict[str, float]) -> tuple[list[int], list[float]]:
    pairs = sorted((int(token_id), float(weight)) for token_id, weight in sparse.items())
    return [token_id for token_id, _ in pairs], [weight for _, weight in pairs]
