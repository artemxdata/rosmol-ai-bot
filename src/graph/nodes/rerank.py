from __future__ import annotations

import asyncio
import gc
import math
import re
from time import perf_counter
from typing import Any

from src.config import get_settings
from src.graph.answer_plan import answer_plan_for_state, current_request_text
from src.graph.provenance import finite_score, rerank_question_provenance
from src.graph.query_normalization import (
    ACCOUNT_DATA_RECOVERY,
    FORUM_DISCOVERY,
    GENERIC_PLATFORM_REGISTRATION,
    GRANT_DIRECTIONS,
    INACTIVE_PLATFORM_APPLICATION_BUTTON,
    PHYSICAL_GRANTS_OVERVIEW,
    PLATFORM_EVENT_NAVIGATION,
    bounded_query_intent,
    expand_query_aliases,
)
from src.graph.question_utils import (
    FALLBACK_QUESTION_MARKERS,
    QueryProvenSourceAspect,
    build_effective_questions,
    query_proven_clause_matches_source_aspects,
    source_aspect_matches_topic,
    unmapped_explicit_request_clauses,
)
from src.graph.response_profiles import (
    asks_event_dates as asks_profile_event_dates,
)
from src.graph.response_profiles import (
    chunk_has_event_date_evidence,
)
from src.graph.state import BotState
from src.kb.fact_cards import compose_fact_cards
from src.kb.fact_extractor import aspects_are_compatible, plan_query_aspects
from src.models import Chunk, Question, ScoredChunk
from src.rag.errors import MLDependencyError
from src.response_contract import get_response_contract

MAX_RERANKED_CHUNKS = 8
MAX_TRACE_RERANK_SCORES = 16
QUESTION_CANDIDATE_LIMIT = 3
QUERY_CANDIDATE_LIMIT = 3
FACTUAL_SOURCE_TYPE = get_response_contract().fact_policy.source_type
TOKEN_PATTERN = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)
STOPWORDS = {
    "для",
    "как",
    "какие",
    "какой",
    "есть",
    "или",
    "при",
    "что",
    "это",
    "нужны",
    "нужно",
    "необходимы",
}
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
    frozenset({"uchastniki_s_ovz"}),
    frozenset({"inostrannye_grazhdane"}),
    frozenset({"sut_foruma_i_napravleniya", "sut_festivalya_i_tematika", "o_meropriyatii"}),
    frozenset({"mesto_i_daty_provedeniya_meropriyatiya", "daty_nachala_meropriyatiya"}),
    frozenset({"dobavlenie_v_chat_i_sluzhba_zaboty", "dobavlenie_v_chat_meropriyatiya"}),
    frozenset({"rosmolodezh_granty", "usloviya_i_sroki_uchastiya_granty"}),
    frozenset({"pismo_vyzov"}),
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
    frozenset({"otkaz_ot_uchastiya"}),
    frozenset({"vnesti_izmeneniya_v_zayavku"}),
    frozenset({"trebovaniya_po_dress_kodu"}),
    frozenset({"poseschenie_festivalya_s_detmi", "registraciya_detey"}),
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
    frozenset({"vozrastnye_ogranicheniya"}),
)


async def rerank(state: BotState) -> dict:
    if state.get("should_escalate"):
        return {}

    started_at = perf_counter()
    tracer = state.get("trace")
    retrieved_chunks = state.get("retrieved_chunks", [])
    chunks = [
        chunk
        for chunk in retrieved_chunks
        if _source_type(chunk) == FACTUAL_SOURCE_TYPE
    ]
    rejected_source_chunks = len(retrieved_chunks) - len(chunks)
    query = (
        state.get("contextual_message")
        or state.get("message_masked")
        or state.get("message")
        or ""
    )
    settings = get_settings()
    if _should_unload_model(settings, "ml_unload_embedder_after_use"):
        await _unload_model_owner(state.get("embedder"))

    if not chunks:
        question_provenance = rerank_question_provenance(
            state.get("retrieval_provenance"),
            [],
        )
        if tracer:
            tracer.add(
                "rerank",
                int((perf_counter() - started_at) * 1000),
                max_confidence=0.0,
                confidence_source="none",
                reranker_invoked=False,
                raw_reranker_scores=[],
                raw_reranker_scores_total=0,
                raw_reranker_scores_recorded=0,
                raw_reranker_scores_truncated_count=0,
                raw_reranker_max=None,
                score_origin="none",
                synthetic_score_applied=False,
                synthetic_high_score_applied=False,
                floor_applied=False,
                rejected_non_yonote_chunks=rejected_source_chunks,
                question_provenance=question_provenance,
            )
        return {
            "reranked_chunks": [],
            "rerank_provenance": question_provenance,
            "max_confidence": 0.0,
            "should_escalate": True,
            "escalation_reason": "no_relevant_chunks",
        }

    execution: dict[str, Any] = {
        "reranker_invoked": False,
        "raw_scores": [],
        "raw_scores_by_chunk": {},
        "raw_scores_total": 0,
    }
    try:
        reranked = await asyncio.to_thread(
            _rerank_for_state,
            state["reranker"],
            state,
            query,
            chunks,
            execution,
        )
    except MLDependencyError as exc:
        if tracer:
            tracer.add_error("rerank", int((perf_counter() - started_at) * 1000), exc)
        return {
            "reranked_chunks": [],
            "max_confidence": 0.0,
            "should_escalate": True,
            "escalation_reason": "ml_dependency_missing",
            "error": str(exc),
        }
    except Exception as exc:
        if tracer:
            tracer.add_error("rerank", int((perf_counter() - started_at) * 1000), exc)
        return {
            "reranked_chunks": [],
            "max_confidence": 0.0,
            "should_escalate": True,
            "escalation_reason": "rerank_failed",
            "error": str(exc),
        }
    finally:
        if _should_unload_model(settings, "ml_unload_reranker_after_use"):
            await _unload_model_owner(state.get("reranker"))

    reranked_output_max = max((chunk.reranker_score for chunk in reranked), default=0.0)
    raw_reranker_scores = list(execution["raw_scores"])
    raw_reranker_max = max(raw_reranker_scores, default=None)
    score_origins = [
        _score_origin(chunk, execution["raw_scores_by_chunk"])
        for chunk in reranked
    ]
    score_origin = (
        score_origins[0]
        if score_origins and len(set(score_origins)) == 1
        else "mixed"
        if score_origins
        else "none"
    )
    synthetic_score_applied = "synthetic" in score_origins
    high_threshold = float(getattr(settings, "reranker_threshold_high", 0.7))
    synthetic_high_score_applied = any(
        origin == "synthetic"
        and math.isclose(
            float(chunk.reranker_score),
            high_threshold,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        for chunk, origin in zip(reranked, score_origins, strict=True)
    )
    max_confidence = reranked_output_max
    confidence_source = "reranker"
    confidence_floor_chunks = _scoped_chunks_for_analysis(
        state.get("analysis"),
        chunks,
        query,
    )
    retrieval_confidence_floor = _retrieval_confidence_floor(
        state,
        confidence_floor_chunks,
    )
    floor_applied = retrieval_confidence_floor > max_confidence
    if floor_applied:
        max_confidence = retrieval_confidence_floor
        confidence_source = "retrieval_exact_filter"
    question_provenance = rerank_question_provenance(
        state.get("retrieval_provenance"),
        reranked,
    )
    if tracer:
        tracer.add(
            "rerank",
            int((perf_counter() - started_at) * 1000),
            max_confidence=max_confidence,
            confidence_source=confidence_source,
            reranker_invoked=execution["reranker_invoked"],
            raw_reranker_scores=raw_reranker_scores,
            raw_reranker_scores_total=execution["raw_scores_total"],
            raw_reranker_scores_recorded=len(raw_reranker_scores),
            raw_reranker_scores_truncated_count=max(
                0,
                execution["raw_scores_total"] - len(raw_reranker_scores),
            ),
            raw_reranker_max=raw_reranker_max,
            score_origin=score_origin,
            synthetic_score_applied=synthetic_score_applied,
            synthetic_high_score_applied=synthetic_high_score_applied,
            floor_applied=floor_applied,
            confidence_components={
                "raw_reranker_max": raw_reranker_max,
                "reranked_output_max": reranked_output_max,
                "retrieval_exact_filter_floor": retrieval_confidence_floor,
                "decision_confidence": max_confidence,
            },
            rejected_non_yonote_chunks=rejected_source_chunks,
            question_provenance=question_provenance,
        )
    if max_confidence <= 0:
        return {
            "reranked_chunks": reranked,
            "rerank_provenance": question_provenance,
            "max_confidence": max_confidence,
            "should_escalate": True,
            "escalation_reason": "no_relevant_chunks",
        }
    result = {
        "reranked_chunks": reranked,
        "rerank_provenance": question_provenance,
        "max_confidence": max_confidence,
    }
    if max_confidence < get_settings().reranker_threshold_low:
        result.update({"should_escalate": True, "escalation_reason": "low_confidence"})
    return result


def _rerank_for_state(
    reranker: Any,
    state: BotState,
    query: str,
    chunks: list[Chunk],
    execution: dict[str, Any] | None = None,
) -> list[ScoredChunk]:
    current_request_text = _current_request_text(state)
    analysis = state.get("analysis")
    query_proven_plan = answer_plan_for_state(
        state,
        analysis,
        current_request_text,
    )
    query_proven_candidates = _query_proven_aspect_candidates(
        (
            query_proven_plan.source_aspects
            if query_proven_plan.questions
            else ()
        ),
        chunks,
    )
    if query_proven_candidates:
        return _boost_source_only_confidence(
            [_source_candidate(chunk) for chunk in query_proven_candidates]
        )
    fact_card_candidates = _complete_fact_card_candidates(
        analysis=analysis,
        request_text=current_request_text,
        chunks=chunks,
        query_proven_plan=query_proven_plan,
    )
    if fact_card_candidates:
        return _boost_source_only_confidence(fact_card_candidates)
    query_proven_resolution_failed = bool(query_proven_plan.questions)
    query_proven_fast_path_blocked = (
        query_proven_plan.incomplete or query_proven_resolution_failed
    )
    partial_request_fast_path_blocked = bool(query_proven_plan.unmapped_clauses)
    query = expand_query_aliases(query)
    scoped_chunks = _scoped_chunks_for_analysis(analysis, chunks, query)
    forum_normalized = str(getattr(analysis, "forum_normalized", None) or "")
    original_priority_candidate = _priority_source_candidate(
        query,
        scoped_chunks,
        forum_normalized=forum_normalized,
    )
    questions = (
        []
        if query_proven_fast_path_blocked
        else list(query_proven_plan.questions)
    )
    if not questions and analysis:
        questions = [
            question
            for question in build_effective_questions(analysis, query)
            if question.text.strip()
        ]
    exact_topic_candidates = (
        []
        if query_proven_fast_path_blocked
        else _exact_topic_fast_path_candidates(
            state,
            analysis,
            questions,
            scoped_chunks,
        )
    )
    if exact_topic_candidates:
        return _boost_source_only_confidence(
            [_source_candidate(chunk) for chunk in exact_topic_candidates]
        )[:MAX_RERANKED_CHUNKS]

    if len(questions) <= 1:
        question = questions[0] if questions else None
        rerank_query = question.text.strip() if question else query
        priority_candidate = (
            _priority_source_candidate(
                query,
                scoped_chunks,
                forum_normalized=forum_normalized,
            )
            or original_priority_candidate
            or _priority_source_candidate(
                rerank_query,
                scoped_chunks,
                forum_normalized=forum_normalized,
            )
        )
        candidates = _candidate_chunks_for_question(
            rerank_query,
            scoped_chunks,
            min(MAX_RERANKED_CHUNKS, max(4, len(scoped_chunks))),
        )
        candidates = _prepend_topic_candidate(
            analysis,
            question,
            scoped_chunks,
            candidates,
            request_text=current_request_text,
        )
        if priority_candidate and priority_candidate.chunk_id not in {
            chunk.chunk_id for chunk in candidates
        }:
            candidates = [priority_candidate, *candidates]
        if (
            not partial_request_fast_path_blocked
            and priority_candidate
            and _is_promotable_priority_candidate(
            query,
            rerank_query,
            priority_candidate,
            forum_normalized=forum_normalized,
            )
        ):
            selected: list[ScoredChunk] = [_priority_scored_candidate(priority_candidate)]
            seen = {priority_candidate.chunk_id}
            for candidate in candidates:
                if candidate.chunk_id in seen:
                    continue
                selected.append(_source_candidate(candidate))
                seen.add(candidate.chunk_id)
                if len(selected) >= 4:
                    break
            return selected
        if (
            not partial_request_fast_path_blocked
            and _source_only_fast_path_allowed(analysis, questions, scoped_chunks)
        ):
            return _source_only_ranked_candidates(candidates, priority_candidate, 4)
        ranked = _call_reranker(
            reranker,
            rerank_query,
            candidates,
            4,
            execution,
        )
        if (
            not partial_request_fast_path_blocked
            and priority_candidate
            and _is_promotable_priority_candidate(
            query,
            rerank_query,
            priority_candidate,
            forum_normalized=forum_normalized,
            )
        ):
            return _prepend_priority_candidate(ranked, priority_candidate)[:4]
        if priority_candidate and priority_candidate.chunk_id not in {
            chunk.chunk_id for chunk in ranked
        }:
            return [_source_candidate(priority_candidate), *ranked][:4]
        return ranked

    selected: list[ScoredChunk] = []
    seen: set[str] = set()
    if (
        not partial_request_fast_path_blocked
        and original_priority_candidate
        and _is_promotable_priority_candidate(
        query,
        query,
        original_priority_candidate,
        forum_normalized=forum_normalized,
        )
    ):
        _append_chunk(selected, seen, _priority_scored_candidate(original_priority_candidate))
    per_question_limit = 2 if len(questions) <= 5 else 1
    group_specs: list[tuple[str, list[Chunk], int]] = []

    for question in questions:
        question_text = question.text.strip()
        candidates = _candidate_chunks_for_question(
            question_text,
            scoped_chunks,
            QUESTION_CANDIDATE_LIMIT,
        )
        candidates = _prepend_topic_candidate(
            analysis,
            question,
            scoped_chunks,
            candidates,
            request_text=current_request_text,
        )
        if candidates:
            _append_chunk(selected, seen, _source_candidate(candidates[0]))
        group_specs.append((question_text, candidates, per_question_limit))

    target_size = min(MAX_RERANKED_CHUNKS, max(4, len(questions) + 2))
    query_candidates = _candidate_chunks_for_question(query, scoped_chunks, QUERY_CANDIDATE_LIMIT)
    if original_priority_candidate and original_priority_candidate.chunk_id not in {
        chunk.chunk_id for chunk in query_candidates
    }:
        query_candidates = [original_priority_candidate, *query_candidates]
    if (
        not partial_request_fast_path_blocked
        and _source_only_fast_path_allowed(analysis, questions, scoped_chunks)
    ):
        for candidate in query_candidates:
            _append_chunk(selected, seen, _source_candidate(candidate))
            if len(selected) >= target_size:
                break
        return _boost_source_only_confidence(selected[:MAX_RERANKED_CHUNKS])
    group_specs.append((query, query_candidates, 4))
    group_results = _rerank_groups(reranker, group_specs, execution)

    for ranked_chunks in group_results[:-1]:
        added_for_question = 0
        for chunk in ranked_chunks:
            before_count = len(seen)
            _append_chunk(selected, seen, chunk)
            if len(seen) > before_count:
                added_for_question += 1
            if added_for_question >= per_question_limit:
                break

    for chunk in group_results[-1]:
        _append_chunk(selected, seen, chunk)
        if len(selected) >= target_size:
            break

    return selected[:MAX_RERANKED_CHUNKS]


def _complete_fact_card_candidates(
    *,
    analysis: Any,
    request_text: str,
    chunks: list[Chunk],
    query_proven_plan: Any,
) -> list[ScoredChunk]:
    """Select a complete published fact set before probabilistic reranking."""

    if analysis is None or not request_text or not chunks:
        return []
    if query_proven_plan.incomplete:
        return []
    forum = str(getattr(analysis, "forum_normalized", None) or "").strip()
    if bounded_query_intent(request_text, forum_normalized=forum) is not None:
        return []
    questions = build_effective_questions(analysis, request_text)
    unmapped = unmapped_explicit_request_clauses(
        analysis,
        request_text,
        aspect_matcher=lambda clause: query_proven_clause_matches_source_aspects(
            clause,
            query_proven_plan.source_aspects,
        ),
        questions=questions,
    )
    if any(not plan_query_aspects(clause) for clause in unmapped):
        return []
    scored = [_source_candidate(chunk) for chunk in chunks]
    draft = compose_fact_cards(
        request_text,
        scored,
        category=getattr(analysis, "category", None),
        forum_normalized=getattr(analysis, "forum_normalized", None),
        response_limit=450,
    )
    if draft is None:
        return []
    by_id = {chunk.chunk_id: chunk for chunk in scored}
    return [
        by_id[chunk_id]
        for chunk_id in draft.cited_sources
        if chunk_id in by_id
    ]


def _query_proven_aspect_candidates(
    aspects: tuple[QueryProvenSourceAspect, ...],
    chunks: list[Chunk],
) -> list[Chunk]:
    if not aspects:
        return []

    selected: list[Chunk] = []
    for aspect in aspects:
        matches = [
            chunk
            for chunk in chunks
            if _query_proven_topic_chunk_matches(
                chunk,
                aspect,
            )
        ]
        if len(matches) != 1:
            return []
        selected.append(matches[0])
    return selected


def _query_proven_topic_chunk_matches(
    chunk: Chunk,
    aspect: QueryProvenSourceAspect,
) -> bool:
    metadata = chunk.metadata or {}
    question = aspect.question
    category = str(question.category or "")
    forum = str(question.forum_normalized or "")
    if _source_type(chunk) != FACTUAL_SOURCE_TYPE:
        return False
    if aspect.structured_source and (
        str(metadata.get("source") or "").strip().casefold() != "yonote_api"
        or str(metadata.get("version") or "").strip() != "yonote-api-v1"
        or str(metadata.get("status") or "").strip().casefold() != "published"
    ):
        return False
    if not source_aspect_matches_topic(aspect, metadata.get("topic")):
        return False
    if metadata.get("category") != category:
        return False
    chunk_forum = str(metadata.get("forum_normalized") or "").strip()
    if category == "платформа_фгаис":
        return not chunk_forum
    if category == "форумы":
        return bool(forum) and chunk_forum == forum
    if source_aspect_matches_topic(aspect, "obschaya_informaciya"):
        return _is_physical_grants_chunk(chunk)
    return category == "гранты"


def _scoped_chunks_for_analysis(
    analysis: Any,
    chunks: list[Chunk],
    query: str = "",
) -> list[Chunk]:
    entity_scoped_chunks = _query_entity_scoped_chunks(query, chunks, analysis)
    if entity_scoped_chunks is not None:
        chunks = entity_scoped_chunks
    if not analysis or not chunks:
        return chunks
    exact_example_chunks = [
        chunk
        for chunk in chunks
        if query
        and _intent_example_matches_question(query, chunk)
        and _protected_chunk_matches_analysis_scope(analysis, chunk)
    ]

    forum = getattr(analysis, "forum_normalized", None)
    category = getattr(analysis, "category", None)
    if forum:
        forum_chunks = [
            chunk
            for chunk in chunks
            if (chunk.metadata or {}).get("forum_normalized") == forum
        ]
        if forum_chunks and category:
            category_chunks = [
                chunk
                for chunk in forum_chunks
                if (chunk.metadata or {}).get("category") == category
            ]
            if category == "\u0444\u043e\u0440\u0443\u043c\u044b":
                category_chunks = _order_official_sources_first(category_chunks)
            if category_chunks:
                if category == "\u0444\u043e\u0440\u0443\u043c\u044b":
                    official_category_chunks = _official_source_chunks(category_chunks)
                    if official_category_chunks:
                        category_chunks = official_category_chunks
                        exact_example_chunks = _official_or_same_chunk_protected_chunks(
                            exact_example_chunks,
                            official_category_chunks,
                        )
                category_ids = {chunk.chunk_id for chunk in category_chunks}
                compatible_chunks = [
                    chunk
                    for chunk in forum_chunks
                    if chunk.chunk_id not in category_ids
                    and _is_same_forum_compatible_category(category, chunk)
                ]
                scoped_chunks = [
                    *category_chunks,
                    *compatible_chunks,
                ]
                return _with_protected_chunks(scoped_chunks, exact_example_chunks)
        if forum_chunks:
            return _with_protected_chunks(forum_chunks, exact_example_chunks)

    if category:
        category_chunks = [
            chunk
            for chunk in chunks
            if (chunk.metadata or {}).get("category") == category
        ]
        compatible_chunks = [
            chunk
            for chunk in chunks
            if _is_compatible_category(category, chunk)
        ]
        if category_chunks:
            if category == "гранты" and not forum:
                category_chunks = _order_unscoped_grant_sources_first(category_chunks)
            category_ids = {chunk.chunk_id for chunk in category_chunks}
            scoped_chunks = [
                *category_chunks,
                *[
                    chunk
                    for chunk in compatible_chunks
                    if chunk.chunk_id not in category_ids
                ],
            ]
            return _with_protected_chunks(scoped_chunks, exact_example_chunks)
        if compatible_chunks:
            return _with_protected_chunks(compatible_chunks, exact_example_chunks)

    return chunks


def _order_official_sources_first(chunks: list[Chunk]) -> list[Chunk]:
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


def _official_source_chunks(chunks: list[Chunk]) -> list[Chunk]:
    return [
        chunk
        for chunk in chunks
        if _source_type(chunk) == FACTUAL_SOURCE_TYPE
    ]


def _order_unscoped_grant_sources_first(chunks: list[Chunk]) -> list[Chunk]:
    return [
        chunk
        for _, chunk in sorted(
            enumerate(chunks),
            key=lambda item: (_unscoped_grant_rank(item[1]), item[0]),
        )
    ]


def _unscoped_grant_rank(chunk: Chunk) -> int:
    return 0 if _is_unscoped_grant_chunk(chunk) else 1


def _is_unscoped_grant_chunk(chunk: Chunk) -> bool:
    metadata = chunk.metadata or {}
    if metadata.get("category") != "гранты":
        return False
    if str(metadata.get("forum_normalized") or "").strip():
        return False
    source_category = _normalize(str(metadata.get("source_category") or ""))
    return not source_category or "грант" in source_category


def _official_or_same_chunk_protected_chunks(
    protected_chunks: list[Chunk],
    official_chunks: list[Chunk],
) -> list[Chunk]:
    official_ids = {chunk.chunk_id for chunk in official_chunks}
    return [
        chunk
        for chunk in protected_chunks
        if chunk.chunk_id in official_ids or _source_type_rank(chunk) == 0
    ]


def _source_type_rank(chunk: Chunk) -> int:
    source_type = _source_type(chunk)
    if source_type == FACTUAL_SOURCE_TYPE:
        return 0
    return 1


def _source_type(chunk: Chunk) -> str:
    return str((chunk.metadata or {}).get("source_type") or "").strip().casefold()


def _source_freshness_rank(chunk: Chunk) -> int:
    source_type = _source_type(chunk)
    if source_type == FACTUAL_SOURCE_TYPE:
        return 0
    return 1


def _source_reliability_score(chunk: Chunk) -> float:
    source_type = _source_type(chunk)
    if source_type == FACTUAL_SOURCE_TYPE:
        return 4.0
    return -3.0


def _is_compatible_category(category: str | None, chunk: Chunk) -> bool:
    if not category:
        return False
    chunk_category = str((chunk.metadata or {}).get("category") or "").strip()
    if not chunk_category or chunk_category == category:
        return False
    return chunk_category in CATEGORY_COMPATIBLE_CATEGORIES.get(category, set())


def _is_same_forum_compatible_category(category: str | None, chunk: Chunk) -> bool:
    if not category:
        return False
    chunk_category = str((chunk.metadata or {}).get("category") or "").strip()
    if not chunk_category or chunk_category == category:
        return False
    return chunk_category in SAME_FORUM_COMPATIBLE_CATEGORIES.get(category, set())


def _with_protected_chunks(chunks: list[Chunk], protected: list[Chunk]) -> list[Chunk]:
    if not protected:
        return chunks
    protected_ids = {chunk.chunk_id for chunk in protected}
    return [*protected, *[chunk for chunk in chunks if chunk.chunk_id not in protected_ids]]


def _protected_chunk_matches_analysis_scope(analysis: Any, chunk: Chunk) -> bool:
    if not analysis:
        return True
    forum = str(getattr(analysis, "forum_normalized", None) or "").strip()
    if not forum:
        return True
    chunk_forum = str((chunk.metadata or {}).get("forum_normalized") or "").strip()
    return not chunk_forum or chunk_forum == forum


def _rerank_groups(
    reranker: Any,
    groups: list[tuple[str, list[Chunk], int]],
    execution: dict[str, Any] | None = None,
) -> list[list[ScoredChunk]]:
    rerank_groups = getattr(reranker, "rerank_groups", None)
    if callable(rerank_groups):
        if execution is not None:
            execution["reranker_invoked"] = True
        results = rerank_groups(groups)
        for result in results:
            _record_raw_reranker_scores(execution, result)
        return results
    return [
        _call_reranker(reranker, query, chunks, top_k, execution)
        for query, chunks, top_k in groups
    ]


def _call_reranker(
    reranker: Any,
    query: str,
    chunks: list[Chunk],
    top_k: int,
    execution: dict[str, Any] | None,
) -> list[ScoredChunk]:
    if execution is not None:
        execution["reranker_invoked"] = True
    result = reranker.rerank(query, chunks, top_k)
    _record_raw_reranker_scores(execution, result)
    return result


def _record_raw_reranker_scores(
    execution: dict[str, Any] | None,
    chunks: list[ScoredChunk],
) -> None:
    if execution is None:
        return
    raw_scores = execution["raw_scores"]
    raw_scores_by_chunk = execution["raw_scores_by_chunk"]
    for chunk in chunks:
        score = finite_score(chunk.reranker_score)
        if score is None:
            continue
        execution["raw_scores_total"] += 1
        raw_scores_by_chunk.setdefault(chunk.chunk_id, []).append(score)
        if len(raw_scores) < MAX_TRACE_RERANK_SCORES:
            raw_scores.append(score)


def _score_origin(
    chunk: ScoredChunk,
    raw_scores_by_chunk: dict[str, list[float]],
) -> str:
    final_score = finite_score(chunk.reranker_score)
    if final_score is None:
        return "synthetic"
    if any(
        math.isclose(final_score, raw_score, rel_tol=1e-12, abs_tol=1e-12)
        for raw_score in raw_scores_by_chunk.get(chunk.chunk_id, ())
    ):
        return "reranker"
    return "synthetic"


def _source_only_fast_path_allowed(
    analysis: Any,
    questions: list[Question],
    scoped_chunks: list[Chunk],
) -> bool:
    if not analysis or not scoped_chunks:
        return False
    forum = str(getattr(analysis, "forum_normalized", None) or "").strip()
    category = str(getattr(analysis, "category", None) or "").strip()
    if not forum or category != "\u0444\u043e\u0440\u0443\u043c\u044b":
        return False
    if not questions or any(
        not _question_has_source_only_ranking_signal(
            question,
            scoped_chunks,
            forum=forum,
        )
        for question in questions
    ):
        return False
    return any(_chunk_matches_exact_forum(chunk, forum) for chunk in scoped_chunks)


def _question_has_source_only_ranking_signal(
    question: Question,
    chunks: list[Chunk],
    *,
    forum: str | None = None,
) -> bool:
    if _question_topic_group(question):
        return True
    ignored_tokens = {
        "какой",
        "какая",
        "какие",
        "куда",
        "когда",
        "можно",
        "нужно",
        "форум",
        "форуме",
        "участник",
        "участники",
        "участников",
    }
    forum_tokens = _tokens(question.forum_normalized or forum or "")
    question_tokens = _tokens(question.text) - ignored_tokens - forum_tokens
    if not question_tokens:
        return False
    for chunk in chunks:
        metadata = chunk.metadata or {}
        haystack = " ".join(
            str(value or "")
            for value in (
                metadata.get("intent_name"),
                metadata.get("topic"),
                metadata.get("source_category"),
                chunk.text[:500],
            )
        )
        if question_tokens & _tokens(haystack):
            return True
    return False


def _exact_topic_fast_path_candidates(
    state: BotState,
    analysis: Any,
    questions: list[Question],
    scoped_chunks: list[Chunk],
) -> list[Chunk]:
    if not _has_trusted_topic_analysis(state) or not analysis or not questions:
        return []
    if any(not str(question.topic or "").strip() for question in questions):
        return []

    selected: list[Chunk] = []
    seen: set[str] = set()
    official_chunks = _official_source_chunks(scoped_chunks)
    for question in questions:
        scoped_topic_chunks = [
            chunk
            for chunk in official_chunks
            if _chunk_matches_trusted_topic_scope(analysis, question, chunk)
        ]
        candidate = _topic_candidate_for_question(
            analysis,
            question,
            scoped_topic_chunks,
            request_text=_current_request_text(state),
        )
        if candidate is None:
            return []
        if candidate.chunk_id in seen:
            continue
        selected.append(candidate)
        seen.add(candidate.chunk_id)
    return selected


def _has_trusted_topic_analysis(state: BotState) -> bool:
    return state.get("analyzer_mode") == "deterministic" or bool(
        state.get("analyzer_fallback")
    )


def _current_request_text(state: BotState) -> str:
    return current_request_text(state)


def _chunk_matches_trusted_topic_scope(
    analysis: Any,
    question: Question,
    chunk: Chunk,
) -> bool:
    metadata = chunk.metadata or {}
    category = str(question.category or getattr(analysis, "category", None) or "").strip()
    chunk_category = str(metadata.get("category") or "").strip()
    if category and chunk_category != category:
        return False

    forum = str(
        question.forum_normalized or getattr(analysis, "forum_normalized", None) or ""
    ).strip()
    chunk_forum = str(metadata.get("forum_normalized") or "").strip()
    if forum:
        return chunk_forum == forum
    if category == "форумы":
        return False
    if category == "гранты" and _is_general_grant_scope(chunk_forum):
        return True
    return not chunk_forum


def _is_general_grant_scope(value: str) -> bool:
    normalized = _normalize(value)
    return "грант" in normalized and "физичес" in normalized


def _chunk_matches_exact_forum(chunk: Chunk, forum: str) -> bool:
    metadata = chunk.metadata or {}
    return metadata.get("forum_normalized") == forum and float(chunk.score or 0.0) >= 0.05


def _source_only_ranked_candidates(
    candidates: list[Chunk],
    priority_candidate: Chunk | None,
    limit: int,
) -> list[ScoredChunk]:
    selected: list[ScoredChunk] = []
    seen: set[str] = set()
    if priority_candidate:
        _append_chunk(selected, seen, _source_candidate(priority_candidate))
    for candidate in candidates:
        _append_chunk(selected, seen, _source_candidate(candidate))
        if len(selected) >= limit:
            break
    return _boost_source_only_confidence(selected)


def _boost_source_only_confidence(chunks: list[ScoredChunk]) -> list[ScoredChunk]:
    threshold = float(get_settings().reranker_threshold_high)
    return [
        chunk.model_copy(
            update={"reranker_score": max(float(chunk.reranker_score or 0.0), threshold)}
        )
        for chunk in chunks
    ]


def _is_promotable_priority_candidate(
    query: str,
    rerank_query: str,
    priority_candidate: Chunk,
    *,
    forum_normalized: str = "",
) -> bool:
    return (
        _intent_example_matches_question(query, priority_candidate)
        or _intent_example_matches_question(rerank_query, priority_candidate)
        or _metadata_matches_promotable_priority_question(
            _normalize(query),
            priority_candidate,
            forum_normalized=forum_normalized,
        )
        or _metadata_matches_promotable_priority_question(
            _normalize(rerank_query),
            priority_candidate,
            forum_normalized=forum_normalized,
        )
    )


def _prepend_topic_candidate(
    analysis: Any,
    question: Question | None,
    chunks: list[Chunk],
    candidates: list[Chunk],
    *,
    request_text: str = "",
) -> list[Chunk]:
    topic_candidate = _topic_candidate_for_question(
        analysis,
        question,
        chunks,
        request_text=request_text,
    )
    if topic_candidate is None:
        return candidates
    return [
        topic_candidate,
        *[chunk for chunk in candidates if chunk.chunk_id != topic_candidate.chunk_id],
    ]


def _topic_candidate_for_question(
    analysis: Any,
    question: Question | None,
    chunks: list[Chunk],
    *,
    request_text: str = "",
) -> Chunk | None:
    if question is None or not chunks:
        return None
    question_topic_group = _question_topic_group(question)
    if not question_topic_group:
        return None

    matches: list[tuple[int, int, int, int, float, int, float, int, Chunk]] = []
    for index, chunk in enumerate(chunks):
        topic_rank = _topic_match_rank(question, chunk)
        if topic_rank > 1 or not _chunk_matches_question_scope(analysis, question, chunk):
            continue
        field_score = _metadata_field_score(_tokens(question.text), chunk)
        matches.append(
            (
                _topic_source_preference_rank(question, chunk, topic_rank),
                _source_freshness_rank(chunk),
                _source_type_rank(chunk),
                _registration_subflow_scope_rank(
                    question,
                    chunk,
                    request_text=request_text,
                ),
                -field_score,
                1 if _is_generic_chunk(chunk) else 0,
                -float(chunk.score or 0.0),
                index,
                chunk,
            )
        )
    if not matches:
        return None
    return min(matches)[-1]


def _registration_subflow_scope_rank(
    question: Question,
    chunk: Chunk,
    *,
    request_text: str = "",
) -> int:
    if _question_topic_group(question) != _equivalent_topic_group(
        "podacha_zayavki_na_proekt"
    ):
        return 0
    normalized_question = _normalize(request_text)
    normalized_source = _normalize(
        " ".join(
            (
                str((chunk.metadata or {}).get("intent_name") or ""),
                str((chunk.metadata or {}).get("topic") or ""),
                chunk.text[:800],
            )
        )
    )
    return sum(
        1
        for marker in ("волонт", "зрител")
        if (marker in normalized_source) != (marker in normalized_question)
    )


def _topic_match_rank(question: Question, chunk: Chunk) -> int:
    question_topic = str(question.topic or "").strip()
    question_topic_group = _question_topic_group(question)
    chunk_topic = str((chunk.metadata or {}).get("topic") or "").strip()
    if not question_topic_group:
        return 1
    if question_topic and chunk_topic == question_topic:
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
    if (
        question_topic_group
        != _equivalent_topic_group("daty_nachala_meropriyatiya")
        and
        str(question.category or "").strip() != "гранты"
        and str((chunk.metadata or {}).get("source_type") or "").strip().casefold()
        == FACTUAL_SOURCE_TYPE
        and aspects_are_compatible(
            question.text,
            chunk.metadata,
            chunk.text,
        )
    ):
        return 1
    return 2


def _topic_source_preference_rank(
    question: Question,
    chunk: Chunk,
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


def _is_preferred_fresh_topic_replacement(question: Question, chunk: Chunk) -> bool:
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


def _infer_topic_from_question_text(normalized_question: str) -> str | None:
    if _asks_decline_participation(normalized_question):
        return "otkaz_ot_uchastiya"
    if _asks_confirmation_org(normalized_question):
        return "podtverzhdenie_uchastiya_i_org_momenty"
    if _asks_digital_week(normalized_question):
        return "cifrovaya_nedelya"
    if _asks_child_visit(normalized_question):
        return "poseschenie_festivalya_s_detmi"
    if _asks_event_program(normalized_question):
        return "programma_foruma"
    if _asks_forum_grants(normalized_question):
        return "rosmolodezh_granty"
    if _asks_event_chat(normalized_question):
        return "dobavlenie_v_chat_meropriyatiya"
    if _asks_foreign_citizens(normalized_question):
        return "inostrannye_grazhdane"
    if _asks_ovz_participation(normalized_question):
        return "uchastniki_s_ovz"
    if _asks_medical_help(normalized_question):
        return "voprosy_po_zdorovyu_medpunkt"
    if _asks_application_change(normalized_question):
        return "vnesti_izmeneniya_v_zayavku"
    if _asks_selection_results(normalized_question):
        return "rezultaty_rm"
    if _asks_event_overview(normalized_question):
        return "sut_foruma_i_napravleniya"
    if _asks_invitation_letter(normalized_question):
        return "pismo_vyzov"
    if _asks_documents_or_packing(normalized_question):
        return "pamyatka_uchastnika_foruma"
    if _asks_transfer(normalized_question):
        return "transfer_do_mesta_provedeniya_meropriyatiya"
    if _asks_housing_conditions(normalized_question):
        return "usloviya_prozhivaniya"
    if _asks_event_dates(normalized_question):
        return "daty_nachala_meropriyatiya"
    if _asks_registration(normalized_question):
        return "podacha_zayavki_na_proekt"
    if _asks_grant_application(normalized_question):
        return "podat_zayavku_na_uchastie"
    return None


def _chunk_matches_question_scope(
    analysis: Any,
    question: Question,
    chunk: Chunk,
) -> bool:
    metadata = chunk.metadata or {}
    forum = str(
        question.forum_normalized
        or getattr(analysis, "forum_normalized", None)
        or ""
    ).strip()
    chunk_forum = str(metadata.get("forum_normalized") or "").strip()
    if forum and chunk_forum and chunk_forum != forum:
        return False

    category = str(
        question.category
        or getattr(analysis, "category", None)
        or ""
    ).strip()
    chunk_category = str(metadata.get("category") or "").strip()
    if not category or not chunk_category or chunk_category == category:
        return True
    if forum and _is_same_forum_compatible_category(category, chunk):
        return True
    return _is_compatible_category(category, chunk)


def _equivalent_topic_group(topic: str) -> str:
    for group in TOPIC_EQUIVALENCE_GROUPS:
        if topic in group:
            return "|".join(sorted(group))
    return topic


def _candidate_chunks_for_question(
    question: str,
    chunks: list[Chunk],
    limit: int,
) -> list[Chunk]:
    question_tokens = _tokens(question)
    if not question_tokens:
        return chunks[:limit]

    has_specific_candidate = any(not _is_generic_chunk(chunk) for chunk in chunks)
    scored = []
    for index, chunk in enumerate(chunks):
        metadata = chunk.metadata or {}
        metadata_haystack = " ".join(
            str(value or "")
            for value in (
                metadata.get("intent_name"),
                metadata.get("topic"),
                metadata.get("source_category"),
                " ".join(str(example or "") for example in metadata.get("intent_examples") or []),
            )
        )
        text_haystack = chunk.text[:500]
        haystack = f"{metadata_haystack} {text_haystack}"
        overlap = len(question_tokens & _tokens(haystack))
        score = (
            _marker_bonus(question, metadata_haystack, weight=20.0)
            + _marker_bonus(question, text_haystack, weight=8.0)
            + _metadata_field_score(question_tokens, chunk)
            + _unscoped_grant_question_score(question, chunk)
            + _generic_penalty(chunk, has_specific_candidate)
            + _source_reliability_score(chunk)
            + overlap
            + float(chunk.score or 0)
        )
        scored.append((score, -index, chunk))

    ranked = [chunk for score, _, chunk in sorted(scored, reverse=True) if score > 0]
    return ranked[:limit] or chunks[:limit]


def _priority_source_candidate(
    question: str,
    chunks: list[Chunk],
    *,
    forum_normalized: str = "",
) -> Chunk | None:
    normalized_question = _normalize(question)
    if _has_bounded_metadata_intent(
        normalized_question,
        forum_normalized=forum_normalized,
    ):
        return _metadata_priority_candidate(
            normalized_question,
            chunks,
            forum_normalized=forum_normalized,
        )
    if (
        _asks_specific_technical_question(normalized_question)
        or _asks_feedback_question(normalized_question)
        or _asks_grant_application(normalized_question)
        or _asks_invitation_letter(normalized_question)
    ):
        metadata_match = _metadata_priority_candidate(
            normalized_question,
            chunks,
            forum_normalized=forum_normalized,
        )
        if metadata_match:
            return metadata_match

    intent_matches = []
    for index, chunk in enumerate(chunks):
        intent_score = _intent_example_match_score(question, chunk)
        if intent_score <= 0:
            continue
        adjusted_score = _adjusted_intent_example_match_score(question, chunk)
        if adjusted_score <= 0:
            continue
        intent_matches.append((adjusted_score, -index, chunk))
    if intent_matches:
        return max(intent_matches)[2]

    return _metadata_priority_candidate(
        normalized_question,
        chunks,
        forum_normalized=forum_normalized,
    )


def _metadata_priority_candidate(
    normalized_question: str,
    chunks: list[Chunk],
    *,
    forum_normalized: str = "",
) -> Chunk | None:
    for chunk in chunks:
        if _metadata_matches_priority_question(
            normalized_question,
            chunk,
            forum_normalized=forum_normalized,
        ):
            return chunk
    return None


def _metadata_matches_priority_question(
    normalized_question: str,
    chunk: Chunk,
    *,
    forum_normalized: str = "",
) -> bool:
    if forum_normalized and not _chunk_matches_explicit_forum(
        chunk,
        forum_normalized,
    ):
        return False
    metadata_haystack = _metadata_haystack(chunk)
    text_haystack = _normalize(chunk.text)

    bounded_match = _bounded_metadata_intent_match(
        normalized_question,
        chunk,
        forum_normalized=forum_normalized,
    )
    if bounded_match is not None:
        return bounded_match

    if _asks_staff_feedback(normalized_question):
        return "ostavit_obratnuyu_svyaz_o_sotrudn" in metadata_haystack
    if _asks_leave_feedback(normalized_question):
        return (
            "ostavit_obratnuyu_svyaz_o" in metadata_haystack
            and "sotrudn" not in metadata_haystack
        )
    if _asks_expert_feedback(normalized_question):
        return "zapros_obratnoy_svyazi_kuratora" in metadata_haystack
    if _asks_password_recovery(normalized_question):
        return "vosstanovit_parol" in metadata_haystack
    if _asks_registration(normalized_question):
        return (
            "kak_zaregistrirovatsya_na_fgais" in metadata_haystack
            or "регистрация_и_заявка" in metadata_haystack
            or "podacha_zayavki" in metadata_haystack
            or "registrac" in metadata_haystack
            or "зарегистрироваться на фгаис" in metadata_haystack
            or "auth/register" in text_haystack
        )
    if _asks_contact_operator(normalized_question):
        return "контакты_и_оператор" in metadata_haystack
    if _asks_language_settings(normalized_question):
        return "yazyki" in metadata_haystack or "язык" in metadata_haystack
    if _asks_unlink_gosuslugi(normalized_question):
        return "otvyazat_gu" in metadata_haystack or "госуслуг" in metadata_haystack
    if _asks_verify_other_account(normalized_question):
        return "verificirovat_drugoy_akkaunt" in metadata_haystack
    if _asks_same_email_for_person_and_org(normalized_question):
        return "pochta_fizlica_i_yurlica_sovpadayut" in metadata_haystack
    if _asks_dual_citizenship(normalized_question):
        return "dvoynoe_grazhdanstvo" in metadata_haystack
    if _asks_responsible_person_change(normalized_question):
        return "kak_smenit_otvetstvennoe_lico" in metadata_haystack
    if _asks_specific_technical_question(normalized_question):
        return False
    if _asks_access_or_technical_error(normalized_question):
        return (
            "доступ_и_техническая_ошибка" in metadata_haystack
            or "tehnicheskaya_oshibka" in metadata_haystack
            or "техническая ошибка" in metadata_haystack
        )
    if _asks_transfer(normalized_question):
        return "transfer" in metadata_haystack or "трансфер" in metadata_haystack
    if _asks_arrival_departure(normalized_question):
        return (
            "vremya_zaezda_i_vyezda" in metadata_haystack
            or ("заезд" in metadata_haystack and "выезд" in metadata_haystack)
        )
    if _asks_invitation_letter(normalized_question):
        return "pismo_vyzov" in metadata_haystack or "письмо-вызов" in metadata_haystack
    if _asks_documents_or_packing(normalized_question):
        return (
            "pamyatka_uchastnika_foruma" in metadata_haystack
            or "документ" in metadata_haystack
            or "паспорт" in metadata_haystack
            or "справк" in metadata_haystack
            or "вещ" in metadata_haystack
        )
    if _asks_event_dates(normalized_question):
        return chunk_has_event_date_evidence(chunk.text, chunk.metadata)
    if _asks_profile_id(normalized_question):
        return (
            "gde_nayti_id_profilya" in metadata_haystack
            or "id профиля" in metadata_haystack
        )
    if _asks_grant_return(normalized_question):
        return (
            "vernut_denezhnye_sredstva" in metadata_haystack
            or "вернуть денежные средства" in metadata_haystack
            or "вернуть грантовые средства" in text_haystack
        )
    if _asks_grant_project_change(normalized_question):
        return (
            "vnesti_izmeneniya_v_proekt" in metadata_haystack
            or "внести изменения в проект" in metadata_haystack
            or "изменить смету" in text_haystack
        )
    if _asks_grant_application(normalized_question):
        return (
            "podat_zayavku_na_uchastie" in metadata_haystack
            and "грант" in metadata_haystack
        )
    if _asks_what_is_rosmol(normalized_question):
        return (
            "chto_takoe_rosmolodezh" in metadata_haystack
            or "что такое росмолодежь" in metadata_haystack
        )
    if _asks_cooperation(normalized_question):
        return (
            "predlozhenie_sotrudnichestva" in metadata_haystack
            or "сотруднич" in metadata_haystack
            or "партнер" in metadata_haystack
            or "партнёр" in metadata_haystack
        )
    if _asks_bot_abilities(normalized_question):
        return (
            "vozmozhnosti_bota_abilities" in metadata_haystack
            or "возможности бота" in metadata_haystack
            or "abilities" in metadata_haystack
        )
    if _asks_farewell(normalized_question):
        return "proschanie" in metadata_haystack or "прощание" in metadata_haystack
    if _asks_student_recommendation(normalized_question):
        return (
            "rekomendacii_studenty" in metadata_haystack
            or "рекомендации.студенты" in metadata_haystack
        )
    if _asks_recommendation(normalized_question):
        return (
            "rekomendacii_obschie" in metadata_haystack
            or "рекомендации общие" in metadata_haystack
            or "рекомендации.общие" in metadata_haystack
        )
    if _asks_application_status_location(normalized_question):
        return (
            "gde_smotret_status_zayavok" in metadata_haystack
            or "где смотреть статус заявок" in metadata_haystack
        )
    if _asks_language_settings(normalized_question):
        return "yazyki" in metadata_haystack or "язык" in metadata_haystack
    if _asks_unlink_gosuslugi(normalized_question):
        return "otvyazat_gu" in metadata_haystack or "госуслуг" in metadata_haystack
    if _asks_verify_other_account(normalized_question):
        return "verificirovat_drugoy_akkaunt" in metadata_haystack
    if _asks_same_email_for_person_and_org(normalized_question):
        return "pochta_fizlica_i_yurlica_sovpadayut" in metadata_haystack
    if _asks_dual_citizenship(normalized_question):
        return "dvoynoe_grazhdanstvo" in metadata_haystack
    if _asks_responsible_person_change(normalized_question):
        return "kak_smenit_otvetstvennoe_lico" in metadata_haystack
    if _asks_contact_operator(normalized_question):
        return "контакты_и_оператор" in metadata_haystack
    if _asks_access_or_technical_error(normalized_question):
        return (
            "доступ_и_техническая_ошибка" in metadata_haystack
            or "tehnicheskaya_oshibka" in metadata_haystack
            or "техническая ошибка" in metadata_haystack
        )
    if _asks_transfer(normalized_question):
        return "transfer" in metadata_haystack or "трансфер" in metadata_haystack
    if _asks_arrival_departure(normalized_question):
        return (
            "vremya_zaezda_i_vyezda" in metadata_haystack
            or ("заезд" in metadata_haystack and "выезд" in metadata_haystack)
        )
    if _asks_event_dates(normalized_question):
        return chunk_has_event_date_evidence(chunk.text, chunk.metadata)
    return False


def _metadata_matches_promotable_priority_question(
    normalized_question: str,
    chunk: Chunk,
    *,
    forum_normalized: str = "",
) -> bool:
    if forum_normalized and not _chunk_matches_explicit_forum(
        chunk,
        forum_normalized,
    ):
        return False
    metadata_haystack = _metadata_haystack(chunk)
    text_haystack = _normalize(chunk.text)
    bounded_match = _bounded_metadata_intent_match(
        normalized_question,
        chunk,
        forum_normalized=forum_normalized,
    )
    if bounded_match is not None:
        return bounded_match
    if _asks_staff_feedback(normalized_question):
        return "ostavit_obratnuyu_svyaz_o_sotrudn" in metadata_haystack
    if _asks_leave_feedback(normalized_question):
        return (
            "ostavit_obratnuyu_svyaz_o" in metadata_haystack
            and "sotrudn" not in metadata_haystack
        )
    if _asks_expert_feedback(normalized_question):
        return "zapros_obratnoy_svyazi_kuratora" in metadata_haystack
    if _asks_password_recovery(normalized_question):
        return "vosstanovit_parol" in metadata_haystack
    if _asks_grant_project_change(normalized_question):
        return (
            "vnesti_izmeneniya_v_proekt" in metadata_haystack
            or "внести изменения в проект" in metadata_haystack
            or "изменить смету" in text_haystack
        )
    if _asks_grant_application(normalized_question):
        return (
            "podat_zayavku_na_uchastie" in metadata_haystack
            and "грант" in metadata_haystack
        )
    if _asks_what_is_rosmol(normalized_question):
        return (
            "chto_takoe_rosmolodezh" in metadata_haystack
            or "что такое росмолодежь" in metadata_haystack
        )
    if _asks_cooperation(normalized_question):
        return (
            "predlozhenie_sotrudnichestva" in metadata_haystack
            or "сотруднич" in metadata_haystack
            or "партнер" in metadata_haystack
            or "партнёр" in metadata_haystack
        )
    if _asks_bot_abilities(normalized_question):
        return (
            "vozmozhnosti_bota_abilities" in metadata_haystack
            or "возможности бота" in metadata_haystack
            or "abilities" in metadata_haystack
        )
    if _asks_farewell(normalized_question):
        return "proschanie" in metadata_haystack or "прощание" in metadata_haystack
    if _asks_student_recommendation(normalized_question):
        return (
            "rekomendacii_studenty" in metadata_haystack
            or "рекомендации.студенты" in metadata_haystack
        )
    if _asks_recommendation(normalized_question):
        return (
            "rekomendacii_obschie" in metadata_haystack
            or "рекомендации общие" in metadata_haystack
            or "рекомендации.общие" in metadata_haystack
        )
    if _asks_application_status_location(normalized_question):
        return (
            "gde_smotret_status_zayavok" in metadata_haystack
            or "где смотреть статус заявок" in metadata_haystack
        )
    if _asks_language_settings(normalized_question):
        return "yazyki" in metadata_haystack or "язык" in metadata_haystack
    if _asks_unlink_gosuslugi(normalized_question):
        return "otvyazat_gu" in metadata_haystack or "госуслуг" in metadata_haystack
    if _asks_verify_other_account(normalized_question):
        return "verificirovat_drugoy_akkaunt" in metadata_haystack
    if _asks_same_email_for_person_and_org(normalized_question):
        return "pochta_fizlica_i_yurlica_sovpadayut" in metadata_haystack
    if _asks_dual_citizenship(normalized_question):
        return "dvoynoe_grazhdanstvo" in metadata_haystack
    if _asks_responsible_person_change(normalized_question):
        return "kak_smenit_otvetstvennoe_lico" in metadata_haystack
    if _asks_invitation_letter(normalized_question):
        return "pismo_vyzov" in metadata_haystack or "письмо-вызов" in metadata_haystack
    return False


def _chunk_matches_explicit_forum(chunk: Chunk, forum_normalized: str) -> bool:
    chunk_forum = str((chunk.metadata or {}).get("forum_normalized") or "")
    return bool(chunk_forum) and _normalize(chunk_forum) == _normalize(forum_normalized)


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in (
            _normalize(raw_token) for raw_token in TOKEN_PATTERN.findall(text)
        )
        if token not in STOPWORDS
    }


def _marker_bonus(question: str, haystack: str, *, weight: float) -> float:
    question_normalized = _normalize(question)
    haystack_normalized = _normalize(haystack)
    bonus = 0.0
    for markers, _ in FALLBACK_QUESTION_MARKERS:
        if not any(marker in question_normalized for marker in markers):
            continue
        if any(marker in haystack_normalized for marker in markers):
            bonus += weight
    return bonus


def _metadata_field_score(question_tokens: set[str], chunk: Chunk) -> float:
    metadata = chunk.metadata or {}
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


def _unscoped_grant_question_score(question: str, chunk: Chunk) -> float:
    if not _is_unscoped_grant_query(question):
        return 0.0
    metadata = chunk.metadata or {}
    if metadata.get("category") != "гранты":
        return 0.0
    return 20.0 if _is_unscoped_grant_chunk(chunk) else -20.0


def _is_unscoped_grant_query(question: str) -> bool:
    question_normalized = _normalize(question)
    return "грант" in question_normalized and "форум" not in question_normalized


def _generic_penalty(chunk: Chunk, has_specific_candidate: bool) -> float:
    if not has_specific_candidate or not _is_generic_chunk(chunk):
        return 0.0
    return -1.5


def _is_generic_chunk(chunk: Chunk) -> bool:
    metadata = chunk.metadata or {}
    value = metadata.get("is_generic")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y"}
    source_category = str(metadata.get("source_category") or "").strip().casefold()
    return source_category.startswith("fallback")


def _source_candidate(chunk: Chunk) -> ScoredChunk:
    return ScoredChunk(
        **chunk.model_dump(exclude={"score"}),
        score=chunk.score,
        reranker_score=0.0,
    )


def _intent_example_matches_question(question: str, chunk: Chunk) -> bool:
    return _intent_example_match_score(question, chunk) > 0


def _adjusted_intent_example_match_score(question: str, chunk: Chunk) -> int:
    score = _intent_example_match_score(question, chunk)
    if score <= 0:
        return 0
    return max(0, score - _generic_topic_penalty(chunk))


def _intent_example_match_score(question: str, chunk: Chunk) -> int:
    metadata = chunk.metadata or {}
    if (
        _is_unscoped_grant_query(question)
        and metadata.get("category") == "гранты"
        and not _is_unscoped_grant_chunk(chunk)
    ):
        return 0
    examples = metadata.get("intent_examples") or []
    if not examples:
        return 0

    question_normalized = _normalize(question).strip()
    question_tokens = _tokens(question)
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


def _generic_topic_penalty(chunk: Chunk) -> int:
    topic = str((chunk.metadata or {}).get("topic") or "").strip().casefold()
    return 2 if topic in {"прочее", "other", "general", "misc"} else 0


def _prepend_priority_candidate(
    ranked: list[ScoredChunk],
    priority_candidate: Chunk,
) -> list[ScoredChunk]:
    priority = _priority_scored_candidate(priority_candidate)
    return [priority, *[chunk for chunk in ranked if chunk.chunk_id != priority.chunk_id]]


def _priority_scored_candidate(priority_candidate: Chunk) -> ScoredChunk:
    settings = get_settings()
    return ScoredChunk(
        **priority_candidate.model_dump(exclude={"score"}),
        score=priority_candidate.score,
        reranker_score=float(
            getattr(
                settings,
                "reranker_threshold_high",
                getattr(settings, "reranker_threshold_low", 0.4),
            )
        ),
    )


def _append_chunk(
    selected: list[ScoredChunk],
    seen: set[str],
    chunk: ScoredChunk,
) -> bool:
    if chunk.chunk_id in seen:
        for index, existing in enumerate(selected):
            if existing.chunk_id != chunk.chunk_id:
                continue
            if chunk.reranker_score > existing.reranker_score:
                selected[index] = chunk
            return True
        return True
    selected.append(chunk)
    seen.add(chunk.chunk_id)
    return True


def _metadata_haystack(chunk: Chunk) -> str:
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
                " ".join(str(example or "") for example in metadata.get("intent_examples") or []),
            )
        )
    )


def _asks_registration(normalized_question: str) -> bool:
    return any(marker in normalized_question for marker in ("регистрац", "зарегистр"))


def _query_entity_scoped_chunks(
    query: str,
    chunks: list[Chunk],
    analysis: Any = None,
) -> list[Chunk] | None:
    """Apply query-proven entity scope before analyzer-derived soft category scope."""
    forum = str(getattr(analysis, "forum_normalized", None) or "").strip()
    normalized = _normalize(query)
    intent = bounded_query_intent(normalized, forum_normalized=forum)
    if not forum and intent in {
        GENERIC_PLATFORM_REGISTRATION,
        PLATFORM_EVENT_NAVIGATION,
        ACCOUNT_DATA_RECOVERY,
        INACTIVE_PLATFORM_APPLICATION_BUTTON,
    }:
        return [
            chunk
            for chunk in chunks
            if _bounded_metadata_intent_match(normalized, chunk) is True
        ]
    if intent == GRANT_DIRECTIONS:
        if len(plan_query_aspects(query)) > 1:
            return None
        return [
            chunk
            for chunk in chunks
            if _bounded_metadata_intent_match(normalized, chunk) is True
        ]
    if intent == PHYSICAL_GRANTS_OVERVIEW:
        return [
            chunk
            for chunk in chunks
            if _bounded_metadata_intent_match(normalized, chunk) is True
        ]
    if intent == FORUM_DISCOVERY:
        return [
            chunk
            for chunk in chunks
            if _bounded_metadata_intent_match(normalized, chunk) is True
        ]
    return None


def _has_bounded_metadata_intent(
    normalized_question: str,
    *,
    forum_normalized: str = "",
) -> bool:
    return (
        bounded_query_intent(
            normalized_question,
            forum_normalized=forum_normalized,
        )
        is not None
    )


def _bounded_metadata_intent_match(
    normalized_question: str,
    chunk: Chunk,
    *,
    forum_normalized: str = "",
) -> bool | None:
    metadata = chunk.metadata or {}
    metadata_haystack = _metadata_haystack(chunk)
    text_haystack = _normalize(chunk.text)
    intent = bounded_query_intent(
        normalized_question,
        forum_normalized=forum_normalized,
    )
    if intent == GENERIC_PLATFORM_REGISTRATION:
        return (
            metadata.get("category") == "платформа_фгаис"
            and not str(metadata.get("forum_normalized") or "").strip()
            and (
                "registraciya_prohodit_po_ssylke" in metadata_haystack
                or "auth/register" in text_haystack
            )
        )
    if intent == PLATFORM_EVENT_NAVIGATION:
        return (
            metadata.get("category") == "платформа_фгаис"
            and not str(metadata.get("forum_normalized") or "").strip()
            and "poisk_i_navigaciya_po_meropriyatiyam" in metadata_haystack
        )
    if intent == ACCOUNT_DATA_RECOVERY:
        return (
            metadata.get("category") == "платформа_фгаис"
            and not str(metadata.get("forum_normalized") or "").strip()
            and "obedinenie_akkauntov" in metadata_haystack
        )
    if intent == INACTIVE_PLATFORM_APPLICATION_BUTTON:
        return (
            metadata.get("category") == "платформа_фгаис"
            and not str(metadata.get("forum_normalized") or "").strip()
            and metadata.get("topic") == "registraciya_na_municipalnoe_meropriyatie"
        )
    if intent == GRANT_DIRECTIONS:
        return _is_generic_rosmol_grants_chunk(chunk) and "nominac" in metadata_haystack
    if intent == PHYSICAL_GRANTS_OVERVIEW:
        return _is_physical_grants_chunk(chunk) and "obschaya_informaciya" in metadata_haystack
    if intent == FORUM_DISCOVERY:
        return (
            not str(metadata.get("forum_normalized") or "").strip()
            and metadata.get("category") in {"общее", "платформа_фгаис"}
            and (
                "events_myrosmol_ru" in metadata_haystack
                or "events.myrosmol.ru" in text_haystack
            )
        )
    return None


def _is_generic_rosmol_grants_chunk(chunk: Chunk) -> bool:
    metadata = chunk.metadata or {}
    if metadata.get("category") != "гранты":
        return False
    forum = _normalize(str(metadata.get("forum_normalized") or ""))
    if not forum:
        return True
    if _is_physical_grants_chunk(chunk):
        return True
    return forum in {
        "гранты",
        "росмолодежь гранты",
        "росмолодежь.гранты",
    }


def _is_physical_grants_chunk(chunk: Chunk) -> bool:
    metadata = chunk.metadata or {}
    return metadata.get("category") == "гранты" and "физичес" in _normalize(
        str(metadata.get("forum_normalized") or metadata.get("source_category") or "")
    )


def _asks_profile_id(normalized_question: str) -> bool:
    return any(marker in normalized_question for marker in ("id проф", "айди", "ид проф"))


def _asks_decline_participation(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
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
            "подтвердил участие",
            "подтвердила участие",
        )
    )


def _asks_medical_help(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in ("медпункт", "медицин", "здоров")
    )


def _asks_ovz_participation(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in ("овз", "ограниченными возможн", "инвалид")
    )


def _asks_foreign_citizens(normalized_question: str) -> bool:
    return any(marker in normalized_question for marker in ("иностран", "иностранц"))


def _asks_forum_grants(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in ("грантовый конкурс", "гранты", "грантов")
    )


def _asks_event_chat(normalized_question: str) -> bool:
    return "чат" in normalized_question or "куратор" in normalized_question


def _asks_selection_results(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in ("результат", "отбор", "одобрен", "статус", "рассмотр", "списки")
    )


def _asks_application_change(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in (
            "изменить заявку",
            "изменить заявк",
            "внести изменения в заявк",
            "поменять заявк",
        )
    )


def _asks_confirmation_org(normalized_question: str) -> bool:
    return any(marker in normalized_question for marker in ("подтверждени", "подтверд"))


def _asks_digital_week(normalized_question: str) -> bool:
    return "цифровая неделя" in normalized_question


def _asks_child_visit(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in ("ребен", "ребён", "дети", "детьми", "ребенком", "ребёнком")
    )


def _asks_event_program(normalized_question: str) -> bool:
    return "программ" in normalized_question or "расписан" in normalized_question


def _asks_grant_return(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in (
            "вернуть грантов",
            "вернуть средства",
            "вернуть деньги",
            "вернуть денеж",
        )
    )


def _asks_grant_project_change(normalized_question: str) -> bool:
    if "грант" not in normalized_question and "проект" not in normalized_question:
        return False
    return any(
        marker in normalized_question
        for marker in (
            "внести измен",
            "изменить проект",
            "изменить смет",
            "поменять смет",
            "скорректировать проект",
            "редактировать проект",
        )
    )


def _asks_grant_application(normalized_question: str) -> bool:
    return "грант" in normalized_question and any(
        marker in normalized_question
        for marker in (
            "подать заяв",
            "подача заяв",
            "заявку на участие",
            "участие в грант",
        )
    )


def _asks_password_recovery(normalized_question: str) -> bool:
    return "парол" in normalized_question and any(
        marker in normalized_question for marker in ("восстанов", "забыл", "сброс")
    )


def _asks_expert_feedback(normalized_question: str) -> bool:
    if "обратн" not in normalized_question:
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
        marker in normalized_question for marker in ("остав", "поделит", "впечатл", "отзыв")
    ) and not any(marker in normalized_question for marker in explicit_expert_markers):
        return False
    return any(
        marker in normalized_question
        for marker in (*explicit_expert_markers, "грант")
    )


def _asks_staff_feedback(normalized_question: str) -> bool:
    return "обратн" in normalized_question and any(
        marker in normalized_question for marker in ("сотрудн", "специалист", "оператор")
    )


def _asks_leave_feedback(normalized_question: str) -> bool:
    if _asks_staff_feedback(normalized_question):
        return False
    if _asks_expert_feedback(normalized_question):
        return False
    return "обратн" in normalized_question and any(
        marker in normalized_question
        for marker in ("остав", "поделит", "впечатл", "отзыв")
    )


def _asks_feedback_question(normalized_question: str) -> bool:
    return (
        _asks_staff_feedback(normalized_question)
        or _asks_expert_feedback(normalized_question)
        or _asks_leave_feedback(normalized_question)
    )


def _asks_what_is_rosmol(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in (
            "что такое росмолод",
            "кто такие росмолод",
            "чем занимается росмолод",
        )
    )


def _asks_cooperation(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in ("сотруднич", "партнерств", "партнёрств", "партнер", "партнёр")
    )


def _asks_bot_abilities(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in (
            "возможности бота",
            "abilities",
            "что умеешь",
            "что ты умеешь",
            "чем можешь помочь",
        )
    )


def _asks_farewell(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in ("до свид", "пока", "прощ", "всего добр", "хорошего дня")
    )


def _asks_recommendation(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in ("рекоменд", "посовет", "подбери", "подойдет", "подойдёт")
    )


def _asks_student_recommendation(normalized_question: str) -> bool:
    return _asks_recommendation(normalized_question) and any(
        marker in normalized_question for marker in ("студент", "студенч")
    )


def _asks_application_status_location(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in (
            "где смотреть статус",
            "где посмотреть статус",
            "где отслеживать статус",
            "статус заявки",
            "статус заявок",
        )
    )


def _asks_contact_operator(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in (
            "оператор",
            "контакт",
            "связаться",
            "поддержк",
            "служба заботы",
        )
    )


def _asks_access_or_technical_error(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
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


def _asks_transfer(normalized_question: str) -> bool:
    return any(marker in normalized_question for marker in ("трансфер", "шаттл", "автобус"))


def _asks_arrival_departure(normalized_question: str) -> bool:
    return (
        "заезд" in normalized_question
        and "выезд" in normalized_question
        or any(
            marker in normalized_question
            for marker in (
                "время заезда",
                "время выезда",
                "когда заезд",
                "когда выезд",
            )
        )
    )


def _asks_invitation_letter(normalized_question: str) -> bool:
    return "письмо" in normalized_question and any(
        marker in normalized_question
        for marker in (
            "вызов",
            "на регион",
            "в регион",
            "для региона",
            "подтверждение участия",
        )
    )


def _asks_event_dates(normalized_question: str) -> bool:
    if _asks_arrival_departure(normalized_question):
        return False
    if "когда добав" in normalized_question and "чат" in normalized_question:
        return False
    if asks_profile_event_dates(normalized_question):
        return True
    return any(
        marker in normalized_question
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
            "когда проходит",
            "когда проводится",
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


def _asks_event_overview(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in (
            "в чем суть",
            "в чём суть",
            "суть форум",
            "о форуме",
            "о мероприятии",
            "тематик",
            "направлен",
        )
    )


def _asks_housing_conditions(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
        for marker in ("проживан", "жиль", "гостиниц", "отел", "размещ", "палат")
    )


def _asks_documents_or_packing(normalized_question: str) -> bool:
    return any(
        marker in normalized_question
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


def _asks_language_settings(normalized_question: str) -> bool:
    return any(marker in normalized_question for marker in ("язык", "языки"))


def _asks_unlink_gosuslugi(normalized_question: str) -> bool:
    return any(marker in normalized_question for marker in ("отвяз", "госуслуг", "есиа"))


def _asks_verify_other_account(normalized_question: str) -> bool:
    return "верифиц" in normalized_question and any(
        marker in normalized_question for marker in ("другой аккаунт", "другую учет", "другую учёт")
    )


def _asks_same_email_for_person_and_org(normalized_question: str) -> bool:
    return any(marker in normalized_question for marker in ("почта", "email", "e-mail")) and any(
        marker in normalized_question for marker in ("физ", "юр", "организац")
    )


def _asks_dual_citizenship(normalized_question: str) -> bool:
    return "двойн" in normalized_question and "граждан" in normalized_question


def _asks_responsible_person_change(normalized_question: str) -> bool:
    return "ответствен" in normalized_question and any(
        marker in normalized_question for marker in ("смен", "измен", "помен")
    )


def _asks_specific_technical_question(normalized_question: str) -> bool:
    return any(
        (
            _asks_language_settings(normalized_question),
            _asks_unlink_gosuslugi(normalized_question),
            _asks_verify_other_account(normalized_question),
            _asks_same_email_for_person_and_org(normalized_question),
            _asks_dual_citizenship(normalized_question),
            _asks_responsible_person_change(normalized_question),
            _asks_password_recovery(normalized_question),
        )
    )


def _normalize(text: str) -> str:
    return expand_query_aliases(text).casefold().replace("ё", "е")


async def _unload_model_owner(owner: Any) -> None:
    unload = getattr(owner, "unload", None)
    if not callable(unload):
        return
    await asyncio.to_thread(unload)
    gc.collect()


def _should_unload_model(settings: Any, field_name: str) -> bool:
    explicit_value = getattr(settings, field_name, None)
    if explicit_value is not None:
        return bool(explicit_value)
    return bool(getattr(settings, "ml_unload_after_use", False))


def _retrieval_confidence_floor(state: BotState, chunks: list[Chunk]) -> float:
    analysis = state.get("analysis")
    forum = getattr(analysis, "forum_normalized", None) if analysis else None
    category = getattr(analysis, "category", None) if analysis else None
    if not chunks or (not forum and not category):
        return 0.0

    top_chunk = max(chunks, key=lambda chunk: float(chunk.score or 0.0))
    metadata = top_chunk.metadata or {}
    if forum:
        if metadata.get("forum_normalized") != forum:
            return 0.0
    elif category and metadata.get("category") != category:
        return 0.0
    top_score = float(top_chunk.score or 0.0)
    if top_score < 0.65:
        return 0.0
    settings = get_settings()
    if top_score >= 0.95:
        return float(settings.reranker_threshold_high)
    return float(settings.reranker_threshold_low)
