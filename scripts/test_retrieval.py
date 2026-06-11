from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from qdrant_client import AsyncQdrantClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import get_settings
from src.rag.embedder import Embedder
from src.rag.retriever import Retriever
from src.rag.seed_retriever import SeedRetriever


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="Кто оплачивает проезд на Машук?")
    parser.add_argument("--forum", default="Машук")
    parser.add_argument("--category", default="форумы")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--kb-seed", type=Path, default=Path("data/knowledge_base_seed.json"))
    args = parser.parse_args()

    filters = {"forum_normalized": args.forum, "category": args.category}
    if args.offline:
        retriever = SeedRetriever.from_path(args.kb_seed)
        chunks = retriever.retrieve(args.query, filters, top_k=args.top_k)
    else:
        settings = get_settings()
        retriever = Retriever(AsyncQdrantClient(url=settings.qdrant_url), Embedder())
        chunks = await retriever.retrieve(args.query, filters, top_k=args.top_k)

    for chunk in chunks:
        print({"chunk_id": chunk.chunk_id, "score": chunk.score, "text": chunk.text[:200]})


if __name__ == "__main__":
    asyncio.run(main())
