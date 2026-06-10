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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="Кто оплачивает проезд на Машук?")
    parser.add_argument("--forum", default="Машук")
    args = parser.parse_args()

    settings = get_settings()
    retriever = Retriever(AsyncQdrantClient(url=settings.qdrant_url), Embedder())
    chunks = await retriever.retrieve(
        args.query,
        {"forum_normalized": args.forum, "category": "форумы"},
        top_k=5,
    )
    for chunk in chunks:
        print({"chunk_id": chunk.chunk_id, "score": chunk.score, "text": chunk.text[:200]})


if __name__ == "__main__":
    asyncio.run(main())
