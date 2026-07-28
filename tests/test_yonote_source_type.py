from __future__ import annotations

from src.graph.nodes.generate import _source_type_rank as generate_source_type_rank
from src.graph.nodes.rerank import _source_type_rank as rerank_source_type_rank
from src.graph.nodes.verify import FACTUAL_SOURCE_TYPE
from src.models import Chunk, ScoredChunk


def test_only_yonote_is_treated_as_factual_source_type() -> None:
    assert FACTUAL_SOURCE_TYPE == "yonote"
    assert generate_source_type_rank(
        ScoredChunk(
            chunk_id="y1",
            text="answer",
            metadata={"source_type": "yonote"},
            reranker_score=0.9,
        )
    ) == 0
    assert rerank_source_type_rank(
        Chunk(chunk_id="y1", text="answer", metadata={"source_type": "yonote"})
    ) == 0
    assert generate_source_type_rank(
        ScoredChunk(
            chunk_id="xlsx1",
            text="legacy answer",
            metadata={"source_type": "xlsx"},
            reranker_score=0.9,
        )
    ) > 0
    assert rerank_source_type_rank(
        Chunk(chunk_id="docx1", text="legacy answer", metadata={"source_type": "docx"})
    ) > 0
