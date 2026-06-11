from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

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


async def check_models(load_models: bool) -> None:
    check_imports()
    if not load_models:
        print("ml_runtime=ok imports=ok models=not_loaded")
        return

    dense, sparse = await asyncio.to_thread(Embedder().encode, "Проверка bge-m3")
    await asyncio.to_thread(Reranker()._load_model)
    print(
        "ml_runtime=ok "
        f"imports=ok dense_dim={len(dense)} sparse_terms={len(sparse)} "
        "reranker_loaded=true"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check optional local ML runtime.")
    parser.add_argument(
        "--load-models",
        action="store_true",
        help="Download/load bge-m3 and run a tiny encode smoke test.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(check_models(args.load_models))
