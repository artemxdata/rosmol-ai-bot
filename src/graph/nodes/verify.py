from __future__ import annotations

import re
from time import perf_counter

from src.config import get_settings
from src.graph.question_utils import FALLBACK_QUESTION_MARKERS, build_effective_questions
from src.graph.state import BotState
from src.llm.cascade import select_judge_model
from src.llm.json_utils import parse_llm_json
from src.llm.prompts import LLM_JUDGE_SYSTEM, build_judge_user
from src.models import Question, ScoredChunk, VerificationResult

SOURCE_RE = re.compile(r"\[src:([^\]]+)\]")
TOKEN_RE = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)
NO_QUESTION_RE = re.compile(
    r"(пока\s+нет\s+вопрос|задайте\s+(?:ваш\s+)?вопрос|готов\s+помочь.*задайте)",
    flags=re.IGNORECASE,
)
COVERAGE_MARKER_GROUPS: tuple[tuple[str, ...], ...] = (
    ("регистрац", "подать заяв", "подать проект", "заявк", "поучаств", "участв", "акци"),
    (
        "проезд",
        "оплат",
        "расход",
        "покрыва",
        "дорог",
        "билет",
        "чартер",
        "доезд",
        "доехать",
        "добраться",
        "ехать",
        "поехать",
        "поездк",
        "возмещ",
    ),
    ("проживан", "жиль", "гостиниц", "отель", "отеле", "отеля"),
    ("питани", "еда", "корм"),
    ("документ", "паспорт", "справк"),
    ("положен",),
    ("возраст", "лет"),
    ("трансфер", "автобус", "шаттл"),
    ("ноутбук", "снаряж", "вещ", "одежд", "взять с собой"),
    ("отказ", "отказаться", "отозвать", "отменить участие"),
    (
        "письмо-вызов",
        "письмо вызов",
        "письмо на регион",
        "письмо в регион",
        "письмо для региона",
        "приглашен",
        "подтверждение участия",
    ),
    ("дата", "даты", "срок", "заезд", "выезд"),
    ("результат", "отбор", "одобрен", "статус", "рассмотр"),
    ("сертификат",),
    ("чат", "куратор"),
    (
        "вернуть грантов",
        "возврат грантов",
        "вернуть средства",
        "возврат средств",
        "вернуть деньги",
        "возврат денег",
        "вернуть денеж",
        "возврат денеж",
        "не удается реализ",
        "не удаётся реализ",
        "не могу реализ",
        "не получается реализ",
        "сорвал",
    ),
    ("отчет", "отчетност", "отчёт", "отчётност"),
    ("id не", "id проф", "айди", "ид проф"),
)
FORUM_SPECIFIC_MARKERS = (
    "дата",
    "даты",
    "мероприят",
    "заезд",
    "выезд",
    "приех",
    "трансфер",
    "аэропорт",
    "вокзал",
    "площадк",
    "кемеров",
    "новокузнец",
)
INSUFFICIENT_SOURCE_RE = re.compile(
    r"(в\s+(?:предоставленн(?:ом|ых)\s+)?источник(?:е|ах)\s+нет\s+(?:конкретной\s+)?информации|"
    r"из\s+(?:представленных|переданных)\s+источников\s+невозможно\s+ответить|"
    r"источники\s+не\s+(?:содержат|подтверждают)|"
    r"информации\s+(?:в\s+источниках\s+)?нет|"
    r"нет\s+информации\s+о|"
    r"источник(?:е|ах)\s+отсутств|"
    r"не\s+указан[аоы]?\s+в\s+(?:предоставленных\s+)?источниках|"
    r"(?:точной|конкретной)\s+информации[^.!?]{0,160}\s+нет|"
    r"информаци[яи][^.!?]{0,160}отсутств)",
    flags=re.IGNORECASE,
)
SPECIALIST_REDIRECT_RE = re.compile(
    r"(переда(?:ю|м|ем|ём|ть|йте)\s+(?:(?:ваш|этот)\s+)?"
    r"(?:вопрос|обращение|запрос)\s+специалист|"
    r"обрат(?:итесь|иться)\s+.*(?:служб[ау]\s+(?:поддержк|забот)|специалист|координатор)|"
    r"рекоменду(?:ю|ем)\s+(?:направить|обратиться|обратитесь)\s+.*"
    r"(?:специалист|служб[ау]\s+(?:поддержк|забот)|координатор))",
    flags=re.IGNORECASE,
)


async def verify(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    response = state.get("generated_response") or ""
    chunks = state.get("reranked_chunks", [])
    if _contradicts_present_question(response, state):
        result = VerificationResult(
            has_hallucination=True,
            confidence=0.0,
            details="Response asks for a question although the user already asked one.",
        )
        if tracer:
            tracer.add("verify", int((perf_counter() - started_at) * 1000), guard=True)
        return {"verification": result, "verifier_triggered": False}

    cited = set(SOURCE_RE.findall(response))
    known = {chunk.chunk_id for chunk in chunks}
    if _requires_source_citations(state) and not cited:
        result = VerificationResult(
            has_hallucination=True,
            confidence=0.0,
            details="LLM response has no source citations.",
        )
        if tracer:
            tracer.add(
                "verify",
                int((perf_counter() - started_at) * 1000),
                guard=True,
                reason="missing_source_citations",
            )
        return {
            "verification": result,
            "verifier_triggered": False,
            "should_escalate": True,
            "escalation_reason": "missing_source_citations",
        }

    unknown_sources = cited - known
    if unknown_sources:
        result = VerificationResult(
            has_hallucination=True,
            confidence=0.0,
            details=f"Unknown source markers: {sorted(unknown_sources)}",
        )
        return {"verification": result, "verifier_triggered": False}

    confidence = float(state.get("max_confidence") or 0)
    if _signals_insufficient_source_escalation(response):
        result = VerificationResult(
            has_hallucination=False,
            confidence=confidence,
            details="Response says the sources are insufficient and redirects to a specialist.",
        )
        if tracer:
            tracer.add("verify", int((perf_counter() - started_at) * 1000), guard=True)
        return {
            "verification": result,
            "verifier_triggered": False,
            "should_escalate": True,
            "escalation_reason": "insufficient_sources",
        }

    ambiguous_forums = _ambiguous_forum_context(state, chunks)
    if ambiguous_forums:
        result = VerificationResult(
            has_hallucination=False,
            confidence=confidence,
            details="Ambiguous forum-specific sources: " + ", ".join(ambiguous_forums),
        )
        if tracer:
            tracer.add(
                "verify",
                int((perf_counter() - started_at) * 1000),
                guard=True,
                ambiguous_forums=ambiguous_forums,
            )
        return {
            "verification": result,
            "verifier_triggered": False,
            "should_escalate": True,
            "escalation_reason": "ambiguous_forum_context",
        }

    missing_coverage = _missing_aspect_coverage(state, chunks)
    if missing_coverage:
        result = VerificationResult(
            has_hallucination=False,
            confidence=confidence,
            details="Missing aspect source coverage: " + "; ".join(missing_coverage),
        )
        if tracer:
            tracer.add(
                "verify",
                int((perf_counter() - started_at) * 1000),
                guard=True,
                missing_coverage=missing_coverage,
            )
        return {
            "verification": result,
            "verifier_triggered": False,
            "should_escalate": True,
            "escalation_reason": "partial_source_coverage",
        }

    missing_coverage = _missing_source_coverage(state, chunks)
    if missing_coverage:
        result = VerificationResult(
            has_hallucination=False,
            confidence=confidence,
            details="Missing source coverage: " + "; ".join(missing_coverage),
        )
        if tracer:
            tracer.add(
                "verify",
                int((perf_counter() - started_at) * 1000),
                guard=True,
                missing_coverage=missing_coverage,
            )
        return {
            "verification": result,
            "verifier_triggered": False,
            "should_escalate": True,
            "escalation_reason": "partial_source_coverage",
        }

    if _can_skip_llm_judge(state, confidence):
        result = VerificationResult(has_hallucination=False, confidence=confidence)
        if tracer:
            tracer.add("verify", int((perf_counter() - started_at) * 1000), judge=False)
        return {"verification": result, "verifier_triggered": False}

    try:
        model = select_judge_model()
        judge_raw = await state["llm_client"].generate(
            model=model,
            system=LLM_JUDGE_SYSTEM,
            user=build_judge_user(response, chunks),
            response_format="json",
            max_tokens=500,
        )
        data = parse_llm_json(judge_raw)
        result = VerificationResult.model_validate({**data, "triggered_llm_judge": True})
    except Exception as exc:
        result = VerificationResult(
            has_hallucination=True,
            confidence=0.0,
            details=f"Judge failed: {exc}",
            triggered_llm_judge=True,
        )

    if tracer:
        tracer.add("verify", int((perf_counter() - started_at) * 1000), judge=True)
    return {"verification": result, "verifier_triggered": True}


def _contradicts_present_question(response: str, state: BotState) -> bool:
    if not response or not NO_QUESTION_RE.search(response):
        return False
    message = str(state.get("message_masked") or state.get("message") or "").strip()
    if "?" in message:
        return True
    analysis = state.get("analysis")
    return bool(getattr(analysis, "questions", None))


def _signals_insufficient_source_escalation(response: str) -> bool:
    if not response:
        return False
    normalized = response.casefold().replace("ё", "е")
    explicit_escalation = "передаю обращение специалисту, чтобы не дать неточный ответ"
    if explicit_escalation in normalized:
        return True
    return bool(INSUFFICIENT_SOURCE_RE.search(normalized))


def _ambiguous_forum_context(state: BotState, chunks: list[ScoredChunk]) -> list[str]:
    analysis = state.get("analysis")
    if getattr(analysis, "forum_normalized", None):
        return []
    if analysis and _detected_forums_for_coverage(analysis.extracted_params):
        return []

    message = _normalize(str(state.get("message_masked") or state.get("message") or ""))
    if not any(marker in message for marker in FORUM_SPECIFIC_MARKERS):
        return []

    cited_sources = set(state.get("cited_sources") or [])
    source_chunks = [
        chunk for chunk in chunks if not cited_sources or chunk.chunk_id in cited_sources
    ]
    forums = {
        forum
        for chunk in source_chunks
        if (forum := str((chunk.metadata or {}).get("forum_normalized") or "").strip())
    }
    if len(forums) <= 1 and cited_sources:
        forums = {
            forum
            for chunk in chunks
            if (forum := str((chunk.metadata or {}).get("forum_normalized") or "").strip())
        }
    return sorted(forums) if len(forums) > 1 else []


def _missing_source_coverage(state: BotState, chunks: list[ScoredChunk]) -> list[str]:
    analysis = state.get("analysis")
    if not analysis or not chunks:
        return []

    detected_forums = _detected_forums_for_coverage(analysis.extracted_params)
    if len(detected_forums) < 2:
        return []

    message = state.get("message_masked") or state.get("message") or ""
    questions = build_effective_questions(analysis, message)
    if len(questions) < 2:
        return []

    missing: list[str] = []
    for question in questions:
        if question.forum_normalized not in detected_forums:
            continue
        if not _question_has_source_coverage(question, chunks):
            missing.append(_coverage_label(question))
    return missing


def _missing_aspect_coverage(state: BotState, chunks: list[ScoredChunk]) -> list[str]:
    analysis = state.get("analysis")
    if not analysis or not chunks:
        return []
    if len(_detected_forums_for_coverage(analysis.extracted_params)) >= 2:
        return []

    message = state.get("message_masked") or state.get("message") or ""
    questions = _aspect_questions_for_coverage(analysis, message)
    if len(questions) < 2:
        return []

    source_chunks = _source_chunks_for_coverage(state, chunks)
    missing = [
        _coverage_label(question)
        for question in questions
        if not _question_has_source_coverage(question, source_chunks)
    ]
    return missing


def _aspect_questions_for_coverage(
    analysis: object,
    message: str,
) -> list[Question]:
    questions = [
        question
        for question in build_effective_questions(analysis, message)
        if _required_marker_groups(question.text)
    ]
    marker_questions = _marker_questions_from_message(analysis, message)
    if len(marker_questions) > len(questions):
        questions = marker_questions

    unique: list[Question] = []
    seen: set[tuple[str, str | None]] = set()
    for question in questions:
        key = (_normalize(question.text), question.forum_normalized)
        if key in seen:
            continue
        seen.add(key)
        unique.append(question)
    return unique


def _marker_questions_from_message(analysis: object, message: str) -> list[Question]:
    normalized = _normalize(message)
    if not normalized:
        return []
    return [
        Question(
            text=question_text,
            category=getattr(analysis, "category", None),
            forum_normalized=getattr(analysis, "forum_normalized", None),
        )
        for markers, question_text in FALLBACK_QUESTION_MARKERS
        if any(marker in normalized for marker in markers)
    ]


def _source_chunks_for_coverage(
    state: BotState,
    chunks: list[ScoredChunk],
) -> list[ScoredChunk]:
    cited_sources = set(state.get("cited_sources") or [])
    if not cited_sources:
        return chunks
    return [chunk for chunk in chunks if chunk.chunk_id in cited_sources]


def _detected_forums_for_coverage(extracted_params: dict) -> set[str]:
    raw_forums = extracted_params.get("detected_forums")
    if not isinstance(raw_forums, list):
        return set()
    return {forum for item in raw_forums if (forum := str(item or "").strip())}


def _question_has_source_coverage(question: Question, chunks: list[ScoredChunk]) -> bool:
    relevant_chunks = [
        chunk for chunk in chunks if _chunk_matches_question_forum(chunk, question)
    ]
    if not relevant_chunks:
        return False

    haystack = " ".join(_chunk_haystack(chunk) for chunk in relevant_chunks)
    required_markers = _required_marker_groups(question.text)
    if required_markers:
        normalized_haystack = _normalize(haystack)
        return all(
            any(marker in normalized_haystack for marker in markers)
            for markers in required_markers
        )

    question_tokens = _tokens(question.text)
    if not question_tokens:
        return True
    overlap = question_tokens & _tokens(haystack)
    return len(overlap) >= min(2, len(question_tokens))


def _chunk_matches_question_forum(chunk: ScoredChunk, question: Question) -> bool:
    if not question.forum_normalized:
        return True
    chunk_forum = str((chunk.metadata or {}).get("forum_normalized") or "").strip()
    return chunk_forum == question.forum_normalized


def _chunk_haystack(chunk: ScoredChunk) -> str:
    metadata = chunk.metadata or {}
    examples = metadata.get("intent_examples") or []
    examples_text = " ".join(str(example or "") for example in examples if example)
    metadata_text = " ".join(
        str(value or "")
        for value in (
            metadata.get("intent_name"),
            metadata.get("topic"),
            metadata.get("source_category"),
            metadata.get("forum_normalized"),
        )
    )
    return f"{metadata_text} {examples_text} {chunk.text}"


def _required_marker_groups(question_text: str) -> list[tuple[str, ...]]:
    normalized_question = _normalize(question_text)
    return [
        markers
        for markers in COVERAGE_MARKER_GROUPS
        if any(marker in normalized_question for marker in markers)
    ]


def _coverage_label(question: Question) -> str:
    forum = question.forum_normalized or "без форума"
    return f"{forum}: {question.text}"


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(_normalize(text)))


def _normalize(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _can_skip_llm_judge(state: BotState, confidence: float) -> bool:
    if state.get("generator_model") != "source_chunk":
        return False
    return confidence >= get_settings().reranker_threshold_high


def _requires_source_citations(state: BotState) -> bool:
    model = state.get("generator_model")
    return bool(model and model not in {"source_chunk", "source_only"})
