from __future__ import annotations

from time import perf_counter

from src.config import get_settings
from src.graph.query_normalization import expand_query_aliases
from src.graph.question_utils import build_effective_questions
from src.graph.state import BotState
from src.models import Question
from src.rag.errors import MLDependencyError

STRICT_RETRIEVAL_TOP_K = 10
BROAD_RETRIEVAL_TOP_K = 30
KEYWORD_RECALL_TOP_K = 6
KEYWORD_RECALL_SCAN_LIMIT = 2048
OFFICIAL_KEYWORD_SOURCE_TYPES = ("xlsx", "docx")
FALLBACK_KEYWORD_SOURCE_TYPES = ("ticket_answer_bank",)
MULTI_ASPECT_MARKER_GROUPS: tuple[tuple[str, ...], ...] = (
    ("документ", "паспорт", "справк"),
    ("трансфер", "автобус", "шаттл"),
    ("проезд", "оплат", "дорог", "билет"),
    ("письмо-вызов", "письмо вызов", "приглашен"),
    ("место проведения",),
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
    ("проживан", "гостиниц", "отел"),
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
    }
    message = (
        state.get("contextual_message")
        or state.get("message_masked")
        or state.get("message")
    )
    base_questions = build_effective_questions(analysis, message)
    allow_strict_forum_stop = len(base_questions) <= 1 and not _has_multi_aspect_message(
        message
    )
    questions = _questions_with_original_message(
        analysis,
        base_questions,
        message,
    )

    chunks = []
    used_filters: list[dict] = []
    for question in questions:
        question_filters = {
            **filters,
            "topic": question.topic,
            "forum_normalized": question.forum_normalized or filters.get("forum_normalized"),
            "category": question.category or filters.get("category"),
        }
        try:
            found = []
            retrieval_query = expand_query_aliases(question.text)
            for attempt_index, candidate_filters in enumerate(_filter_attempts(question_filters)):
                used_filters.append(candidate_filters)
                top_k = _top_k_for_attempt(candidate_filters, attempt_index)
                attempt_chunks = await state["retriever"].retrieve(
                    retrieval_query,
                    candidate_filters,
                    top_k=top_k,
                )
                found.extend(attempt_chunks)
                found.extend(
                    await _keyword_recall_candidates(
                        state["retriever"],
                        retrieval_query,
                        candidate_filters,
                        attempt_index=attempt_index,
                        tracer=tracer,
                        started_at=started_at,
                    )
                )
                if attempt_chunks and not _should_continue_filter_attempts(
                    attempt_index,
                    candidate_filters,
                    attempt_chunks,
                    allow_strict_forum_stop=allow_strict_forum_stop,
                ):
                    break
            chunks.extend(found)
        except MLDependencyError as exc:
            if tracer:
                tracer.add_error("retrieve", int((perf_counter() - started_at) * 1000), exc)
            return {
                "retrieved_chunks": [],
                "metadata_filter": question_filters,
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
                "should_escalate": True,
                "escalation_reason": "retrieval_failed",
                "error": str(exc),
            }

    deduped = {chunk.chunk_id: chunk for chunk in chunks}
    if tracer:
        tracer.add(
            "retrieve",
            int((perf_counter() - started_at) * 1000),
            chunks=len(deduped),
            filters=filters,
            filter_attempts=used_filters,
        )
    return {
        "retrieved_chunks": list(deduped.values()),
        "metadata_filter": filters,
        "retrieval_filter_attempts": used_filters,
    }


def _filter_attempts(filters: dict) -> list[dict]:
    attempts = [_compact_filter(filters)]
    forum = filters.get("forum_normalized")
    category = filters.get("category")
    if forum:
        attempts.append(_compact_filter({**filters, "category": None, "topic": None}))
    if category:
        attempts.append(_compact_filter({"category": category}))
    if attempts[0]:
        attempts.append({})
    return _dedupe_filters(attempts)


def _should_continue_filter_attempts(
    attempt_index: int,
    filters: dict,
    attempt_chunks: list,
    *,
    allow_strict_forum_stop: bool,
) -> bool:
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

    source_types = list(OFFICIAL_KEYWORD_SOURCE_TYPES)
    if attempt_index > 0 or not filters:
        source_types.extend(FALLBACK_KEYWORD_SOURCE_TYPES)

    candidates = []
    try:
        for source_type in source_types:
            candidates.extend(
                await retrieve_keyword_candidates(
                    query,
                    filters,
                    top_k=KEYWORD_RECALL_TOP_K,
                    scan_limit=KEYWORD_RECALL_SCAN_LIMIT,
                    min_score=2.0,
                    source_type=source_type,
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
