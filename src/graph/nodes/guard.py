from __future__ import annotations

import re
from time import perf_counter

from src.config import get_settings
from src.graph.question_utils import build_effective_questions
from src.graph.state import BotState
from src.kb.event_facts import (
    concise_event_place_date_fact,
    foreign_registration_fact,
)
from src.kb.temporal import expired_registration_fact
from src.models import Chunk, QueryAnalysis, Question, ScoredChunk


async def apply_response_guards(state: BotState) -> dict:
    """Apply deterministic factual guards before the final verifier.

    Guards used to replace the response in the final ``respond`` node, after verification.
    Returning a sourced generated response here makes the exact text and its source visible to
    the normal verifier and to request traces.
    """

    started_at = perf_counter()
    tracer = state.get("trace")
    message = state.get("message_masked") or state.get("message") or ""
    analysis = _single_forum_analysis(state.get("analysis"))
    chunks = state.get("reranked_chunks") or []
    settings = get_settings()

    if analysis is None:
        if tracer:
            tracer.add("guard", int((perf_counter() - started_at) * 1000), applied=False)
        return {}

    guarded = foreign_registration_fact(
        message=message,
        analysis=analysis,
        chunks=chunks,
        seed_path=settings.kb_seed_path,
    )
    guard_name = "foreign_registration" if guarded else None
    if guarded is None:
        guarded = concise_event_place_date_fact(
            message=message,
            analysis=analysis,
            chunks=chunks,
        )
        guard_name = "place_and_date" if guarded else None
    if guarded is None:
        guarded = expired_registration_fact(
            message=message,
            analysis=analysis,
            chunks=chunks,
            seed_path=settings.kb_seed_path,
        )
        guard_name = "registration_closed" if guarded else None

    if guarded is None:
        if tracer:
            tracer.add("guard", int((perf_counter() - started_at) * 1000), applied=False)
        return {}

    questions = build_effective_questions(analysis, message)
    if not _guard_covers_all_aspects(guard_name, questions, message):
        if tracer:
            tracer.add(
                "guard",
                int((perf_counter() - started_at) * 1000),
                applied=False,
                reason="multi_aspect_request",
                candidate_guard=guard_name,
            )
        return {}

    response, source_chunk = guarded
    sourced_response = f"{response} [src:{source_chunk.chunk_id}]"
    reranked_chunks = _include_source_chunk(chunks, source_chunk)
    if tracer:
        tracer.add(
            "guard",
            int((perf_counter() - started_at) * 1000),
            applied=True,
            guard=guard_name,
            source_id=source_chunk.chunk_id,
        )
    return {
        "generated_response": sourced_response,
        "cited_sources": [source_chunk.chunk_id],
        "generator_model": "source_chunk",
        "reranked_chunks": reranked_chunks,
        "response_guard": guard_name,
    }


def _single_forum_analysis(analysis: QueryAnalysis | None) -> QueryAnalysis | None:
    if not isinstance(analysis, QueryAnalysis):
        return None
    forums = {
        str(value).strip()
        for value in (
            analysis.forum_normalized,
            *(analysis.extracted_params.get("detected_forums") or []),
            *(question.forum_normalized for question in analysis.questions),
        )
        if str(value or "").strip()
    }
    normalized = {forum.casefold(): forum for forum in forums}
    if len(normalized) != 1:
        return None
    forum = next(iter(normalized.values()))
    if analysis.forum_normalized == forum:
        return analysis
    return analysis.model_copy(update={"forum": forum, "forum_normalized": forum})


def _guard_covers_all_aspects(
    guard_name: str | None,
    questions: list[Question],
    message: str,
) -> bool:
    aspects = {_question_aspect(question, message) for question in questions}
    aspects.discard(None)
    if not aspects:
        return True
    if guard_name == "foreign_registration":
        return aspects <= {"registration", "foreign"}
    if guard_name == "place_and_date":
        return aspects <= {"place_date"}
    if guard_name == "registration_closed":
        return aspects <= {"registration"}
    return False


def _question_aspect(question: Question, message: str) -> str | None:
    topic = str(getattr(question, "topic", None) or "").casefold()
    text = str(getattr(question, "text", "") or "").casefold().replace("ё", "е")
    if "inostrann" in topic or "иностран" in text:
        return "foreign"
    if any(marker in topic for marker in ("registr", "zayavk")) or any(
        marker in text for marker in ("регистрац", "зарегистр", "заявк")
    ):
        return "registration"
    is_place_date = topic in {
        "opisanie",
        "daty_nachala_meropriyatiya",
        "mesto_i_ploschadka_provedeniya",
    } or any(
        marker in text
        for marker in (
            "где проходит",
            "где и когда",
            "когда проходит",
            "даты и сроки",
            "место проведения",
        )
    )
    if is_place_date:
        if _is_registration_timing_query(message) and not _is_event_place_date_query(message):
            return "registration"
        return "place_date"
    return topic or text or None


def _is_registration_timing_query(message: str) -> bool:
    normalized = str(message or "").casefold().replace("ё", "е")
    registration_markers = ("регистрац", "зарегистр", "заявк")
    timing_markers = (
        "срок регистрац",
        "срок подачи заяв",
        "дедлайн регистрац",
        "дедлайн подачи заяв",
        "когда закончится регистрац",
        "когда заканчивается регистрац",
        "до какого числа регистрац",
        "до какого числа подать заяв",
    )
    return any(marker in normalized for marker in registration_markers) and any(
        marker in normalized for marker in timing_markers
    )


def _is_event_place_date_query(message: str) -> bool:
    normalized = str(message or "").casefold().replace("ё", "е")
    return bool(
        re.search(
            r"\b(?:где\s+и\s+когда|когда\s+и\s+где)\s+(?:будет\s+)?проход\w*",
            normalized,
        )
    )


def _include_source_chunk(
    chunks: list[ScoredChunk],
    source_chunk: Chunk,
) -> list[ScoredChunk]:
    if any(chunk.chunk_id == source_chunk.chunk_id for chunk in chunks):
        return list(chunks)
    score = float(source_chunk.score or 0.0)
    return [
        *chunks,
        ScoredChunk(
            chunk_id=source_chunk.chunk_id,
            text=source_chunk.text,
            metadata=source_chunk.metadata,
            score=source_chunk.score,
            reranker_score=score,
        ),
    ]
