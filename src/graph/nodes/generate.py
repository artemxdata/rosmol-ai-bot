from __future__ import annotations

from time import perf_counter

from src.config import get_settings
from src.graph.question_utils import build_effective_questions
from src.graph.state import BotState
from src.llm.cascade import select_generator_model
from src.llm.prompts import RESPONSE_GENERATOR_SYSTEM, build_generator_user
from src.models import Complexity, QueryAnalysis, Question, ScoredChunk


async def generate(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    analysis = state["analysis"]
    questions = effective_questions(state, analysis)
    model = select_generator_model(analysis.complexity)
    chunks = state.get("reranked_chunks", [])
    if not chunks:
        if tracer:
            tracer.add(
                "generate",
                int((perf_counter() - started_at) * 1000),
                skipped=True,
                reason="no_sources",
            )
        return {
            "should_escalate": True,
            "escalation_reason": "no_sources_for_generation",
            "generated_response": "",
            "generator_model": model,
            "cited_sources": [],
        }

    source_response = build_deterministic_source_response(
        analysis,
        questions,
        chunks,
        float(state.get("max_confidence") or 0),
    )
    if source_response is not None:
        if tracer:
            tracer.add(
                "generate",
                int((perf_counter() - started_at) * 1000),
                mode="source_chunk",
            )
        return {
            "generated_response": source_response,
            "generator_model": "source_chunk",
            "cited_sources": [chunks[0].chunk_id],
        }

    try:
        response = await state["llm_client"].generate(
            model=model,
            system=RESPONSE_GENERATOR_SYSTEM,
            user=build_generator_user(
                questions,
                chunks,
                state.get("session"),
                analysis.extracted_params,
            ),
        )
        if tracer:
            tracer.add("generate", int((perf_counter() - started_at) * 1000), model=model)
        return {
            "generated_response": response,
            "generator_model": model,
            "cited_sources": [chunk.chunk_id for chunk in chunks],
        }
    except Exception as exc:
        if tracer:
            tracer.add_error("generate", int((perf_counter() - started_at) * 1000), exc)
        return {
            "should_escalate": True,
            "escalation_reason": "generation_failed",
            "error": str(exc),
        }


def build_deterministic_source_response(
    analysis: QueryAnalysis,
    questions: list[Question],
    chunks: list[ScoredChunk],
    max_confidence: float,
) -> str | None:
    if analysis.complexity != Complexity.SIMPLE:
        return None
    if len(questions) != 1:
        return None
    if max_confidence < get_settings().reranker_threshold_high:
        return None

    chunk = chunks[0]
    text = chunk.text.strip()
    if not text:
        return None
    return f"{text} [src:{chunk.chunk_id}]"


def effective_questions(state: BotState, analysis: QueryAnalysis) -> list[Question]:
    return build_effective_questions(
        analysis,
        state.get("message_masked") or state.get("message"),
    )
