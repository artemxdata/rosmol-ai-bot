from __future__ import annotations

import asyncio
import gc
import re
from time import perf_counter
from typing import Any

from src.config import get_settings
from src.graph.query_normalization import expand_query_aliases
from src.graph.question_utils import FALLBACK_QUESTION_MARKERS, build_effective_questions
from src.graph.state import BotState
from src.models import Chunk, ScoredChunk
from src.rag.errors import MLDependencyError

MAX_RERANKED_CHUNKS = 8
QUESTION_CANDIDATE_LIMIT = 3
QUERY_CANDIDATE_LIMIT = 3
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
SAFE_CROSS_CATEGORY_ANSWER_BANK_TOPICS = {
    "доступ_и_техническая_ошибка",
    "контакты_и_оператор",
    "личный_кабинет_и_профиль",
    "письмо_и_уведомления",
    "регистрация_и_заявка",
    "статус_заявки",
}


async def rerank(state: BotState) -> dict:
    if state.get("should_escalate"):
        return {}

    started_at = perf_counter()
    tracer = state.get("trace")
    chunks = state.get("retrieved_chunks", [])
    query = state.get("message_masked") or state.get("message") or ""
    settings = get_settings()
    if _should_unload_model(settings, "ml_unload_embedder_after_use"):
        await _unload_model_owner(state.get("embedder"))

    try:
        reranked = await asyncio.to_thread(
            _rerank_for_state,
            state["reranker"],
            state,
            query,
            chunks,
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

    max_confidence = max((chunk.reranker_score for chunk in reranked), default=0.0)
    confidence_source = "reranker"
    retrieval_confidence_floor = _retrieval_confidence_floor(state, chunks)
    if retrieval_confidence_floor > max_confidence:
        max_confidence = retrieval_confidence_floor
        confidence_source = "retrieval_exact_filter"
    if tracer:
        tracer.add(
            "rerank",
            int((perf_counter() - started_at) * 1000),
            max_confidence=max_confidence,
            confidence_source=confidence_source,
        )
    if max_confidence <= 0:
        return {
            "reranked_chunks": reranked,
            "max_confidence": max_confidence,
            "should_escalate": True,
            "escalation_reason": "no_relevant_chunks",
        }
    result = {"reranked_chunks": reranked, "max_confidence": max_confidence}
    if max_confidence < get_settings().reranker_threshold_low:
        result.update({"should_escalate": True, "escalation_reason": "low_confidence"})
    return result


def _rerank_for_state(
    reranker: Any,
    state: BotState,
    query: str,
    chunks: list[Chunk],
) -> list[ScoredChunk]:
    query = expand_query_aliases(query)
    analysis = state.get("analysis")
    scoped_chunks = _scoped_chunks_for_analysis(analysis, chunks, query)
    original_priority_candidate = _priority_source_candidate(query, scoped_chunks)
    questions = (
        [
            question.text.strip()
            for question in build_effective_questions(analysis, query)
            if question.text.strip()
        ]
        if analysis
        else []
    )
    if len(questions) <= 1:
        rerank_query = questions[0] if questions else query
        priority_candidate = _priority_source_candidate(
            query,
            scoped_chunks,
        ) or original_priority_candidate or _priority_source_candidate(rerank_query, scoped_chunks)
        candidates = _candidate_chunks_for_question(
            rerank_query,
            scoped_chunks,
            min(MAX_RERANKED_CHUNKS, max(4, len(scoped_chunks))),
        )
        if priority_candidate and priority_candidate.chunk_id not in {
            chunk.chunk_id for chunk in candidates
        }:
            candidates = [priority_candidate, *candidates]
        if priority_candidate and _is_promotable_priority_candidate(
            query,
            rerank_query,
            priority_candidate,
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
        if _source_only_fast_path_allowed(analysis, scoped_chunks):
            return _source_only_ranked_candidates(candidates, priority_candidate, 4)
        ranked = reranker.rerank(rerank_query, candidates, 4)
        if priority_candidate and _is_promotable_priority_candidate(
            query,
            rerank_query,
            priority_candidate,
        ):
            return _prepend_priority_candidate(ranked, priority_candidate)[:4]
        if priority_candidate and priority_candidate.chunk_id not in {
            chunk.chunk_id for chunk in ranked
        }:
            return [_source_candidate(priority_candidate), *ranked][:4]
        return ranked

    selected: list[ScoredChunk] = []
    seen: set[str] = set()
    if original_priority_candidate and _is_promotable_priority_candidate(
        query,
        query,
        original_priority_candidate,
    ):
        _append_chunk(selected, seen, _priority_scored_candidate(original_priority_candidate))
    per_question_limit = 2 if len(questions) <= 5 else 1
    group_specs: list[tuple[str, list[Chunk], int]] = []

    for question in questions:
        candidates = _candidate_chunks_for_question(
            question,
            scoped_chunks,
            QUESTION_CANDIDATE_LIMIT,
        )
        if candidates:
            _append_chunk(selected, seen, _source_candidate(candidates[0]))
        group_specs.append((question, candidates, per_question_limit))

    target_size = min(MAX_RERANKED_CHUNKS, max(4, len(questions) + 2))
    query_candidates = _candidate_chunks_for_question(query, scoped_chunks, QUERY_CANDIDATE_LIMIT)
    if original_priority_candidate and original_priority_candidate.chunk_id not in {
        chunk.chunk_id for chunk in query_candidates
    }:
        query_candidates = [original_priority_candidate, *query_candidates]
    if _source_only_fast_path_allowed(analysis, scoped_chunks):
        for candidate in query_candidates:
            _append_chunk(selected, seen, _source_candidate(candidate))
            if len(selected) >= target_size:
                break
        return _boost_source_only_confidence(selected[:MAX_RERANKED_CHUNKS])
    group_specs.append((query, query_candidates, 4))
    group_results = _rerank_groups(reranker, group_specs)

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


def _scoped_chunks_for_analysis(
    analysis: Any,
    chunks: list[Chunk],
    query: str = "",
) -> list[Chunk]:
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
            key=lambda item: (_source_type_rank(item[1]), item[0]),
        )
    ]


def _official_source_chunks(chunks: list[Chunk]) -> list[Chunk]:
    return [
        chunk
        for chunk in chunks
        if str((chunk.metadata or {}).get("source_type") or "").strip()
        in {"docx", "xlsx"}
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
    source_type = str((chunk.metadata or {}).get("source_type") or "").strip()
    if source_type in {"docx", "xlsx"}:
        return 0
    if source_type == "ticket_answer_bank":
        return 2
    return 1


def _source_reliability_score(chunk: Chunk) -> float:
    source_type = str((chunk.metadata or {}).get("source_type") or "").strip()
    if source_type in {"docx", "xlsx"}:
        return 3.0
    if source_type == "ticket_answer_bank":
        return -3.0
    return 0.0


def _is_compatible_category(category: str | None, chunk: Chunk) -> bool:
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
) -> list[list[ScoredChunk]]:
    rerank_groups = getattr(reranker, "rerank_groups", None)
    if callable(rerank_groups):
        return rerank_groups(groups)
    return [reranker.rerank(query, chunks, top_k) for query, chunks, top_k in groups]


def _source_only_fast_path_allowed(analysis: Any, scoped_chunks: list[Chunk]) -> bool:
    if not analysis or not scoped_chunks:
        return False
    forum = str(getattr(analysis, "forum_normalized", None) or "").strip()
    category = str(getattr(analysis, "category", None) or "").strip()
    if not forum or category != "\u0444\u043e\u0440\u0443\u043c\u044b":
        return False
    return any(_chunk_matches_exact_forum(chunk, forum) for chunk in scoped_chunks)


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
) -> bool:
    return (
        _intent_example_matches_question(query, priority_candidate)
        or _intent_example_matches_question(rerank_query, priority_candidate)
        or _metadata_matches_promotable_priority_question(
            _normalize(query),
            priority_candidate,
        )
        or _metadata_matches_promotable_priority_question(
            _normalize(rerank_query),
            priority_candidate,
        )
    )


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


def _priority_source_candidate(question: str, chunks: list[Chunk]) -> Chunk | None:
    normalized_question = _normalize(question)
    if (
        _asks_specific_technical_question(normalized_question)
        or _asks_feedback_question(normalized_question)
        or _asks_grant_application(normalized_question)
        or _asks_invitation_letter(normalized_question)
    ):
        metadata_match = _metadata_priority_candidate(normalized_question, chunks)
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

    return _metadata_priority_candidate(normalized_question, chunks)


def _metadata_priority_candidate(
    normalized_question: str,
    chunks: list[Chunk],
) -> Chunk | None:
    for chunk in chunks:
        if _metadata_matches_priority_question(normalized_question, chunk):
            return chunk
    return None


def _metadata_matches_priority_question(normalized_question: str, chunk: Chunk) -> bool:
    metadata_haystack = _metadata_haystack(chunk)
    text_haystack = _normalize(chunk.text)

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
        return (
            "daty_nachala" in metadata_haystack
            or "mesto_i_daty" in metadata_haystack
            or "sut_festivalya_i_data" in metadata_haystack
            or ("даты" in metadata_haystack and "мероприят" in metadata_haystack)
        )
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
        return (
            "daty_nachala" in metadata_haystack
            or "mesto_i_daty" in metadata_haystack
            or "sut_festivalya_i_data" in metadata_haystack
            or ("даты" in metadata_haystack and "мероприят" in metadata_haystack)
        )
    return False


def _metadata_matches_promotable_priority_question(
    normalized_question: str,
    chunk: Chunk,
) -> bool:
    metadata_haystack = _metadata_haystack(chunk)
    text_haystack = _normalize(chunk.text)
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


def _asks_profile_id(normalized_question: str) -> bool:
    return any(marker in normalized_question for marker in ("id проф", "айди", "ид проф"))


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
        )
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
