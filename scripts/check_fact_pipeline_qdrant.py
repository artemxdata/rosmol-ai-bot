"""Run the fact-first graph against the real read-only Qdrant retrieval path.

This is a zero-LLM regression calibration diagnostic.  It exercises
analyze -> retrieve -> rerank -> generate -> guards -> respond -> score with
the production embedding and reranker models, but it is not an independent
holdout or a production conversion measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.run_ask import score_case
from scripts.pilot50 import build_materialized_cases
from src.graph.nodes.analyze import analyze_query
from src.graph.nodes.generate import generate
from src.graph.nodes.guard import apply_response_guards
from src.graph.nodes.rerank import rerank
from src.graph.nodes.respond import respond
from src.graph.nodes.retrieve import retrieve
from src.rag.embedder import Embedder
from src.rag.reranker import Reranker
from src.rag.retriever import Retriever

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "eval/cases/pilot50_balanced_v5.json"
EXPECTED_DATASET_ID = "pilot50_balanced_v5"
EXPECTED_CASES_TOTAL = 50
MINIMUM_PASS_COUNT = 49
SHA_RE = re.compile(r"[0-9a-f]{64}")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
SAFE_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,95}")
PUBLIC_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
OWNER_PREVIEW_ORDINALS = (1, 9, 26, 44, 48)


class ForbiddenLLM:
    """Fail closed if any graph path tries to spend an LLM request."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **_kwargs: object) -> str:
        self.calls += 1
        raise AssertionError("Qdrant fact-core diagnostic must not call an LLM")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-candidate-sha", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-cases-sha256", required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--output-mode",
        choices=("aggregate", "owner-preview"),
        default="aggregate",
    )
    args = parser.parse_args()
    if GIT_SHA_RE.fullmatch(args.expected_candidate_sha) is None:
        parser.error("expected candidate SHA must be a full lowercase Git SHA")
    for name in ("expected_manifest_sha256", "expected_cases_sha256"):
        if SHA_RE.fullmatch(getattr(args, name)) is None:
            parser.error(f"{name.replace('_', '-')} must be a lowercase SHA-256")
    return args


def _qdrant_settings() -> tuple[str, str | None, str]:
    url = os.environ.get("QDRANT_URL", "").strip()
    collection = os.environ.get("QDRANT_KNOWLEDGE_COLLECTION", "").strip()
    api_key = os.environ.get("QDRANT_API_KEY", "")
    if url != "http://qdrant-readonly:6333":
        raise RuntimeError("diagnostic Qdrant URL is not the read-only proxy")
    if collection != "knowledge_base":
        raise RuntimeError("diagnostic Qdrant collection is not the frozen knowledge base")
    if api_key != "read-only-proxy":
        raise RuntimeError("diagnostic Qdrant proxy credential is invalid")
    return url, api_key, collection


def _trace(
    *,
    query: str,
    analyzed: dict[str, Any],
    state: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    retrieved_chunks = state.get("retrieved_chunks", [])
    reranked_chunks = state.get("reranked_chunks", [])
    return {
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


async def _score_cases(
    cases: list[dict[str, Any]],
    *,
    retriever: Retriever,
    reranker: Reranker,
) -> tuple[list[dict[str, Any]], int]:
    scored_cases: list[dict[str, Any]] = []
    llm_calls = 0
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
        state.update(await retrieve(state))
        state.update(await rerank(state))
        generated = await generate(state)
        guarded = await apply_response_guards({**state, **generated})
        responded = await respond({**state, **generated, **guarded})
        result = {**generated, **guarded, **responded}
        scored_cases.append(
            score_case(
                case,
                {
                    "http_status": 200,
                    "response": result.get("final_response", ""),
                },
                _trace(
                    query=query,
                    analyzed=analyzed,
                    state=state,
                    result=result,
                ),
            )
        )
        llm_calls += llm.calls
    return scored_cases, llm_calls


def build_summary(
    *,
    candidate_sha: str,
    manifest_sha256: str,
    cases_sha256: str,
    scored_cases: list[dict[str, Any]],
    groups: list[str],
    llm_calls: int,
) -> dict[str, Any]:
    if len(scored_cases) != EXPECTED_CASES_TOTAL or len(groups) != len(scored_cases):
        raise ValueError("diagnostic case count is invalid")
    if any(group not in {"typical", "atypical"} for group in groups):
        raise ValueError("diagnostic case group is invalid")

    passed_by_group: Counter[str] = Counter()
    no_operator_by_group: Counter[str] = Counter()
    failures: list[dict[str, Any]] = []
    retrieval_complete = 0
    citation_complete = 0
    for ordinal, (group, scored) in enumerate(
        zip(groups, scored_cases, strict=True),
        start=1,
    ):
        if scored.get("passed") is True:
            passed_by_group[group] += 1
        if scored.get("was_escalated") is False:
            no_operator_by_group[group] += 1
        retrieval_hit = scored.get("expected_or_equivalent_chunk_hit")
        if retrieval_hit is None:
            retrieval_hit = scored.get("expected_chunk_hit")
        if retrieval_hit is True:
            retrieval_complete += 1
        citation_hit = scored.get("expected_cited_or_equivalent_chunk_hit")
        if citation_hit is None:
            citation_hit = scored.get("expected_cited_chunk_hit")
        if citation_hit is True:
            citation_complete += 1
        if scored.get("passed") is not True:
            reasons = sorted(
                {
                    str(reason)
                    for reason in scored.get("failure_reasons", [])
                    if SAFE_REASON_RE.fullmatch(str(reason)) is not None
                }
            )
            failures.append({"ordinal": ordinal, "reasons": reasons})

    passed = sum(passed_by_group.values())
    no_operator = sum(no_operator_by_group.values())
    status = (
        "GO"
        if passed >= MINIMUM_PASS_COUNT
        and retrieval_complete == EXPECTED_CASES_TOTAL
        and llm_calls == 0
        else "STOP"
    )
    return {
        "schema_version": "fact-core-qdrant-postprocess-calibration-v1",
        "classification": "calibration_only",
        "disclaimer": (
            "Mechanical first-turn regression calibration; not an independent "
            "holdout, human product verdict, or production traffic conversion."
        ),
        "candidate_sha": candidate_sha,
        "dataset_id": EXPECTED_DATASET_ID,
        "manifest_sha256": manifest_sha256,
        "cases_sha256": cases_sha256,
        "minimum_passed": MINIMUM_PASS_COUNT,
        "counts": {
            "total": len(scored_cases),
            "passed": passed,
            "typical_passed": passed_by_group["typical"],
            "atypical_passed": passed_by_group["atypical"],
            "no_operator": no_operator,
            "typical_no_operator": no_operator_by_group["typical"],
            "atypical_no_operator": no_operator_by_group["atypical"],
            "retrieval_complete": retrieval_complete,
            "citation_complete": citation_complete,
            "llm_calls": llm_calls,
        },
        "failures": failures,
        "status": status,
    }


def _nearest_rank(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(len(ordered) * percentile + 0.999999)))
    return ordered[rank - 1]


def build_owner_preview(
    *,
    candidate_sha: str,
    manifest_sha256: str,
    cases_sha256: str,
    scored_cases: list[dict[str, Any]],
    groups: list[str],
    llm_calls: int,
) -> dict[str, Any]:
    """Expose five bounded public calibration examples for owner review."""
    if len(scored_cases) != EXPECTED_CASES_TOTAL or len(groups) != len(scored_cases):
        raise ValueError("owner preview case count is invalid")
    if any(group not in {"typical", "atypical"} for group in groups):
        raise ValueError("owner preview case group is invalid")
    if llm_calls != 0:
        raise ValueError("owner preview must not call an LLM")

    responses = [str(row.get("response") or "") for row in scored_cases]
    response_lengths = [len(response) for response in responses]
    link_counts = [len(PUBLIC_URL_RE.findall(response)) for response in responses]
    paragraph_counts = [
        len([part for part in re.split(r"\n\s*\n", response) if part.strip()])
        if response.strip()
        else 0
        for response in responses
    ]
    previews: list[dict[str, Any]] = []
    for ordinal in OWNER_PREVIEW_ORDINALS:
        row = scored_cases[ordinal - 1]
        query = str(row.get("query") or "")
        response = str(row.get("response") or "")
        if not query.strip() or len(query) > 1000:
            raise ValueError("owner preview query is invalid")
        if not response.strip() or len(response) > 4000:
            raise ValueError("owner preview response is invalid")
        if any(ord(char) < 32 and char not in "\n\t" for char in query + response):
            raise ValueError("owner preview contains control characters")
        passed = row.get("passed")
        was_escalated = row.get("was_escalated")
        if type(passed) is not bool or type(was_escalated) is not bool:
            raise ValueError("owner preview verdict is invalid")
        previews.append(
            {
                "ordinal": ordinal,
                "group": groups[ordinal - 1],
                "query": query,
                "response": response,
                "response_chars": len(response),
                "link_count": len(PUBLIC_URL_RE.findall(response)),
                "passed": passed,
                "was_escalated": was_escalated,
            }
        )

    return {
        "schema_version": "fact-core-qdrant-owner-preview-v1",
        "classification": "calibration_only",
        "disclaimer": (
            "Five bounded examples from the exposed Pilot50 calibration set; "
            "not an independent holdout or production traffic conversion."
        ),
        "candidate_sha": candidate_sha,
        "dataset_id": EXPECTED_DATASET_ID,
        "manifest_sha256": manifest_sha256,
        "cases_sha256": cases_sha256,
        "counts": {
            "total": len(scored_cases),
            "passed": sum(row.get("passed") is True for row in scored_cases),
            "no_operator": sum(
                row.get("was_escalated") is False for row in scored_cases
            ),
            "llm_calls": llm_calls,
        },
        "response_shape": {
            "min_chars": min(response_lengths),
            "median_chars": _nearest_rank(response_lengths, 0.50),
            "p95_chars": _nearest_rank(response_lengths, 0.95),
            "max_chars": max(response_lengths),
            "empty_responses": sum(not response.strip() for response in responses),
            "responses_over_1000_chars": sum(length > 1000 for length in response_lengths),
            "max_links": max(link_counts),
            "max_paragraphs": max(paragraph_counts),
        },
        "preview_ordinals": list(OWNER_PREVIEW_ORDINALS),
        "previews": previews,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    cases, receipt = build_materialized_cases(args.manifest)
    if receipt.get("dataset_id") != EXPECTED_DATASET_ID:
        raise RuntimeError("diagnostic manifest dataset is invalid")
    if receipt.get("manifest_sha256") != args.expected_manifest_sha256:
        raise RuntimeError("diagnostic manifest SHA-256 mismatch")
    if receipt.get("cases_sha256") != args.expected_cases_sha256:
        raise RuntimeError("diagnostic cases SHA-256 mismatch")

    qdrant_url, qdrant_api_key, collection = _qdrant_settings()
    client = AsyncQdrantClient(url=qdrant_url, api_key=qdrant_api_key)
    embedder = Embedder()
    reranker = Reranker()
    try:
        scored_cases, llm_calls = await _score_cases(
            cases,
            retriever=Retriever(client, embedder, collection),
            reranker=reranker,
        )
    finally:
        reranker.unload()
        embedder.unload()
        await client.close()

    common = {
        "candidate_sha": args.expected_candidate_sha,
        "manifest_sha256": args.expected_manifest_sha256,
        "cases_sha256": args.expected_cases_sha256,
        "scored_cases": scored_cases,
        "groups": [str(case["pilot50_group"]) for case in cases],
        "llm_calls": llm_calls,
    }
    if args.output_mode == "owner-preview":
        return build_owner_preview(**common)
    return build_summary(**common)


def main() -> int:
    args = _parse_args()
    with redirect_stdout(sys.stderr):
        payload = asyncio.run(_run(args))
    sys.stdout.write(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
