from __future__ import annotations

import re
from time import perf_counter

from src.config import get_settings
from src.graph.question_utils import FALLBACK_QUESTION_MARKERS, build_effective_questions
from src.graph.state import BotState
from src.llm.cascade import select_generator_model
from src.llm.prompts import RESPONSE_GENERATOR_SYSTEM, build_generator_user
from src.models import QueryAnalysis, Question, ScoredChunk

TOKEN_RE = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)


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
    if len(questions) != 1:
        return None
    settings = get_settings()
    if max_confidence < getattr(settings, "reranker_threshold_low", 0.4):
        return None
    if not _source_chunk_covers_question(questions[0], chunks[0]):
        return None

    chunk = chunks[0]
    text = chunk.text.strip()
    if not text:
        return None
    return f"{text} [src:{chunk.chunk_id}]"


def _source_chunk_covers_question(question: Question, chunk: ScoredChunk) -> bool:
    question_normalized = _normalize(question.text)
    haystack = _normalize(chunk.text)
    for markers, _question_text in FALLBACK_QUESTION_MARKERS:
        if not any(marker in question_normalized for marker in markers):
            continue
        return any(marker in haystack for marker in markers)
    question_tokens = _tokens(question.text)
    if not question_tokens:
        return False
    overlap = question_tokens & _tokens(chunk.text)
    return len(overlap) >= min(2, len(question_tokens))


def _normalize(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(_normalize(text)))


def effective_questions(state: BotState, analysis: QueryAnalysis) -> list[Question]:
    return build_effective_questions(
        analysis,
        state.get("message_masked") or state.get("message"),
    )
