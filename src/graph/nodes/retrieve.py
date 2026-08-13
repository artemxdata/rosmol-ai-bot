from __future__ import annotations

from time import perf_counter

from src.config import get_settings
from src.graph.provenance import (
    MAX_PROVENANCE_ATTEMPTS,
    MAX_PROVENANCE_FILTER_ATTEMPTS,
    MAX_PROVENANCE_QUESTIONS,
    PROVENANCE_SCHEMA_VERSION,
    chunk_candidate_batch,
    chunk_id_batch,
    filter_scope,
    question_id,
    safe_filter,
    truncation_counts,
)
from src.graph.query_normalization import (
    ACCOUNT_DATA_RECOVERY,
    FORUM_DISCOVERY,
    GENERIC_PLATFORM_REGISTRATION,
    GRANT_DIRECTIONS,
    INACTIVE_PLATFORM_APPLICATION_BUTTON,
    PHYSICAL_GRANTS_OVERVIEW,
    PLATFORM_EVENT_NAVIGATION,
    bounded_query_intent,
    bounded_query_intent_hint,
    expand_query_aliases,
)
from src.graph.question_utils import (
    QueryProvenSourceAspect,
    build_effective_questions,
    build_query_proven_topic_plan,
)
from src.graph.state import BotState
from src.models import Question
from src.rag.errors import MLDependencyError
from src.response_contract import get_response_contract

STRICT_RETRIEVAL_TOP_K = 10
BROAD_RETRIEVAL_TOP_K = 30
KEYWORD_RECALL_TOP_K = 6
KEYWORD_RECALL_SCAN_LIMIT = 2048
FACTUAL_SOURCE_TYPE = get_response_contract().fact_policy.source_type
GRANT_DIRECTIONS_TOPIC = "nominacii_grantovyh_konkursov"
INACTIVE_APPLICATION_BUTTON_TOPIC = "registraciya_na_municipalnoe_meropriyatie"
DIRECTIONAL_TOPIC_LOOKUP_ALIASES: dict[str, tuple[str, ...]] = {
    "poluchenie_i_naznachenie_bileta": (
        "bilet_ne_prishel_povtornoe_poluchenie",
        "poluchenie_biletov_i_org_momenty",
    )
}
STRICT_TOPIC_ONLY = frozenset(
    {
        "poluchenie_i_naznachenie_bileta",
        "bilet_ne_prishel_povtornoe_poluchenie",
        "poluchenie_biletov_i_org_momenty",
    }
)
TOPIC_LOOKUP_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    (
        "o_meropriyatii",
        "sut_foruma_i_napravleniya",
        "sut_festivalya_i_tematika",
        "sut_festivalya_i_data",
        "daty_nachala_meropriyatiya",
        "vremya_nachala_i_raspisanie",
        "mesto_i_daty_provedeniya_meropriyatiya",
        "mesto_i_ploschadka_provedeniya",
    ),
    (
        "kak_zaregistrirovatsya_na_fgais",
        "registraciya_na_meropriyatie",
        "registraciya_bez_max",
        "podacha_zayavki_na_proekt",
        "podat_zayavku_na_uchastie",
        "registraciya_s_pomoschyu_sozdaniya_kabineta",
        "registraciya",
        "volonterskaya_pomosch",
        "forum",
    ),
    (
        "oplata_proezda",
        "oplata_proezda_palatok_i_pitaniya",
        "kompensaciya",
    ),
    (
        "programma_foruma",
        "programma_i_artisty",
        "programma_artisty",
        "vremya_nachala_i_raspisanie",
        "dokumenty_meropriyatiya",
    ),
    (
        "daty_nachala_meropriyatiya",
        "vremya_nachala_i_raspisanie",
        "sut_festivalya_i_data",
        "mesto_i_daty_provedeniya_meropriyatiya",
        "mesto_i_ploschadka_provedeniya",
    ),
    (
        "poseschenie_festivalya_s_detmi",
        "registraciya_detey",
    ),
    (
        "dokumenty_meropriyatiya",
        "spisok_veschey_i_dokumentov",
        "pamyatka_uchastnika_foruma",
    ),
    (
        "usloviya_prozhivaniya",
        "oplata_proezda_prozhivaniya_i_charter",
        "oplata_proezda",
        "pitanie_i_prozhivanie",
    ),
    (
        "usloviya_pitaniya_i_tochki_s_vodoy",
        "pitanie_i_pite",
        "pitanie_dlya_vegetariancev",
        "informaciya_o_ploschadke_pitanie",
        "informaciya_o_ploschadke_pitanie_pite",
        "informaciya_o_ploschadke_pitanie_pite_i",
        "pitanie_i_prozhivanie",
    ),
    (
        "otkaz_ot_uchastiya",
        "kolichestvo_person_otmena_registracii",
    ),
)
MULTI_ASPECT_MARKER_GROUPS: tuple[tuple[str, ...], ...] = (
    ("регистрац", "зарегистр", "создать кабинет", "создать аккаунт"),
    ("найти мероприят", "отфильтр", "фильтр", "по регион"),
    ("потер", "старого профил", "перенест", "перенос данн"),
    ("статус", "одобрен", "отклонен", "отклонён"),
    ("номинац", "направлен", "тематика проект"),
    ("шаги подач", "основные шаг", "подать заяв", "подач заяв"),
    ("что за", "что такое", "в двух слов", "общий период"),
    ("какие смен", "даты смен", "первая смен", "вторая смен"),
    ("первой и второй", "первую и вторую", "каждой смен", "обеих смен"),
    ("календар", "какие даты", "даты у"),
    ("когда она идет", "когда она идёт", "период форума"),
    ("кто может участв", "кто вообще может участв", "участник"),
    ("крайний срок", "до какого числа", "срок подачи"),
    ("соглашен",),
    ("итоговый отчет", "итоговый отчёт", "грантовый отчет", "грантовый отчёт"),
    ("результат", "отбор"),
    ("программ",),
    ("волонтер", "волонтёр"),
    ("документ", "паспорт", "справк"),
    ("трансфер", "автобус", "шаттл"),
    ("проезд", "оплат", "дорог", "билет"),
    ("письмо-вызов", "письмо вызов", "приглашен"),
    (
        "место проведения",
        "где проходит",
        "где пройдет",
        "где пройдёт",
        "где будет проходить",
        "где проводится",
        "адрес площадки",
        "локац",
    ),
    ("дресс", "одежд"),
    ("магазин",),
    ("памятк",),
    ("медицин",),
    ("овз",),
    ("иностран",),
    (
        "отказ",
        "отозвать",
        "отменить",
        "не могу поехать",
        "не смогу поехать",
        "не смогу приехать",
        "не получается поехать",
        "не получается приехать",
        "подтвердил участие",
        "подтвердила участие",
    ),
    ("проживан", "жиль", "жить", "где жить", "гостиниц", "отел"),
)


async def retrieve(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    analysis = state.get("analysis")
    if analysis is None:
        return {"retrieved_chunks": [], "metadata_filter": {}}

    filters = {
        "forum_normalized": analysis.forum_normalized,
        "category": analysis.category,
        "source_type": FACTUAL_SOURCE_TYPE,
    }
    current_message = state.get("message_masked") or state.get("message") or ""
    message = (
        state.get("contextual_message")
        or current_message
    )
    current_plan = build_query_proven_topic_plan(analysis, current_message)
    current_questions = list(current_plan.questions) or build_effective_questions(
        analysis,
        current_message,
    )
    query_scope_override = _query_scope_override(
        current_message,
        analysis,
        effective_questions=current_questions,
    )
    if query_scope_override:
        filters.update(query_scope_override)
    base_questions = list(current_plan.questions) or build_effective_questions(
        analysis,
        message,
    )
    allow_strict_forum_stop = len(base_questions) <= 1 and not _has_multi_aspect_message(
        message
    )
    questions = _questions_with_original_message(
        analysis,
        base_questions,
        message,
    )
    planned_aspects = (
        current_plan.source_aspects
        if current_plan.questions
        and len(current_plan.source_aspects) == len(current_plan.questions)
        else ()
    )

    chunks = []
    used_filters: list[dict] = []
    question_provenance: list[dict] = []
    topic_question_count = sum(1 for question in questions if question.topic)
    # Topic metadata is an efficient precision path, but a successful scroll only
    # proves that *some* topic alias exists.  It does not prove that every aspect
    # of a compound question (or the query-proven bounded source) was recalled.
    # Keep one semantic pass inside the already proven category/forum scope for
    # those cases.  Broader filter relaxation remains reserved for an actual
    # strict-topic miss below.
    proactive_shared_scope_recall = bool(query_scope_override) or _has_multi_aspect_message(
        message
    )
    needs_shared_broad_fallback = proactive_shared_scope_recall
    needs_relaxed_shared_fallback = False
    retrieval_methods: set[str] = set()
    metadata_lookup_attempted = False
    metadata_lookup_succeeded = False
    metadata_lookup_result_count = 0
    hybrid_candidates_present = False
    for question_index, question in enumerate(questions):
        source_aspect = (
            planned_aspects[question_index]
            if question_index < len(planned_aspects)
            else None
        )
        provenance = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "question_id": question_id(question_index),
            "attempts": [],
            "retrieved_chunk_ids": [],
        }
        if len(question_provenance) < MAX_PROVENANCE_QUESTIONS:
            question_provenance.append(provenance)
        if topic_question_count > 1 and not question.topic:
            provenance["skipped_reason"] = "unscoped_multi_topic_question"
            continue

        question_filters = {
            **filters,
            "topic": question.topic,
            "forum_normalized": question.forum_normalized or filters.get("forum_normalized"),
            "category": question.category or filters.get("category"),
        }
        if query_scope_override:
            question_filters.update(query_scope_override)
        try:
            found = []
            retrieval_query = _retrieval_query_for_intent(
                question.text,
                current_message,
                analysis,
            )
            requires_exact_topic = _requires_exact_topic_coverage(question_filters)
            strict_topic_only = requires_exact_topic or _should_defer_broad_topic_attempts(
                question_filters,
                topic_question_count,
            )
            strict_found = False
            for attempt_index, candidate_filters in enumerate(_filter_attempts(question_filters)):
                if strict_topic_only and attempt_index > 0:
                    break
                used_filters.append(candidate_filters)
                top_k = _top_k_for_attempt(candidate_filters, attempt_index)
                attempt_chunks, used_metadata_lookup = await _retrieve_attempt(
                    state["retriever"],
                    retrieval_query,
                    candidate_filters,
                    top_k=top_k,
                    current_message=current_message,
                    source_aspect=source_aspect,
                )
                attempt_method = "metadata" if used_metadata_lookup else "hybrid"
                retrieval_methods.add(attempt_method)
                attempt_metadata_succeeded = bool(
                    used_metadata_lookup and attempt_chunks
                )
                attempt_metadata_result_count = (
                    len(attempt_chunks) if used_metadata_lookup else 0
                )
                metadata_lookup_attempted = (
                    metadata_lookup_attempted or used_metadata_lookup
                )
                metadata_lookup_succeeded = (
                    metadata_lookup_succeeded or attempt_metadata_succeeded
                )
                metadata_lookup_result_count += attempt_metadata_result_count
                attempt_hybrid_candidates_present = bool(
                    not used_metadata_lookup and attempt_chunks
                )
                hybrid_candidates_present = (
                    hybrid_candidates_present or attempt_hybrid_candidates_present
                )
                keyword_chunks = []
                if not (used_metadata_lookup and attempt_chunks):
                    keyword_chunks = await _keyword_recall_candidates(
                        state["retriever"],
                        retrieval_query,
                        candidate_filters,
                        attempt_index=attempt_index,
                        tracer=tracer,
                        started_at=started_at,
                    )
                candidate_rows, candidate_counts = chunk_candidate_batch(
                    (
                        (
                            attempt_chunks,
                            "metadata" if used_metadata_lookup else "hybrid",
                        ),
                        (keyword_chunks, "keyword"),
                    )
                )
                if len(provenance["attempts"]) < MAX_PROVENANCE_ATTEMPTS:
                    provenance["attempts"].append(
                        {
                            "attempt_no": attempt_index + 1,
                            "scope": filter_scope(candidate_filters, question_filters),
                            "filters": safe_filter(candidate_filters),
                            "top_k": top_k,
                            "retrieval_method": attempt_method,
                            "metadata_lookup_attempted": used_metadata_lookup,
                            "metadata_lookup_succeeded": attempt_metadata_succeeded,
                            "metadata_lookup_result_count": attempt_metadata_result_count,
                            "hybrid_candidates_present": (
                                attempt_hybrid_candidates_present
                            ),
                            "candidates": candidate_rows,
                            **candidate_counts,
                        }
                    )
                provenance.update(
                    truncation_counts(
                        total=attempt_index + 1,
                        recorded=len(provenance["attempts"]),
                        label="attempts",
                    )
                )
                if attempt_index == 0 and (attempt_chunks or keyword_chunks):
                    strict_found = True
                found.extend(attempt_chunks)
                found.extend(keyword_chunks)
                if used_metadata_lookup and attempt_chunks:
                    break
                if attempt_chunks and not _should_continue_filter_attempts(
                    attempt_index,
                    candidate_filters,
                    attempt_chunks,
                    allow_strict_forum_stop=allow_strict_forum_stop,
                ):
                    break
            if strict_topic_only and not strict_found and not requires_exact_topic:
                needs_shared_broad_fallback = True
                needs_relaxed_shared_fallback = True
            retrieved_ids, retrieved_counts = chunk_id_batch(found)
            provenance["retrieved_chunk_ids"] = retrieved_ids
            provenance.update(
                {
                    "retrieved_chunk_ids_total": retrieved_counts["chunk_ids_total"],
                    "retrieved_chunk_ids_recorded": retrieved_counts["chunk_ids_recorded"],
                    "retrieved_chunk_ids_truncated_count": retrieved_counts[
                        "chunk_ids_truncated_count"
                    ],
                }
            )
            chunks.extend(found)
        except MLDependencyError as exc:
            if tracer:
                tracer.add_error("retrieve", int((perf_counter() - started_at) * 1000), exc)
            return {
                "retrieved_chunks": [],
                "metadata_filter": question_filters,
                "retrieval_provenance": question_provenance,
                "should_escalate": True,
                "escalation_reason": "ml_dependency_missing",
                "error": str(exc),
            }
        except Exception as exc:
            if tracer:
                tracer.add_error("retrieve", int((perf_counter() - started_at) * 1000), exc)
            return {
                "retrieved_chunks": [],
                "metadata_filter": question_filters,
                "retrieval_provenance": question_provenance,
                "should_escalate": True,
                "escalation_reason": "retrieval_failed",
                "error": str(exc),
            }

    if needs_shared_broad_fallback:
        shared_provenance = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "question_id": "shared",
            "attempts": [],
            "retrieved_chunk_ids": [],
        }
        if len(question_provenance) < MAX_PROVENANCE_QUESTIONS:
            question_provenance.append(shared_provenance)
        try:
            fallback_query = expand_query_aliases(str(message or "").strip())
            fallback_filters = _compact_filter(filters)
            shared_found = []
            shared_filter_attempts = (
                _filter_attempts(fallback_filters)
                if needs_relaxed_shared_fallback
                else [fallback_filters]
            )
            for attempt_index, candidate_filters in enumerate(shared_filter_attempts):
                used_filters.append(candidate_filters)
                attempt_chunks = await state["retriever"].retrieve(
                    fallback_query,
                    candidate_filters,
                    top_k=BROAD_RETRIEVAL_TOP_K,
                )
                retrieval_methods.add("hybrid")
                attempt_hybrid_candidates_present = bool(attempt_chunks)
                hybrid_candidates_present = (
                    hybrid_candidates_present or attempt_hybrid_candidates_present
                )
                keyword_chunks = await _keyword_recall_candidates(
                    state["retriever"],
                    fallback_query,
                    candidate_filters,
                    attempt_index=attempt_index + 1,
                    tracer=tracer,
                    started_at=started_at,
                )
                candidate_rows, candidate_counts = chunk_candidate_batch(
                    (
                        (attempt_chunks, "shared_hybrid"),
                        (keyword_chunks, "shared_keyword"),
                    )
                )
                if len(shared_provenance["attempts"]) < MAX_PROVENANCE_ATTEMPTS:
                    shared_provenance["attempts"].append(
                        {
                            "attempt_no": attempt_index + 1,
                            "scope": filter_scope(candidate_filters, fallback_filters),
                            "filters": safe_filter(candidate_filters),
                            "top_k": BROAD_RETRIEVAL_TOP_K,
                            "retrieval_method": "hybrid",
                            "metadata_lookup_attempted": False,
                            "metadata_lookup_succeeded": False,
                            "metadata_lookup_result_count": 0,
                            "hybrid_candidates_present": (
                                attempt_hybrid_candidates_present
                            ),
                            "candidates": candidate_rows,
                            **candidate_counts,
                        }
                    )
                shared_provenance.update(
                    truncation_counts(
                        total=attempt_index + 1,
                        recorded=len(shared_provenance["attempts"]),
                        label="attempts",
                    )
                )
                shared_found.extend(attempt_chunks)
                shared_found.extend(keyword_chunks)
                chunks.extend(attempt_chunks)
                chunks.extend(keyword_chunks)
            retrieved_ids, retrieved_counts = chunk_id_batch(shared_found)
            shared_provenance["retrieved_chunk_ids"] = retrieved_ids
            shared_provenance.update(
                {
                    "retrieved_chunk_ids_total": retrieved_counts["chunk_ids_total"],
                    "retrieved_chunk_ids_recorded": retrieved_counts["chunk_ids_recorded"],
                    "retrieved_chunk_ids_truncated_count": retrieved_counts[
                        "chunk_ids_truncated_count"
                    ],
                }
            )
        except MLDependencyError as exc:
            if tracer:
                tracer.add_error("retrieve", int((perf_counter() - started_at) * 1000), exc)
            return {
                "retrieved_chunks": [],
                "metadata_filter": filters,
                "retrieval_provenance": question_provenance,
                "should_escalate": True,
                "escalation_reason": "ml_dependency_missing",
                "error": str(exc),
            }
        except Exception as exc:
            if tracer:
                tracer.add_error("retrieve", int((perf_counter() - started_at) * 1000), exc)
            return {
                "retrieved_chunks": [],
                "metadata_filter": filters,
                "retrieval_provenance": question_provenance,
                "should_escalate": True,
                "escalation_reason": "retrieval_failed",
                "error": str(exc),
            }

    provenance_question_total = len(questions) + int(needs_shared_broad_fallback)
    provenance_question_counts = truncation_counts(
        total=provenance_question_total,
        recorded=len(question_provenance),
        label="questions",
    )
    if question_provenance:
        question_provenance[0].update(
            {
                **provenance_question_counts,
                "attributable_questions_total": len(questions),
            }
        )

    deduped = {chunk.chunk_id: chunk for chunk in chunks}
    if tracer:
        retrieval_method = (
            next(iter(retrieval_methods))
            if len(retrieval_methods) == 1
            else "mixed"
            if retrieval_methods
            else "none"
        )
        traced_filter_attempts = [
            safe_filter(item)
            for item in used_filters[:MAX_PROVENANCE_FILTER_ATTEMPTS]
        ]
        tracer.add(
            "retrieve",
            int((perf_counter() - started_at) * 1000),
            chunks=len(deduped),
            retrieval_method=retrieval_method,
            metadata_lookup_attempted=metadata_lookup_attempted,
            metadata_lookup_succeeded=metadata_lookup_succeeded,
            metadata_lookup_result_count=metadata_lookup_result_count,
            hybrid_candidates_present=hybrid_candidates_present,
            filters=safe_filter(filters),
            filter_attempts=traced_filter_attempts,
            **truncation_counts(
                total=len(used_filters),
                recorded=len(traced_filter_attempts),
                label="filter_attempts",
            ),
            **provenance_question_counts,
            attributable_questions_total=len(questions),
            question_provenance=question_provenance,
        )
    return {
        "retrieved_chunks": list(deduped.values()),
        "metadata_filter": filters,
        "retrieval_filter_attempts": used_filters,
        "retrieval_provenance": question_provenance,
    }


def _filter_attempts(filters: dict) -> list[dict]:
    filters = {**filters, "source_type": FACTUAL_SOURCE_TYPE}
    attempts = [_compact_filter(filters)]
    forum = filters.get("forum_normalized")
    category = filters.get("category")
    source_filter = {"source_type": FACTUAL_SOURCE_TYPE}
    if forum:
        attempts.append(_compact_filter({**filters, "category": None, "topic": None}))
    if category:
        attempts.append(_compact_filter({**source_filter, "category": category}))
    if attempts[0]:
        attempts.append(source_filter)
    return _dedupe_filters(attempts)


def _should_defer_broad_topic_attempts(
    filters: dict,
    topic_question_count: int,
) -> bool:
    if not filters.get("topic"):
        return False
    return topic_question_count > 1


def _requires_exact_topic_coverage(filters: dict) -> bool:
    topic = str(filters.get("topic") or "").strip()
    return topic in STRICT_TOPIC_ONLY


async def _retrieve_attempt(
    retriever: object,
    query: str,
    filters: dict,
    *,
    top_k: int,
    current_message: str | None,
    source_aspect: QueryProvenSourceAspect | None = None,
) -> tuple[list, bool]:
    retrieve_by_metadata = getattr(retriever, "retrieve_by_metadata", None)
    if filters.get("topic") and callable(retrieve_by_metadata):
        topic = str(filters.get("topic") or "").strip()
        directional_topic = _directional_topic_lookup(
            topic,
            current_message,
            filters,
        )
        if directional_topic:
            topic_values = [directional_topic]
        elif source_aspect is not None and source_aspect.source_topics:
            topic_values = list(dict.fromkeys(source_aspect.source_topics))
        else:
            topic_values = list(dict.fromkeys([topic, *_topic_lookup_aliases(topic)]))
        lookup_filters = {
            **filters,
            "topic": topic_values if len(topic_values) > 1 else topic_values[0],
        }
        return await retrieve_by_metadata(lookup_filters, top_k=top_k), True
    return await retriever.retrieve(query, filters, top_k=top_k), False


def _directional_topic_lookup(
    topic: str,
    current_message: str | None,
    filters: dict,
) -> str | None:
    if topic != "podacha_zayavki_na_proekt":
        return None
    if filters.get("category") != "платформа_фгаис" or filters.get("forum_normalized"):
        return None
    intent = bounded_query_intent(str(current_message or ""), forum_normalized=None)
    if intent != INACTIVE_PLATFORM_APPLICATION_BUTTON:
        return None
    return INACTIVE_APPLICATION_BUTTON_TOPIC


def _topic_lookup_aliases(topic: str) -> list[str]:
    topic = topic.strip()
    if not topic:
        return []
    if topic in DIRECTIONAL_TOPIC_LOOKUP_ALIASES:
        return list(DIRECTIONAL_TOPIC_LOOKUP_ALIASES[topic])
    for group in TOPIC_LOOKUP_ALIAS_GROUPS:
        if topic in group:
            return [candidate for candidate in group if candidate != topic]
    return []


def _should_continue_filter_attempts(
    attempt_index: int,
    filters: dict,
    attempt_chunks: list,
    *,
    allow_strict_forum_stop: bool,
) -> bool:
    if (
        attempt_index == 0
        and filters.get("topic")
        and filters.get("forum_normalized")
        and filters.get("category")
        and attempt_chunks
    ):
        return False
    if (
        allow_strict_forum_stop
        and attempt_index == 0
        and filters.get("forum_normalized")
        and filters.get("category")
        and len(attempt_chunks) >= get_settings().retrieval_strict_forum_stop_min_chunks
    ):
        return False
    return True


def _has_multi_aspect_message(message: str | None) -> bool:
    normalized = _normalize_question_text(str(message or ""))
    if not normalized:
        return False

    matched_groups = 0
    for markers in MULTI_ASPECT_MARKER_GROUPS:
        if any(marker in normalized for marker in markers):
            matched_groups += 1
            if matched_groups > 1:
                return True
    return False


def _top_k_for_attempt(filters: dict, attempt_index: int) -> int:
    if attempt_index > 0 or not filters:
        return BROAD_RETRIEVAL_TOP_K
    return STRICT_RETRIEVAL_TOP_K


def _questions_with_original_message(
    analysis: object,
    questions: list[Question],
    message: str | None,
) -> list[Question]:
    text = str(message or "").strip()
    if not text:
        return questions

    if any(question.topic for question in questions):
        return questions

    normalized_text = _normalize_question_text(text)
    if any(_normalize_question_text(question.text) == normalized_text for question in questions):
        return questions

    return [
        *questions,
        Question(
            text=text,
            category=getattr(analysis, "category", None),
            forum_normalized=getattr(analysis, "forum_normalized", None),
        ),
    ]


async def _keyword_recall_candidates(
    retriever: object,
    query: str,
    filters: dict,
    *,
    attempt_index: int,
    tracer: object | None,
    started_at: float,
) -> list:
    retrieve_keyword_candidates = getattr(retriever, "retrieve_keyword_candidates", None)
    if not callable(retrieve_keyword_candidates):
        return []

    filters = {**filters, "source_type": FACTUAL_SOURCE_TYPE}
    candidates = []
    try:
        candidates.extend(
            await retrieve_keyword_candidates(
                query,
                filters,
                top_k=KEYWORD_RECALL_TOP_K,
                scan_limit=KEYWORD_RECALL_SCAN_LIMIT,
                min_score=2.0,
                source_type=FACTUAL_SOURCE_TYPE,
            )
        )
        return candidates
    except Exception as exc:
        if tracer:
            tracer.add_error("keyword_recall", int((perf_counter() - started_at) * 1000), exc)
        return []


def _compact_filter(filters: dict) -> dict:
    return {key: value for key, value in filters.items() if value}


def _dedupe_filters(filters: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple[tuple[str, object], ...]] = set()
    for item in filters:
        key = tuple(sorted(item.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _normalize_question_text(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def _query_scope_override(
    message: str | None,
    analysis: object | None = None,
    *,
    effective_questions: list[Question] | None = None,
) -> dict[str, str | None]:
    """Keep generic platform/grant intents out of event-specific retrieval scopes."""
    forum = str(getattr(analysis, "forum_normalized", None) or "").strip()
    intent = bounded_query_intent(
        str(message or ""),
        forum_normalized=forum,
    )
    if intent is None:
        return {}
    if not forum and intent in {
        GENERIC_PLATFORM_REGISTRATION,
        PLATFORM_EVENT_NAVIGATION,
        ACCOUNT_DATA_RECOVERY,
        INACTIVE_PLATFORM_APPLICATION_BUTTON,
    }:
        return {"category": "платформа_фгаис", "forum_normalized": None}
    if intent == GRANT_DIRECTIONS:
        scope = {
            "category": "гранты",
            "forum_normalized": None,
        }
        if (
            len(effective_questions or []) == 1
            and not _has_multi_aspect_message(message)
        ):
            scope["topic"] = GRANT_DIRECTIONS_TOPIC
        return scope
    if intent == PHYSICAL_GRANTS_OVERVIEW:
        return {"category": "гранты", "forum_normalized": None}
    if intent == FORUM_DISCOVERY:
        return {"category": "общее", "forum_normalized": None}
    return {}


def _retrieval_query_for_intent(
    question: str,
    current_message: str | None,
    analysis: object | None = None,
) -> str:
    query = expand_query_aliases(question)
    forum = str(getattr(analysis, "forum_normalized", None) or "").strip()
    hint = bounded_query_intent_hint(
        str(current_message or ""),
        forum_normalized=forum,
    )
    return " ".join((query, hint)).strip()
