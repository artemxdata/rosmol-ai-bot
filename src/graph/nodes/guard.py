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
from src.kb.temporal import expired_registration_fact, extract_registration_deadline
from src.models import Chunk, QueryAnalysis, Question, ScoredChunk

SOURCE_REF_RE = re.compile(r"\[src:([^\]]+)\]", re.IGNORECASE)
FULL_DATE_RE = re.compile(
    r"\b(?:\d{1,2}[./-]\d{1,2}[./-]\d{4}|"
    r"\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|"
    r"сентября|октября|ноября|декабря)\s+\d{4})\b",
    re.IGNORECASE,
)
REGISTRATION_CLAIM_START_RE = re.compile(
    r"^\s*(?:[-–—*•]\s*|\d+[.)]?\s*)?"
    r"(?:регистрац\w*|при[её]м\w*\s+заяв\w*|подач\w*\s+заяв\w*|"
    r"подать\s+заяв\w*|окончани\w*\s+(?:при[её]ма|подачи)\s+заяв\w*)\b",
    re.IGNORECASE,
)
DEADLINE_TAIL_RE = re.compile(
    r"^\s*(?:г(?:ода)?\.?)?\s*"
    r"(?:\(?\s*(?:включительно\s*,?\s*)?(?:(?:до|в)\s*)?"
    r"\d{1,2}:\d{2}\s*(?:мск)?\s*\)?)?\s*[.!?]*\s*$",
    re.IGNORECASE,
)


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

    response, source_chunk = guarded
    questions = build_effective_questions(analysis, message)
    response_guard = guard_name
    cited_sources = [source_chunk.chunk_id]
    if not _guard_covers_all_aspects(guard_name, questions, message):
        merged = (
            _merge_registration_closed_response(state, response, source_chunk)
            if guard_name == "registration_closed"
            else None
        )
        if merged is None:
            if tracer:
                tracer.add(
                    "guard",
                    int((perf_counter() - started_at) * 1000),
                    applied=False,
                    reason="multi_aspect_request",
                    candidate_guard=guard_name,
                )
            return {}
        sourced_response, cited_sources = merged
        response_guard = "registration_closed_multi_aspect"
    else:
        sourced_response = f"{response} [src:{source_chunk.chunk_id}]"
    reranked_chunks = _include_source_chunk(chunks, source_chunk)
    if tracer:
        tracer.add(
            "guard",
            int((perf_counter() - started_at) * 1000),
            applied=True,
            guard=response_guard,
            source_id=source_chunk.chunk_id,
        )
    return {
        "generated_response": sourced_response,
        "cited_sources": cited_sources,
        "generator_model": "source_chunk",
        "reranked_chunks": reranked_chunks,
        "response_guard": response_guard,
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


def _merge_registration_closed_response(
    state: BotState,
    closed_response: str,
    source_chunk: Chunk,
) -> tuple[str, list[str]] | None:
    """Replace only the stale registration claim in a fully sourced answer.

    A multi-aspect answer is preserved only when the expired deadline source is
    isolated in exactly one source-bound paragraph. Any ambiguous citation
    shape fails closed to the existing no-replacement behavior.
    """

    generated = str(state.get("generated_response") or "").strip()
    source_id = str(source_chunk.chunk_id or "").strip()
    if not generated or not source_id:
        return None
    blocks = [block.strip() for block in re.split(r"\n{2,}", generated) if block.strip()]
    matching_indexes = [
        index
        for index, block in enumerate(blocks)
        if SOURCE_REF_RE.findall(block) == [source_id]
    ]
    if len(matching_indexes) != 1:
        return None
    if any(not SOURCE_REF_RE.search(block) for block in blocks):
        return None

    output_cited_sources = list(dict.fromkeys(SOURCE_REF_RE.findall(generated)))
    state_cited_sources = [
        str(value).strip()
        for value in state.get("cited_sources", [])
        if str(value or "").strip()
    ]
    if (
        source_id not in output_cited_sources
        or (state_cited_sources and state_cited_sources != output_cited_sources)
    ):
        return None

    replacement = _replace_isolated_registration_deadline_claim(
        blocks[matching_indexes[0]],
        closed_response,
        source_id,
    )
    if replacement is None:
        return None
    blocks[matching_indexes[0]] = replacement
    return "\n\n".join(blocks), output_cited_sources


def _replace_isolated_registration_deadline_claim(
    block: str,
    closed_response: str,
    source_id: str,
) -> str | None:
    visible = SOURCE_REF_RE.sub("", block).strip()
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", visible)
        if sentence.strip()
    ]
    deadline_indexes = [
        index
        for index, sentence in enumerate(sentences)
        if extract_registration_deadline(sentence) is not None
    ]
    if len(deadline_indexes) != 1:
        return None

    deadline_index = deadline_indexes[0]
    deadline_sentence = sentences[deadline_index]
    if not _is_isolated_registration_deadline_sentence(deadline_sentence):
        return None
    sentences[deadline_index] = closed_response.strip()
    return f"{' '.join(sentences)} [src:{source_id}]"


def _is_isolated_registration_deadline_sentence(sentence: str) -> bool:
    if REGISTRATION_CLAIM_START_RE.search(sentence) is None:
        return False
    dates = list(FULL_DATE_RE.finditer(sentence))
    if not dates:
        return False
    return DEADLINE_TAIL_RE.fullmatch(sentence[dates[-1].end() :]) is not None


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
