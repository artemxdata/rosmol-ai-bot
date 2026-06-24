from __future__ import annotations

import re
from time import perf_counter

from src.config import get_settings
from src.graph.question_utils import FALLBACK_QUESTION_MARKERS, build_effective_questions
from src.graph.state import BotState
from src.models import QueryAnalysis, Question, ScoredChunk

TOKEN_RE = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)
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

    source_chunks = select_deterministic_source_chunks(
        analysis,
        questions,
        chunks,
        float(state.get("max_confidence") or 0),
        state.get("message_masked") or state.get("message"),
    )
    if source_chunks:
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
    original_question = _original_question(analysis, message)
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
    if trusted_raw_official is not None and len(questions) == 1:
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
        source_chunk = _exact_source_for_original_question(
            original_question,
            question_candidates,
        )
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


def build_deterministic_source_response(chunks: list[ScoredChunk] | ScoredChunk) -> str | None:
    if isinstance(chunks, ScoredChunk):
        chunks = [chunks]
    parts = [
        f"{chunk.text.strip()} [src:{chunk.chunk_id}]"
        for chunk in chunks
        if chunk.text.strip()
    ]
    if not parts:
        return None
    return "\n\n".join(parts)


def _candidate_source_chunks(
    analysis: QueryAnalysis,
    chunks: list[ScoredChunk],
) -> list[ScoredChunk]:
    if _should_prefer_unscoped_grant_source(analysis):
        unscoped_grant_chunks = [
            chunk
            for chunk in chunks
            if (chunk.metadata or {}).get("category") == "гранты"
            and not str((chunk.metadata or {}).get("forum_normalized") or "").strip()
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
            if category_chunks:
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
) -> tuple[float, int, float, int, float]:
    intent_score = _adjusted_intent_example_match_score(question, chunk)
    field_score = _source_metadata_field_score(question, chunk)
    generic_rank = 1 if _is_generic_chunk(chunk) else 0
    unscoped_grant_rank = _unscoped_grant_rank(analysis, chunk)
    confidence = float(chunk.reranker_score or chunk.score or 0)
    if intent_score:
        return (
            0,
            unscoped_grant_rank,
            -float(intent_score * 100) - field_score,
            generic_rank,
            -confidence,
        )
    if _metadata_matches_specific_question(analysis, question, chunk):
        return (1, unscoped_grant_rank, -field_score, generic_rank, -confidence)
    if field_score > 0:
        return (2, unscoped_grant_rank, -field_score, generic_rank, -confidence)
    return (3, unscoped_grant_rank, 0, generic_rank, -confidence)


def _original_question(analysis: QueryAnalysis, message: str | None) -> Question | None:
    text = str(message or "").strip()
    if not text:
        return None
    return Question(
        text=text,
        category=analysis.category,
        forum_normalized=analysis.forum_normalized,
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
    if float(top_chunk.reranker_score or 0) < threshold:
        return None
    if float(top_chunk.score or 0) < 0.95:
        return None
    return top_chunk


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

    if _asks_access_or_technical_error(question_normalized):
        return (
            "доступ_и_техническая_ошибка" in metadata_haystack
            or "tehnicheskaya_oshibka" in metadata_haystack
            or "техническая ошибка" in metadata_haystack
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

    if _asks_transfer(question_normalized):
        return "transfer" in metadata_haystack or "трансфер" in metadata_haystack

    if _asks_arrival_departure(question_normalized):
        return (
            "vremya_zaezda_i_vyezda" in metadata_haystack
            or ("заезд" in metadata_haystack and "выезд" in metadata_haystack)
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

    if _asks_what_is_rosmol(question_normalized):
        return (
            "chto_takoe_rosmolodezh" in metadata_haystack
            or "что такое росмолодежь" in metadata_haystack
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
    forum = str((chunk.metadata or {}).get("forum_normalized") or "").strip()
    return 1 if forum else 0


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


def _asks_what_is_rosmol(question_normalized: str) -> bool:
    return any(
        marker in question_normalized
        for marker in (
            "что такое росмолод",
            "кто такие росмолод",
            "чем занимается росмолод",
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
    return text.casefold().replace("ё", "е")


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(_normalize(text)))


def effective_questions(state: BotState, analysis: QueryAnalysis) -> list[Question]:
    questions = build_effective_questions(
        analysis,
        state.get("message_masked") or state.get("message"),
    )
    if questions:
        return questions

    text = str(state.get("message_masked") or state.get("message") or "").strip()
    if not text:
        return []
    return [
        Question(
            text=text,
            category=analysis.category,
            forum_normalized=analysis.forum_normalized,
        )
    ]
