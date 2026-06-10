from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState


async def retrieve(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    analysis = state.get("analysis")
    if analysis is None:
        return {"retrieved_chunks": [], "metadata_filter": {}}

    filters = {
        "forum_normalized": analysis.forum_normalized,
        "category": analysis.category,
    }
    chunks = []
    for question in analysis.questions:
        question_filters = {
            **filters,
            "topic": question.topic,
            "forum_normalized": question.forum_normalized or filters.get("forum_normalized"),
            "category": question.category or filters.get("category"),
        }
        found = await state["retriever"].retrieve(question.text, question_filters, top_k=10)
        chunks.extend(found)

    deduped = {chunk.chunk_id: chunk for chunk in chunks}
    if tracer:
        tracer.add(
            "retrieve",
            int((perf_counter() - started_at) * 1000),
            chunks=len(deduped),
            filters=filters,
        )
    return {"retrieved_chunks": list(deduped.values()), "metadata_filter": filters}
