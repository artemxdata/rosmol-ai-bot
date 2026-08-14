"""Exercise analyze/retrieve/rerank/compose against the frozen local seed.

This is a zero-cost wiring diagnostic. Lexical seed retrieval is not the
production embedding stack, and the calibration cases are not a holdout.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.run_ask import score_case
from scripts.check_fact_card_oracle import ForbiddenLLM, _cases, _seed
from src.graph.answer_plan import plan_answer
from src.graph.nodes.analyze import analyze_query
from src.graph.nodes.generate import generate
from src.graph.nodes.guard import apply_response_guards
from src.graph.nodes.rerank import rerank
from src.graph.nodes.retrieve import retrieve
from src.models import Chunk, ScoredChunk
from src.rag.seed_retriever import (
    SeedRetriever,
    _canonicalize_record_forum,
    _matches_filters,
)

ROOT = Path(__file__).resolve().parents[1]


class AsyncSeedRetriever:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.inner = SeedRetriever(rows)

    async def retrieve(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> list[Chunk]:
        return self.inner.retrieve(query, filters, top_k)

    async def retrieve_by_metadata(
        self,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> list[Chunk]:
        rows = [
            row
            for row in self.inner.records
            if _matches_filters(row, filters or {})
        ][:top_k]
        return [
            Chunk(
                chunk_id=str(row["chunk_id"]),
                text=str(row.get("text_clean") or row.get("text") or ""),
                metadata=_canonicalize_record_forum(row),
                score=1.0,
            )
            for row in rows
        ]

    async def retrieve_keyword_candidates(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        *,
        top_k: int = 6,
        **_kwargs: object,
    ) -> list[Chunk]:
        return self.inner.retrieve(query, filters, top_k)


class LexicalReranker:
    def rerank(
        self,
        query: str,
        chunks: list[Chunk],
        top_k: int = 4,
    ) -> list[ScoredChunk]:
        records: list[dict[str, Any]] = []
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        for chunk in chunks:
            record = dict(chunk.metadata or {})
            record.update(
                {
                    "chunk_id": chunk.chunk_id,
                    "text_clean": chunk.text,
                    "status": record.get("status") or "published",
                }
            )
            records.append(record)
        ranked = SeedRetriever(records).retrieve(query, top_k=top_k)
        if not ranked:
            ranked = chunks[:top_k]
        return [
            ScoredChunk(
                chunk_id=chunk.chunk_id,
                text=chunk.text,
                metadata=chunk.metadata,
                score=by_id[chunk.chunk_id].score,
                reranker_score=max(0.4, 0.99 - ordinal * 0.05),
            )
            for ordinal, chunk in enumerate(ranked)
        ]


async def main() -> int:
    seed_by_id = _seed()
    retriever = AsyncSeedRetriever(list(seed_by_id.values()))
    reranker = LexicalReranker()
    cases = _cases()
    passed = 0
    llm_calls = 0
    recall_complete = 0
    failures: list[tuple[str, list[str], list[str], list[str], str]] = []
    for case in cases:
        query = str(case["query"])
        llm = ForbiddenLLM()
        analyzed = await analyze_query(
            {
                "message": query,
                "message_masked": query,
                "routing_hint": {},
                "llm_client": llm,
            }
        )
        state: dict[str, Any] = {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed.get("contextual_message", query),
            "analysis": analyzed["analysis"],
            "llm_client": llm,
            "retriever": retriever,
            "reranker": reranker,
        }
        state.update(plan_answer(state))
        retrieved = await retrieve(state)
        state.update(retrieved)
        reranked = await rerank(state)
        state.update(reranked)
        generated = await generate(state)
        guarded = await apply_response_guards({**state, **generated})
        result = {**generated, **guarded}
        retrieved_chunks = state.get("retrieved_chunks", [])
        reranked_chunks = state.get("reranked_chunks", [])
        retrieved_ids = [chunk.chunk_id for chunk in retrieved_chunks]
        expected_ids = [str(item) for item in case.get("expected_chunk_ids", [])]
        if set(expected_ids).issubset(retrieved_ids):
            recall_complete += 1
        trace = {
            "message_masked": query,
            "query_analysis": analyzed["analysis"].model_dump(mode="json"),
            "retrieved_chunks": [
                {"chunk_id": chunk.chunk_id, "metadata": chunk.metadata}
                for chunk in retrieved_chunks
            ],
            "reranker_scores": [
                {"chunk_id": chunk.chunk_id, "metadata": chunk.metadata}
                for chunk in reranked_chunks
            ],
            "cited_sources": result.get("cited_sources", []),
            "generator_model": result.get("generator_model"),
            "was_escalated": bool(result.get("should_escalate")),
            "escalation_reason": result.get("escalation_reason"),
        }
        scored = score_case(
            case,
            {"http_status": 200, "response": result.get("generated_response", "")},
            trace,
        )
        llm_calls += llm.calls
        if scored["passed"] is True:
            passed += 1
        else:
            failures.append(
                (
                    str(case["id"]),
                    list(scored["failure_reasons"]),
                    retrieved_ids,
                    [chunk.chunk_id for chunk in reranked_chunks],
                    str(result.get("generated_response", "")),
                )
            )

    print(
        f"offline_fact_pipeline={passed}/{len(cases)} "
        f"retrieval_recall={recall_complete}/{len(cases)} llm_calls={llm_calls}"
    )
    for case_id, reasons, retrieved_ids, reranked_ids, response in failures:
        print(f"FAIL {case_id}: {','.join(reasons)}")
        print(f"retrieved={','.join(retrieved_ids)}")
        print(f"reranked={','.join(reranked_ids)}")
        print(response)
    return int(
        passed < 49
        or recall_complete != len(cases)
        or llm_calls != 0
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
