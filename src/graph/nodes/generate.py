from __future__ import annotations

import re
from time import perf_counter

from src.config import get_settings
from src.graph.query_normalization import expand_query_aliases
from src.graph.question_utils import FALLBACK_QUESTION_MARKERS, build_effective_questions
from src.graph.state import BotState
from src.llm.cascade import select_generator_model
from src.llm.prompts import RESPONSE_GENERATOR_SYSTEM, build_generator_user
from src.models import Complexity, QueryAnalysis, Question, ScoredChunk

TOKEN_RE = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)
SOURCE_RE = re.compile(r"\[src:([^\]]+)\]")
INSUFFICIENT_SOURCE_RESPONSE_RE = re.compile(
    r"(в\s+(?:предоставленн(?:ом|ых)\s+)?источник(?:е|ах)\s+нет\s+(?:конкретной\s+)?информации|"
    r"в\s+(?:предоставленн(?:ом|ых)\s+)?источник(?:е|ах)\s+нет\s+(?:достаточных\s+)?(?:данных|сведений)|"
    r"из\s+(?:представленных|переданных)\s+источников\s+невозможно\s+ответить|"
    r"источники\s+не\s+(?:содержат|подтверждают)|"
    r"информации\s+(?:в\s+источниках\s+)?нет|"
    r"(?:достаточных\s+)?(?:данных|сведений)\s+(?:в\s+источниках\s+)?нет|"
    r"нет\s+информации\s+о|"
    r"источник(?:е|ах)\s+отсутств|"
    r"не\s+указан[аоы]?\s+в\s+(?:предоставленных\s+)?источниках|"
    r"(?:точной|конкретной)\s+информации[^.!?]{0,160}\s+нет|"
    r"информаци[яи][^.!?]{0,160}отсутств)",
    flags=re.IGNORECASE,
)
HOUSING_CONDITION_TERMS = (
    "формат проживан",
    "условия проживан",
    "палат",
    "гостиниц",
    "отел",
    "размещ",
    "спальн",
    "коврик",
    "номер",
)
SAME_FORUM_COMPATIBLE_CATEGORIES = {
    "форумы": {"навигация", "платформа_фгаис", "техподдержка"},
    "навигация": {"форумы", "платформа_фгаис", "техподдержка"},
    "платформа_фгаис": {"форумы", "навигация", "техподдержка"},
    "техподдержка": {"форумы", "навигация", "платформа_фгаис"},
}
CATEGORY_COMPATIBLE_CATEGORIES = {
    "форумы": {"навигация", "платформа_фгаис", "техподдержка"},
    "общее": {"навигация", "платформа_фгаис"},
    "рекомендации": {"общее"},
    "гранты": {"платформа_фгаис"},
}
SAFE_CROSS_CATEGORY_ANSWER_BANK_TOPICS = {
    "доступ_и_техническая_ошибка",
    "контакты_и_оператор",
    "личный_кабинет_и_профиль",
    "письмо_и_уведомления",
    "регистрация_и_заявка",
    "статус_заявки",
}


async def generate(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    analysis = state["analysis"]
    questions = effective_questions(state, analysis)
    chunks = state.get("reranked_chunks", [])
    max_confidence = float(state.get("max_confidence") or 0)
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
            "generator_model": "source_only",
            "cited_sources": [],
        }

    if analysis.complexity == Complexity.COMPLEX:
        llm_source_chunks = _select_llm_source_chunks(
            analysis,
            questions,
            chunks,
            max_confidence,
        )
        if llm_source_chunks:
            if _should_synthesize_with_llm(state, analysis, questions, llm_source_chunks):
                return await _generate_with_llm_or_source_fallback(
                    state=state,
                    analysis=analysis,
                    questions=questions,
                    source_chunks=llm_source_chunks,
                    started_at=started_at,
                )
            if _should_use_extractive_multi_source_answer(analysis, llm_source_chunks):
                source_response = build_deterministic_source_response(llm_source_chunks)
                if source_response:
                    if tracer:
                        tracer.add(
                            "generate",
                            int((perf_counter() - started_at) * 1000),
                            mode="complex_source_chunk",
                            chunks=len(llm_source_chunks),
                        )
                    return {
                        "generated_response": source_response,
                        "generator_model": "source_chunk",
                        "cited_sources": [chunk.chunk_id for chunk in llm_source_chunks],
                    }
            return await _generate_with_llm_or_source_fallback(
                state=state,
                analysis=analysis,
                questions=questions,
                source_chunks=llm_source_chunks,
                started_at=started_at,
            )
        source_chunks = select_deterministic_source_chunks(
            analysis,
            questions,
            chunks,
            max_confidence,
            _state_message_for_search(state),
        )
        if source_chunks and _should_synthesize_with_llm(
            state,
            analysis,
            questions,
            source_chunks,
        ):
            return await _generate_with_llm_or_source_fallback(
                state=state,
                analysis=analysis,
                questions=questions,
                source_chunks=source_chunks,
                started_at=started_at,
            )
        if source_chunks and _should_use_extractive_multi_source_answer(analysis, source_chunks):
            source_response = build_deterministic_source_response(source_chunks)
            if source_response:
                if tracer:
                    tracer.add(
                        "generate",
                        int((perf_counter() - started_at) * 1000),
                        mode="complex_deterministic_source_chunk",
                        chunks=len(source_chunks),
                    )
                return {
                    "generated_response": source_response,
                    "generator_model": "source_chunk",
                    "cited_sources": [chunk.chunk_id for chunk in source_chunks],
                }
        single_official_source = _trusted_single_official_source(
            chunks,
            getattr(get_settings(), "reranker_threshold_high", 0.7),
            analysis=analysis,
        )
        if single_official_source is not None:
            source_response = build_deterministic_source_response(single_official_source)
            if source_response:
                if tracer:
                    tracer.add(
                        "generate",
                        int((perf_counter() - started_at) * 1000),
                        mode="complex_single_official_source_chunk",
                        chunks=1,
                    )
                return {
                    "generated_response": source_response,
                    "generator_model": "source_chunk",
                    "cited_sources": [single_official_source.chunk_id],
                }
        if tracer:
            tracer.add(
                "generate",
                int((perf_counter() - started_at) * 1000),
                mode="complex_source_only_escalation",
                reason="insufficient_source_coverage",
            )
        return {
            "should_escalate": True,
            "escalation_reason": "insufficient_sources",
            "generated_response": "",
            "generator_model": "source_only",
            "cited_sources": [],
        }

    source_chunks = select_deterministic_source_chunks(
        analysis,
        questions,
        chunks,
        max_confidence,
        _state_message_for_search(state),
    )
    if source_chunks:
        if _should_synthesize_with_llm(state, analysis, questions, source_chunks):
            return await _generate_with_llm_or_source_fallback(
                state=state,
                analysis=analysis,
                questions=questions,
                source_chunks=source_chunks,
                started_at=started_at,
            )
        source_response = build_deterministic_source_response(source_chunks)
        if tracer:
            tracer.add(
                "generate",
                int((perf_counter() - started_at) * 1000),
                mode="source_chunk",
                chunks=len(source_chunks),
            )
        return {
            "generated_response": source_response,
            "generator_model": "source_chunk",
            "cited_sources": [chunk.chunk_id for chunk in source_chunks],
        }

    llm_source_chunks = _select_llm_source_chunks(analysis, questions, chunks, max_confidence)
    if llm_source_chunks:
        return await _generate_with_llm_or_source_fallback(
            state=state,
            analysis=analysis,
            questions=questions,
            source_chunks=llm_source_chunks,
            started_at=started_at,
        )

    if tracer:
        tracer.add(
            "generate",
            int((perf_counter() - started_at) * 1000),
            mode="source_only_escalation",
            reason="insufficient_source_coverage",
        )
    return {
        "should_escalate": True,
        "escalation_reason": "insufficient_sources",
        "generated_response": "",
        "generator_model": "source_only",
        "cited_sources": [],
    }


async def _generate_with_llm_or_source_fallback(
    *,
    state: BotState,
    analysis: QueryAnalysis,
    questions: list[Question],
    source_chunks: list[ScoredChunk],
    started_at: float,
) -> dict:
    result = await _generate_with_llm(
        state=state,
        analysis=analysis,
        questions=questions,
        source_chunks=source_chunks,
        started_at=started_at,
    )
    should_fallback_to_sources = result.get(
        "escalation_reason"
    ) == "llm_generation_failed" or _llm_result_misses_source_coverage(
        result,
        questions,
        source_chunks,
    ) or _response_signals_insufficient_sources(
        str(result.get("generated_response") or ""),
    )
    if not should_fallback_to_sources:
        return result

    source_response = build_deterministic_source_response(source_chunks)
    if not source_response:
        return result

    tracer = state.get("trace")
    fallback_reason = (
        "llm_failed_source_chunk_fallback"
        if result.get("escalation_reason") == "llm_generation_failed"
        else (
            "llm_insufficient_sources_source_chunk_fallback"
            if _response_signals_insufficient_sources(
                str(result.get("generated_response") or "")
            )
            else "llm_missing_sources_source_chunk_fallback"
        )
    )
    if tracer:
        tracer.add(
            "generate",
            int((perf_counter() - started_at) * 1000),
            mode=fallback_reason,
            chunks=len(source_chunks),
        )
    return {
        "generated_response": source_response,
        "generator_model": "source_chunk",
        "cited_sources": [chunk.chunk_id for chunk in source_chunks],
    }


def _response_signals_insufficient_sources(response: str) -> bool:
    normalized = _normalize(response)
    if not normalized:
        return False
    if INSUFFICIENT_SOURCE_RESPONSE_RE.search(normalized):
        return True
    return any(
        marker in normalized
        for marker in (
            "в источниках нет",
            "источники не содержат",
            "источники не подтверждают",
            "информации нет",
            "нет информации о",
            "нет сведений",
            "недостаточно данных",
            "недостаточно подтвержден",
            "недостаточно подтвержд",
            "нет достаточных",
            "не хватает данных",
            "не могу ответить по источникам",
            "не указан",
            "не указана",
            "не указано",
            "отсутствует в источниках",
            "неточный ответ",
            "передаю обращение специалисту",
            "передаю специалисту",
        )
    )


def _llm_result_misses_source_coverage(
    result: dict,
    questions: list[Question],
    source_chunks: list[ScoredChunk],
) -> bool:
    if result.get("should_escalate"):
        return False
    if len(source_chunks) <= 1:
        return False

    cited_ids = set(result.get("cited_sources") or [])
    if not cited_ids:
        return True

    cited_chunks = [chunk for chunk in source_chunks if chunk.chunk_id in cited_ids]
    if not cited_chunks:
        return True

    distinct_questions = [
        question
        for question in questions
        if str(question.text or "").strip()
    ]
    if _has_multiple_distinct_questions(distinct_questions):
        return any(
            not any(_source_chunk_covers_question(question, chunk) for chunk in cited_chunks)
            for question in distinct_questions
        )

    return any(chunk.chunk_id not in cited_ids for chunk in source_chunks)


async def _generate_with_llm(
    *,
    state: BotState,
    analysis: QueryAnalysis,
    questions: list[Question],
    source_chunks: list[ScoredChunk],
    started_at: float,
) -> dict:
    tracer = state.get("trace")
    generator_complexity = _generator_complexity(
        state,
        analysis,
        questions,
        source_chunks,
    )
    model = select_generator_model(generator_complexity)
    try:
        content = await state["llm_client"].generate(
            model=model,
            system=RESPONSE_GENERATOR_SYSTEM,
            user=build_generator_user(
                questions=questions,
                chunks=source_chunks,
                session=state.get("session"),
                params=analysis.extracted_params,
            ),
            response_format="text",
            temperature=0.1,
            max_tokens=1500,
        )
    except Exception as exc:
        if tracer:
            tracer.add_error("generate", int((perf_counter() - started_at) * 1000), exc)
        return {
            "should_escalate": True,
            "escalation_reason": "llm_generation_failed",
            "generated_response": "",
            "generator_model": model,
            "cited_sources": [],
            "error": str(exc),
        }

    content = _repair_recipient_drift(content, source_chunks)
    content = _repair_source_refs(content, source_chunks)
    cited_sources = _known_source_refs(content, source_chunks)
    if tracer:
        tracer.add(
            "generate",
            int((perf_counter() - started_at) * 1000),
            mode="llm",
            model=model,
            chunks=len(source_chunks),
            cited_sources=len(cited_sources),
        )
    return {
        "generated_response": content,
        "generator_model": model,
        "cited_sources": cited_sources,
    }


def _repair_recipient_drift(response: str, source_chunks: list[ScoredChunk]) -> str:
    if not response or not _sources_request_contact_us(source_chunks):
        return response
    repaired = response
    replacements = (
        (r"\bсообщите\s+организаторам\b", "сообщи нам"),
        (r"\bсообщи\s+организаторам\b", "сообщи нам"),
        (r"\bнапишите\s+организаторам\b", "напиши нам"),
        (r"\bнапиши\s+организаторам\b", "напиши нам"),
        (r"\bсвяжитесь\s+с\s+организаторами\b", "свяжись с нами"),
        (r"\bсвяжись\s+с\s+организаторами\b", "свяжись с нами"),
        (r"\bобратитесь\s+к\s+организаторам\b", "напиши нам"),
        (r"\bобратись\s+к\s+организаторам\b", "напиши нам"),
    )
    for pattern, replacement in replacements:
        repaired = re.sub(pattern, replacement, repaired, flags=re.IGNORECASE)
    return repaired


def _repair_source_refs(response: str, chunks: list[ScoredChunk]) -> str:
    if not response:
        return response
    known_ids = [chunk.chunk_id for chunk in chunks]
    known = set(known_ids)
    aliases: dict[str, str | None] = {}
    for chunk_id in known_ids:
        key = _source_ref_key(chunk_id)
        aliases[key] = chunk_id if key not in aliases else None

    def replace(match: re.Match[str]) -> str:
        source_id = match.group(1)
        if source_id in known:
            return match.group(0)
        repaired = aliases.get(_source_ref_key(source_id))
        if not repaired:
            return match.group(0)
        return f"[src:{repaired}]"

    return SOURCE_RE.sub(replace, response)


def _source_ref_key(source_id: str) -> str:
    return (
        str(source_id or "")
        .strip()
        .casefold()
        .replace("ё", "е")
        .replace("projekt", "proekt")
    )


def _sources_request_contact_us(source_chunks: list[ScoredChunk]) -> bool:
    haystack = _normalize(" ".join(chunk.text for chunk in source_chunks))
    return any(
        marker in haystack
        for marker in (
            "сообщи нам",
            "сообщите нам",
            "напиши нам",
            "напишите нам",
            "свяжись с нами",
            "свяжитесь с нами",
        )
    )


def _should_synthesize_with_llm(
    state: BotState,
    analysis: QueryAnalysis,
    questions: list[Question],
    source_chunks: list[ScoredChunk],
) -> bool:
    if not source_chunks:
        return False
    if _is_contextual_synthesis_case(state):
        return True
    if _should_use_extractive_multi_source_answer(analysis, source_chunks):
        return False
    if _can_answer_from_single_official_source(questions, source_chunks):
        return False
    if analysis.complexity == Complexity.COMPLEX:
        return True
    return len(source_chunks) > 1 and _has_multiple_distinct_questions(questions)


def _can_answer_from_single_official_source(
    questions: list[Question],
    source_chunks: list[ScoredChunk],
) -> bool:
    if len(source_chunks) != 1:
        return False
    if _source_type_rank(source_chunks[0]) != 0:
        return False
    return not _has_multiple_distinct_questions(questions)


def _generator_complexity(
    state: BotState,
    analysis: QueryAnalysis,
    questions: list[Question],
    source_chunks: list[ScoredChunk],
) -> Complexity:
    if _is_contextual_synthesis_case(state):
        return Complexity.COMPLEX
    if analysis.complexity == Complexity.COMPLEX:
        return Complexity.COMPLEX
    if len(source_chunks) > 1 and _has_multiple_distinct_questions(questions):
        return Complexity.COMPLEX
    return Complexity.SIMPLE


def _is_contextual_synthesis_case(state: BotState) -> bool:
    contextual_message = str(state.get("contextual_message") or "").strip()
    if not contextual_message:
        return False
    original_message = str(state.get("message_masked") or state.get("message") or "").strip()
    return bool(original_message and contextual_message != original_message)


def _should_use_extractive_multi_source_answer(
    analysis: QueryAnalysis,
    source_chunks: list[ScoredChunk],
) -> bool:
    if len(source_chunks) == 1:
        return _source_type_rank(source_chunks[0]) == 0
    if analysis.category != "\u0444\u043e\u0440\u0443\u043c\u044b":
        return False
    if len(source_chunks) < 2:
        return False
    return all(_source_type_rank(chunk) == 0 for chunk in source_chunks)


def _select_llm_source_chunks(
    analysis: QueryAnalysis,
    questions: list[Question],
    chunks: list[ScoredChunk],
    max_confidence: float,
) -> list[ScoredChunk]:
    settings = get_settings()
    if max_confidence < getattr(settings, "reranker_threshold_low", 0.4):
        return []
    if not questions:
        return []

    candidates = _candidate_source_chunks(analysis, chunks)
    selected: list[ScoredChunk] = []
    selected_ids: set[str] = set()
    for question in questions:
        if _selected_source_chunks_cover_question(question, selected):
            continue

        source_chunk = _topic_source_for_question(analysis, question, candidates)
        if source_chunk is not None:
            if _should_skip_selected_source_chunk(source_chunk, selected, selected_ids):
                continue
            selected.append(source_chunk)
            selected_ids.add(source_chunk.chunk_id)
            continue

        ranked = _rank_source_candidates_for_question(analysis, question, candidates)
        source_chunk = next(
            (
                chunk
                for chunk in ranked
                if _source_chunk_covers_question(question, chunk)
                and (
                    _metadata_matches_specific_question(analysis, question, chunk)
                    or _has_textual_source_overlap(question, chunk)
                )
            ),
            None,
        )
        if source_chunk is None:
            return []
        if _should_skip_selected_source_chunk(source_chunk, selected, selected_ids):
            continue
        selected.append(source_chunk)
        selected_ids.add(source_chunk.chunk_id)
    return selected


def _has_textual_source_overlap(question: Question, chunk: ScoredChunk) -> bool:
    question_tokens = _tokens(question.text)
    if not question_tokens:
        return False
    if question_tokens & _tokens(chunk.text):
        return True

    question_normalized = _normalize(question.text)
    haystack = _source_coverage_haystack(chunk)
    return any(
        any(marker in question_normalized for marker in markers)
        and any(marker in haystack for marker in markers)
        for markers, _question_text in FALLBACK_QUESTION_MARKERS
    )


def _known_source_refs(response: str, chunks: list[ScoredChunk]) -> list[str]:
    known = {chunk.chunk_id for chunk in chunks}
    cited: list[str] = []
    for source_id in SOURCE_RE.findall(response or ""):
        if source_id not in known or source_id in cited:
            continue
        cited.append(source_id)
    return cited


def select_deterministic_source_chunks(
    analysis: QueryAnalysis,
    questions: list[Question],
    chunks: list[ScoredChunk],
    max_confidence: float,
    message: str | None = None,
) -> list[ScoredChunk]:
    if not questions:
        return []
    settings = get_settings()
    if max_confidence < getattr(settings, "reranker_threshold_low", 0.4):
        return []

    selected: list[ScoredChunk] = []
    selected_ids: set[str] = set()
    has_multiple_questions = _has_multiple_distinct_questions(questions)
    original_question = _original_question(analysis, message)
    if original_question is None and len(questions) == 1:
        original_question = questions[0]
    if not has_multiple_questions and (
        _is_specific_technical_question(original_question)
        or _is_feedback_question(original_question)
    ):
        specific_raw_source = _specific_source_for_original_question(
            original_question,
            chunks,
            analysis=analysis,
        )
        if specific_raw_source is not None:
            return [specific_raw_source]

    if not has_multiple_questions:
        exact_raw_source = _exact_source_for_original_question(
            original_question,
            chunks,
            analysis=analysis,
            min_intent_score=2,
        )
        if exact_raw_source is not None:
            return [exact_raw_source]
        specific_raw_source = _specific_source_for_original_question(
            original_question,
            chunks,
            analysis=analysis,
        )
        if specific_raw_source is not None:
            return [specific_raw_source]
        trusted_raw_answer_bank = _trusted_top_answer_bank_source(
            chunks,
            getattr(settings, "reranker_threshold_high", 0.7),
            analysis=analysis,
            original_question=original_question,
            require_original_match=True,
        )
        if trusted_raw_answer_bank is not None:
            return [trusted_raw_answer_bank]
        trusted_compatible_answer_bank = _trusted_top_compatible_answer_bank_source(
            chunks,
            getattr(settings, "reranker_threshold_high", 0.7),
            analysis=analysis,
        )
        if trusted_compatible_answer_bank is not None:
            return [trusted_compatible_answer_bank]
        trusted_raw_official = _trusted_top_official_source(
            chunks,
            getattr(settings, "reranker_threshold_high", 0.7),
            analysis=analysis,
        )
        if trusted_raw_official is not None:
            return [trusted_raw_official]

    candidates = _candidate_source_chunks(analysis, chunks)
    if not candidates:
        return []
    top_answer_bank = _trusted_top_answer_bank_source(
        candidates,
        getattr(settings, "reranker_threshold_high", 0.7),
        analysis=analysis,
    )
    if top_answer_bank is not None and len(questions) == 1:
        return [top_answer_bank]
    top_official_source = _trusted_top_official_source(
        candidates,
        getattr(settings, "reranker_threshold_high", 0.7),
        analysis=analysis,
    )
    if top_official_source is not None and len(questions) == 1:
        return [top_official_source]
    for question in questions:
        if _selected_source_chunks_cover_question(question, selected):
            continue

        source_chunk = _topic_source_for_question(analysis, question, candidates)
        if source_chunk is not None:
            if _should_skip_selected_source_chunk(source_chunk, selected, selected_ids):
                continue
            selected.append(source_chunk)
            selected_ids.add(source_chunk.chunk_id)
            continue

        question_candidates = _rank_source_candidates_for_question(
            analysis,
            question,
            candidates,
        )
        source_chunk = _exact_source_for_original_question(question, question_candidates)
        if source_chunk is not None:
            if _should_skip_selected_source_chunk(source_chunk, selected, selected_ids):
                continue
            selected.append(source_chunk)
            selected_ids.add(source_chunk.chunk_id)
            continue
        source_chunk = next(
            (
                chunk
                for chunk in question_candidates
                if _source_chunk_covers_question(question, chunk)
            ),
            None,
        )
        if source_chunk is None:
            return []
        if _should_skip_selected_source_chunk(source_chunk, selected, selected_ids):
            continue
        selected.append(source_chunk)
        selected_ids.add(source_chunk.chunk_id)
    return selected


def _selected_source_chunks_cover_question(
    question: Question,
    selected: list[ScoredChunk],
) -> bool:
    return any(_source_chunk_strictly_covers_question(question, chunk) for chunk in selected)


def _source_chunk_strictly_covers_question(question: Question, chunk: ScoredChunk) -> bool:
    if _intent_example_matches_question(question, chunk):
        return True

    if question.forum_normalized:
        chunk_forum = str((chunk.metadata or {}).get("forum_normalized") or "").strip()
        if chunk_forum and chunk_forum != question.forum_normalized:
            return False

    question_normalized = _normalize(question.text)
    if _metadata_matches_specific_question(
        QueryAnalysis(
            category=question.category,
            forum_normalized=question.forum_normalized,
        ),
        question,
        chunk,
    ):
        return True

    if _asks_selection_results(question_normalized):
        haystack = _source_coverage_haystack(chunk)
        return "результат" in haystack and "отбор" in haystack

    return False


def _should_skip_selected_source_chunk(
    chunk: ScoredChunk,
    selected: list[ScoredChunk],
    selected_ids: set[str],
) -> bool:
    return chunk.chunk_id in selected_ids or _is_redundant_source_chunk(chunk, selected)


def _is_redundant_source_chunk(chunk: ScoredChunk, selected: list[ScoredChunk]) -> bool:
    for existing in selected:
        if not _chunks_share_response_scope(chunk, existing):
            continue
        if _source_text_overlap(chunk.text, existing.text) >= 0.72:
            return True
    return False


def _chunks_share_response_scope(left: ScoredChunk, right: ScoredChunk) -> bool:
    left_metadata = left.metadata or {}
    right_metadata = right.metadata or {}
    for key in ("category", "forum_normalized"):
        left_value = str(left_metadata.get(key) or "").strip()
        right_value = str(right_metadata.get(key) or "").strip()
        if left_value and right_value and left_value != right_value:
            return False
    return True


def _source_text_overlap(left: str, right: str) -> float:
    left_tokens = _content_tokens(left)
    right_tokens = _content_tokens(right)
    if len(left_tokens) < 6 or len(right_tokens) < 6:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def _content_tokens(text: str) -> set[str]:
    stopwords = {
        "будет",
        "будут",
        "после",
        "этого",
        "форум",
        "форума",
        "мероприятие",
        "мероприятия",
        "платформе",
        "росмолодежь",
        "росмолодёжь",
    }
    return {token for token in _tokens(text) if token not in stopwords}


def _topic_source_for_question(
    analysis: QueryAnalysis,
    question: Question,
    candidates: list[ScoredChunk],
) -> ScoredChunk | None:
    if not str(question.topic or "").strip():
        return None
    exact_matches = [
        chunk
        for chunk in candidates
        if _source_topic_match_rank(question, chunk) == 0
        and _chunk_matches_analysis_scope(chunk, analysis)
    ]
    if exact_matches:
        return _rank_source_candidates_for_question(analysis, question, exact_matches)[0]
    matches = [
        chunk
        for chunk in candidates
        if _source_topic_match_rank(question, chunk) <= 1
        and _chunk_matches_analysis_scope(chunk, analysis)
    ]
    if not matches:
        return None
    return _rank_source_candidates_for_question(analysis, question, matches)[0]


def select_deterministic_source_chunk(
    analysis: QueryAnalysis,
    questions: list[Question],
    chunks: list[ScoredChunk],
    max_confidence: float,
) -> ScoredChunk | None:
    source_chunks = select_deterministic_source_chunks(
        analysis,
        questions,
        chunks,
        max_confidence,
    )
    if len(source_chunks) != 1:
        return None
    return source_chunks[0]


def _has_multiple_distinct_questions(questions: list[Question]) -> bool:
    normalized_questions = {
        _normalize(question.text)
        for question in questions
        if str(question.text or "").strip()
    }
    return len(normalized_questions) > 1


def build_deterministic_source_response(chunks: list[ScoredChunk] | ScoredChunk) -> str | None:
    if isinstance(chunks, ScoredChunk):
        chunks = [chunks]

    if len(chunks) == 1:
        text = chunks[0].text.strip()
        return f"{text} [src:{chunks[0].chunk_id}]" if text else None

    parts: list[str] = []
    seen_paragraphs: set[str] = set()
    seen_links: set[str] = set()
    for chunk in chunks:
        text = _compact_source_chunk_text(
            chunk.text,
            seen_paragraphs=seen_paragraphs,
            seen_links=seen_links,
        )
        if not text:
            continue
        parts.append(f"{text} [src:{chunk.chunk_id}]")
    if not parts:
        return None
    return "\n\n".join(parts)


def _compact_source_chunk_text(
    text: str,
    *,
    seen_paragraphs: set[str],
    seen_links: set[str],
) -> str:
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n{1,}", text.strip())]
    kept: list[str] = []
    for paragraph in paragraphs:
        if not paragraph:
            continue
        normalized = _normalize_source_paragraph(paragraph)
        if not normalized or normalized in seen_paragraphs:
            continue
        links = set(re.findall(r"https?://\S+", paragraph))
        if links and links <= seen_links and _is_link_repeat_paragraph(paragraph):
            continue
        seen_paragraphs.add(normalized)
        seen_links.update(links)
        kept.append(paragraph)
    return "\n".join(kept).strip()


def _normalize_source_paragraph(text: str) -> str:
    normalized = re.sub(r"https?://\S+", "<url>", text.casefold().replace("ё", "е"))
    normalized = re.sub(r"[^\w\s<>]+", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def _is_link_repeat_paragraph(text: str) -> bool:
    normalized = _normalize_source_paragraph(text)
    return len(normalized.split()) <= 18


def _candidate_source_chunks(
    analysis: QueryAnalysis,
    chunks: list[ScoredChunk],
) -> list[ScoredChunk]:
    if _should_prefer_unscoped_grant_source(analysis):
        unscoped_grant_chunks = [
            chunk
            for chunk in chunks
            if _is_unscoped_grant_chunk(chunk)
        ]
        return [*unscoped_grant_chunks, *chunks]

    if analysis.forum_normalized:
        forum_chunks = [
            chunk
            for chunk in chunks
            if (chunk.metadata or {}).get("forum_normalized") == analysis.forum_normalized
        ]
        if forum_chunks and analysis.category:
            category_chunks = [
                chunk
                for chunk in forum_chunks
                if (chunk.metadata or {}).get("category") == analysis.category
            ]
            if analysis.category == "\u0444\u043e\u0440\u0443\u043c\u044b":
                category_chunks = _order_official_sources_first(category_chunks)
            if category_chunks:
                if analysis.category == "\u0444\u043e\u0440\u0443\u043c\u044b":
                    official_category_chunks = _official_source_chunks(category_chunks)
                    if official_category_chunks:
                        category_chunks = official_category_chunks
                category_ids = {chunk.chunk_id for chunk in category_chunks}
                return [
                    *category_chunks,
                    *[
                        chunk
                        for chunk in forum_chunks
                        if chunk.chunk_id not in category_ids
                        and _is_same_forum_compatible_category(
                            analysis.category,
                            chunk,
                        )
                    ],
                ]
        if forum_chunks:
            return forum_chunks

    if analysis.category:
        category_chunks = [
            chunk
            for chunk in chunks
            if (chunk.metadata or {}).get("category") == analysis.category
        ]
        compatible_chunks = [
            chunk
            for chunk in chunks
            if _is_compatible_category(analysis.category, chunk)
        ]
        if category_chunks:
            category_ids = {chunk.chunk_id for chunk in category_chunks}
            return [
                *category_chunks,
                *[
                    chunk
                    for chunk in compatible_chunks
                    if chunk.chunk_id not in category_ids
                ],
            ]
        if compatible_chunks:
            return compatible_chunks

    return chunks


def _order_official_sources_first(chunks: list[ScoredChunk]) -> list[ScoredChunk]:
    return [
        chunk
        for _, chunk in sorted(
            enumerate(chunks),
            key=lambda item: (_source_type_rank(item[1]), item[0]),
        )
    ]


def _official_source_chunks(chunks: list[ScoredChunk]) -> list[ScoredChunk]:
    return [
        chunk
        for chunk in chunks
        if str((chunk.metadata or {}).get("source_type") or "").strip()
        in {"docx", "xlsx"}
    ]


def _source_type_rank(chunk: ScoredChunk) -> int:
    source_type = str((chunk.metadata or {}).get("source_type") or "").strip()
    if source_type in {"docx", "xlsx"}:
        return 0
    if source_type == "ticket_answer_bank":
        return 2
    return 1


def _rank_source_candidates_for_question(
    analysis: QueryAnalysis,
    question: Question,
    chunks: list[ScoredChunk],
) -> list[ScoredChunk]:
    return sorted(
        chunks,
        key=lambda chunk: _source_candidate_priority(analysis, question, chunk),
    )


TOPIC_EQUIVALENCE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"oplata_proezda", "oplata_proezda_palatok_i_pitaniya"}),
    frozenset(
        {
            "transfer_do_mesta_provedeniya",
            "transfer_do_mesta_provedeniya_meropriyatiya",
            "transfer_do_ploschadki_festivalya",
            "transfer_po_gorodu",
        }
    ),
    frozenset(
        {
            "usloviya_pitaniya_i_tochki_s_vodoy",
            "pitanie_i_pite",
            "pitanie_dlya_vegetariancev",
            "informaciya_o_ploschadke_pitanie",
            "informaciya_o_ploschadke_pitanie_pite",
            "informaciya_o_ploschadke_pitanie_pite_i",
        }
    ),
    frozenset(
        {
            "spisok_veschey_i_dokumentov",
            "dokumenty_meropriyatiya",
            "pamyatka_uchastnika_foruma",
        }
    ),
    frozenset({"voprosy_po_zdorovyu_medpunkt", "informaciya_o_ploschadke_medicina"}),
    frozenset({"sut_foruma_i_napravleniya", "sut_festivalya_i_tematika", "o_meropriyatii"}),
    frozenset(
        {
            "daty_nachala_meropriyatiya",
            "mesto_i_daty_provedeniya_meropriyatiya",
            "mesto_i_ploschadka_provedeniya",
            "vremya_nachala_i_raspisanie",
            "sut_festivalya_i_data",
        }
    ),
    frozenset({"dobavlenie_v_chat_i_sluzhba_zaboty", "dobavlenie_v_chat_meropriyatiya"}),
    frozenset({"rosmolodezh_granty", "usloviya_i_sroki_uchastiya_granty"}),
    frozenset({"inostrannye_grazhdane"}),
    frozenset({"uchastniki_s_ovz"}),
    frozenset({"voprosy_po_zdorovyu_medpunkt", "informaciya_o_ploschadke_medicina"}),
    frozenset({"kogda_budet_sertifikat", "mozhno_li_poluchit_sertifikat_za_uchastie"}),
    frozenset(
        {
            "kak_zaregistrirovatsya_na_fgais",
            "registraciya_na_meropriyatie",
            "registraciya_bez_max",
            "podacha_zayavki_na_proekt",
            "podat_zayavku_na_uchastie",
        }
    ),
    frozenset({"otkaz_ot_uchastiya", "kolichestvo_person_otmena_registracii"}),
    frozenset({"vnesti_izmeneniya_v_zayavku"}),
    frozenset({"podtverzhdenie_uchastiya_i_org_momenty"}),
    frozenset({"cifrovaya_nedelya"}),
    frozenset({"rezultaty_rm", "rezultaty_otbora_i_spiski"}),
    frozenset({"usloviya_prozhivaniya", "oplata_proezda_prozhivaniya_i_charter"}),
    frozenset({"trebovaniya_po_dress_kodu"}),
    frozenset(
        {
            "programma_foruma",
            "programma_i_artisty",
            "programma_artisty",
            "vremya_nachala_i_raspisanie",
        }
    ),
    frozenset({"poseschenie_festivalya_s_detmi", "registraciya_detey"}),
    frozenset({"vozrastnye_ogranicheniya"}),
)

DATE_TOPIC_ALIASES = frozenset(
    {
        "daty_nachala_meropriyatiya",
        "mesto_i_daty_provedeniya_meropriyatiya",
        "mesto_i_ploschadka_provedeniya",
        "vremya_nachala_i_raspisanie",
        "sut_festivalya_i_data",
    }
)


def _source_topic_match_rank(question: Question, chunk: ScoredChunk) -> int:
    question_topic = str(question.topic or "").strip()
    question_topic_group = _question_topic_group(question)
    chunk_topic = str((chunk.metadata or {}).get("topic") or "").strip()
    if not question_topic_group:
        return 1
    if chunk_topic == question_topic:
        return 0
    if _equivalent_topic_group(chunk_topic) == question_topic_group:
        return 1
    return 2


def _question_topic_group(question: Question) -> str | None:
    question_topic = str(question.topic or "").strip()
    if question_topic:
        return _equivalent_topic_group(question_topic)
    inferred_topic = _infer_topic_from_question_text(_normalize(question.text))
    return _equivalent_topic_group(inferred_topic) if inferred_topic else None


def _infer_topic_from_question_text(question_normalized: str) -> str | None:
    if _asks_decline_participation(question_normalized):
        return "otkaz_ot_uchastiya"
    if _asks_confirmation_org(question_normalized):
        return "podtverzhdenie_uchastiya_i_org_momenty"
    if _asks_digital_week(question_normalized):
        return "cifrovaya_nedelya"
    if _asks_child_visit(question_normalized):
        return "poseschenie_festivalya_s_detmi"
    if _asks_event_program(question_normalized):
        return "programma_foruma"
    if _asks_age_restrictions(question_normalized):
        return "vozrastnye_ogranicheniya"
    if _asks_forum_grants(question_normalized):
        return "rosmolodezh_granty"
    if _asks_event_chat(question_normalized):
        return "dobavlenie_v_chat_meropriyatiya"
    if _asks_foreign_citizens(question_normalized):
        return "inostrannye_grazhdane"
    if _asks_ovz_participation(question_normalized):
        return "uchastniki_s_ovz"
    if _asks_medical_help(question_normalized):
        return "voprosy_po_zdorovyu_medpunkt"
    if _asks_application_change(question_normalized):
        return "vnesti_izmeneniya_v_zayavku"
    if _asks_selection_results(question_normalized):
        return "rezultaty_rm"
    if _asks_event_overview(question_normalized):
        return "sut_foruma_i_napravleniya"
    if _asks_invitation_letter(question_normalized):
        return "pismo_vyzov"
    if _asks_documents_or_packing(question_normalized):
        return "pamyatka_uchastnika_foruma"
    if _asks_transfer(question_normalized):
        return "transfer_do_mesta_provedeniya_meropriyatiya"
    if _asks_housing_conditions(question_normalized):
        return "usloviya_prozhivaniya"
    if _asks_event_dates_or_marker(question_normalized):
        return "daty_nachala_meropriyatiya"
    if _asks_registration(question_normalized):
        return "podacha_zayavki_na_proekt"
    if _asks_grant_application(question_normalized):
        return "podat_zayavku_na_uchastie"
    return None


def _equivalent_topic_group(topic: str) -> str:
    for group in TOPIC_EQUIVALENCE_GROUPS:
        if topic in group:
            return "|".join(sorted(group))
    return topic


def _source_candidate_priority(
    analysis: QueryAnalysis,
    question: Question,
    chunk: ScoredChunk,
) -> tuple[float, int, int, int, float, int, int, float]:
    if (
        _is_specific_technical_question(question) or _is_feedback_question(question)
    ) and _metadata_matches_specific_question(analysis, question, chunk):
        field_score = _source_metadata_field_score(question, chunk)
        source_rank = _source_type_rank(chunk)
        generic_rank = 1 if _is_generic_chunk(chunk) else 0
        unscoped_grant_rank = _unscoped_grant_rank(analysis, chunk)
        grant_source_category_rank = _grant_source_category_rank(analysis, question, chunk)
        topic_rank = _source_topic_match_rank(question, chunk)
        confidence = float(chunk.reranker_score or chunk.score or 0)
        return (
            0,
            unscoped_grant_rank,
            grant_source_category_rank,
            topic_rank,
            -field_score,
            source_rank,
            generic_rank,
            -confidence,
        )

    intent_score = _adjusted_intent_example_match_score(question, chunk)
    field_score = _source_metadata_field_score(question, chunk)
    source_rank = _source_type_rank(chunk)
    generic_rank = 1 if _is_generic_chunk(chunk) else 0
    unscoped_grant_rank = _unscoped_grant_rank(analysis, chunk)
    grant_source_category_rank = _grant_source_category_rank(analysis, question, chunk)
    topic_rank = _source_topic_match_rank(question, chunk)
    confidence = float(chunk.reranker_score or chunk.score or 0)
    if str(question.topic or "").strip() and topic_rank <= 1:
        return (
            0,
            unscoped_grant_rank,
            grant_source_category_rank,
            topic_rank,
            -field_score,
            source_rank,
            generic_rank,
            -confidence,
        )
    if intent_score:
        return (
            0,
            unscoped_grant_rank,
            grant_source_category_rank,
            topic_rank,
            -float(intent_score * 100) - field_score,
            source_rank,
            generic_rank,
            -confidence,
        )
    if _metadata_matches_specific_question(analysis, question, chunk):
        return (
            1,
            unscoped_grant_rank,
            grant_source_category_rank,
            topic_rank,
            -field_score,
            source_rank,
            generic_rank,
            -confidence,
        )
    if field_score > 0:
        return (
            2,
            unscoped_grant_rank,
            grant_source_category_rank,
            topic_rank,
            -field_score,
            source_rank,
            generic_rank,
            -confidence,
        )
    return (
        3,
        unscoped_grant_rank,
        grant_source_category_rank,
        topic_rank,
        0,
        source_rank,
        generic_rank,
        -confidence,
    )


def _original_question(analysis: QueryAnalysis, message: str | None) -> Question | None:
    text = str(message or "").strip()
    if not text:
        return None
    return Question(
        text=text,
        category=analysis.category,
        forum_normalized=analysis.forum_normalized,
    )


def _is_specific_technical_question(question: Question | None) -> bool:
    if question is None:
        return False
    return _asks_specific_technical_question(_normalize(question.text))


def _is_feedback_question(question: Question | None) -> bool:
    if question is None:
        return False
    normalized = _normalize(question.text)
    return (
        _asks_staff_feedback(normalized)
        or _asks_expert_feedback(normalized)
        or _asks_leave_feedback(normalized)
    )


def _exact_source_for_original_question(
    original_question: Question | None,
    chunks: list[ScoredChunk],
    *,
    analysis: QueryAnalysis | None = None,
    min_intent_score: int = 1,
) -> ScoredChunk | None:
    if original_question is None:
        return None
    matches = [
        (
            _adjusted_intent_example_match_score(original_question, chunk),
            -index,
            chunk,
        )
        for index, chunk in enumerate(chunks)
        if _source_chunk_covers_question(original_question, chunk)
        and (analysis is None or _chunk_matches_analysis_scope(chunk, analysis))
        and (
            not str(original_question.topic or "").strip()
            or _source_topic_match_rank(original_question, chunk) <= 1
        )
        and _intent_example_match_score(original_question, chunk) >= min_intent_score
        and _adjusted_intent_example_match_score(original_question, chunk) > 0
    ]
    if not matches:
        return None
    return max(matches)[2]


def _specific_source_for_original_question(
    original_question: Question | None,
    chunks: list[ScoredChunk],
    *,
    analysis: QueryAnalysis,
) -> ScoredChunk | None:
    if original_question is None:
        return None
    specific_analysis = QueryAnalysis(
        category=original_question.category or analysis.category,
        forum_normalized=original_question.forum_normalized or analysis.forum_normalized,
    )
    matches = [
        chunk
        for chunk in chunks
        if _chunk_matches_analysis_scope(chunk, analysis)
        and _metadata_matches_specific_question(specific_analysis, original_question, chunk)
        and _source_chunk_covers_question(original_question, chunk)
    ]
    if not matches:
        return None
    return _rank_source_candidates_for_question(analysis, original_question, matches)[0]


def _trusted_top_answer_bank_source(
    chunks: list[ScoredChunk],
    threshold: float,
    *,
    analysis: QueryAnalysis | None = None,
    original_question: Question | None = None,
    require_original_match: bool = False,
) -> ScoredChunk | None:
    if not chunks:
        return None
    top_chunk = chunks[0]
    metadata = top_chunk.metadata or {}
    if metadata.get("source_type") != "ticket_answer_bank":
        return None
    if _generic_topic_penalty(top_chunk):
        return None
    if float(top_chunk.reranker_score or 0) < threshold:
        return None
    if require_original_match:
        if original_question is None:
            return None
        if _intent_example_match_score(original_question, top_chunk) < 2:
            return None
        if analysis and not _answer_bank_matches_relaxed_scope(top_chunk, analysis):
            return None
    elif analysis and not _chunk_matches_analysis_scope(top_chunk, analysis):
        return None
    return top_chunk


def _trusted_top_compatible_answer_bank_source(
    chunks: list[ScoredChunk],
    threshold: float,
    *,
    analysis: QueryAnalysis,
) -> ScoredChunk | None:
    if not chunks or not analysis.forum_normalized:
        return None
    top_chunk = chunks[0]
    metadata = top_chunk.metadata or {}
    if metadata.get("source_type") != "ticket_answer_bank":
        return None
    if _is_generic_chunk(top_chunk):
        return None
    if not _chunk_matches_analysis_scope(top_chunk, analysis):
        return None
    if not _is_same_forum_compatible_category(analysis.category, top_chunk):
        return None
    if float(top_chunk.reranker_score or 0) < threshold:
        return None
    return top_chunk


def _trusted_top_official_source(
    chunks: list[ScoredChunk],
    threshold: float,
    *,
    analysis: QueryAnalysis | None = None,
) -> ScoredChunk | None:
    if not chunks:
        return None
    top_chunk = chunks[0]
    metadata = top_chunk.metadata or {}
    if metadata.get("source_type") not in {"xlsx", "docx"}:
        return None
    if analysis and not _chunk_matches_analysis_scope(top_chunk, analysis):
        return None
    if analysis and _should_prefer_unscoped_grant_source(analysis):
        source_category = _normalize(str(metadata.get("source_category") or ""))
        if "грант" not in source_category:
            return None
    if float(top_chunk.reranker_score or 0) < threshold:
        return None
    if float(top_chunk.score or 0) < 0.95:
        return None
    return top_chunk


def _trusted_single_official_source(
    chunks: list[ScoredChunk],
    threshold: float,
    *,
    analysis: QueryAnalysis,
) -> ScoredChunk | None:
    if len(chunks) != 1:
        return None
    chunk = chunks[0]
    metadata = chunk.metadata or {}
    if metadata.get("source_type") not in {"xlsx", "docx"}:
        return None
    if not _chunk_matches_analysis_scope(chunk, analysis):
        return None
    if float(chunk.reranker_score or 0) < threshold:
        return None
    return chunk


def _chunk_matches_analysis_scope(chunk: ScoredChunk, analysis: QueryAnalysis) -> bool:
    metadata = chunk.metadata or {}
    forum = str(metadata.get("forum_normalized") or "").strip()
    if not analysis.forum_normalized:
        return True
    if forum != analysis.forum_normalized:
        return False
    category = str(metadata.get("category") or "").strip()
    if not analysis.category or not category or category == analysis.category:
        return True
    if (
        metadata.get("source_type") == "ticket_answer_bank"
        and _is_same_forum_compatible_category(analysis.category, chunk)
    ):
        return True
    if analysis.category and category and category != analysis.category:
        return False
    return True


def _answer_bank_matches_relaxed_scope(
    chunk: ScoredChunk,
    analysis: QueryAnalysis,
) -> bool:
    metadata = chunk.metadata or {}
    chunk_forum = str(metadata.get("forum_normalized") or "").strip()
    if analysis.forum_normalized and chunk_forum and chunk_forum != analysis.forum_normalized:
        return False

    chunk_category = str(metadata.get("category") or "").strip()
    if not analysis.category or not chunk_category or chunk_category == analysis.category:
        return True
    if _is_same_forum_compatible_category(analysis.category, chunk):
        return True
    if _is_compatible_category(analysis.category, chunk):
        return True

    topic = str(metadata.get("topic") or "").strip()
    return topic in SAFE_CROSS_CATEGORY_ANSWER_BANK_TOPICS


def _is_same_forum_compatible_category(category: str | None, chunk: ScoredChunk) -> bool:
    if not category:
        return False
    chunk_category = str((chunk.metadata or {}).get("category") or "").strip()
    if not chunk_category or chunk_category == category:
        return False
    return chunk_category in SAME_FORUM_COMPATIBLE_CATEGORIES.get(category, set())


def _is_compatible_category(category: str | None, chunk: ScoredChunk) -> bool:
    if not category:
        return False
    metadata = chunk.metadata or {}
    if (
        metadata.get("source_type") == "ticket_answer_bank"
        and str(metadata.get("topic") or "").strip()
        in SAFE_CROSS_CATEGORY_ANSWER_BANK_TOPICS
    ):
        return True
    chunk_category = str((chunk.metadata or {}).get("category") or "").strip()
    if not chunk_category or chunk_category == category:
        return False
    return chunk_category in CATEGORY_COMPATIBLE_CATEGORIES.get(category, set())


def _intent_example_matches_question(question: Question, chunk: ScoredChunk) -> bool:
    return _intent_example_match_score(question, chunk) > 0


def _adjusted_intent_example_match_score(question: Question, chunk: ScoredChunk) -> int:
    score = _intent_example_match_score(question, chunk)
    if score <= 0:
        return 0
    return max(0, score - _generic_topic_penalty(chunk))


def _intent_example_match_score(question: Question, chunk: ScoredChunk) -> int:
    metadata = chunk.metadata or {}
    examples = metadata.get("intent_examples") or []
    if not examples:
        return 0

    question_normalized = _normalize(question.text).strip()
    question_tokens = _tokens(question.text)
    if not question_normalized or not question_tokens:
        return 0

    best_score = 0
    for example in examples:
        example_text = str(example or "")
        example_normalized = _normalize(example_text).strip()
        example_tokens = _tokens(example_text)
        if not example_tokens:
            continue
        if example_normalized == question_normalized:
            best_score = max(best_score, 3)
            continue
        if question_normalized in example_normalized or example_normalized in question_normalized:
            best_score = max(best_score, 2)
            continue
        overlap = question_tokens & example_tokens
        if len(overlap) >= min(3, len(question_tokens), len(example_tokens)):
            best_score = max(best_score, 1)
    return best_score


def _generic_topic_penalty(chunk: ScoredChunk) -> int:
    topic = str((chunk.metadata or {}).get("topic") or "").strip().casefold()
    return 2 if topic in {"прочее", "other", "general", "misc"} else 0


def _metadata_matches_specific_question(
    analysis: QueryAnalysis,
    question: Question,
    chunk: ScoredChunk,
) -> bool:
    metadata_haystack = _metadata_haystack(chunk)
    question_normalized = _normalize(question.text)

    if _asks_staff_feedback(question_normalized):
        return "ostavit_obratnuyu_svyaz_o_sotrudn" in metadata_haystack

    if _asks_leave_feedback(question_normalized):
        return (
            "ostavit_obratnuyu_svyaz_o" in metadata_haystack
            and "sotrudn" not in metadata_haystack
        )

    if _asks_expert_feedback(question_normalized):
        return "zapros_obratnoy_svyazi_kuratora" in metadata_haystack

    if _asks_password_recovery(question_normalized):
        return "vosstanovit_parol" in metadata_haystack

    if analysis.category == "платформа_фгаис":
        if _asks_registration(question_normalized):
            return (
                "kak_zaregistrirovatsya_na_fgais" in metadata_haystack
                or "регистрация_и_заявка" in metadata_haystack
                or "podacha_zayavki" in metadata_haystack
                or "registrac" in metadata_haystack
                or "зарегистрироваться на фгаис" in metadata_haystack
                or "auth/register" in _normalize(chunk.text)
            )
    if _asks_registration(question_normalized):
        return (
            "регистрация_и_заявка" in metadata_haystack
            or "podacha_zayavki" in metadata_haystack
            or "registrac" in metadata_haystack
            or "заявк" in metadata_haystack
        )

    if _asks_contact_operator(question_normalized):
        return "контакты_и_оператор" in metadata_haystack

    if _asks_decline_participation(question_normalized):
        return (
            "otkaz_ot_uchastiya" in metadata_haystack
            or "отказ от участия" in metadata_haystack
            or "отозвать заявку" in _normalize(chunk.text)
            or "отказаться от участия" in _normalize(chunk.text)
        )

    if _asks_medical_help(question_normalized):
        return (
            "voprosy_po_zdorovyu_medpunkt" in metadata_haystack
            or "informaciya_o_ploschadke_medicina" in metadata_haystack
            or "медпункт" in metadata_haystack
            or "медицин" in metadata_haystack
        )

    if _asks_ovz_participation(question_normalized):
        return "uchastniki_s_ovz" in metadata_haystack or "овз" in metadata_haystack

    if _asks_foreign_citizens(question_normalized):
        return (
            "inostrannye_grazhdane" in metadata_haystack
            or "иностран" in metadata_haystack
        )

    if _asks_forum_grants(question_normalized):
        return (
            "rosmolodezh_granty" in metadata_haystack
            or "usloviya_i_sroki_uchastiya_granty" in metadata_haystack
            or "грантов" in metadata_haystack
        )

    if _asks_event_chat(question_normalized):
        return (
            "dobavlenie_v_chat_i_sluzhba_zaboty" in metadata_haystack
            or "dobavlenie_v_chat_meropriyatiya" in metadata_haystack
            or "чат" in metadata_haystack
        )

    if _asks_selection_results(question_normalized):
        return (
            "rezultaty_rm" in metadata_haystack
            or "rezultaty_otbora_i_spiski" in metadata_haystack
            or "результат" in metadata_haystack
        )

    if _asks_application_change(question_normalized):
        return "vnesti_izmeneniya_v_zayavku" in metadata_haystack

    if _asks_confirmation_org(question_normalized):
        return "podtverzhdenie_uchastiya_i_org_momenty" in metadata_haystack

    if _asks_digital_week(question_normalized):
        return "cifrovaya_nedelya" in metadata_haystack

    if _asks_event_program(question_normalized):
        return "programma_foruma" in metadata_haystack

    if _asks_age_restrictions(question_normalized):
        return "vozrastnye_ogranicheniya" in metadata_haystack or "возраст" in metadata_haystack

    if _asks_language_settings(question_normalized):
        return "yazyki" in metadata_haystack or "язык" in metadata_haystack

    if _asks_unlink_gosuslugi(question_normalized):
        return "otvyazat_gu" in metadata_haystack or "госуслуг" in metadata_haystack

    if _asks_verify_other_account(question_normalized):
        return "verificirovat_drugoy_akkaunt" in metadata_haystack

    if _asks_same_email_for_person_and_org(question_normalized):
        return "pochta_fizlica_i_yurlica_sovpadayut" in metadata_haystack

    if _asks_dual_citizenship(question_normalized):
        return "dvoynoe_grazhdanstvo" in metadata_haystack

    if _asks_responsible_person_change(question_normalized):
        return "kak_smenit_otvetstvennoe_lico" in metadata_haystack

    if _asks_municipal_admin_access(question_normalized):
        return "dostup_municipalnogo_administratora" in metadata_haystack

    if _asks_specific_technical_question(question_normalized):
        return False

    if _asks_access_or_technical_error(question_normalized):
        return (
            "доступ_и_техническая_ошибка" in metadata_haystack
            or "tehnicheskaya_oshibka" in metadata_haystack
            or "техническая ошибка" in metadata_haystack
        )

    if _asks_transfer(question_normalized):
        return "transfer" in metadata_haystack or "трансфер" in metadata_haystack

    if _asks_arrival_departure(question_normalized):
        return (
            "vremya_zaezda_i_vyezda" in metadata_haystack
            or ("заезд" in metadata_haystack and "выезд" in metadata_haystack)
        )

    if _asks_invitation_letter(question_normalized):
        return "pismo_vyzov" in metadata_haystack or "письмо-вызов" in metadata_haystack

    if _asks_documents_or_packing(question_normalized):
        return (
            "pamyatka_uchastnika_foruma" in metadata_haystack
            or "документ" in metadata_haystack
            or "паспорт" in metadata_haystack
            or "справк" in metadata_haystack
            or "вещ" in metadata_haystack
        )

    if _asks_event_overview(question_normalized):
        return (
            "o_meropriyatii" in metadata_haystack
            or "sut_foruma" in metadata_haystack
            or "sut_festivalya" in metadata_haystack
            or "tematika" in metadata_haystack
            or "napravleniya" in metadata_haystack
            or "о мероприятии" in metadata_haystack
            or "о форуме" in metadata_haystack
        )

    if _asks_event_dates_or_marker(question_normalized):
        return (
            _chunk_has_date_topic_alias(chunk)
            or "daty_nachala" in metadata_haystack
            or "mesto_i_daty" in metadata_haystack
            or ("даты" in metadata_haystack and "мероприят" in metadata_haystack)
        )

    if analysis.category in {"платформа_фгаис", "техподдержка"} and _asks_profile_id(
        question_normalized
    ):
        return (
            "gde_nayti_id_profilya" in metadata_haystack
            or "id профиля" in metadata_haystack
        )

    if analysis.category == "гранты" and _asks_grant_return(question_normalized):
        return (
            "vernut_denezhnye_sredstva" in metadata_haystack
            or "вернуть денежные средства" in metadata_haystack
            or "вернуть грантовые средства" in _normalize(chunk.text)
        )

    if analysis.category == "гранты" and _asks_grant_project_change(question_normalized):
        text_haystack = _normalize(chunk.text)
        return (
            "vnesti_izmeneniya_v_proekt" in metadata_haystack
            or "внести изменения в проект" in metadata_haystack
            or "изменить смету" in text_haystack
        )

    if analysis.category == "гранты" and _asks_grant_application(question_normalized):
        return (
            "podat_zayavku_na_uchastie" in metadata_haystack
            and "грант" in metadata_haystack
        )

    if _asks_what_is_rosmol(question_normalized):
        return (
            "chto_takoe_rosmolodezh" in metadata_haystack
            or "что такое росмолодежь" in metadata_haystack
        )

    if _asks_cooperation(question_normalized):
        return (
            "predlozhenie_sotrudnichestva" in metadata_haystack
            or "сотруднич" in metadata_haystack
            or "партнер" in metadata_haystack
            or "партнёр" in metadata_haystack
        )

    if _asks_bot_abilities(question_normalized):
        return (
            "vozmozhnosti_bota_abilities" in metadata_haystack
            or "возможности бота" in metadata_haystack
            or "abilities" in metadata_haystack
        )

    if _asks_student_recommendation(question_normalized):
        return (
            "rekomendacii_studenty" in metadata_haystack
            or "рекомендации.студенты" in metadata_haystack
        )

    if _asks_sport_recommendation(question_normalized):
        return (
            "rekomendacii_sport" in metadata_haystack
            or "физическая культура" in metadata_haystack
            or "спорт" in metadata_haystack
        )

    if _asks_farewell(question_normalized):
        return "proschanie" in metadata_haystack or "прощание" in metadata_haystack

    if _asks_recommendation(question_normalized):
        return (
            "rekomendacii_obschie" in metadata_haystack
            or "рекомендации общие" in metadata_haystack
            or "рекомендации.общие" in metadata_haystack
        )

    if _asks_application_status_location(question_normalized):
        return (
            "gde_smotret_status_zayavok" in metadata_haystack
            or "где смотреть статус заявок" in metadata_haystack
        )

    return False


def _should_prefer_unscoped_grant_source(analysis: QueryAnalysis) -> bool:
    return analysis.category == "гранты" and not analysis.forum_normalized


def _unscoped_grant_rank(analysis: QueryAnalysis, chunk: ScoredChunk) -> int:
    if not _should_prefer_unscoped_grant_source(analysis):
        return 0
    return 0 if _is_unscoped_grant_chunk(chunk) else 1


def _is_unscoped_grant_chunk(chunk: ScoredChunk) -> bool:
    metadata = chunk.metadata or {}
    if metadata.get("category") != "гранты":
        return False
    if str(metadata.get("forum_normalized") or "").strip():
        return False
    source_category = _normalize(str(metadata.get("source_category") or ""))
    return not source_category or "грант" in source_category


def _grant_source_category_rank(
    analysis: QueryAnalysis,
    question: Question,
    chunk: ScoredChunk,
) -> int:
    if analysis.category != "гранты":
        return 0
    question_normalized = _normalize(question.text)
    if "грант" not in question_normalized:
        return 0
    source_category = _normalize(str((chunk.metadata or {}).get("source_category") or ""))
    return 0 if "грант" in source_category else 1


def _source_chunk_covers_question(question: Question, chunk: ScoredChunk) -> bool:
    if _intent_example_matches_question(question, chunk):
        return True

    if question.forum_normalized:
        chunk_forum = str((chunk.metadata or {}).get("forum_normalized") or "").strip()
        if chunk_forum and chunk_forum != question.forum_normalized:
            return False

    question_normalized = _normalize(question.text)
    if _metadata_matches_specific_question(
        QueryAnalysis(
            category=question.category,
            forum_normalized=question.forum_normalized,
        ),
        question,
        chunk,
    ):
        return True
    if _asks_housing_conditions(question_normalized):
        return _chunk_has_housing_conditions(chunk)
    if _asks_event_date_marker(question_normalized):
        return _chunk_has_date_topic_alias(chunk)

    haystack = _source_coverage_haystack(chunk)
    for markers, _question_text in FALLBACK_QUESTION_MARKERS:
        if not any(marker in question_normalized for marker in markers):
            continue
        return any(marker in haystack for marker in markers)
    question_tokens = _tokens(question.text)
    if not question_tokens:
        return False
    overlap = question_tokens & _tokens(haystack)
    return len(overlap) >= min(2, len(question_tokens))


def _source_metadata_field_score(question: Question, chunk: ScoredChunk) -> float:
    metadata = chunk.metadata or {}
    question_tokens = _tokens(question.text)
    if not question_tokens:
        return 0.0

    examples = " ".join(str(example or "") for example in metadata.get("intent_examples") or [])
    weighted_fields = (
        (str(metadata.get("topic") or "").replace("_", " "), 5.0),
        (examples, 4.0),
        (str(metadata.get("intent_name") or ""), 3.0),
        (str(metadata.get("source_category") or ""), 2.0),
        (str(metadata.get("forum_normalized") or ""), 1.0),
    )
    score = 0.0
    for field_text, weight in weighted_fields:
        field_tokens = _tokens(field_text)
        if not field_tokens:
            continue
        score += len(question_tokens & field_tokens) * weight
    return score


def _is_generic_chunk(chunk: ScoredChunk) -> bool:
    metadata = chunk.metadata or {}
    value = metadata.get("is_generic")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y"}
    source_category = str(metadata.get("source_category") or "").strip().casefold()
    return source_category.startswith("fallback")


def _asks_housing_conditions(question_normalized: str) -> bool:
    return "условия проживан" in question_normalized or (
        "проживан" in question_normalized
        and not any(marker in question_normalized for marker in ("оплат", "стоимост"))
    )


def _asks_age_restrictions(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in ("возраст", "сколько лет", "какой возраст", "какие годы")
    )


def _asks_registration(question_normalized: str) -> bool:
    return any(marker in question_normalized for marker in ("регистрац", "зарегистр"))


def _asks_profile_id(question_normalized: str) -> bool:
    return any(marker in question_normalized for marker in ("id проф", "айди", "ид проф"))


def _asks_decline_participation(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in (
            "отказ",
            "отказаться",
            "отозвать",
            "отменить участие",
            "отмена заявки",
            "не могу поехать",
            "не смогу поехать",
            "не могу приехать",
            "не смогу приехать",
            "не могу посетить",
            "не смогу посетить",
            "не получается поехать",
            "не получается приехать",
            "не выйдет поехать",
            "не выйдет приехать",
            "потом отказаться",
            "подтвердил участие",
            "подтвердила участие",
        )
    )


def _asks_medical_help(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in ("медпункт", "медицин", "здоров")
    )


def _asks_ovz_participation(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in ("овз", "ограниченными возможн", "инвалид")
    )


def _asks_foreign_citizens(question_normalized: str) -> bool:
    return any(marker in question_normalized for marker in ("иностран", "иностранц"))


def _asks_forum_grants(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in ("грантовый конкурс", "гранты", "грантов")
    )


def _asks_event_chat(question_normalized: str) -> bool:
    return "чат" in question_normalized or "куратор" in question_normalized


def _asks_selection_results(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in ("где посмотреть результ", "результат", "списки", "отбор")
    )


def _asks_application_change(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in (
            "изменить заявку",
            "изменить заявк",
            "внести изменения в заявк",
            "поменять заявк",
        )
    )


def _asks_confirmation_org(question_normalized: str) -> bool:
    return (
        "подтверждени" in question_normalized
        or "подтверд" in question_normalized
        or "подтвердил участие" in question_normalized
    )


def _asks_digital_week(question_normalized: str) -> bool:
    return "цифровая неделя" in question_normalized


def _asks_child_visit(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in ("ребен", "ребён", "дети", "детьми", "ребенком", "ребёнком")
    )


def _asks_event_program(question_normalized: str) -> bool:
    return "программ" in question_normalized


def _asks_grant_return(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in (
            "вернуть грантов",
            "вернуть средства",
            "вернуть деньги",
            "вернуть денеж",
        )
    )


def _asks_grant_project_change(question_normalized: str) -> bool:
    if "грант" not in question_normalized and "проект" not in question_normalized:
        return False
    return any(
        marker in question_normalized
        for marker in (
            "внести измен",
            "изменить проект",
            "изменить смет",
            "поменять смет",
            "скорректировать проект",
            "редактировать проект",
        )
    )


def _asks_grant_application(question_normalized: str) -> bool:
    return "грант" in question_normalized and any(
        marker in question_normalized
        for marker in (
            "подать заяв",
            "подача заяв",
            "заявку на участие",
            "участие в грант",
        )
    )


def _asks_password_recovery(question_normalized: str) -> bool:
    return "парол" in question_normalized and any(
        marker in question_normalized for marker in ("восстанов", "забыл", "сброс")
    )


def _asks_expert_feedback(question_normalized: str) -> bool:
    if "обратн" not in question_normalized:
        return False
    explicit_expert_markers = (
        "эксперт",
        "оценк",
        "балл",
        "разбаллов",
        "куратор",
        "заявк",
        "проект",
    )
    if any(
        marker in question_normalized for marker in ("остав", "поделит", "впечатл", "отзыв")
    ) and not any(marker in question_normalized for marker in explicit_expert_markers):
        return False
    return any(
        marker in question_normalized
        for marker in (*explicit_expert_markers, "грант")
    )


def _asks_staff_feedback(question_normalized: str) -> bool:
    return "обратн" in question_normalized and any(
        marker in question_normalized for marker in ("сотрудн", "специалист", "оператор")
    )


def _asks_leave_feedback(question_normalized: str) -> bool:
    if _asks_staff_feedback(question_normalized):
        return False
    if _asks_expert_feedback(question_normalized):
        return False
    return "обратн" in question_normalized and any(
        marker in question_normalized
        for marker in ("остав", "поделит", "впечатл", "отзыв")
    )


def _asks_what_is_rosmol(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in (
            "что такое росмолод",
            "кто такие росмолод",
            "чем занимается росмолод",
        )
    )


def _asks_cooperation(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in ("сотруднич", "партнерств", "партнёрств", "партнер", "партнёр")
    )


def _asks_bot_abilities(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in (
            "возможности бота",
            "abilities",
            "что умеешь",
            "что ты умеешь",
            "чем можешь помочь",
        )
    )


def _asks_farewell(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in ("до свид", "пока", "прощ", "всего добр", "хорошего дня")
    )


def _asks_recommendation(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in ("рекоменд", "посовет", "подбери", "подойдет", "подойдёт")
    )


def _asks_sport_recommendation(question_normalized: str) -> bool:
    return _asks_recommendation(question_normalized) and any(
        marker in question_normalized for marker in ("спорт", "физкультур", "физическая")
    )


def _asks_student_recommendation(question_normalized: str) -> bool:
    return _asks_recommendation(question_normalized) and any(
        marker in question_normalized for marker in ("студент", "студенч")
    )


def _asks_municipal_admin_access(question_normalized: str) -> bool:
    return any(marker in question_normalized for marker in ("муниципаль", "мо ")) and any(
        marker in question_normalized for marker in ("администратор", "админ")
    )


def _asks_application_status_location(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in (
            "где смотреть статус",
            "где посмотреть статус",
            "где отслеживать статус",
            "статус заявки",
            "статус заявок",
        )
    )


def _asks_contact_operator(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in (
            "оператор",
            "контакт",
            "связаться",
            "поддержк",
            "служба заботы",
        )
    )


def _asks_access_or_technical_error(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in (
            "техническ",
            "ошиб",
            "не работает",
            "не открывается",
            "не загружается",
            "не могу войти",
            "не получается войти",
            "не могу зайти",
            "авторизац",
            "баг",
        )
    )


def _asks_transfer(question_normalized: str) -> bool:
    return any(marker in question_normalized for marker in ("трансфер", "шаттл", "автобус"))


def _asks_arrival_departure(question_normalized: str) -> bool:
    return (
        "заезд" in question_normalized
        and "выезд" in question_normalized
        or any(
            marker in question_normalized
            for marker in (
                "время заезда",
                "время выезда",
                "когда заезд",
                "когда выезд",
            )
        )
    )


def _asks_invitation_letter(question_normalized: str) -> bool:
    return "письмо" in question_normalized and any(
        marker in question_normalized
        for marker in (
            "вызов",
            "на регион",
            "в регион",
            "для региона",
            "подтверждение участия",
        )
    )


def _asks_event_dates(question_normalized: str) -> bool:
    if _asks_arrival_departure(question_normalized):
        return False
    if "когда добав" in question_normalized and "чат" in question_normalized:
        return False
    return any(
        marker in question_normalized
        for marker in (
            "даты проведения",
            "дата проведения",
            "даты мероприятия",
            "дата мероприятия",
            "даты начала",
            "дата начала",
            "когда пройдет",
            "когда пройдёт",
            "когда проходит",
            "когда проводится",
            "когда состоится",
            "когда начинается",
            "когда начнется",
            "когда начнётся",
            "период проведения",
            "место проведения",
            "где проходит",
            "где пройдет",
            "где пройдёт",
            "где будет проходить",
            "где проводится",
            "адрес площадки",
            "локац",
        )
    )


def _asks_event_dates_or_marker(question_normalized: str) -> bool:
    return _asks_event_dates(question_normalized) or _asks_event_date_marker(
        question_normalized
    )


def _asks_event_date_marker(question_normalized: str) -> bool:
    return ("дата" in question_normalized or "даты" in question_normalized) and (
        "срок" in question_normalized
    )


def _asks_event_overview(question_normalized: str) -> bool:
    if "направлен" in question_normalized and "заявк" in question_normalized:
        return False
    if any(
        marker in question_normalized
        for marker in (
            "суть форум",
            "суть мероприятия",
            "о форуме",
            "о мероприятии",
            "что за форум",
            "что это за форум",
            "тематика",
        )
    ):
        return True
    return "направлен" in question_normalized and any(
        marker in question_normalized for marker in ("форум", "мероприят", "трек")
    )


def _asks_documents_or_packing(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in (
            "документ",
            "паспорт",
            "справк",
            "памятк",
            "что взять",
            "взять с собой",
            "вещ",
            "одежд",
            "список необходим",
        )
    )


def _asks_language_settings(question_normalized: str) -> bool:
    return any(marker in question_normalized for marker in ("язык", "языки"))


def _asks_unlink_gosuslugi(question_normalized: str) -> bool:
    return any(marker in question_normalized for marker in ("отвяз", "госуслуг", "есиа"))


def _asks_verify_other_account(question_normalized: str) -> bool:
    return "верифиц" in question_normalized and any(
        marker in question_normalized for marker in ("другой аккаунт", "другую учет", "другую учёт")
    )


def _asks_same_email_for_person_and_org(question_normalized: str) -> bool:
    return any(marker in question_normalized for marker in ("почта", "email", "e-mail")) and any(
        marker in question_normalized for marker in ("физ", "юр", "организац")
    )


def _asks_dual_citizenship(question_normalized: str) -> bool:
    return "двойн" in question_normalized and "граждан" in question_normalized


def _asks_responsible_person_change(question_normalized: str) -> bool:
    return "ответствен" in question_normalized and any(
        marker in question_normalized for marker in ("смен", "измен", "помен")
    )


def _asks_specific_technical_question(question_normalized: str) -> bool:
    return any(
        (
            _asks_language_settings(question_normalized),
            _asks_unlink_gosuslugi(question_normalized),
            _asks_verify_other_account(question_normalized),
            _asks_same_email_for_person_and_org(question_normalized),
            _asks_dual_citizenship(question_normalized),
            _asks_responsible_person_change(question_normalized),
            _asks_password_recovery(question_normalized),
        )
    )


def _chunk_has_housing_conditions(chunk: ScoredChunk) -> bool:
    metadata = chunk.metadata or {}
    if metadata.get("topic") == "usloviya_prozhivaniya":
        return True
    haystack = _normalize(chunk.text)
    return any(term in haystack for term in HOUSING_CONDITION_TERMS)


def _chunk_has_date_topic_alias(chunk: ScoredChunk) -> bool:
    metadata = chunk.metadata or {}
    topic = str(metadata.get("topic") or "").strip()
    if topic in DATE_TOPIC_ALIASES:
        return True
    metadata_haystack = _metadata_haystack(chunk)
    return any(alias in metadata_haystack for alias in DATE_TOPIC_ALIASES)


def _metadata_haystack(chunk: ScoredChunk) -> str:
    metadata = chunk.metadata or {}
    return _normalize(
        " ".join(
            str(value or "")
            for value in (
                metadata.get("chunk_id"),
                metadata.get("intent_name"),
                metadata.get("topic"),
                metadata.get("source_category"),
                metadata.get("forum_normalized"),
            )
        )
    )


def _source_coverage_haystack(chunk: ScoredChunk) -> str:
    metadata = chunk.metadata or {}
    examples = metadata.get("intent_examples") or []
    examples_text = " ".join(str(example or "") for example in examples if example)
    return _normalize(f"{chunk.text} {_metadata_haystack(chunk)} {examples_text}")


def _normalize(text: str) -> str:
    return expand_query_aliases(text).casefold().replace("ё", "е")


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(_normalize(text)))


def effective_questions(state: BotState, analysis: QueryAnalysis) -> list[Question]:
    message = _state_message_for_search(state)
    questions = build_effective_questions(
        analysis,
        message,
    )
    if questions:
        return questions

    text = str(message or "").strip()
    if not text:
        return []
    return [
        Question(
            text=text,
            category=analysis.category,
            forum_normalized=analysis.forum_normalized,
        )
    ]


def _state_message_for_search(state: BotState) -> str:
    return str(
        state.get("contextual_message")
        or state.get("message_masked")
        or state.get("message")
        or ""
    )
