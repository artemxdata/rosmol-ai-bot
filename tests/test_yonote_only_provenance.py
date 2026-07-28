from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.graph.nodes.rerank import rerank
from src.graph.nodes.retrieve import _filter_attempts, retrieve
from src.graph.nodes.verify import verify
from src.models import Chunk, QueryAnalysis, Question, ScoredChunk


class RecordingRetriever:
    def __init__(self) -> None:
        self.semantic_calls: list[tuple[str, dict, int]] = []
        self.keyword_calls: list[tuple[str, dict, str]] = []

    async def retrieve(self, query: str, filters: dict, top_k: int) -> list[Chunk]:
        self.semantic_calls.append((query, filters, top_k))
        return []

    async def retrieve_keyword_candidates(
        self,
        query: str,
        filters: dict,
        *,
        top_k: int,
        scan_limit: int,
        min_score: float,
        source_type: str,
    ) -> list[Chunk]:
        self.keyword_calls.append((query, filters, source_type))
        return []


class RecordingReranker:
    def __init__(self) -> None:
        self.seen_chunks: list[Chunk] = []

    def rerank(self, query: str, chunks: list[Chunk], top_k: int) -> list[ScoredChunk]:
        self.seen_chunks = list(chunks)
        return [
            ScoredChunk(
                **chunk.model_dump(exclude={"score"}),
                score=chunk.score,
                reranker_score=0.9,
            )
            for chunk in chunks[:top_k]
        ]


class FailingReranker:
    def rerank(self, query: str, chunks: list[Chunk], top_k: int) -> list[ScoredChunk]:
        raise AssertionError("Non-Yonote chunks must not reach the cross-encoder.")


def _chunk(chunk_id: str, source_type: str | None) -> Chunk:
    metadata = {"chunk_id": chunk_id}
    if source_type is not None:
        metadata["source_type"] = source_type
    return Chunk(
        chunk_id=chunk_id,
        text=f"Source {chunk_id}",
        metadata=metadata,
        score=0.8,
    )


def _scored_chunk(chunk_id: str, source_type: str | None) -> ScoredChunk:
    chunk = _chunk(chunk_id, source_type)
    return ScoredChunk(
        **chunk.model_dump(exclude={"score"}),
        score=chunk.score,
        reranker_score=0.9,
    )


def test_all_retrieval_fallbacks_preserve_yonote_source_type() -> None:
    expected = [
        {
            "forum_normalized": "Машук",
            "category": "форумы",
            "topic": "daty_nachala_meropriyatiya",
            "source_type": "yonote",
        },
        {"forum_normalized": "Машук", "source_type": "yonote"},
        {"category": "форумы", "source_type": "yonote"},
        {"source_type": "yonote"},
    ]
    assert _filter_attempts(
        {
            "forum_normalized": "Машук",
            "category": "форумы",
            "topic": "daty_nachala_meropriyatiya",
            "source_type": "xlsx",
        }
    ) == expected


@pytest.mark.asyncio
async def test_retrieve_uses_only_yonote_for_semantic_and_keyword_recall() -> None:
    retriever = RecordingRetriever()

    result = await retrieve(
        {
            "analysis": QueryAnalysis(
                category="гранты",
                questions=[
                    Question(
                        text="Как подать заявку на грант?",
                        category="гранты",
                    )
                ],
            ),
            "message_masked": "Как подать заявку на грант?",
            "retriever": retriever,
        }
    )

    assert result["retrieved_chunks"] == []
    assert retriever.semantic_calls
    assert all(
        filters.get("source_type") == "yonote"
        for _, filters, _ in retriever.semantic_calls
    )
    assert retriever.keyword_calls
    assert all(
        filters.get("source_type") == source_type == "yonote"
        for _, filters, source_type in retriever.keyword_calls
    )


@pytest.mark.asyncio
async def test_rerank_discards_non_yonote_and_unknown_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
            ml_unload_embedder_after_use=False,
            ml_unload_reranker_after_use=False,
        ),
    )
    reranker = RecordingReranker()

    result = await rerank(
        {
            "analysis": QueryAnalysis(
                category="форумы",
                questions=[Question(text="Когда проходит форум?")],
            ),
            "message_masked": "Когда проходит форум?",
            "retrieved_chunks": [
                _chunk("yonote", "yonote"),
                _chunk("xlsx", "xlsx"),
                _chunk("docx", "docx"),
                _chunk("answer_bank", "ticket_answer_bank"),
                _chunk("unknown", None),
            ],
            "reranker": reranker,
        }
    )

    assert [chunk.chunk_id for chunk in reranker.seen_chunks] == ["yonote"]
    assert [chunk.chunk_id for chunk in result["reranked_chunks"]] == ["yonote"]


@pytest.mark.asyncio
async def test_rerank_escalates_when_only_non_yonote_sources_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
            ml_unload_embedder_after_use=False,
            ml_unload_reranker_after_use=False,
        ),
    )

    result = await rerank(
        {
            "analysis": QueryAnalysis(category="форумы"),
            "message_masked": "Когда проходит форум?",
            "retrieved_chunks": [
                _chunk("xlsx", "xlsx"),
                _chunk("answer_bank", "ticket_answer_bank"),
            ],
            "reranker": FailingReranker(),
        }
    )

    assert result["reranked_chunks"] == []
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "no_relevant_chunks"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_type", ["xlsx", "docx", "ticket_answer_bank", None])
async def test_verify_rejects_non_yonote_factual_source(
    source_type: str | None,
) -> None:
    chunk = _scored_chunk("legacy_source", source_type)

    result = await verify(
        {
            "generated_response": "Фактический ответ. [src:legacy_source]",
            "generator_model": "source_chunk",
            "cited_sources": ["legacy_source"],
            "reranked_chunks": [chunk],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is True
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "non_yonote_source"


@pytest.mark.asyncio
async def test_verify_accepts_yonote_factual_source() -> None:
    chunk = _scored_chunk("yonote_source", "yonote")

    result = await verify(
        {
            "generated_response": "Фактический ответ. [src:yonote_source]",
            "generator_model": "source_chunk",
            "cited_sources": ["yonote_source"],
            "reranked_chunks": [chunk],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert result["verifier_triggered"] is False
    assert "should_escalate" not in result


@pytest.mark.asyncio
async def test_verify_checks_response_markers_and_state_citations_together() -> None:
    result = await verify(
        {
            "generated_response": (
                "Ответ смешивает источники. [src:yonote_source] [src:xlsx_source]"
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["yonote_source"],
            "reranked_chunks": [
                _scored_chunk("yonote_source", "yonote"),
                _scored_chunk("xlsx_source", "xlsx"),
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is True
    assert result["escalation_reason"] == "non_yonote_source"
