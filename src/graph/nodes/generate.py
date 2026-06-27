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
            return await _generate_with_llm(
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
    if result.get("escalation_reason") != "llm_generation_failed":
        return result

    source_response = build_deterministic_source_response(source_chunks)
    if not source_response:
        return result

    tracer = state.get("trace")
    if tracer:
        tracer.add(
            "generate",
            int((perf_counter() - started_at) * 1000),
            mode="llm_failed_source_chunk_fallback",
            chunks=len(source_chunks),
        )
    return {
        "generated_response": source_response,
        "generator_model": "source_chunk",
        "cited_sources": [chunk.chunk_id for chunk in source_chunks],
    }


async def _generate_with_llm(
    *,
    state: BotState,
    analysis: QueryAnalysis,
    questions: list[Question],
    source_chunks: list[ScoredChunk],
    started_at: float,
) -> dict:
    tracer = state.get("trace")
    model = select_generator_model(analysis.complexity)
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
    if analysis.complexity == Complexity.COMPLEX:
        return True
    return len(source_chunks) > 1 and _has_multiple_distinct_questions(questions)


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
        if source_chunk.chunk_id in selected_ids:
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
        question_candidates = _rank_source_candidates_for_question(
            analysis,
            question,
            candidates,
        )
        source_chunk = _exact_source_for_original_question(question, question_candidates)
        if source_chunk is not None:
            if source_chunk.chunk_id in selected_ids:
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
        if source_chunk.chunk_id in selected_ids:
            continue
        selected.append(source_chunk)
        selected_ids.add(source_chunk.chunk_id)
    return selected


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


def _source_candidate_priority(
    analysis: QueryAnalysis,
    question: Question,
    chunk: ScoredChunk,
) -> tuple[float, int, int, float, int, int, float]:
    if (
        _is_specific_technical_question(question) or _is_feedback_question(question)
    ) and _metadata_matches_specific_question(analysis, question, chunk):
        field_score = _source_metadata_field_score(question, chunk)
        source_rank = _source_type_rank(chunk)
        generic_rank = 1 if _is_generic_chunk(chunk) else 0
        unscoped_grant_rank = _unscoped_grant_rank(analysis, chunk)
        grant_source_category_rank = _grant_source_category_rank(analysis, question, chunk)
        confidence = float(chunk.reranker_score or chunk.score or 0)
        return (
            0,
            unscoped_grant_rank,
            grant_source_category_rank,
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
    confidence = float(chunk.reranker_score or chunk.score or 0)
    if intent_score:
        return (
            0,
            unscoped_grant_rank,
            grant_source_category_rank,
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
            -field_score,
            source_rank,
            generic_rank,
            -confidence,
        )
    return (
        3,
        unscoped_grant_rank,
        grant_source_category_rank,
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

    if _asks_event_dates(question_normalized):
        return (
            "daty_nachala" in metadata_haystack
            or "mesto_i_daty" in metadata_haystack
            or "sut_festivalya_i_data" in metadata_haystack
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
            "подтвердил участие",
            "подтвердила участие",
        )
    )


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
            "когда состоится",
            "когда начинается",
            "когда начнется",
            "когда начнётся",
        )
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
