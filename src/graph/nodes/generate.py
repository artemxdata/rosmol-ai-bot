from __future__ import annotations

import re
from collections.abc import Callable
from time import perf_counter

from src.config import get_settings
from src.graph.nodes.respond import normalize_final_response
from src.graph.provenance import (
    MAX_PROVENANCE_SOURCE_IDS,
    PROVENANCE_SCHEMA_VERSION,
    bounded_id_sequence,
    safe_reason,
    source_selection_provenance,
    truncation_counts,
)
from src.graph.query_normalization import expand_query_aliases
from src.graph.question_utils import (
    FALLBACK_QUESTION_MARKERS,
    build_effective_questions,
    named_section_entities,
)
from src.graph.response_profiles import (
    DATE_VALUE_RE,
    LEGACY_EVENT_DATE_TOPICS,
    chunk_has_event_date_evidence,
    detect_response_profiles,
    infer_response_profile,
    response_has_cross_aspect_drift,
    response_has_cross_aspect_drift_for_profiles,
)
from src.graph.response_profiles import (
    asks_event_dates as asks_profile_event_dates,
)
from src.graph.state import BotState
from src.llm.cascade import select_generator_model
from src.llm.prompts import RESPONSE_GENERATOR_SYSTEM, build_generator_user
from src.models import Complexity, QueryAnalysis, Question, ScoredChunk
from src.response_contract import ResponseProfileName, get_response_contract

_RESPONSE_CONTRACT = get_response_contract()
TOKEN_RE = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)
SOURCE_RE = re.compile(r"\[src:([^\]]+)\]", re.IGNORECASE)
SOURCE_GROUP_RE = re.compile(r"(?:\s*\[src:[^\]]+\])+", re.IGNORECASE)
FACT_NUMBER_RE = re.compile(r"(?<![\w])\d+(?![\w])", re.UNICODE)
FACT_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*[./]\s*(\d{1,2})"
    r"(?:\s*[./]\s*(\d{2,4}))?(?!\d)"
)
RUSSIAN_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
WORD_DATE_RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:[-–—]|\s+по\s+)\s*(\d{1,2})\s+"
    rf"({'|'.join(RUSSIAN_MONTHS)})(?:\s+(\d{{4}}))?",
    re.IGNORECASE,
)
WORD_DATE_RE = re.compile(
    rf"(?<!\d)(\d{{1,2}})\s+({'|'.join(RUSSIAN_MONTHS)})"
    r"(?:\s+(\d{4}))?",
    re.IGNORECASE,
)
AGE_RANGE_RE = re.compile(
    r"(?<!\d)(?:от\s+)?(\d{1,2})\s*(?:[-–—]|\s+до\s+)\s*"
    r"(\d{1,2})\s*(?:лет|года?)\b",
    re.IGNORECASE,
)
EXPLICIT_AGE_RE = re.compile(
    r"\b(?:мне|участнику|участнице)\s+(\d{1,2})\s*(?:лет|года?|год)\b|"
    r"\bвозраст(?:\s+участника|\s+участницы)?\s*[:=–—-]?\s*"
    r"(\d{1,2})\s*(?:лет|года?|год)?\b|"
    r"\b(\d{1,2})\s*[-‑–—]?\s*летн(?:ий|яя|ие|их|им|ими|его|ему|ей|ую)\b|"
    r"\bмне\s+(\d{1,2})(?=\s*(?:[,.;!?]|когда\b|могу\b|можно\b|"
    r"подхожу\b|$))",
    re.IGNORECASE,
)
MINOR_AGE_ALIAS_RE = re.compile(
    r"\b(?:несовершеннолетн|подрост|дет(?:ск|и\b|ей\b)|реб[её]н)\w*\b",
    re.IGNORECASE,
)
ADULT_AGE_ALIAS_RE = re.compile(
    r"\b(?:совершеннолетн|взросл)\w*\b",
    re.IGNORECASE,
)
AUDIENCE_ROLE_STEMS = {
    "настав": "mentors",
    "школьн": "school_students",
    "студент": "students",
    "очн": "in_person",
    "заочн": "remote",
    "педагог": "teachers",
    "учител": "teachers",
    "волонт": "volunteers",
}
URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
TECHNICAL_ACTION_MARKERS = (
    "очисти кеш",
    "очистить кеш",
    "очисти кэш",
    "очистить кэш",
    "cookie",
    "куки",
    "другой браузер",
    "другом браузере",
    "режим инкогнито",
    "другое устройство",
    "другом устройстве",
    "обнови страницу",
    "перезагрузи страницу",
    "проверь интернет",
    "проверь соединение",
    "попробуй позднее",
)
EMOJI_RE = re.compile(
    "["
    "\U0001f1e6-\U0001f1ff"
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001faff"
    "\u2600-\u27bf"
    "\ufe0f"
    "\u200d"
    "]+",
)
SIMPLE_RESPONSE_MAX_CHARS = _RESPONSE_CONTRACT.limits.simple_max_chars
COMPLEX_RESPONSE_MAX_CHARS = _RESPONSE_CONTRACT.limits.compound_max_chars
FACTUAL_SOURCE_TYPE = _RESPONSE_CONTRACT.fact_policy.source_type
# Kept as a public compatibility name for tests/imports. The effective limit is
# selected per response by `_response_char_limit`.
MAX_EXTRACTIVE_SINGLE_SOURCE_CHARS = SIMPLE_RESPONSE_MAX_CHARS
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
APPLICATION_RESPONSE_TOPIC_FAMILIES = frozenset(
    {
        "otkaz_ot_uchastiya",
        "kolichestvo_person_otmena_registracii",
        "poluchenie_i_naznachenie_bileta",
        "bilet_ne_prishel_povtornoe_poluchenie",
    }
)


async def generate(state: BotState) -> dict:
    result = await _generate_core(state)
    selected_source_ids = _known_source_ids(
        state,
        result.get("_selected_source_ids") or result.get("cited_sources"),
    )
    guarded = await _enforce_generation_contract(state, result)
    guarded = _without_internal_generation_markers(guarded)
    _trace_generation_selection(state, guarded, selected_source_ids)
    return guarded


async def _generate_core(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    analysis = state["analysis"]
    questions = effective_questions(state, analysis)
    chunks = [
        chunk
        for chunk in state.get("reranked_chunks", [])
        if str((chunk.metadata or {}).get("source_type") or "").strip().casefold()
        == FACTUAL_SOURCE_TYPE
    ]
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

    catalog_source = _general_catalog_source(analysis.questions or questions, chunks)
    if catalog_source is not None:
        source_response = build_deterministic_source_response([catalog_source])
        if source_response:
            if tracer:
                tracer.add(
                    "generate",
                    int((perf_counter() - started_at) * 1000),
                    mode="general_catalog_source_chunk",
                    chunks=1,
                )
            return {
                "generated_response": source_response,
                "generator_model": "source_chunk",
                "cited_sources": [catalog_source.chunk_id],
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
            questions=questions,
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
        partial_chunks, missing_questions = select_partial_source_chunks(
            analysis,
            questions,
            chunks,
            max_confidence,
        )
        partial_response = build_partial_source_response(partial_chunks, missing_questions)
        if partial_response:
            if tracer:
                tracer.add(
                    "generate",
                    int((perf_counter() - started_at) * 1000),
                    mode="complex_partial_source_chunk",
                    chunks=len(partial_chunks),
                    missing=len(missing_questions),
                )
            return {
                "generated_response": partial_response,
                "generator_model": "source_chunk",
                "cited_sources": [chunk.chunk_id for chunk in partial_chunks],
                "partial_source_missing_coverage": [
                    _partial_question_label(question) for question in missing_questions
                ],
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

    partial_chunks, missing_questions = select_partial_source_chunks(
        analysis,
        questions,
        chunks,
        max_confidence,
    )
    partial_response = build_partial_source_response(partial_chunks, missing_questions)
    if partial_response:
        if tracer:
            tracer.add(
                "generate",
                int((perf_counter() - started_at) * 1000),
                mode="partial_source_chunk",
                chunks=len(partial_chunks),
                missing=len(missing_questions),
            )
        return {
            "generated_response": partial_response,
            "generator_model": "source_chunk",
            "cited_sources": [chunk.chunk_id for chunk in partial_chunks],
            "partial_source_missing_coverage": [
                _partial_question_label(question) for question in missing_questions
            ],
        }

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


async def _enforce_generation_contract(state: BotState, result: dict) -> dict:
    if result.get("should_escalate"):
        return _without_internal_generation_markers(result)

    response = str(result.get("generated_response") or "").strip()
    if not response:
        return _generation_contract_failure(
            result,
            reason="empty_generated_response",
        )

    analysis = state["analysis"]
    questions = effective_questions(state, analysis)
    response_limit = _response_char_limit(analysis, questions)
    generator_model = str(result.get("generator_model") or "")

    if generator_model == "source_chunk":
        sanitized = _strip_dynamic_emoji(response)
        if not sanitized:
            return _generation_contract_failure(
                result,
                reason="source_response_contract_failed",
            )
        if result.get("partial_source_missing_coverage"):
            cited_ids = list(result.get("cited_sources") or [])
            chunks_by_id = {
                chunk.chunk_id: chunk
                for chunk in state.get("reranked_chunks", [])
                if str((chunk.metadata or {}).get("source_type") or "")
                .strip()
                .casefold()
                == FACTUAL_SOURCE_TYPE
            }
            selected_chunks = [
                chunks_by_id[chunk_id]
                for chunk_id in cited_ids
                if chunk_id in chunks_by_id
            ]
            bounded_source_result = _bounded_published_source_result(
                analysis=analysis,
                questions=questions,
                source_chunks=selected_chunks,
                response_limit=response_limit,
                request_text=_state_current_user_message(state),
            )
            if bounded_source_result is not None:
                if tracer := state.get("trace"):
                    tracer.add(
                        "generate",
                        0,
                        mode="bounded_published_source_chunk",
                        chunks=len(selected_chunks),
                    )
                return bounded_source_result
            if (
                _visible_response_length(sanitized) > response_limit
                or _response_url_count(sanitized) > 1
                or not result.get("cited_sources")
            ):
                return _generation_contract_failure(
                    result,
                    reason="source_response_contract_failed",
                )
            guarded = dict(result)
            guarded["generated_response"] = sanitized
            return _without_internal_generation_markers(guarded)
        cited_ids = list(result.get("cited_sources") or [])
        requires_synthesis = (
            len(cited_ids) > 1
            or _visible_response_length(sanitized) > response_limit
            or _response_url_count(sanitized) > 1
            or _violates_response_profile(sanitized, analysis, questions)
        )
        if not requires_synthesis:
            guarded = dict(result)
            guarded["generated_response"] = sanitized
            return _without_internal_generation_markers(guarded)

        chunks_by_id = {
            chunk.chunk_id: chunk
            for chunk in state.get("reranked_chunks", [])
            if str((chunk.metadata or {}).get("source_type") or "").strip().casefold()
            == FACTUAL_SOURCE_TYPE
        }
        source_chunks = [
            chunks_by_id[chunk_id]
            for chunk_id in cited_ids
            if chunk_id in chunks_by_id
        ]
        if not source_chunks:
            return _generation_contract_failure(
                result,
                reason="source_response_contract_failed",
            )
        return await _generate_with_llm_or_source_fallback(
            state=state,
            analysis=analysis,
            questions=questions,
            source_chunks=source_chunks,
            started_at=perf_counter(),
            response_limit=response_limit,
        )

    sanitized = _strip_dynamic_emoji(response)
    if not sanitized:
        return _generation_contract_failure(
            result,
            reason="llm_response_contract_failed",
        )
    if _visible_response_length(sanitized) > response_limit:
        return _generation_contract_failure(
            result,
            reason="llm_response_too_long",
        )
    if _response_url_count(sanitized) > 1:
        return _generation_contract_failure(
            result,
            reason="llm_response_contract_failed",
        )

    guarded = dict(result)
    guarded["generated_response"] = sanitized
    return _without_internal_generation_markers(guarded)


async def _generate_with_llm_or_source_fallback(
    *,
    state: BotState,
    analysis: QueryAnalysis,
    questions: list[Question],
    source_chunks: list[ScoredChunk],
    started_at: float,
    response_limit: int | None = None,
) -> dict:
    tracer = state.get("trace")
    bounded_source_result = _bounded_published_source_result(
        analysis=analysis,
        questions=questions,
        source_chunks=source_chunks,
        response_limit=(
            response_limit or _response_char_limit(analysis, questions)
        ),
        request_text=_state_current_user_message(state),
    )
    if bounded_source_result is not None:
        if tracer:
            tracer.add(
                "generate",
                int((perf_counter() - started_at) * 1000),
                mode="bounded_published_source_chunk",
                chunks=len(source_chunks),
            )
        return bounded_source_result

    last_result: dict = {}
    retry_reason: str | None = None
    rejected_draft: str | None = None
    for attempt in range(2):
        result = await _generate_with_llm(
            state=state,
            analysis=analysis,
            questions=questions,
            source_chunks=source_chunks,
            started_at=started_at,
            response_limit=response_limit,
            retry_reason=retry_reason,
            rejected_draft=rejected_draft,
        )
        last_result = result
        candidate = str(result.get("_rejected_candidate") or "").strip()
        if candidate:
            rejected_draft = candidate
        invalid_coverage = (
            not result.get("should_escalate")
            and (
                _llm_result_misses_source_coverage(
                    result,
                    questions,
                    source_chunks,
                    explicit_questions=list(analysis.questions or []),
                )
                or _response_signals_insufficient_sources(
                    str(result.get("generated_response") or ""),
                )
            )
        )
        if invalid_coverage:
            rejected_draft = str(result.get("generated_response") or "").strip()
        if not result.get("should_escalate") and not invalid_coverage:
            return _with_selected_source_ids(result, source_chunks)
        retry_reason = (
            str(result.get("escalation_reason") or "")
            or "llm_source_coverage_failed"
        )
        if attempt == 0:
            if tracer:
                tracer.add(
                    "generate_retry",
                    int((perf_counter() - started_at) * 1000),
                    reason=retry_reason,
                    chunks=len(source_chunks),
                )
            continue

    return _with_selected_source_ids(
        _generation_contract_failure(
            last_result,
            reason=retry_reason or "llm_response_contract_failed",
        ),
        source_chunks,
    )


def _response_char_limit(
    analysis: QueryAnalysis,
    questions: list[Question],
) -> int:
    if (
        analysis.complexity == Complexity.COMPLEX
        or _has_multiple_distinct_questions(questions)
    ):
        return COMPLEX_RESPONSE_MAX_CHARS
    return SIMPLE_RESPONSE_MAX_CHARS


def _generator_prompt_char_limit(
    response_limit: int,
    retry_reason: str | None,
) -> int:
    # Leave deterministic headroom for tone/spacing normalization. On retry the
    # model sees the rejected draft and can repair it to a tighter target while
    # the immutable response-contract limit remains unchanged.
    numerator = 13 if retry_reason else 16
    return max(1, response_limit * numerator // 20)


def _visible_response_length(response: str) -> int:
    return len(normalize_final_response(response))


def _response_url_count(response: str) -> int:
    return len(URL_RE.findall(normalize_final_response(response)))


def _strip_dynamic_emoji(response: str) -> str:
    sanitized = EMOJI_RE.sub("", response)
    sanitized = re.sub(r"[ \t]{2,}", " ", sanitized)
    sanitized = re.sub(r"[ \t]+\n", "\n", sanitized)
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()


def _generation_contract_failure(result: dict, *, reason: str) -> dict:
    failed = {
        "should_escalate": True,
        "escalation_reason": reason,
        "generated_response": "",
        "generator_model": result.get("generator_model") or "source_only",
        "cited_sources": [],
    }
    if error := result.get("error"):
        failed["error"] = error
    if rejected_candidate := str(result.get("_rejected_candidate") or "").strip():
        failed["_rejected_candidate"] = rejected_candidate
    return failed


def _without_internal_generation_markers(result: dict) -> dict:
    internal_markers = {
        "_llm_synthesis_attempted",
        "_rejected_candidate",
        "_selected_source_ids",
    }
    if not (internal_markers & result.keys()):
        return result
    cleaned = dict(result)
    cleaned.pop("_llm_synthesis_attempted", None)
    cleaned.pop("_rejected_candidate", None)
    cleaned.pop("_selected_source_ids", None)
    return cleaned


def _with_selected_source_ids(result: dict, chunks: list[ScoredChunk]) -> dict:
    enriched = dict(result)
    enriched["_selected_source_ids"] = [
        str(chunk.chunk_id) for chunk in chunks if chunk.chunk_id
    ]
    return enriched


def _known_source_ids(state: BotState, values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    known = {
        str(chunk.chunk_id)
        for chunk in state.get("reranked_chunks", [])
        if chunk.chunk_id
    }
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        chunk_id = str(value or "").strip()
        if not chunk_id or chunk_id not in known or chunk_id in seen:
            continue
        seen.add(chunk_id)
        result.append(chunk_id)
    return result


def _trace_generation_selection(
    state: BotState,
    result: dict,
    selected_source_ids: list[str],
) -> None:
    tracer = state.get("trace")
    if not tracer:
        return
    cited_source_ids = _known_source_ids(state, result.get("cited_sources"))
    bounded_selected_source_ids, selected_total = bounded_id_sequence(
        selected_source_ids,
        limit=MAX_PROVENANCE_SOURCE_IDS,
    )
    bounded_cited_source_ids, cited_total = bounded_id_sequence(
        cited_source_ids,
        limit=MAX_PROVENANCE_SOURCE_IDS,
    )
    question_source_overlaps, candidate_uncovered_question_ids, overlap_counts = (
        source_selection_provenance(
            state.get("retrieval_provenance"),
            selected_source_ids,
        )
    )
    if result.get("should_escalate"):
        contract_status = "failed"
        reason = safe_reason(
            result.get("escalation_reason"),
            default="generation_failed",
        )
    elif result.get("partial_source_missing_coverage"):
        contract_status = "partial"
        reason = "partial_source_coverage"
    else:
        contract_status = "passed"
        reason = "passed"
    mode = "unknown"
    for event in reversed(tracer.events):
        if event.node != "generate":
            continue
        mode = safe_reason(event.metadata.get("mode"), default="unknown")
        break
    tracer.add(
        "generate_selection",
        0,
        schema_version=PROVENANCE_SCHEMA_VERSION,
        mode=mode,
        generator_path=mode,
        source_chunk_applied=(
            str(result.get("generator_model") or "").strip() == "source_chunk"
        ),
        selected_source_ids=bounded_selected_source_ids,
        **truncation_counts(
            total=selected_total,
            recorded=len(bounded_selected_source_ids),
            label="selected_source_ids",
        ),
        cited_source_ids=bounded_cited_source_ids,
        **truncation_counts(
            total=cited_total,
            recorded=len(bounded_cited_source_ids),
            label="cited_source_ids",
        ),
        selection_binding_scope="global_exact_question_unattributed",
        question_source_overlaps=question_source_overlaps,
        candidate_uncovered_question_ids=candidate_uncovered_question_ids,
        **overlap_counts,
        contract_status=contract_status,
        reason=reason,
    )


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
    *,
    explicit_questions: list[Question] | None = None,
) -> bool:
    if result.get("should_escalate"):
        return False
    response = str(result.get("generated_response") or "")
    if len(source_chunks) == 1 and _response_misses_explicit_answer_aspect(
        response,
        explicit_questions or [],
    ):
        return True
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


def _response_misses_explicit_answer_aspect(
    response: str,
    questions: list[Question],
) -> bool:
    """Fail closed when grounded claims omit a bounded explicit aspect.

    Counting citations is not enough: a model can repeat the same cited fact
    twice and still omit the second question.  Only explicit analyzer
    questions whose topic has a deterministic text predicate participate here;
    contextual/inferred questions and unknown topics keep the older source
    coverage contract instead of being guessed from free text.
    """

    detectors_by_group: dict[str, list[Callable[[str], bool]]] = {}
    for topic, detector in _answer_aspect_text_detectors():
        detectors_by_group.setdefault(_answer_aspect_topic_key(topic), []).append(detector)
    required: dict[str, tuple[Callable[[str], bool], ...]] = {}
    for question in questions:
        if not str(question.text or "").strip() or not str(question.topic or "").strip():
            continue
        topic_group = _question_answer_aspect_key(question)
        detectors = detectors_by_group.get(topic_group or "")
        if detectors:
            required[topic_group or ""] = tuple(detectors)

    if len(required) < 2:
        return False

    claims = _grounded_claim_texts(response)
    if not claims:
        return True
    normalized_claims = [_normalize(claim) for claim in claims]
    return any(
        not any(
            detector(claim)
            for detector in detectors
            for claim in normalized_claims
        )
        for detectors in required.values()
    )


def _grounded_claim_texts(response: str) -> list[str]:
    claims: list[str] = []
    previous_end = 0
    for marker_group in SOURCE_GROUP_RE.finditer(response):
        claim = SOURCE_RE.sub("", response[previous_end : marker_group.start()]).strip()
        previous_end = marker_group.end()
        if claim:
            claims.append(claim)
    return claims


def _answer_aspect_text_detectors() -> tuple[
    tuple[str, Callable[[str], bool]],
    ...,
]:
    """Return the bounded topic-to-text contract used for aspect coverage."""

    return (
        ("otkaz_ot_uchastiya", _asks_decline_participation),
        ("podtverzhdenie_uchastiya_i_org_momenty", _asks_confirmation_org),
        ("cifrovaya_nedelya", _asks_digital_week),
        ("poseschenie_festivalya_s_detmi", _asks_child_visit),
        ("programma_foruma", _asks_event_program),
        ("vozrastnye_ogranicheniya", _asks_age_restrictions),
        ("rosmolodezh_granty", _asks_forum_grants),
        ("dobavlenie_v_chat_meropriyatiya", _asks_event_chat),
        ("inostrannye_grazhdane", _asks_foreign_citizens),
        ("uchastniki_s_ovz", _asks_ovz_participation),
        ("voprosy_po_zdorovyu_medpunkt", _asks_medical_help),
        ("vnesti_izmeneniya_v_zayavku", _asks_application_change),
        ("rezultaty_rm", _asks_selection_results),
        ("sut_foruma_i_napravleniya", _asks_event_overview),
        ("pismo_vyzov", _asks_invitation_letter),
        ("pamyatka_uchastnika_foruma", _asks_documents_or_packing),
        ("oplata_proezda", _asks_travel_payment),
        ("transfer_do_mesta_provedeniya_meropriyatiya", _asks_transfer),
        ("usloviya_prozhivaniya", _asks_housing_conditions),
        ("daty_nachala_meropriyatiya", _asks_event_dates_or_marker),
        ("podacha_zayavki_na_proekt", _asks_registration),
        ("podat_zayavku_na_uchastie", _asks_grant_application),
        (
            "registraciya_s_pomoschyu_sozdaniya_kabineta",
            _asks_account_creation,
        ),
        ("volonterskaya_pomosch", _asks_volunteer_application),
    )


async def _generate_with_llm(
    *,
    state: BotState,
    analysis: QueryAnalysis,
    questions: list[Question],
    source_chunks: list[ScoredChunk],
    started_at: float,
    response_limit: int | None = None,
    retry_reason: str | None = None,
    rejected_draft: str | None = None,
) -> dict:
    tracer = state.get("trace")
    generator_complexity = _generator_complexity(
        state,
        analysis,
        questions,
        source_chunks,
    )
    effective_limit = response_limit or _response_char_limit(analysis, questions)
    prompt_limit = _generator_prompt_char_limit(effective_limit, retry_reason)
    model = select_generator_model(generator_complexity)
    try:
        response_profile = _RESPONSE_CONTRACT.profile(analysis.response_profile)
        content = await state["llm_client"].generate(
            model=model,
            system=RESPONSE_GENERATOR_SYSTEM,
            user=build_generator_user(
                questions=questions,
                chunks=source_chunks,
                session=state.get("session"),
                params=analysis.extracted_params,
                max_chars=prompt_limit,
                response_profile=response_profile.name.value,
                profile_guidance=response_profile.guidance,
                retry_reason=retry_reason,
                rejected_draft=rejected_draft,
            ),
            response_format="text",
            temperature=0.1,
            max_tokens=500 if effective_limit == SIMPLE_RESPONSE_MAX_CHARS else 900,
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
    content = _strip_dynamic_emoji(content)
    if not content:
        return _generation_contract_failure(
            {"generator_model": model},
            reason="llm_response_contract_failed",
        )
    if _visible_response_length(content) > effective_limit:
        return _generation_contract_failure(
            {"generator_model": model, "_rejected_candidate": content},
            reason="llm_response_too_long",
        )
    if _response_url_count(content) > 1:
        return _generation_contract_failure(
            {"generator_model": model, "_rejected_candidate": content},
            reason="llm_response_contract_failed",
        )
    if _response_signals_insufficient_sources(content):
        return _generation_contract_failure(
            {"generator_model": model, "_rejected_candidate": content},
            reason="llm_response_contract_failed",
        )
    cited_sources = _known_source_refs(content, source_chunks)
    if (
        not cited_sources
        or _has_unknown_source_refs(content, source_chunks)
        or (
            len(source_chunks) == 1
            and cited_sources != [source_chunks[0].chunk_id]
        )
    ):
        return _generation_contract_failure(
            {"generator_model": model, "_rejected_candidate": content},
            reason="llm_source_citation_failed",
        )
    if _violates_response_profile(content, analysis, questions):
        return _generation_contract_failure(
            {"generator_model": model, "_rejected_candidate": content},
            reason="llm_response_profile_failed",
        )
    if not _llm_claims_have_bound_source_facts(
        content,
        source_chunks,
        questions,
    ):
        return _generation_contract_failure(
            {"generator_model": model, "_rejected_candidate": content},
            reason="llm_source_fact_binding_failed",
        )
    if tracer:
        tracer.add(
            "generate",
            int((perf_counter() - started_at) * 1000),
            mode="llm",
            model=model,
            chunks=len(source_chunks),
            cited_sources=len(cited_sources),
            response_chars=_visible_response_length(content),
            response_limit=effective_limit,
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
        source_id = match.group(1).strip()
        if source_id in known:
            return f"[src:{source_id}]"
        repaired = aliases.get(_source_ref_key(source_id))
        if not repaired:
            return match.group(0)
        return f"[src:{repaired}]"

    return SOURCE_RE.sub(replace, response)


def _llm_claims_have_bound_source_facts(
    response: str,
    source_chunks: list[ScoredChunk],
    questions: list[Question] | None = None,
) -> bool:
    """Require every cited claim to be bound to one relevant source.

    A union-of-sources check cannot detect swapped facts. Requiring one source per
    claim makes conditional associations verifiable. With multiple retrieved
    sources, the cited source must also cover at least one of the questions that
    the answer is supposed to resolve.
    """

    chunks_by_id = {chunk.chunk_id: chunk for chunk in source_chunks}
    previous_end = 0
    found_claim = False
    for marker_group in SOURCE_GROUP_RE.finditer(response):
        claim = response[previous_end : marker_group.start()]
        previous_end = marker_group.end()
        if not SOURCE_RE.sub("", claim).strip():
            return False
        found_claim = True
        cited_ids = list(dict.fromkeys(SOURCE_RE.findall(marker_group.group(0))))
        if len(cited_ids) != 1:
            return False
        cited_chunk = chunks_by_id.get(cited_ids[0])
        if cited_chunk is None:
            return False
        relevant_questions = (
            _questions_for_cited_claim(claim, questions) if questions else []
        )
        if not _chunk_supports_claim_facts(
            cited_chunk,
            claim,
            relevant_questions,
        ):
            return False
        coverage_questions = relevant_questions or list(questions or [])
        if (
            coverage_questions
            and len(source_chunks) > 1
            and not any(
                _source_chunk_covers_question(question, cited_chunk)
                for question in coverage_questions
            )
        ):
            return False

    trailing_claim = response[previous_end:]
    if re.sub(r"[\s.!?…,:;—–-]+", "", SOURCE_RE.sub("", trailing_claim)):
        return False
    return found_claim


def _questions_for_cited_claim(
    claim: str,
    questions: list[Question],
) -> list[Question]:
    claim_profile = infer_response_profile(QueryAnalysis(), claim)
    if claim_profile == ResponseProfileName.GENERIC:
        return questions
    matching = [
        question
        for question in questions
        if _question_response_profile(question) == claim_profile
    ]
    return matching or questions


def _question_response_profile(question: Question) -> ResponseProfileName:
    topic_group = _question_topic_group(question)
    if topic_group and any(
        topic_group == _equivalent_topic_group(topic)
        for topic in APPLICATION_RESPONSE_TOPIC_FAMILIES
    ):
        return ResponseProfileName.APPLICATION
    return infer_response_profile(
        QueryAnalysis(
            category=question.category,
            forum_normalized=question.forum_normalized,
            questions=[question],
        ),
        question.text,
    )


def _claim_fact_numbers(text: str) -> set[str]:
    without_list_marker = re.sub(
        r"^\s*(?:[-*•]\s+|\d+[.)]\s+)",
        "",
        text,
        flags=re.MULTILINE,
    )
    return {
        str(int(value))
        for value in FACT_NUMBER_RE.findall(without_list_marker)
    }


def _chunk_supports_claim_facts(
    chunk: ScoredChunk,
    claim: str,
    questions: list[Question] | None = None,
) -> bool:
    claim_numbers = _claim_fact_numbers(claim)
    metadata = chunk.metadata or {}
    conditions_summary = metadata.get("conditions_summary")
    summary_text = (
        "\n".join(str(item) for item in conditions_summary)
        if isinstance(conditions_summary, list)
        else str(conditions_summary or "")
    )
    metadata_fact_text = "\n".join(
        (
            "\n".join(str(item) for item in value)
            if isinstance(value, list)
            else str(value)
        )
        for key in (
            "forum_normalized",
            "topic",
            "source_category",
            "intent_name",
            "dates_mentioned",
            "parent_chunk_id",
            "source_heading_path",
            "linked_section_names",
        )
        if (value := metadata.get(key)) not in (None, "", [], {})
    )
    source_text = f"{chunk.text}\n{summary_text}\n{metadata_fact_text}".strip()
    source_numbers = _claim_fact_numbers(source_text)
    if not claim_numbers.issubset(source_numbers):
        return False
    if not set(_date_signatures(claim)).issubset(_date_signatures(source_text)):
        return False
    if not _typed_fact_dimensions_match(
        _critical_nonnumeric_fact_keys(claim),
        _critical_nonnumeric_fact_keys(source_text),
    ):
        return False
    if not bool(metadata.get("has_conditional_logic")):
        return True

    groups = _conditional_fact_groups(source_text)
    if len(groups) <= 1:
        return True
    required_condition_keys = _condition_keys(claim)
    claim_dimensions = {
        _condition_dimension(key) for key in required_condition_keys
    }
    for question in questions or []:
        required_condition_keys.update(
            key
            for key in _condition_keys(question.text)
            if _condition_dimension(key) not in claim_dimensions
        )
    explicit_age_text = claim
    if not _explicit_age_values(explicit_age_text):
        explicit_age_text = " ".join(
            question.text
            for question in questions or []
            if _explicit_age_values(question.text)
        )
    claim_typed_facts = _critical_nonnumeric_fact_keys(claim)
    return any(
        claim_numbers.issubset(_claim_fact_numbers(group))
        and set(_date_signatures(claim)).issubset(_date_signatures(group))
        and _condition_dimensions_match(
            required_condition_keys,
            _condition_keys(group),
        )
        and (
            not explicit_age_text
            or _source_matches_explicit_age_constraints(explicit_age_text, group)
        )
        and _typed_fact_dimensions_match(
            claim_typed_facts,
            _critical_nonnumeric_fact_keys(group),
        )
        for group in groups
    )


def _critical_nonnumeric_fact_keys(text: str) -> set[str]:
    normalized = _normalize(text)
    keys: set[str] = set()

    payer_context = any(
        marker in normalized
        for marker in (
            "оплач",
            "компенс",
            "возмещ",
            "за счет",
            "за счёт",
        )
    )
    responsibility_context = any(
        marker in normalized
        for marker in (
            "предостав",
            "организован",
            "организует",
            "организуют",
            "организовы",
            "обеспеч",
            "отвечает за",
            "обязан",
            "должен",
        )
    )
    actors = _fact_actor_keys(normalized)
    if payer_context:
        keys.update(f"payer:{actor}" for actor in actors)
    if responsibility_context:
        keys.update(f"responsible:{actor}" for actor in actors)

    if re.search(r"\b(?:нельзя|запрещен\w*|не допуска\w*)\b", normalized):
        keys.add("permission:forbidden")
    elif re.search(
        r"\b(?:разрешен\w*|допуска\w*|можно\s+(?:участв\w*|приех\w*|"
        r"прийти|проход\w*|войти|брать|взять|принес\w*|принос\w*|подать|измен\w*))\b",
        normalized,
    ):
        keys.add("permission:allowed")

    if re.search(r"\b(?:не нужно|не требуется|не обязательно)\b", normalized):
        keys.add("requirement:not_required")
    elif re.search(
        r"\b(?:обязательно|необходимо|требуется|"
        r"нужно\s+(?:предоставить|приложить|взять|подать|загрузить|оформить))\b",
        normalized,
    ):
        keys.add("requirement:required")

    if any(marker in normalized for marker in ("может участв", "могут участв", "допуска")):
        keys.update(
            f"eligible:{audience.partition(':')[2]}"
            for audience in _audience_condition_keys(normalized)
        )
    return keys


def _fact_actor_keys(normalized: str) -> set[str]:
    actors: set[str] = set()
    if any(
        marker in normalized
        for marker in (
            "участник оплач",
            "участники оплач",
            "участником",
            "самостоятельно",
            "за свой счет",
            "за свой счёт",
        )
    ):
        actors.add("participant")
    if any(
        marker in normalized
        for marker in (
            "организатор",
            "организующая сторона",
            "за счет организатор",
            "за счёт организатор",
        )
    ):
        actors.add("organizer")
    if "направляющ" in normalized and "сторон" in normalized:
        actors.add("sending_party")
    return actors


def _typed_fact_dimensions_match(
    required_keys: set[str],
    source_keys: set[str],
) -> bool:
    for dimension in {key.partition(":")[0] for key in required_keys}:
        required = {
            key for key in required_keys if key.partition(":")[0] == dimension
        }
        available = {key for key in source_keys if key.partition(":")[0] == dimension}
        if not available or not required & available:
            return False
    return True


def _condition_dimensions_match(
    required_keys: set[str],
    group_keys: set[str],
) -> bool:
    if not required_keys:
        return True

    required_dimensions = {_condition_dimension(key) for key in required_keys}
    return all(
        bool(
            {
                key
                for key in required_keys
                if _condition_dimension(key) == required_dimension
            }
            & {
                key
                for key in group_keys
                if _condition_dimension(key) == required_dimension
            }
        )
        for required_dimension in required_dimensions
    )


def _condition_dimension(key: str) -> str:
    prefix = key.partition(":")[0]
    return "age" if prefix in {"age", "age_group"} else prefix


def _date_signatures(text: str) -> set[str]:
    signatures: set[str] = set()
    years = set(re.findall(r"\b20\d{2}\b", text))
    inherited_year = next(iter(years)) if len(years) == 1 else ""

    def add(day: str, month: int | str, year: str | None = None) -> None:
        normalized_year = str(year or inherited_year or "")
        if normalized_year and len(normalized_year) == 2:
            normalized_year = f"20{normalized_year}"
        base_signature = f"{int(day):02d}.{int(month):02d}"
        signatures.add(base_signature)
        if normalized_year:
            signatures.add(f"{base_signature}.{normalized_year}")

    for day, month, year in FACT_DATE_RE.findall(text):
        add(day, month, year)
    for start_day, end_day, month_name, year in WORD_DATE_RANGE_RE.findall(text):
        month = RUSSIAN_MONTHS[month_name.casefold()]
        add(start_day, month, year)
        add(end_day, month, year)
    for day, month_name, year in WORD_DATE_RE.findall(text):
        add(day, RUSSIAN_MONTHS[month_name.casefold()], year)
    return signatures


def _conditional_fact_groups(text: str) -> list[str]:
    text = re.sub(
        r",\s+(?=(?:для\s+|участник\w*\s+(?:от\s+)?\d|"
        r"(?:перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят)\w*"
        r"\s+смен))",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    clauses = [
        clause.strip()
        for clause in re.split(r"\n+|(?<=[.!?;])\s+", text)
        if clause.strip()
    ]
    condition_keys = {
        key
        for clause in clauses
        for key in _condition_keys(clause)
    }
    numeric_clauses = [
        clause for clause in clauses if _claim_fact_numbers(clause)
    ]
    if len(condition_keys) <= 1 and len(numeric_clauses) <= 1:
        return [text]

    groups: list[str] = []
    current: list[str] = []
    for clause in clauses:
        if _condition_keys(clause) and current:
            groups.append(" ".join(current))
            current = []
        current.append(clause)
    if current:
        groups.append(" ".join(current))
    if condition_keys and groups:
        return groups

    return [
        " ".join(
            [
                *(
                    [clauses[index - 1]]
                    if index > 0
                    and not _claim_fact_numbers(clauses[index - 1])
                    else []
                ),
                clause,
            ]
        )
        for index, clause in enumerate(clauses)
        if _claim_fact_numbers(clause)
    ] or [text]


def _condition_keys(text: str) -> set[str]:
    normalized = _normalize(text)
    age_ranges = [
        (int(start), int(end))
        for start, end in AGE_RANGE_RE.findall(normalized)
    ]
    keys = {f"age:{start}-{end}" for start, end in age_ranges}
    if any(start <= 17 for start, _end in age_ranges):
        keys.add("age_group:minor")
    if any(end >= 18 for _start, end in age_ranges):
        keys.add("age_group:adult")
    if MINOR_AGE_ALIAS_RE.search(normalized):
        keys.add("age_group:minor")
    if ADULT_AGE_ALIAS_RE.search(normalized):
        keys.add("age_group:adult")
    for age in _explicit_age_values(normalized):
        keys.add(f"age_group:{'minor' if age < 18 else 'adult'}")
    keys.update(_audience_condition_keys(normalized))
    ordinal_stems = {
        1: "перв",
        2: "втор",
        3: "трет",
        4: "четверт",
        5: "пят",
        6: "шест",
        7: "седьм",
        8: "восьм",
        9: "девят",
        10: "десят",
    }
    keys.update(
        f"shift:{number}"
        for number, stem in ordinal_stems.items()
        if any(
            re.search(pattern, normalized)
            for pattern in (
                rf"\b{stem}\w*\s+смен\w*\b",
                rf"(?<!\d){number}\s*(?:[-–—]\s*(?:я|ая|й))?"
                rf"\s+смен\w*\b",
                rf"\bсмен\w*\s*(?:№|#|номер)?\s*{number}(?!\d)",
            )
        )
    )
    if not keys:
        for raw_label in re.findall(
            r"(?:^|[.!?\n])\s*((?:для\s+)?[а-яa-z][^:\n.!?]{0,60})\s*:",
            normalized,
        ):
            label = re.sub(r"^для\s+", "", raw_label).strip(" -–—")
            if label and label not in {
                "условия запроса",
                "дата",
                "даты",
                "участник",
                "участники",
            }:
                keys.add(f"label:{label}")
    return keys


def _audience_condition_keys(text: str) -> set[str]:
    normalized = _normalize(text)
    ignored = {
        "участия",
        "регистрации",
        "заявки",
        "получения",
        "поездки",
        "смены",
        "форума",
        "мероприятия",
        "проекта",
    }
    keys: set[str] = set()
    for stem, canonical in AUDIENCE_ROLE_STEMS.items():
        if re.search(rf"\b{re.escape(stem)}\w*\b", normalized):
            keys.add(f"audience:{canonical}")
    for token in re.findall(r"\bдля\s+([а-яё-]{3,40})\b", normalized):
        if (
            token in ignored
            or token.startswith(("участник", "участниц"))
            or MINOR_AGE_ALIAS_RE.fullmatch(token)
            or ADULT_AGE_ALIAS_RE.fullmatch(token)
        ):
            continue
        canonical = next(
            (
                value
                for stem, value in AUDIENCE_ROLE_STEMS.items()
                if token.startswith(stem)
            ),
            None,
        )
        keys.add(f"audience:{canonical or token}")
    return keys


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
    if len(source_chunks) > 1:
        return True
    if (
        bool((source_chunks[0].metadata or {}).get("has_conditional_logic"))
        and len(_conditional_fact_groups(source_chunks[0].text)) > 1
    ):
        return True
    source_response = build_deterministic_source_response(source_chunks)
    if not source_response:
        return True
    if _visible_response_length(source_response) > _response_char_limit(
        analysis,
        questions,
    ):
        return True
    if _response_url_count(source_response) > 1:
        return True
    if _violates_response_profile(source_response, analysis, questions):
        return True
    if _has_multiple_answer_aspects(questions):
        return True
    if _is_contextual_synthesis_case(state) and _single_source_has_unrequested_clauses(
        questions,
        source_chunks,
    ):
        return True
    if _can_answer_from_single_official_source(questions, source_chunks):
        return False
    if _is_contextual_synthesis_case(state):
        return True
    if analysis.complexity == Complexity.COMPLEX:
        return True
    return False


def _single_source_has_unrequested_clauses(
    questions: list[Question],
    source_chunks: list[ScoredChunk],
) -> bool:
    if len(source_chunks) != 1 or not questions:
        return False
    clauses = [
        clause.strip()
        for clause in re.split(r"\n+|(?<=[.!?])\s+", source_chunks[0].text)
        if clause.strip()
    ]
    if len(clauses) <= 1:
        return False

    question_tokens = _content_tokens(" ".join(question.text for question in questions))
    substantive_clauses = [
        _content_tokens(clause)
        for clause in clauses
        if len(_content_tokens(clause)) >= 4
    ]
    return bool(substantive_clauses) and any(
        not clause_tokens & question_tokens
        for clause_tokens in substantive_clauses
    )


def _violates_response_profile(
    response: str,
    analysis: QueryAnalysis,
    questions: list[Question],
) -> bool:
    contract_questions = analysis.questions or questions
    if _has_multiple_distinct_questions(contract_questions):
        expected_profiles = {
            _question_response_profile(question)
            for question in contract_questions
        }
        return response_has_cross_aspect_drift_for_profiles(
            expected_profiles,
            SOURCE_RE.sub("", response).strip(),
        )

    visible = SOURCE_RE.sub("", response).strip()
    if analysis.response_profile == ResponseProfileName.TECHNICAL:
        detected = detect_response_profiles(visible)
        critical_business = {
            ResponseProfileName.DATES,
            ResponseProfileName.APPLICATION,
            ResponseProfileName.DOCUMENTS,
            ResponseProfileName.SELECTION_STATUS,
            ResponseProfileName.TRAVEL,
        }
        return bool(
            detected & critical_business
            and (
                not _has_technical_resolution_action(visible)
                or not _has_technical_recovery_context(visible)
            )
        )
    if analysis.response_profile != ResponseProfileName.DATES:
        return response_has_cross_aspect_drift(analysis.response_profile, visible)

    first_answer_part = re.split(r"(?:[.!?](?:\s|$)|\n)", visible, maxsplit=1)[0]
    if not DATE_VALUE_RE.search(first_answer_part):
        return True

    question_text = _normalize(" ".join(question.text for question in contract_questions))
    response_text = _normalize(visible)
    unsolicited_groups = (
        ("регистрац", "заявк"),
        ("куратор",),
        ("чат",),
        ("проезд", "дорог", "трансфер"),
        ("прожив", "размещ"),
        ("питан",),
    )
    return any(
        any(marker in response_text for marker in group)
        and not any(marker in question_text for marker in group)
        for group in unsolicited_groups
    )


def _has_technical_resolution_action(response: str) -> bool:
    normalized = _normalize(response)
    if any(marker in normalized for marker in TECHNICAL_ACTION_MARKERS):
        return True
    action_objects = (
        (("заполн",), ("данн", "пол", "форм", "анкет")),
        (("нажм",), ("кнопк", "подать заяв", "отправить")),
        (("проверь",), ("пол", "данн", "форм", "почт", "даты регистрац")),
        (("восстанов",), ("доступ", "парол")),
        (("введ",), ("данн", "код", "парол")),
        (("выбер",), ("пол", "значен", "мероприят")),
        (("прикреп",), ("файл", "документ")),
        (("повтор",), ("нажм", "отправ", "загруз", "авториз", "вход")),
    )
    return any(
        any(action in normalized for action in actions)
        and any(subject in normalized for subject in subjects)
        for actions, subjects in action_objects
    )


def _has_technical_recovery_context(response: str) -> bool:
    normalized = _normalize(response)
    return any(
        marker in normalized
        for marker in (
            "ошиб",
            "не работа",
            "не откры",
            "не загруж",
            "не груз",
            "не получ",
            "не приш",
            "не отображ",
            "недоста",
            "обязательн",
            "повтор",
            "проверь",
            "очист",
            "обнов",
            "перезагруз",
            "восстанов",
            "другой браузер",
            "другое устройство",
            "инкогнито",
            "после авторизац",
            "заново вой",
        )
    )


def _general_catalog_source(
    questions: list[Question],
    chunks: list[ScoredChunk],
) -> ScoredChunk | None:
    if len(questions) != 1 or questions[0].topic != "rekomendacii_obschie":
        return None
    candidates = [
        chunk
        for chunk in chunks
        if str((chunk.metadata or {}).get("topic") or "").strip()
        == "rekomendacii_obschie"
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda chunk: (
            _source_type_rank(chunk),
            -float(chunk.reranker_score or chunk.score or 0),
            chunk.chunk_id,
        ),
    )


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
    if _has_multiple_answer_aspects(questions):
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

        source_chunk = _linked_named_section_date_source(question, candidates)
        if source_chunk is None:
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


def _has_unknown_source_refs(
    response: str,
    chunks: list[ScoredChunk],
) -> bool:
    known = {chunk.chunk_id for chunk in chunks}
    return any(source_id not in known for source_id in SOURCE_RE.findall(response or ""))


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
    if not has_multiple_questions and original_question is not None:
        linked_named_source = _linked_named_section_date_source(
            original_question,
            chunks,
        )
        if linked_named_source is not None:
            return [linked_named_source]
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
            original_question=original_question,
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
        original_question=questions[0] if len(questions) == 1 else None,
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


def select_partial_source_chunks(
    analysis: QueryAnalysis,
    questions: list[Question],
    chunks: list[ScoredChunk],
    max_confidence: float,
) -> tuple[list[ScoredChunk], list[Question]]:
    if not _has_multiple_distinct_questions(questions):
        return [], []

    settings = get_settings()
    if max_confidence < getattr(settings, "reranker_threshold_low", 0.4):
        return [], []

    candidates = _candidate_source_chunks(analysis, chunks)
    if not candidates:
        return [], []

    selected: list[ScoredChunk] = []
    selected_ids: set[str] = set()
    missing: list[Question] = []

    for question in questions:
        if _selected_source_chunks_cover_question(question, selected):
            continue

        source_chunk = _topic_source_for_question(analysis, question, candidates)
        if source_chunk is None:
            question_candidates = _rank_source_candidates_for_question(
                analysis,
                question,
                candidates,
            )
            source_chunk = _exact_source_for_original_question(question, question_candidates)
            if source_chunk is None:
                source_chunk = next(
                    (
                        chunk
                        for chunk in question_candidates
                        if _source_chunk_covers_question(question, chunk)
                    ),
                    None,
                )

        if source_chunk is None:
            missing.append(question)
            continue
        if _should_skip_selected_source_chunk(source_chunk, selected, selected_ids):
            continue

        selected.append(source_chunk)
        selected_ids.add(source_chunk.chunk_id)

    if not selected or not missing:
        return [], []
    return selected, missing


def build_partial_source_response(
    chunks: list[ScoredChunk],
    missing_questions: list[Question],
) -> str | None:
    source_response = build_deterministic_source_response(chunks)
    if not source_response:
        return None

    missing_labels = [_partial_question_label(question) for question in missing_questions]
    if not missing_labels:
        return source_response

    missing_text = "; ".join(missing_labels[:5])
    if len(missing_labels) > 5:
        missing_text += f"; ещё {len(missing_labels) - 5}"
    return (
        f"{source_response}\n\n"
        f"По этим пунктам в базе нет подтверждённых данных: {missing_text}. "
        "Чтобы не выдумывать, я не добавляю по ним информацию."
    )


def _partial_question_label(question: Question) -> str:
    topic = str(question.topic or "").replace("_", " ").strip()
    text = " ".join(str(question.text or "").split())
    label = topic or text
    forum = str(question.forum_normalized or "").strip()
    if forum and label and forum not in label:
        return f"{forum}: {label}"
    return label or "неуточнённый пункт"


def _selected_source_chunks_cover_question(
    question: Question,
    selected: list[ScoredChunk],
) -> bool:
    return any(_source_chunk_strictly_covers_question(question, chunk) for chunk in selected)


def _source_chunk_strictly_covers_question(question: Question, chunk: ScoredChunk) -> bool:
    if not _source_matches_explicit_question_constraints(question, chunk):
        return False
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
        if _chunk_has_conditional_fact_binding(chunk) or _chunk_has_conditional_fact_binding(
            existing
        ):
            continue
        text_overlap = _source_text_overlap(chunk.text, existing.text)
        if text_overlap >= 0.72:
            return True
        if text_overlap >= 0.58 and _chunks_share_future_date_notice(chunk, existing):
            return True
    return False


def _chunk_has_conditional_fact_binding(chunk: ScoredChunk) -> bool:
    metadata = chunk.metadata or {}
    return bool(
        metadata.get("has_conditional_logic")
        or metadata.get("conditions_summary")
    )


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


def _chunks_share_future_date_notice(left: ScoredChunk, right: ScoredChunk) -> bool:
    left_text = _normalize(left.text)
    right_text = _normalize(right.text)
    required_markers = ("актуальные даты", "будут объявлены")
    return all(marker in left_text and marker in right_text for marker in required_markers)


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


def _linked_named_section_date_source(
    question: Question,
    candidates: list[ScoredChunk],
) -> ScoredChunk | None:
    entities = _named_question_entities(question)
    if not entities or not asks_profile_event_dates(_normalize(question.text)):
        return None

    anchors: list[ScoredChunk] = []
    for chunk in candidates:
        metadata = chunk.metadata or {}
        anchor_text = _normalize(
            " ".join(
                [
                    chunk.text,
                    str(metadata.get("intent_name") or ""),
                    str(metadata.get("topic") or "").replace("_", " "),
                    " ".join(
                        str(part or "")
                        for part in metadata.get("source_heading_path") or []
                    ),
                ]
            )
        )
        anchor_tokens = _tokens(anchor_text)
        if all(_tokens(entity) and _tokens(entity) <= anchor_tokens for entity in entities):
            anchors.append(chunk)
    if not anchors:
        return None

    linked: list[tuple[int, float, ScoredChunk, ScoredChunk]] = []
    for candidate in candidates:
        metadata = candidate.metadata or {}
        if not chunk_has_event_date_evidence(candidate.text, metadata):
            continue
        candidate_document = str(metadata.get("source_document_id") or "").strip()
        candidate_parent = str(metadata.get("parent_chunk_id") or "").strip()
        try:
            candidate_row = int(metadata.get("source_row"))
        except (TypeError, ValueError):
            candidate_row = -1
        for anchor in anchors:
            anchor_metadata = anchor.metadata or {}
            same_parent = candidate_parent == anchor.chunk_id
            same_document = (
                bool(candidate_document)
                and candidate_document
                == str(anchor_metadata.get("source_document_id") or "").strip()
            )
            try:
                anchor_row = int(anchor_metadata.get("source_row"))
            except (TypeError, ValueError):
                anchor_row = -1
            row_distance = candidate_row - anchor_row
            if not same_parent and not (same_document and row_distance == 1):
                continue
            if question.forum_normalized:
                candidate_forum = str(metadata.get("forum_normalized") or "").strip()
                if candidate_forum and candidate_forum != question.forum_normalized:
                    continue
            linked.append(
                (
                    0 if same_parent else row_distance,
                    -float(candidate.reranker_score or candidate.score or 0),
                    candidate,
                    anchor,
                )
            )
    if not linked:
        return None
    _rank, _score, candidate, anchor = min(
        linked,
        key=lambda item: (item[0], item[1], item[2].chunk_id),
    )
    metadata = {
        **(candidate.metadata or {}),
        "linked_section_names": sorted(entities),
        "linked_anchor_chunk_id": anchor.chunk_id,
    }
    return candidate.model_copy(update={"metadata": metadata})


def _topic_source_for_question(
    analysis: QueryAnalysis,
    question: Question,
    candidates: list[ScoredChunk],
) -> ScoredChunk | None:
    linked_named_source = _linked_named_section_date_source(question, candidates)
    if linked_named_source is not None:
        return linked_named_source
    if not str(question.topic or "").strip():
        return None
    matches = [
        chunk
        for chunk in candidates
        if _source_topic_match_rank(question, chunk) <= 1
        and _chunk_matches_analysis_scope(chunk, analysis)
        and _source_matches_explicit_question_constraints(question, chunk)
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


def _has_multiple_answer_aspects(questions: list[Question]) -> bool:
    return len(_answer_aspect_keys(questions)) > 1


def _answer_aspect_keys(questions: list[Question]) -> set[tuple[str, str]]:
    aspects: set[tuple[str, str]] = set()
    for question in questions:
        if not str(question.text or "").strip():
            continue
        if topic_group := _question_answer_aspect_key(question):
            aspects.add(("topic", topic_group))
            continue
        aspects.add(("profile", _question_response_profile(question).value))
    return aspects


ANSWER_ASPECT_TOPIC_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"oplata_proezda", "oplata_proezda_palatok_i_pitaniya", "kompensaciya"}),
    frozenset(
        {
            "transfer_do_mesta_provedeniya",
            "transfer_do_mesta_provedeniya_meropriyatiya",
            "transfer_do_ploschadki_festivalya",
            "transfer_po_gorodu",
        }
    ),
    frozenset({"programma_foruma", "programma_i_artisty", "programma_artisty"}),
    frozenset(
        {
            "spisok_veschey_i_dokumentov",
            "dokumenty_meropriyatiya",
            "pamyatka_uchastnika_foruma",
        }
    ),
    frozenset({"rezultaty_rm", "rezultaty_otbora_i_spiski"}),
)


def _question_answer_aspect_key(question: Question) -> str | None:
    topic = str(question.topic or "").strip()
    if _normalize(topic).replace(" ", "_") == "оплата_проезда":
        topic = "oplata_proezda"
    if not topic:
        topic = _infer_topic_from_question_text(_normalize(question.text)) or ""
    return _answer_aspect_topic_key(topic) if topic else None


def _answer_aspect_topic_key(topic: str) -> str:
    for group in ANSWER_ASPECT_TOPIC_ALIAS_GROUPS:
        if topic in group:
            return "|".join(sorted(group))
    return topic


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


def _bounded_published_source_result(
    *,
    analysis: QueryAnalysis,
    questions: list[Question],
    source_chunks: list[ScoredChunk],
    response_limit: int,
    request_text: str = "",
) -> dict | None:
    """Build a strict, bounded answer from selected published Yonote clauses.

    This fast path is deliberately narrow: it only handles known structured
    source topics where the requested fact can be copied from a stable clause.
    The resulting draft still has to satisfy the same length, URL, profile,
    citation, fact-binding and question-coverage checks as an LLM draft.
    """

    contract_questions = _published_contract_questions(
        list(analysis.questions or []),
        questions,
    )
    if not contract_questions or not source_chunks:
        return None
    if len(source_chunks) > 1 and any(
        not str(question.topic or "").strip()
        for question in contract_questions
    ):
        return None
    if any(not _is_published_yonote_release_chunk(chunk) for chunk in source_chunks):
        return None

    claims: list[str] = []
    cited_sources: list[str] = []
    covered_question_ids: set[int] = set()
    for chunk in source_chunks:
        matching_questions = _questions_matching_published_source(
            analysis,
            contract_questions,
            chunk,
            request_text=request_text,
        )
        if not matching_questions:
            return None
        covered_question_ids.update(id(question) for question in matching_questions)
        excerpts = _published_source_excerpts(
            chunk,
            matching_questions,
            request_text=request_text,
        )
        if not excerpts:
            return None
        cited_sources.append(chunk.chunk_id)
        claims.extend(
            f"{excerpt} [src:{chunk.chunk_id}]"
            for excerpt in excerpts
            if excerpt.strip()
        )

    if any(
        id(question) not in covered_question_ids
        for question in contract_questions
    ):
        return None

    response = _strip_dynamic_emoji("\n\n".join(claims).strip())
    if not response or _visible_response_length(response) > response_limit:
        return None
    if _response_url_count(response) > 1:
        return None
    if _has_unknown_source_refs(response, source_chunks):
        return None
    if _known_source_refs(response, source_chunks) != cited_sources:
        return None
    if _violates_response_profile(response, analysis, contract_questions):
        return None
    if not _bounded_claims_have_bound_source_facts(
        response,
        source_chunks,
        analysis,
        contract_questions,
        request_text=request_text,
    ):
        return None

    result = {
        "generated_response": response,
        "generator_model": "source_chunk",
        "cited_sources": cited_sources,
    }
    return result


def _is_published_yonote_release_chunk(chunk: ScoredChunk) -> bool:
    metadata = chunk.metadata or {}
    return (
        str(metadata.get("source_type") or "").strip().casefold()
        == FACTUAL_SOURCE_TYPE
        and str(metadata.get("source") or "").strip().casefold() == "yonote_api"
        and str(metadata.get("version") or "").strip() == "yonote-api-v1"
        and str(metadata.get("status") or "").strip().casefold() == "published"
    )


def _published_contract_questions(
    explicit_questions: list[Question],
    effective_questions: list[Question],
) -> list[Question]:
    merged: list[Question] = []
    seen: set[tuple[str, str, str, str]] = set()
    for question in [*explicit_questions, *effective_questions]:
        key = (
            _normalize(str(question.text or "")).strip(),
            str(question.topic or "").strip().casefold(),
            str(question.category or "").strip().casefold(),
            _normalize(str(question.forum_normalized or "")).strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(question)
    return merged


def _questions_matching_published_source(
    analysis: QueryAnalysis,
    questions: list[Question],
    chunk: ScoredChunk,
    *,
    request_text: str = "",
) -> list[Question]:
    chunk_topic = str((chunk.metadata or {}).get("topic") or "").strip()
    matching: list[Question] = []
    for question in questions:
        if not _published_source_matches_question_scope(analysis, question, chunk):
            continue
        explicit_topic = str(question.topic or "").strip().casefold()
        if explicit_topic:
            if _published_topic_matches_question(
                chunk_topic,
                explicit_topic,
                question.text,
            ) or _published_grant_application_alias_matches(
                analysis,
                question,
                chunk,
                request_text=request_text,
            ):
                matching.append(question)
            continue
        if _source_chunk_covers_question(question, chunk):
            matching.append(question)
            continue
        if _metadata_matches_specific_question(analysis, question, chunk):
            matching.append(question)
    return matching


def _published_source_matches_question_scope(
    analysis: QueryAnalysis,
    question: Question,
    chunk: ScoredChunk,
) -> bool:
    metadata = chunk.metadata or {}
    expected_forum = str(
        question.forum_normalized or analysis.forum_normalized or ""
    ).strip()
    source_forum = str(metadata.get("forum_normalized") or "").strip()
    if expected_forum and (
        not source_forum or _normalize(source_forum) != _normalize(expected_forum)
    ):
        return False

    expected_category = str(question.category or analysis.category or "").strip()
    source_category = str(metadata.get("category") or "").strip()
    return not expected_category or (
        bool(source_category) and source_category == expected_category
    )


def _published_topic_matches_question(
    chunk_topic: str,
    question_topic: str,
    question_text: str,
) -> bool:
    chunk_topic = chunk_topic.strip().casefold()
    question_topic = question_topic.strip().casefold()
    if not chunk_topic or not question_topic:
        return False
    shift_match = re.fullmatch(r"(\d+)_smena_\d+_\d+_avgusta", chunk_topic)
    if shift_match:
        requested_ordinals = _question_shift_ordinals(question_text, question_topic)
        return bool(
            requested_ordinals
            and int(shift_match.group(1)) in requested_ordinals
            and (
                question_topic == chunk_topic
                or question_topic == "daty_nachala_meropriyatiya"
            )
        )
    if chunk_topic == question_topic:
        return True
    if chunk_topic in {"rezultaty", "rezultaty_rm", "rezultaty_otbora_i_spiski"}:
        return question_topic in {
            "rezultaty",
            "rezultaty_rm",
            "rezultaty_otbora_i_spiski",
        }
    return False


def _published_grant_application_alias_matches(
    analysis: QueryAnalysis,
    question: Question,
    chunk: ScoredChunk,
    *,
    request_text: str,
) -> bool:
    """Bind the first-season grant application intent to its published guide.

    The deterministic analyzer intentionally uses the generic application topic
    and drops grant pseudo-forums. Keep this bridge tied to the explicit user
    wording and the exact published first-season source scope so another grant
    season, category or response profile cannot inherit these instructions.
    """

    metadata = chunk.metadata or {}
    if analysis.response_profile != ResponseProfileName.APPLICATION:
        return False
    if str(question.topic or "").strip().casefold() != "podacha_zayavki_na_proekt":
        return False
    if str(metadata.get("topic") or "").strip().casefold() != "poshagovyy_algoritm":
        return False
    if str(question.category or analysis.category or "").strip() != "гранты":
        return False
    if str(metadata.get("category") or "").strip() != "гранты":
        return False
    if _normalize(str(metadata.get("forum_normalized") or "").strip()) != _normalize(
        "Росмолодёжь.Гранты 1 сезон"
    ):
        return False

    normalized_request = _normalize(request_text)
    return (
        _asks_grant_application(normalized_request)
        and "росмолодеж" in normalized_request
        and _explicit_grant_season_ordinals(normalized_request) == {1}
        and not _grant_first_season_is_negated(normalized_request)
    )


def _grant_first_season_is_negated(normalized_request: str) -> bool:
    first_season = r"(?:перв\w*|1\s*(?:[-–]?\s*(?:й|ый|ой))?)\s+сезон\w*"
    return re.search(
        rf"\bне\s+(?:(?:на|в)\s+)?{first_season}\b",
        normalized_request,
    ) is not None


def _explicit_grant_season_ordinals(normalized_request: str) -> set[int]:
    if "сезон" not in normalized_request:
        return set()

    ordinals: set[int] = set()
    word_stems = (
        ("перв", 1),
        ("втор", 2),
        ("трет", 3),
        ("четверт", 4),
        ("пят", 5),
        ("шест", 6),
        ("седьм", 7),
        ("восьм", 8),
        ("девят", 9),
        ("десят", 10),
    )
    for stem, ordinal in word_stems:
        if re.search(rf"\b{stem}\w*\b", normalized_request):
            ordinals.add(ordinal)

    ordinal_suffix = r"(?:[-–]?\s*(?:й|ый|ой|го|му|м|я|ая|ую|ом))?"
    ordinals.update(
        int(value)
        for value in re.findall(
            r"\b(\d+)\s*[-–]?\s*(?:й|ый|ой|го|му|м|я|ая|ую|ом)\b",
            normalized_request,
        )
    )
    ordinals.update(
        int(value)
        for value in re.findall(
            rf"\b(\d+)\s*{ordinal_suffix}\s+сезон\w*\b",
            normalized_request,
        )
    )
    ordinals.update(
        int(value)
        for value in re.findall(
            rf"\bсезон\w*\s+(\d+)\s*{ordinal_suffix}\b",
            normalized_request,
        )
    )
    return ordinals


def _question_shift_ordinals(question_text: str, question_topic: str) -> set[int]:
    normalized = _normalize(question_text)
    ordinals: set[int] = set()
    if re.search(r"\bперв\w*\s+смен", normalized):
        ordinals.add(1)
    if re.search(r"\bвтор\w*\s+смен", normalized):
        ordinals.add(2)
    ordinals.update(
        int(value)
        for value in re.findall(
            r"\b(\d+)\s*(?:[-–]\s*)?(?:я|й|ая|ой)?\s+смен",
            normalized,
        )
    )
    if not ordinals and (topic_match := re.match(r"^(\d+)_smena_", question_topic)):
        ordinals.add(int(topic_match.group(1)))
    return ordinals


def _bounded_claims_have_bound_source_facts(
    response: str,
    source_chunks: list[ScoredChunk],
    analysis: QueryAnalysis,
    questions: list[Question],
    *,
    request_text: str = "",
) -> bool:
    """Apply fact binding with exact published-topic question attribution.

    Generic coverage matching intentionally tolerates broad category/topic
    aliases for retrieval. This fast path uses a stricter published-topic map,
    so a copied claim may only bind to the exact source selected for that
    question while retaining the existing numeric, date and conditional checks.
    """

    chunks_by_id = {chunk.chunk_id: chunk for chunk in source_chunks}
    previous_end = 0
    found_claim = False
    for marker_group in SOURCE_GROUP_RE.finditer(response):
        claim = response[previous_end : marker_group.start()]
        previous_end = marker_group.end()
        if not SOURCE_RE.sub("", claim).strip():
            return False
        found_claim = True
        cited_ids = list(dict.fromkeys(SOURCE_RE.findall(marker_group.group(0))))
        if len(cited_ids) != 1:
            return False
        cited_chunk = chunks_by_id.get(cited_ids[0])
        if cited_chunk is None:
            return False
        relevant_questions = _questions_matching_published_source(
            analysis,
            questions,
            cited_chunk,
            request_text=request_text,
        )
        if not relevant_questions or not _chunk_supports_claim_facts(
            cited_chunk,
            claim,
            relevant_questions,
        ):
            return False

    trailing_claim = response[previous_end:]
    if re.sub(r"[\s.!?…,:;—–-]+", "", SOURCE_RE.sub("", trailing_claim)):
        return False
    return found_claim


def _published_source_excerpts(
    chunk: ScoredChunk,
    questions: list[Question],
    *,
    request_text: str = "",
) -> list[str]:
    topic = str((chunk.metadata or {}).get("topic") or "").strip().casefold()
    question_text = _normalize(
        " ".join([*(question.text for question in questions), request_text])
    )
    lines = [line.strip() for line in chunk.text.splitlines() if line.strip()]
    sentences = _published_source_sentences(lines[1:] if len(lines) > 1 else lines)

    if topic == "registraciya" and "заяв" in question_text and any(
        marker in question_text
        for marker in ("до какого", "крайн", "срок", "дедлайн")
    ):
        return [
            line
            for line in lines
            if "заяв" in _normalize(line) and _date_signatures(line)
        ][:1]

    if topic == "uchastniki" and any(
        marker in question_text
        for marker in ("кто", "участв", "возраст", "подход")
    ):
        return [
            _compact_published_eligibility_line(line.rstrip(","))
            for line in lines
            if (
                "гражданин российской федерации" in _normalize(line)
                or "юридическое лицо" in _normalize(line)
            )
            and "возраст" in _normalize(line)
        ]

    if topic == "poshagovyy_algoritm" and _asks_grant_application(question_text):
        selected = [
            sentence
            for sentence in sentences
            if any(
                marker in _normalize(sentence)
                for marker in (
                    "необходимо верифицировать",
                    "ознакомиться с объявлением",
                    "оформить идею проекта",
                    "подать заявку на фгаис",
                )
            )
        ]
        return _keep_one_application_link(selected)

    if (
        topic == "registraciya_s_pomoschyu_sozdaniya_kabineta"
        and _asks_account_creation(question_text)
    ):
        return [
            line
            for line in lines[1:]
            if any(
                marker in _normalize(line)
                for marker in (
                    "необходимо заполнить",
                    "придет письмо",
                    "подтверждени",
                    "аккаунт будет создан",
                )
            )
        ]

    if topic == "volonterskaya_pomosch" and _asks_volunteer_application(question_text):
        return [
            line
            for line in lines[1:]
            if any(
                marker in _normalize(line)
                for marker in (
                    "настройка фильтров поиска",
                    "для подачи заявки нужно кликнуть",
                    "разные следующие шаги",
                )
            )
        ]

    if topic == "obedinenie_akkauntov" and (
        "почт" in question_text
        and any(marker in question_text for marker in ("потер", "доступ"))
        and any(marker in question_text for marker in ("данн", "профил", "аккаунт"))
    ):
        return [
            line
            for line in lines[1:]
            if (
                "создать аккаунт" in _normalize(line)
                and "специалисты перенесут" in _normalize(line)
            )
            or (
                "объединить аккаунты невозможно" in _normalize(line)
                and "активная заявка" in _normalize(line)
            )
        ]

    if topic == "statusy_zayavok" and "статус" in question_text:
        return _published_requested_status_lines(lines[1:], question_text)

    if topic in {
        "proverka_proekta_grantovogo_soglasheniya",
        "proverka_otcheta",
    } and any(marker in question_text for marker in ("сколько", "срок", "провер")):
        return [
            sentence
            for sentence in sentences
            if re.search(r"\bдо\s+\d+\s+(?:рабочих\s+)?дн", _normalize(sentence))
        ][:1]

    if topic in {"rezultaty", "rezultaty_rm", "rezultaty_otbora_i_spiski"} and (
        _asks_selection_results(question_text)
    ):
        return [
            sentence
            for sentence in sentences
            if "результат" in _normalize(sentence)
            and re.search(
                r"\bза\s+\d+\s+(?:календарн\w+\s+)?дн",
                _normalize(sentence),
            )
        ][:1]

    if topic in {"programma_foruma", "programma_i_artisty", "programma_artisty"} and (
        _asks_event_program(question_text)
    ):
        return [
            sentence
            for sentence in sentences
            if "программ" in _normalize(sentence)
            and any(
                marker in _normalize(sentence)
                for marker in ("за сутки", "не позднее", "будет доступна")
            )
        ][:1]

    if re.fullmatch(r"\d+_smena_\d+_\d+_avgusta", topic) and any(
        marker in question_text
        for marker in ("дат", "период", "когда", "разъезд", "отъезд")
    ):
        dated_lines = [line for line in lines[1:] if _date_signatures(line)]
        if not dated_lines:
            return []
        boundary_lines = [dated_lines[0]]
        closing_line = next(
            (
                line
                for line in reversed(dated_lines)
                if any(marker in _normalize(line) for marker in ("разъезд", "отъезд"))
            ),
            None,
        )
        if closing_line and closing_line != boundary_lines[0]:
            boundary_lines.append(closing_line)
        return boundary_lines

    return []


def _compact_published_eligibility_line(line: str) -> str:
    if len(line) <= 220:
        return line
    normalized = _normalize(line)
    if "юридическое лицо" not in normalized:
        return line
    subject = re.match(
        r"(.+?зарегистрированн\w*\s+на\s+территории\s+"
        r"(?:Российской Федерации|иностранного государства))",
        line,
        flags=re.IGNORECASE,
    )
    age = re.search(
        r"\(представитель\s+юр\.лица\s+в\s+возрасте\s+от\s+18\s+до\s+55\s+лет\)",
        line,
        flags=re.IGNORECASE,
    )
    project_condition = re.search(
        r"(успешно\s+реализовавш\w*\s+или\s+реализующ\w*\s+"
        r"социально\s+значимые\s+проекты\s+и/или\s+инициативы)",
        line,
        flags=re.IGNORECASE,
    )
    if not subject or not age or not project_condition:
        return line
    return (
        f"{subject.group(1)}, {project_condition.group(1)} — "
        f"{age.group(0).strip('()')}."
    )


def _published_source_sentences(lines: list[str]) -> list[str]:
    return [
        sentence.strip()
        for line in lines
        for sentence in re.split(r"(?<=[.!?])\s+", line)
        if sentence.strip()
    ]


def _published_requested_status_lines(
    lines: list[str],
    question_normalized: str,
) -> list[str]:
    """Return only status definitions whose published label the user named."""

    selected: list[str] = []
    for line in lines:
        match = re.match(r"^\s*\d+[.)]?\s*([^.]+)\.", line)
        if not match:
            continue
        label = _normalize(match.group(1)).strip()
        if label and label in question_normalized:
            selected.append(line)
    return selected


def _keep_one_application_link(sentences: list[str]) -> list[str]:
    linked = [sentence for sentence in sentences if URL_RE.search(sentence)]
    preferred = next(
        (
            sentence
            for sentence in linked
            if "подать заявку" in _normalize(sentence)
        ),
        linked[-1] if linked else None,
    )
    result: list[str] = []
    for sentence in sentences:
        if sentence != preferred:
            sentence = re.sub(
                r"\s+по\s+ссылке\s+https?://\S+",
                "",
                sentence,
                flags=re.IGNORECASE,
            )
            sentence = URL_RE.sub("", sentence)
            sentence = re.sub(r"\s+([.,])", r"\1", sentence).strip()
        if sentence:
            result.append(sentence)
    return result


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
            key=lambda item: (
                _source_type_rank(item[1]),
                _source_freshness_rank(item[1]),
                item[0],
            ),
        )
    ]


def _official_source_chunks(chunks: list[ScoredChunk]) -> list[ScoredChunk]:
    return [
        chunk
        for chunk in chunks
        if str((chunk.metadata or {}).get("source_type") or "").strip()
        == FACTUAL_SOURCE_TYPE
    ]


def _source_type_rank(chunk: ScoredChunk) -> int:
    source_type = str((chunk.metadata or {}).get("source_type") or "").strip()
    if source_type == FACTUAL_SOURCE_TYPE:
        return 0
    return 1


def _source_freshness_rank(chunk: ScoredChunk) -> int:
    source_type = str((chunk.metadata or {}).get("source_type") or "").strip()
    if source_type == FACTUAL_SOURCE_TYPE:
        return 0
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


HOUSING_COMPATIBLE_TRAVEL_TOPICS = frozenset(
    {"oplata_proezda", "oplata_proezda_palatok_i_pitaniya"}
)
HOUSING_TEXT_MARKERS = ("прожив", "размещ", "жиль", "жить")
TOPIC_EQUIVALENCE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset(
        {"oplata_proezda", "oplata_proezda_palatok_i_pitaniya", "kompensaciya"}
    ),
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
            "pitanie_i_prozhivanie",
        }
    ),
    frozenset(
        {
            "spisok_veschey_i_dokumentov",
            "dokumenty_meropriyatiya",
            "pamyatka_uchastnika_foruma",
            "programma_foruma",
            "programma_i_artisty",
            "programma_artisty",
            "vremya_nachala_i_raspisanie",
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
            "registraciya_s_pomoschyu_sozdaniya_kabineta",
            "registraciya",
            "volonterskaya_pomosch",
        }
    ),
    frozenset({"otkaz_ot_uchastiya", "kolichestvo_person_otmena_registracii"}),
    frozenset({"vnesti_izmeneniya_v_zayavku"}),
    frozenset({"podtverzhdenie_uchastiya_i_org_momenty"}),
    frozenset({"cifrovaya_nedelya"}),
    frozenset({"rezultaty_rm", "rezultaty_otbora_i_spiski"}),
    frozenset(
        {
            "usloviya_prozhivaniya",
            "oplata_proezda_prozhivaniya_i_charter",
            "pitanie_i_prozhivanie",
        }
    ),
    frozenset({"trebovaniya_po_dress_kodu"}),
    frozenset({"poseschenie_festivalya_s_detmi", "registraciya_detey"}),
    frozenset({"vozrastnye_ogranicheniya"}),
)

DATE_TOPIC_ALIASES = LEGACY_EVENT_DATE_TOPICS


def _source_topic_match_rank(question: Question, chunk: ScoredChunk) -> int:
    question_topic = str(question.topic or "").strip()
    question_topic_group = _question_topic_group(question)
    chunk_topic = str((chunk.metadata or {}).get("topic") or "").strip()
    if not question_topic_group:
        return 1
    if chunk_topic == question_topic:
        return 0
    if (
        chunk_topic == "forum"
        and question_topic_group
        == _equivalent_topic_group("podacha_zayavki_na_proekt")
        and any(marker in chunk.text.casefold() for marker in ("регистрац", "заявк"))
    ):
        return 1
    if (
        question_topic_group
        == _equivalent_topic_group("daty_nachala_meropriyatiya")
        and chunk_has_event_date_evidence(chunk.text, chunk.metadata)
    ):
        return 1
    if _is_combined_food_housing_source(question_topic_group, chunk_topic, chunk.text):
        return 1
    if _equivalent_topic_group(chunk_topic) == question_topic_group:
        return 1
    if _is_housing_compatible_travel_source(question_topic_group, chunk_topic, chunk.text):
        return 1
    return 2


def _topic_source_preference_rank(
    question: Question,
    chunk: ScoredChunk,
    topic_rank: int,
) -> int:
    source_type = str((chunk.metadata or {}).get("source_type") or "").strip()
    is_fresh_yonote = source_type == "yonote"
    if is_fresh_yonote and topic_rank == 0:
        return 0
    if is_fresh_yonote and _is_preferred_fresh_topic_replacement(question, chunk):
        return 1
    if topic_rank == 0:
        return 2
    if topic_rank <= 1:
        return 3 if is_fresh_yonote else 4
    return 5


def _is_preferred_fresh_topic_replacement(
    question: Question,
    chunk: ScoredChunk,
) -> bool:
    question_topic_group = _question_topic_group(question)
    chunk_topic = str((chunk.metadata or {}).get("topic") or "").strip()
    if (
        chunk_topic == "kompensaciya"
        and question_topic_group == _equivalent_topic_group("oplata_proezda")
    ):
        return True
    if (
        chunk_topic == "forum"
        and question_topic_group
        == _equivalent_topic_group("podacha_zayavki_na_proekt")
        and any(marker in chunk.text.casefold() for marker in ("регистрац", "заявк"))
    ):
        return True
    if chunk_topic != "pitanie_i_prozhivanie":
        return False
    return question_topic_group == _equivalent_topic_group(
        "informaciya_o_ploschadke_pitanie_pite"
    ) or _is_combined_food_housing_source(
        question_topic_group,
        chunk_topic,
        chunk.text,
    )


def _question_topic_group(question: Question) -> str | None:
    question_topic = str(question.topic or "").strip()
    # Keep explicit analyzer topics authoritative. Only normalize the one
    # observed human-readable alias that is equivalent to the canonical KB
    # topic; inferring from arbitrary Cyrillic topics can silently change an
    # application's "documents" aspect into a forum packing-list aspect.
    if _normalize(question_topic).replace(" ", "_") == "оплата_проезда":
        question_topic = "oplata_proezda"
    if question_topic:
        return _equivalent_topic_group(question_topic)
    inferred_topic = _infer_topic_from_question_text(_normalize(question.text))
    return _equivalent_topic_group(inferred_topic) if inferred_topic else None


def _is_housing_compatible_travel_source(
    question_topic_group: str | None,
    chunk_topic: str,
    chunk_text: str,
) -> bool:
    if question_topic_group != _equivalent_topic_group("usloviya_prozhivaniya"):
        return False
    if chunk_topic not in HOUSING_COMPATIBLE_TRAVEL_TOPICS:
        return False
    normalized_text = _normalize(chunk_text)
    return any(marker in normalized_text for marker in HOUSING_TEXT_MARKERS)


def _is_combined_food_housing_source(
    question_topic_group: str | None,
    chunk_topic: str,
    chunk_text: str,
) -> bool:
    if question_topic_group != _equivalent_topic_group("usloviya_prozhivaniya"):
        return False
    if chunk_topic != "pitanie_i_prozhivanie":
        return False
    normalized_text = _normalize(chunk_text)
    return any(marker in normalized_text for marker in HOUSING_TEXT_MARKERS)


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
    if _asks_travel_payment(question_normalized):
        return "oplata_proezda"
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
) -> tuple[float, int, int, int, int, int, float, int, int, float]:
    if (
        _is_specific_technical_question(question) or _is_feedback_question(question)
    ) and _metadata_matches_specific_question(analysis, question, chunk):
        field_score = _source_metadata_field_score(question, chunk)
        source_rank = _source_type_rank(chunk)
        generic_rank = 1 if _is_generic_chunk(chunk) else 0
        unscoped_grant_rank = _unscoped_grant_rank(analysis, chunk)
        grant_source_category_rank = _grant_source_category_rank(analysis, question, chunk)
        topic_rank = _source_topic_match_rank(question, chunk)
        topic_preference_rank = _topic_source_preference_rank(question, chunk, topic_rank)
        constraint_rank = _explicit_constraint_match_rank(question, chunk)
        freshness_rank = _source_freshness_rank(chunk)
        confidence = float(chunk.reranker_score or chunk.score or 0)
        return (
            0,
            unscoped_grant_rank,
            grant_source_category_rank,
            topic_preference_rank,
            constraint_rank,
            freshness_rank,
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
    topic_preference_rank = _topic_source_preference_rank(question, chunk, topic_rank)
    constraint_rank = _explicit_constraint_match_rank(question, chunk)
    freshness_rank = _source_freshness_rank(chunk)
    confidence = float(chunk.reranker_score or chunk.score or 0)
    if str(question.topic or "").strip() and topic_rank <= 1:
        return (
            0,
            unscoped_grant_rank,
            grant_source_category_rank,
            topic_preference_rank,
            constraint_rank,
            freshness_rank,
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
            topic_preference_rank,
            constraint_rank,
            freshness_rank,
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
            topic_preference_rank,
            constraint_rank,
            freshness_rank,
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
            topic_preference_rank,
            constraint_rank,
            freshness_rank,
            -field_score,
            source_rank,
            generic_rank,
            -confidence,
        )
    return (
        3,
        unscoped_grant_rank,
        grant_source_category_rank,
        topic_preference_rank,
        constraint_rank,
        freshness_rank,
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
    if (
        asks_profile_event_dates(_normalize(original_question.text))
        and not _named_question_entities(original_question)
    ):
        unlinked_matches = [
            match
            for match in matches
            if not str((match[2].metadata or {}).get("parent_chunk_id") or "").strip()
        ]
        if unlinked_matches:
            # A generic date question must not inherit the date of a neighbouring
            # named-section child while an event-level date source is available.
            matches = unlinked_matches
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
    if (
        asks_profile_event_dates(_normalize(original_question.text))
        and not _named_question_entities(original_question)
    ):
        unlinked_matches = [
            chunk
            for chunk in matches
            if not str((chunk.metadata or {}).get("parent_chunk_id") or "").strip()
        ]
        if unlinked_matches:
            matches = unlinked_matches
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
    original_question: Question | None = None,
) -> ScoredChunk | None:
    if not chunks:
        return None
    top_chunk = chunks[0]
    metadata = top_chunk.metadata or {}
    if metadata.get("source_type") != FACTUAL_SOURCE_TYPE:
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
    if (
        original_question is not None
        and asks_profile_event_dates(_normalize(original_question.text))
        and not _named_question_entities(original_question)
        and str(metadata.get("parent_chunk_id") or "").strip()
    ):
        # A high-scoring child row may describe one named shift. For a generic
        # event-date question, let topic-aware ranking prefer the overall period;
        # the child remains available when it is the only grounded date source.
        return None
    if (
        original_question is not None
        and analysis is not None
        and analysis.response_profile != ResponseProfileName.GENERIC
        and not _source_chunk_covers_question(original_question, top_chunk)
    ):
        return None
    return top_chunk


def _trusted_single_official_source(
    chunks: list[ScoredChunk],
    threshold: float,
    *,
    analysis: QueryAnalysis,
    questions: list[Question],
) -> ScoredChunk | None:
    if len(chunks) != 1:
        return None
    chunk = chunks[0]
    metadata = chunk.metadata or {}
    if metadata.get("source_type") != FACTUAL_SOURCE_TYPE:
        return None
    if not _chunk_matches_analysis_scope(chunk, analysis):
        return None
    if float(chunk.reranker_score or 0) < threshold:
        return None
    if (
        analysis.response_profile != ResponseProfileName.GENERIC
        and (
            not questions
            or not all(
                _source_chunk_covers_question(question, chunk)
                for question in questions
            )
        )
    ):
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


def _source_matches_explicit_question_constraints(
    question: Question,
    chunk: ScoredChunk,
) -> bool:
    metadata = chunk.metadata or {}
    source_haystack = " ".join(
        [
            chunk.text,
            str(metadata.get("topic") or "").replace("_", " "),
            str(metadata.get("intent_name") or ""),
            " ".join(str(item) for item in metadata.get("linked_section_names") or []),
        ]
    )
    if not _source_matches_explicit_age_constraints(
        question.text,
        source_haystack,
    ):
        return False
    named_entities = (
        _named_question_entities(question)
        if asks_profile_event_dates(_normalize(question.text))
        else set()
    )
    if named_entities:
        source_tokens = _tokens(source_haystack)
        if not all(
            _tokens(entity) and _tokens(entity) <= source_tokens
            for entity in named_entities
        ):
            return False
    question_ranges = _age_ranges(question.text)
    source_ranges = _age_ranges(source_haystack)
    if question_ranges and (
        not source_ranges
        or not all(
            any(
                source_start <= requested_start
                and requested_end <= source_end
                for source_start, source_end in source_ranges
            )
            for requested_start, requested_end in question_ranges
        )
    ):
        return False
    raw_question_keys = _condition_keys(question.text)
    raw_source_keys = _condition_keys(source_haystack)
    question_audiences = {
        key for key in raw_question_keys if key.startswith("audience:")
    }
    source_audiences = {
        key for key in raw_source_keys if key.startswith("audience:")
    }
    if source_audiences and not question_audiences.issubset(source_audiences):
        return False
    question_keys = {
        key
        for key in raw_question_keys
        if not key.startswith(("age:", "audience:"))
    }
    if not question_keys:
        return True
    source_keys = {
        key
        for key in raw_source_keys
        if not key.startswith(("age:", "audience:"))
    }
    for dimension in {_condition_dimension(key) for key in question_keys}:
        required = {
            key for key in question_keys if _condition_dimension(key) == dimension
        }
        available = {
            key for key in source_keys if _condition_dimension(key) == dimension
        }
        if available and not required & available:
            return False
    return True


def _explicit_constraint_match_rank(question: Question, chunk: ScoredChunk) -> int:
    question_keys = _condition_keys(question.text)
    if not question_keys:
        return 0
    metadata = chunk.metadata or {}
    source_haystack = " ".join(
        [
            chunk.text,
            str(metadata.get("topic") or "").replace("_", " "),
            str(metadata.get("intent_name") or ""),
            " ".join(str(item) for item in metadata.get("linked_section_names") or []),
        ]
    )
    source_keys = _condition_keys(source_haystack)
    matched_dimensions = {
        dimension
        for dimension in {_condition_dimension(key) for key in question_keys}
        if {
            key for key in question_keys if _condition_dimension(key) == dimension
        }
        & {
            key for key in source_keys if _condition_dimension(key) == dimension
        }
    }
    return 0 if len(matched_dimensions) == len(
        {_condition_dimension(key) for key in question_keys}
    ) else 1


def _named_question_entities(question: Question) -> set[str]:
    return {
        _normalize(entity)
        for entity in named_section_entities(
            str(question.text or ""),
            str(question.forum_normalized or "") or None,
        )
        if _normalize(entity)
    }


def _source_matches_explicit_age_constraints(
    question_text: str,
    source_haystack: str,
) -> bool:
    requested_ages = _explicit_age_values(question_text)
    requested_aliases = _age_audience_aliases(question_text)
    if not requested_ages and not requested_aliases:
        return True

    source_ranges = _age_ranges(source_haystack)
    source_aliases = _age_audience_aliases(source_haystack)

    for age in requested_ages:
        if source_ranges:
            if not any(start <= age <= end for start, end in source_ranges):
                return False
            continue
        expected_alias = "minor" if age < 18 else "adult"
        if expected_alias not in source_aliases:
            return False

    for alias in requested_aliases:
        if source_ranges:
            if alias == "minor":
                if not any(start <= 17 for start, _end in source_ranges):
                    return False
            elif not any(end >= 18 for _start, end in source_ranges):
                return False
            continue
        if alias not in source_aliases:
            return False
    return True


def _age_ranges(text: str) -> list[tuple[int, int]]:
    return [
        (int(start), int(end))
        for start, end in AGE_RANGE_RE.findall(_normalize(text))
    ]


def _explicit_age_values(text: str) -> set[int]:
    normalized = _normalize(text)
    without_ranges = AGE_RANGE_RE.sub(" ", normalized)
    values: set[int] = set()
    for groups in EXPLICIT_AGE_RE.findall(without_ranges):
        value = next((item for item in groups if item), "")
        if value:
            values.add(int(value))
    return values


def _age_audience_aliases(text: str) -> set[str]:
    normalized = _normalize(text)
    aliases: set[str] = set()
    if MINOR_AGE_ALIAS_RE.search(normalized):
        aliases.add("minor")
    if ADULT_AGE_ALIAS_RE.search(normalized):
        aliases.add("adult")
    return aliases


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
            chunk_has_event_date_evidence(chunk.text, chunk.metadata)
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
    if not _source_matches_explicit_question_constraints(question, chunk):
        return False
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
        return chunk_has_event_date_evidence(chunk.text, chunk.metadata)

    haystack = _source_coverage_haystack(chunk)
    for markers, _question_text in FALLBACK_QUESTION_MARKERS:
        if not any(marker in question_normalized for marker in markers):
            continue
        return any(marker in haystack for marker in markers)
    requested_profile = infer_response_profile(
        QueryAnalysis(
            category=question.category,
            forum_normalized=question.forum_normalized,
            questions=[question],
        ),
        question.text,
    )
    if requested_profile != ResponseProfileName.GENERIC:
        return False
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


def _asks_account_creation(question_normalized: str) -> bool:
    return _asks_registration(question_normalized) or any(
        marker in question_normalized
        for marker in ("создать аккаунт", "создать кабинет", "аккаунт будет создан")
    )


def _asks_volunteer_application(question_normalized: str) -> bool:
    return "волонт" in question_normalized and any(
        marker in question_normalized
        for marker in ("заяв", "мероприят", "ваканс", "помощ")
    )


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


def _asks_travel_payment(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in ("проезд", "дорог", "билет")
    ) and any(
        marker in question_normalized
        for marker in ("оплач", "платит", "за счет", "за счёт", "компенс")
    )


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
    if asks_profile_event_dates(question_normalized):
        return True
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
    return chunk_has_event_date_evidence(chunk.text, chunk.metadata)


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


def _state_current_user_message(state: BotState) -> str:
    return str(state.get("message_masked") or state.get("message") or "")
