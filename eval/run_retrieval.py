from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.ask_cases import seed_smoke_query, select_balanced_records
from src.config import get_settings
from src.rag.embedder import Embedder
from src.rag.retriever import Retriever
from src.rag.seed_retriever import SeedRetriever


def compute_recall_at_k(results: list[dict[str, Any]]) -> float | None:
    return compute_recall(results)


def compute_recall(results: list[dict[str, Any]], cutoff: int | None = None) -> float | None:
    scored = [item for item in results if item["expected_chunk_ids"]]
    if not scored:
        return None
    hits = sum(
        bool(set(_retrieved_for_cutoff(item, cutoff)) & set(item["expected_chunk_ids"]))
        for item in scored
    )
    return hits / len(scored)


def rank_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [item for item in results if item["expected_chunk_ids"]]
    ranks = [item["expected_rank"] for item in scored if item.get("expected_rank") is not None]
    reciprocal_ranks = [1 / int(rank) for rank in ranks]
    rank_counts = Counter(int(rank) for rank in ranks)
    return {
        "hits": len(ranks),
        "misses": len(scored) - len(ranks),
        "mrr": _average(reciprocal_ranks),
        "avg_expected_rank": _average([float(rank) for rank in ranks]),
        "expected_rank_histogram": {
            str(rank): rank_counts[rank] for rank in sorted(rank_counts)
        },
    }


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


async def run_eval(
    golden_path: Path,
    output_path: Path,
    top_k: int,
    backend: str = "qdrant",
    kb_seed_path: Path = Path("data/knowledge_base_seed.json"),
    auto_smoke_cases: bool = False,
    max_smoke_cases: int = 100,
    markdown_path: Path | None = None,
) -> dict[str, Any]:
    golden_raw = await asyncio.to_thread(_read_json, golden_path)
    cases = [_normalize_case(item) for item in golden_raw]
    generated_smoke_cases = False
    seed_records: list[dict[str, Any]] | None = None
    if not cases and auto_smoke_cases:
        seed_records = await asyncio.to_thread(_read_json, kb_seed_path)
        cases = build_seed_smoke_cases(seed_records, max_cases=max_smoke_cases)
        generated_smoke_cases = True
    if not cases:
        metrics = {
            "recall_at_k": None,
            "recall_at_1": None,
            "recall_at_3": None,
            "recall_at_5": None,
            "recall_at_10": None,
            "top_k": top_k,
            "backend": backend,
            "cases_total": 0,
            "cases_scored": 0,
            "generated_smoke_cases": False,
            "message": "golden_set is empty",
        }
        await asyncio.to_thread(_write_json, output_path, metrics)
        if markdown_path:
            await asyncio.to_thread(_write_markdown, markdown_path, metrics)
        return metrics

    results: list[dict[str, Any]] = []
    if backend == "lexical":
        if seed_records is None:
            seed_records = await asyncio.to_thread(_read_json, kb_seed_path)
        seed_retriever = SeedRetriever(seed_records)
        for case in cases:
            chunks = seed_retriever.retrieve(case["query"], case["filters"], top_k=top_k)
            results.append(_case_result(case, [chunk.chunk_id for chunk in chunks]))
    elif backend == "qdrant":
        settings = get_settings()
        qdrant = AsyncQdrantClient(url=settings.qdrant_url)
        retriever = Retriever(qdrant, Embedder())
        try:
            for case in cases:
                chunks = await retriever.retrieve(case["query"], case["filters"], top_k=top_k)
                results.append(_case_result(case, [chunk.chunk_id for chunk in chunks]))
        finally:
            await qdrant.close()
    else:
        raise ValueError("backend must be qdrant or lexical")

    recall = compute_recall_at_k(results)
    recall_cutoffs = {
        f"recall_at_{cutoff}": compute_recall(results, cutoff)
        for cutoff in (1, 3, 5, 10)
        if top_k >= cutoff
    }
    metrics = {
        "recall_at_k": recall,
        "recall_at_5": recall_cutoffs.get("recall_at_5"),
        "recall_at_10": recall_cutoffs.get("recall_at_10"),
        "top_k": top_k,
        "backend": backend,
        "cases_total": len(cases),
        "cases_scored": sum(bool(item["expected_chunk_ids"]) for item in results),
        "generated_smoke_cases": generated_smoke_cases,
        "results": results,
    }
    metrics.update(recall_cutoffs)
    metrics.update(rank_summary(results))
    await asyncio.to_thread(_write_json, output_path, metrics)
    if markdown_path:
        await asyncio.to_thread(_write_markdown, markdown_path, metrics)
    return metrics


def build_seed_smoke_cases(
    records: list[dict[str, Any]],
    max_cases: int = 100,
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    selected = select_balanced_records(records, max_cases=max_cases, per_forum_limit=3)
    for record in selected:
        query = seed_smoke_query(record)
        filters: dict[str, Any] = {}
        if record.get("category"):
            filters["category"] = record["category"]
        if record.get("forum_normalized"):
            filters["forum_normalized"] = record["forum_normalized"]
        cases.append(
            {
                "id": f"seed_smoke::{record['chunk_id']}",
                "query": query,
                "filters": filters,
                "expected_chunk_ids": [str(record["chunk_id"])],
            }
        )
    return cases


def _case_result(case: dict[str, Any], retrieved_ids: list[str]) -> dict[str, Any]:
    expected_rank = _expected_rank(retrieved_ids, case["expected_chunk_ids"])
    return {
        "id": case["id"],
        "query": case["query"],
        "filters": case["filters"],
        "expected_chunk_ids": case["expected_chunk_ids"],
        "retrieved_chunk_ids": retrieved_ids,
        "expected_rank": expected_rank,
        "hit": bool(set(retrieved_ids) & set(case["expected_chunk_ids"])),
    }


def _expected_rank(retrieved_ids: list[str], expected_ids: list[str]) -> int | None:
    expected = set(expected_ids)
    for index, chunk_id in enumerate(retrieved_ids, start=1):
        if chunk_id in expected:
            return index
    return None


def _retrieved_for_cutoff(item: dict[str, Any], cutoff: int | None) -> list[str]:
    retrieved = item["retrieved_chunk_ids"]
    if cutoff is None:
        return retrieved
    return retrieved[:cutoff]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_markdown(path: Path, metrics: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    top_k = metrics.get("top_k")
    recall = metrics.get("recall_at_k")
    lines = [
        "# Retrieval Eval Report",
        "",
        f"- Backend: `{metrics.get('backend')}`",
        f"- Cases: `{metrics.get('cases_total')}`",
        f"- Scored cases: `{metrics.get('cases_scored')}`",
        f"- Recall@{top_k}: `{_format_rate(recall)}`",
        f"- Recall@5: `{_format_rate(metrics.get('recall_at_5'))}`",
        f"- Recall@10: `{_format_rate(metrics.get('recall_at_10'))}`",
        f"- MRR: `{_format_float(metrics.get('mrr'))}`",
        f"- Avg expected rank: `{_format_float(metrics.get('avg_expected_rank'))}`",
        f"- Generated smoke cases: `{metrics.get('generated_smoke_cases')}`",
    ]

    misses = [item for item in metrics.get("results", []) if not item.get("hit")]
    if misses:
        lines.extend(["", "## Missed Cases", ""])
        for item in misses[:20]:
            expected = ", ".join(item.get("expected_chunk_ids") or [])
            retrieved = ", ".join(item.get("retrieved_chunk_ids") or [])
            lines.append(
                f"- `{item.get('id')}` expected=`{expected}` retrieved=`{retrieved}`"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _format_float(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.4f}"


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", default="data/golden_set.json")
    parser.add_argument("--output", default="eval/metrics.json")
    parser.add_argument("--markdown", default="")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--backend", choices=["qdrant", "lexical"], default="qdrant")
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
    parser.add_argument("--auto-smoke-cases", action="store_true")
    parser.add_argument("--max-smoke-cases", type=int, default=100)
    args = parser.parse_args()

    metrics = asyncio.run(
        run_eval(
            Path(args.golden),
            Path(args.output),
            args.top_k,
            backend=args.backend,
            kb_seed_path=Path(args.kb_seed),
            auto_smoke_cases=args.auto_smoke_cases,
            max_smoke_cases=args.max_smoke_cases,
            markdown_path=Path(args.markdown) if args.markdown else None,
        )
    )
    print(
        json.dumps(
            {key: value for key, value in metrics.items() if key != "results"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
