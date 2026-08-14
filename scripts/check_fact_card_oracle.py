"""Check source-to-answer correctness with the labelled source set.

This diagnostic deliberately bypasses retrieval.  It isolates the fact-card
composer and response guards and must not be reported as end-to-end quality.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.run_ask import score_case
from src.graph.nodes.analyze import analyze_query
from src.graph.nodes.generate import generate
from src.graph.nodes.guard import apply_response_guards
from src.models import ScoredChunk

ROOT = Path(__file__).resolve().parents[1]


class ForbiddenLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **_kwargs: object) -> str:
        self.calls += 1
        raise AssertionError("fact-card oracle must not call an LLM")


def _cases() -> list[dict[str, object]]:
    manifest = json.loads(
        (ROOT / "eval/cases/pilot50_balanced_v4.json").read_text(encoding="utf-8")
    )
    result: list[dict[str, object]] = []
    for source in manifest["sources"]:
        result.extend(
            json.loads((ROOT / source["path"]).read_text(encoding="utf-8"))
        )
    return result


def _seed() -> dict[str, dict[str, object]]:
    rows = json.loads(
        (ROOT / "data/knowledge_base_seed.json").read_text(encoding="utf-8")
    )
    return {str(row["chunk_id"]): row for row in rows}


def _chunk(row: dict[str, object]) -> ScoredChunk:
    metadata = {
        key: value
        for key, value in row.items()
        if key not in {"text_raw", "text_clean"}
    }
    return ScoredChunk(
        chunk_id=str(row["chunk_id"]),
        text=str(row["text_clean"]),
        metadata=metadata,
        score=0.99,
        reranker_score=0.99,
    )


async def main() -> int:
    seed = _seed()
    passed = 0
    llm_calls = 0
    failures: list[tuple[str, list[str], str]] = []
    for case in _cases():
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
        chunk_ids = [str(item) for item in case.get("expected_chunk_ids", [])]
        chunks = [_chunk(seed[chunk_id]) for chunk_id in chunk_ids]
        state = {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed.get("contextual_message", query),
            "analysis": analyzed["analysis"],
            "reranked_chunks": chunks,
            "max_confidence": 0.99,
            "llm_client": llm,
        }
        generated = await generate(state)
        guarded = await apply_response_guards({**state, **generated})
        result = {**generated, **guarded}
        metadata_rows = [
            {"chunk_id": chunk.chunk_id, "metadata": chunk.metadata}
            for chunk in chunks
        ]
        trace = {
            "message_masked": query,
            "query_analysis": analyzed["analysis"].model_dump(mode="json"),
            "retrieved_chunks": metadata_rows,
            "reranker_scores": metadata_rows,
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
                    str(result.get("generated_response", "")),
                )
            )

    print(f"fact_card_oracle={passed}/{len(_cases())} llm_calls={llm_calls}")
    for case_id, reasons, response in failures:
        print(f"FAIL {case_id}: {','.join(reasons)}")
        print(response)
    return 0 if passed >= 49 and llm_calls == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
