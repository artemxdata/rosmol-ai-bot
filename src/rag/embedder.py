from __future__ import annotations

from threading import Lock
from typing import Any

import numpy as np


class Embedder:
    _instance: Embedder | None = None
    _lock = Lock()

    def __new__(cls) -> Embedder:
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._model = None
        return cls._instance

    def _load_model(self) -> Any:
        if self._model is None:
            from FlagEmbedding import BGEM3FlagModel

            self._model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)
        return self._model

    def encode(self, text: str) -> tuple[np.ndarray, dict[str, float]]:
        model = self._load_model()
        output = model.encode([text], return_dense=True, return_sparse=True)
        dense = np.asarray(output["dense_vecs"][0], dtype=np.float32)
        sparse_raw = output["lexical_weights"][0]
        sparse = {str(key): float(value) for key, value in sparse_raw.items()}
        return dense, sparse


def sparse_to_indices_values(sparse: dict[str, float]) -> tuple[list[int], list[float]]:
    pairs = sorted((int(token_id), float(weight)) for token_id, weight in sparse.items())
    return [token_id for token_id, _ in pairs], [weight for _, weight in pairs]
