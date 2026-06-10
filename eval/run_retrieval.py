from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient

from src.config import get_settings
from src.rag.embedder import Embedder
from src.rag.retriever import Retriever


def compute_recall_at_k(results: list[dict[str, Any]]) -> float | None:
    scored = [item for item in results if item["expected_chunk_ids"]]
    if not scored:
        return None
    hits = sum(
        bool(set(item["retrieved_chunk_ids"]) & set(item["expected_chunk_ids"]))
        for item in scored
    )
    return hits / len(scored)


def _normalize_case(raw: dict[str, Any]) -> dict[str, Any]:
    query = raw.get("query") or raw.get("question") or raw.get("text")
    if not query:
        raise ValueError("golden case must contain query, question, or text")

    expected = (
        raw.get("expected_chunk_ids")
        or raw.get("expected_chunks")
        or raw.get("relevant_chunk_ids")
        or []
    )
    if isinstance(expected, str):
        expected = [expected]

    filters = dict(raw.get("filters") or {})
    for key in ("forum_normalized", "category", "topic"):
        if raw.get(key) and key not in filters:
            filters[key] = raw[key]

    return {
        "id": raw.get("id") or raw.get("case_id") or query,
        "query": str(query),
        "filters": filters,
        "expected_chunk_ids": [str(chunk_id) for chunk_id in expected],
    }


async def run_eval(golden_path: Path, output_path: Path, top_k: int) -> dict[str, Any]:
    golden_raw = await asyncio.to_thread(_read_json, golden_path)
    cases = [_normalize_case(item) for item in golden_raw]
    if not cases:
        metrics = {
            "recall_at_k": None,
            "recall_at_5": None if top_k == 5 else None,
            "top_k": top_k,
            "cases_total": 0,
            "cases_scored": 0,
            "message": "golden_set is empty",
        }
        await asyncio.to_thread(_write_json, output_path, metrics)
        return metrics

    settings = get_settings()
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    retriever = Retriever(qdrant, Embedder())
    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            chunks = await retriever.retrieve(case["query"], case["filters"], top_k=top_k)
            retrieved_ids = [chunk.chunk_id for chunk in chunks]
            results.append(
                {
                    "id": case["id"],
                    "query": case["query"],
                    "filters": case["filters"],
                    "expected_chunk_ids": case["expected_chunk_ids"],
                    "retrieved_chunk_ids": retrieved_ids,
                    "hit": bool(set(retrieved_ids) & set(case["expected_chunk_ids"])),
                }
            )
    finally:
        await qdrant.close()

    recall = compute_recall_at_k(results)
    metrics = {
        "recall_at_k": recall,
        "recall_at_5": recall if top_k == 5 else None,
        "top_k": top_k,
        "cases_total": len(cases),
        "cases_scored": sum(bool(item["expected_chunk_ids"]) for item in results),
        "results": results,
    }
    await asyncio.to_thread(_write_json, output_path, metrics)
    return metrics


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", default="data/golden_set.json")
    parser.add_argument("--output", default="eval/metrics.json")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    metrics = asyncio.run(run_eval(Path(args.golden), Path(args.output), args.top_k))
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
