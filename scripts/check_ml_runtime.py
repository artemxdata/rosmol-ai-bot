from __future__ import annotations

import argparse
import asyncio
import gc
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.models import Chunk
from src.rag.embedder import Embedder
from src.rag.errors import MLDependencyError
from src.rag.reranker import Reranker


def check_imports() -> None:
    try:
        import FlagEmbedding  # noqa: F401
        import torch  # noqa: F401
    except ImportError as exc:
        raise MLDependencyError(
            "ML runtime is not installed. Build the ML image with: "
            "docker compose -f docker-compose.yml -f docker-compose.ml.yml "
            "build index-kb"
        ) from exc


async def check_models(load_embedder: bool, load_reranker: bool) -> None:
    check_imports()
    if not load_embedder and not load_reranker:
        print("ml_runtime=ok imports=ok models=not_loaded")
        return

    parts = ["ml_runtime=ok", "imports=ok"]
    if load_embedder:
        embedder = Embedder()
        try:
            dense, sparse = await asyncio.to_thread(embedder.encode, "Проверка bge-m3")
        except Exception as exc:
            raise MLDependencyError(
                "Embedder runtime smoke test failed. Check FlagEmbedding/bge-m3 "
                "installation and model cache."
            ) from exc
        parts.append(f"embedder_loaded=true dense_dim={len(dense)} sparse_terms={len(sparse)}")
        if load_reranker:
            embedder._model = None
            del dense, sparse
            gc.collect()
    if load_reranker:
        smoke_chunk = Chunk(
            chunk_id="ml-runtime-reranker-smoke",
            text="Подать заявку на форум можно через платформу Росмолодежь.",
            metadata={},
            score=1.0,
        )
        try:
            reranked = await asyncio.to_thread(
                Reranker().rerank,
                "Как подать заявку на форум?",
                [smoke_chunk],
                1,
            )
        except Exception as exc:
            raise MLDependencyError(
                "Reranker runtime smoke test failed. Check FlagEmbedding/transformers "
                "compatibility for bge-reranker-v2-m3."
            ) from exc
        score = reranked[0].reranker_score if reranked else None
        if score is None:
            parts.append("reranker_loaded=true reranker_smoke=empty")
        else:
            parts.append(f"reranker_loaded=true reranker_score={score:.6f}")

    print(" ".join(parts))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check optional local ML runtime.")
    parser.add_argument(
        "--load-models",
        action="store_true",
        help="Download/load bge-m3 and bge-reranker-v2-m3.",
    )
    parser.add_argument(
        "--load-embedder",
        action="store_true",
        help="Download/load bge-m3 and run a tiny encode smoke test.",
    )
    parser.add_argument(
        "--load-reranker",
        action="store_true",
        help="Download/load bge-reranker-v2-m3.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        check_models(
            load_embedder=args.load_models or args.load_embedder,
            load_reranker=args.load_models or args.load_reranker,
        )
    )
