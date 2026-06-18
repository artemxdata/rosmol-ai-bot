from __future__ import annotations

import re
from time import perf_counter

from src.config import get_settings
from src.graph.question_utils import build_effective_questions
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
    ("регистрац", "подать заяв", "заявк"),
    ("проезд", "дорог", "билет", "чартер"),
    ("проживан", "жиль", "гостиниц", "отел"),
    ("питани", "еда", "корм"),
    ("документ", "паспорт", "справк"),
    ("возраст", "лет"),
    ("трансфер", "автобус", "шаттл"),
    ("сертификат",),
    ("чат", "куратор"),
)
INSUFFICIENT_SOURCE_RE = re.compile(
    r"(в\s+(?:предоставленн(?:ом|ых)\s+)?источник(?:е|ах)\s+нет\s+(?:конкретной\s+)?информации|"
    r"из\s+(?:представленных|переданных)\s+источников\s+невозможно\s+ответить|"
    r"источники\s+не\s+(?:содержат|подтверждают)|"
    r"информации\s+(?:в\s+источниках\s+)?нет|"
    r"нет\s+информации\s+о|"
    r"источник(?:е|ах)\s+отсутств|"
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
    metadata_text = " ".join(
        str(value or "")
        for value in (
            metadata.get("intent_name"),
            metadata.get("topic"),
            metadata.get("source_category"),
            metadata.get("forum_normalized"),
        )
    )
    return f"{metadata_text} {chunk.text}"


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
