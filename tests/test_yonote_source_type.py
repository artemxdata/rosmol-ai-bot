from __future__ import annotations

from src.graph.nodes.generate import _source_type_rank as generate_source_type_rank
from src.graph.nodes.rerank import _source_type_rank as rerank_source_type_rank
from src.graph.nodes.verify import OFFICIAL_SOURCE_TYPES
from src.models import Chunk, ScoredChunk


def test_yonote_is_treated_as_official_source_type() -> None:
    assert "yonote" in OFFICIAL_SOURCE_TYPES
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
