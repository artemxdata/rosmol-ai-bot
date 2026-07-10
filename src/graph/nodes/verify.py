from __future__ import annotations

import re
from time import perf_counter

from src.config import get_settings
from src.graph.question_utils import (
    ADDITIONAL_FALLBACK_QUESTION_MARKERS,
    FALLBACK_QUESTION_MARKERS,
    build_effective_questions,
)
from src.graph.state import BotState
from src.llm.cascade import select_judge_model
from src.llm.json_utils import parse_llm_json
from src.llm.prompts import LLM_JUDGE_SYSTEM, build_judge_user
from src.models import Question, ScoredChunk, VerificationResult

SOURCE_RE = re.compile(r"\[src:([^\]]+)\]")
TOKEN_RE = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)
OFFICIAL_SOURCE_TYPES = {"xlsx", "docx", "yonote"}
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
    (
        "ноутбук",
        "снаряж",
        "вещ",
        "одежд",
        "взять с собой",
        "dokumenty_meropriyatiya",
        "spisok_veschey",
        "pamyatka_uchastnika",
    ),
    (
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
    ),
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
    ("результат", "отбор", "одобрен", "статус", "рассмотр"),
    ("сертификат",),
    ("чат", "куратор"),
    ("медпункт", "медицин", "здоров"),
    ("овз", "ограниченными возможн", "инвалид"),
    ("иностран", "иностранц"),
    ("грантовый конкурс", "гранты", "грантов"),
    ("цифровая неделя",),
    ("подтверждение участ",),
    ("изменить заявку", "изменить заявк", "внести изменения в заявк", "поменять заявк"),
    ("где посмотреть результ", "результат", "списки", "отбор"),
    ("в чем суть", "суть форум", "о форуме", "тематик"),
    ("программ", "расписан"),
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
DATE_COVERAGE_MARKERS = frozenset(
    (
        "дата",
        "даты",
        "срок",
        "заезд",
        "выезд",
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
DATE_TOPIC_ALIASES = frozenset(
    {
        "daty_nachala_meropriyatiya",
        "mesto_i_daty_provedeniya_meropriyatiya",
        "mesto_i_ploschadka_provedeniya",
        "vremya_nachala_i_raspisanie",
        "sut_festivalya_i_data",
    }
)
OVERVIEW_COVERAGE_MARKERS = frozenset(
    (
        "в чем суть",
        "суть форум",
        "о форуме",
        "тематик",
    )
)
OVERVIEW_TOPIC_ALIASES = frozenset(
    {
        "o_meropriyatii",
        "sut_foruma_i_napravleniya",
        "sut_festivalya_i_tematika",
        "sut_festivalya_i_data",
    }
)

FORUM_SPECIFIC_MARKERS = (
    "форум",
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
FORUM_SPECIFIC_MARKERS = FORUM_SPECIFIC_MARKERS + (
    "\u0432\u043e\u0437\u0440\u0430\u0441\u0442",
    "\u043b\u0435\u0442",
    "\u043f\u043e\u0434\u0430\u0442",
    "\u0443\u0447\u0430\u0441\u0442",
    "\u0440\u0435\u0433\u0438\u0441\u0442\u0440",
    "\u043f\u0440\u043e\u0435\u0437\u0434",
    "\u0434\u043e\u0440\u043e\u0433",
    "\u0431\u0438\u043b\u0435\u0442",
    "\u043e\u043f\u043b\u0430\u0442",
    "\u0432\u043e\u0437\u043c\u0435\u0449",
    "\u043f\u0440\u043e\u0436\u0438\u0432",
    "\u0440\u0430\u0437\u043c\u0435\u0449",
    "\u043f\u0430\u043b\u0430\u0442",
    "\u0433\u043e\u0441\u0442\u0438\u043d\u0438\u0446",
    "\u043e\u0442\u0435\u043b",
    "\u043f\u0438\u0442\u0430\u043d",
    "\u0435\u0434\u0430",
)
INSUFFICIENT_SOURCE_RE = re.compile(
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
SPECIALIST_REDIRECT_RE = re.compile(
    r"(переда(?:ю|м|ем|ём|ть|йте)\s+(?:(?:ваш|этот)\s+)?"
    r"(?:вопрос|обращение|запрос)\s+специалист|"
    r"обрат(?:итесь|иться)\s+.*(?:служб[ау]\s+(?:поддержк|забот)|специалист|координатор)|"
    r"рекоменду(?:ю|ем)\s+(?:направить|обратиться|обратитесь)\s+.*"
    r"(?:специалист|служб[ау]\s+(?:поддержк|забот)|координатор))",
    flags=re.IGNORECASE,
)
UNSUPPORTED_DIRECTIVE_MARKERS: tuple[str, ...] = (
    "обратитесь напрямую",
    "обратитесь к организатор",
    "сообщите организатор",
    "сообщи организатор",
    "свяжитесь с организатор",
    "связаться с организатор",
    "напишите организатор",
    "предоставьте следующую информацию",
    "полные фио",
    "регион и насел",
    "электронную почту",
    "уточнить вашу регистрацию",
    "подтвердить участие",
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

    unsupported_directives = _unsupported_directive_markers(response, state, chunks)
    if unsupported_directives:
        result = VerificationResult(
            has_hallucination=True,
            confidence=0.0,
            details="Unsupported directive markers: " + "; ".join(unsupported_directives),
        )
        if tracer:
            tracer.add(
                "verify",
                int((perf_counter() - started_at) * 1000),
                guard=True,
                unsupported_directives=unsupported_directives,
            )
        return {
            "verification": result,
            "verifier_triggered": False,
            "should_escalate": True,
            "escalation_reason": "unsupported_instruction",
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
            "generated_response": (
                "Уточни, пожалуйста, название форума или мероприятия. "
                "По твоему вопросу найдены похожие источники по разным событиям, "
                "и я не хочу смешать условия."
            ),
            "should_escalate": False,
            "escalation_reason": None,
        }

    missing_coverage = _missing_aspect_coverage(state, chunks)
    if missing_coverage:
        result = VerificationResult(
            has_hallucination=False,
            confidence=confidence,
            details="Missing aspect source coverage: " + "; ".join(missing_coverage),
        )
        partial_response = _partial_response_with_missing_note(state, missing_coverage)
        if tracer:
            tracer.add(
                "verify",
                int((perf_counter() - started_at) * 1000),
                guard=True,
                missing_coverage=missing_coverage,
                partial_allowed=bool(partial_response)
                or _allows_partial_source_response(state, missing_coverage),
            )
        if partial_response:
            return {
                "verification": result,
                "verifier_triggered": False,
                "generated_response": partial_response,
                "partial_source_missing_coverage": missing_coverage,
                "should_escalate": False,
                "escalation_reason": None,
            }
        if _allows_partial_source_response(state, missing_coverage):
            return {"verification": result, "verifier_triggered": False}
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
        partial_response = _partial_response_with_missing_note(state, missing_coverage)
        if tracer:
            tracer.add(
                "verify",
                int((perf_counter() - started_at) * 1000),
                guard=True,
                missing_coverage=missing_coverage,
                partial_allowed=bool(partial_response)
                or _allows_partial_source_response(state, missing_coverage),
            )
        if partial_response:
            return {
                "verification": result,
                "verifier_triggered": False,
                "generated_response": partial_response,
                "partial_source_missing_coverage": missing_coverage,
                "should_escalate": False,
                "escalation_reason": None,
            }
        if _allows_partial_source_response(state, missing_coverage):
            return {"verification": result, "verifier_triggered": False}
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
        judge_chunks = _source_chunks_for_response(response, state, chunks)
        judge_raw = await state["llm_client"].generate(
            model=model,
            system=LLM_JUDGE_SYSTEM,
            user=build_judge_user(response, judge_chunks),
            response_format="json",
            max_tokens=200,
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


def _allows_partial_source_response(state: BotState, missing_coverage: list[str]) -> bool:
    if not missing_coverage:
        return False
    declared_missing = state.get("partial_source_missing_coverage") or []
    if not isinstance(declared_missing, list) or not declared_missing:
        return False
    response = str(state.get("generated_response") or "").casefold()
    return "в базе нет подтверждённых данных" in response


def _partial_response_with_missing_note(
    state: BotState, missing_coverage: list[str]
) -> str | None:
    if not missing_coverage:
        return None
    response = str(state.get("generated_response") or "").strip()
    if not response or _signals_insufficient_source_escalation(response):
        return None
    cited_sources = state.get("cited_sources") or SOURCE_RE.findall(response)
    if not cited_sources:
        return None
    lowered = response.casefold()
    if "в базе нет подтверждённых данных" in lowered:
        return response
    missing_text = "; ".join(
        _humanize_missing_coverage(item) for item in missing_coverage if item
    )
    if not missing_text:
        return None
    ending = "" if missing_text.endswith((".", "?", "!")) else "."
    return (
        f"{response}\n\n"
        f"По этим пунктам в базе нет подтверждённых данных: {missing_text}{ending} "
        "Чтобы не выдумывать, я не добавляю по ним информацию."
    )


def _humanize_missing_coverage(item: str) -> str:
    value = str(item or "").strip().rstrip(".")
    if ": " in value:
        value = value.split(": ", 1)[1].strip()
    return value


def _ambiguous_forum_context(state: BotState, chunks: list[ScoredChunk]) -> list[str]:
    analysis = state.get("analysis")
    if getattr(analysis, "forum_normalized", None):
        return []
    if analysis and _detected_forums_for_coverage(analysis.extracted_params):
        return []
    category = str(getattr(analysis, "category", None) or "").strip()
    if category and category != "форумы":
        return []

    message = _normalize(str(_state_message_for_search(state)))
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
    if len(forums) == 1 and _is_unanchored_forum_specific_question(message):
        return sorted(forums)
    if len(forums) <= 1 and cited_sources and forums:
        forums = {
            forum
            for chunk in chunks
            if (forum := str((chunk.metadata or {}).get("forum_normalized") or "").strip())
        }
    return sorted(forums) if len(forums) > 1 else []


def _is_unanchored_forum_specific_question(message: str) -> bool:
    if "форум" in message or "мероприят" in message:
        return True
    return any(
        marker in message
        for marker in (
            "подат",
            "участ",
            "регист",
            "проезд",
            "прожив",
            "питан",
            "трансфер",
            "документ",
            "памятк",
            "заезд",
            "выезд",
        )
    )


def _unsupported_directive_markers(
    response: str,
    state: BotState,
    chunks: list[ScoredChunk],
) -> list[str]:
    normalized_response = _normalize(SOURCE_RE.sub(" ", response))
    markers_in_response = [
        marker for marker in UNSUPPORTED_DIRECTIVE_MARKERS if marker in normalized_response
    ]
    if not markers_in_response:
        return []

    source_chunks = _source_chunks_for_response(response, state, chunks)
    normalized_sources = _normalize(" ".join(chunk.text for chunk in source_chunks))
    return [
        marker
        for marker in markers_in_response
        if not _directive_supported_by_sources(marker, normalized_sources)
    ]


def _directive_supported_by_sources(marker: str, normalized_sources: str) -> bool:
    if marker in normalized_sources:
        return True
    if marker == "подтвердить участие":
        return (
            "подтверд" in normalized_sources or "подтвержд" in normalized_sources
        ) and "участи" in normalized_sources
    if marker == "электронную почту":
        return any(
            source_marker in normalized_sources
            for source_marker in ("почт", "email", "e-mail")
        )
    return False


def _source_chunks_for_response(
    response: str,
    state: BotState,
    chunks: list[ScoredChunk],
) -> list[ScoredChunk]:
    cited_sources = set(state.get("cited_sources") or SOURCE_RE.findall(response))
    if not cited_sources:
        return chunks
    return [chunk for chunk in chunks if chunk.chunk_id in cited_sources]


def _missing_source_coverage(state: BotState, chunks: list[ScoredChunk]) -> list[str]:
    analysis = state.get("analysis")
    if not analysis or not chunks:
        return []

    detected_forums = _detected_forums_for_coverage(analysis.extracted_params)
    if len(detected_forums) < 2:
        return []

    message = _state_message_for_search(state)
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

    message = _state_message_for_search(state)
    if _is_feedback_coverage_exempt(message):
        return []
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
        for markers, question_text in (
            *FALLBACK_QUESTION_MARKERS,
            *ADDITIONAL_FALLBACK_QUESTION_MARKERS,
        )
        if any(marker in normalized for marker in markers)
        and not _should_skip_marker_question(question_text, normalized)
    ]


def _is_feedback_coverage_exempt(message: str) -> bool:
    normalized = _normalize(message)
    if "обратн" not in normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "эксперт",
            "оценк",
            "балл",
            "разбаллов",
            "куратор",
            "отзыв",
            "впечатл",
        )
    )


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
            _marker_group_has_source_coverage(markers, normalized_haystack)
            for markers in required_markers
        )

    question_tokens = _tokens(question.text)
    if not question_tokens:
        return True
    overlap = question_tokens & _tokens(haystack)
    return len(overlap) >= min(2, len(question_tokens))


def _marker_group_has_source_coverage(
    markers: tuple[str, ...],
    normalized_haystack: str,
) -> bool:
    if any(marker in DATE_COVERAGE_MARKERS for marker in markers) and any(
        alias in normalized_haystack for alias in DATE_TOPIC_ALIASES
    ):
        return True
    if any(marker in OVERVIEW_COVERAGE_MARKERS for marker in markers) and any(
        alias in normalized_haystack for alias in OVERVIEW_TOPIC_ALIASES
    ):
        return True
    return any(marker in normalized_haystack for marker in markers)


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


def _should_skip_marker_question(question_text: str, normalized_message: str) -> bool:
    normalized_question = _normalize(question_text)
    if "документ" in normalized_question and "нуж" in normalized_question:
        return _has_personal_document_context(
            normalized_message
        ) and not _has_event_document_context(normalized_message)
    if question_text == "Какие даты и сроки?":
        return _has_personal_date_context(normalized_message) and not _has_event_date_context(
            normalized_message
        )
    if not _has_decline_participation_context(normalized_message):
        return False
    if question_text == "Как подать заявку или зарегистрироваться?":
        return True
    if question_text == "Что с подтверждением участия?":
        return True
    if question_text != "Кто оплачивает проезд?":
        return False
    return not any(
        marker in normalized_message
        for marker in (
            "проезд",
            "дорог",
            "билет",
            "чартер",
            "доезд",
            "добраться",
            "оплат",
            "стоимост",
            "возмещ",
        )
    )


def _has_decline_participation_context(normalized_message: str) -> bool:
    return any(
        marker in normalized_message
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


def _has_personal_document_context(normalized_message: str) -> bool:
    return any(
        marker in normalized_message
        for marker in ("[документ]", "[snils]", "снилс", "паспорт")
    )


def _has_event_document_context(normalized_message: str) -> bool:
    return any(
        marker in normalized_message
        for marker in (
            "какие документы",
            "документы нужны",
            "что взять",
            "список вещ",
            "памятк",
        )
    )


def _has_personal_date_context(normalized_message: str) -> bool:
    return "дата рождения" in normalized_message or "[дата]" in normalized_message


def _has_event_date_context(normalized_message: str) -> bool:
    return any(
        marker in normalized_message
        for marker in (
            "дата форум",
            "даты форум",
            "дата меропр",
            "даты меропр",
            "когда проходит",
            "когда начинается",
            "сроки регистрац",
            "срок приема",
            "срок приёма",
            "заезд",
            "выезд",
        )
    )


def _state_message_for_search(state: BotState) -> str:
    return str(
        state.get("contextual_message")
        or state.get("message_masked")
        or state.get("message")
        or ""
    )


def _coverage_label(question: Question) -> str:
    forum = question.forum_normalized or "без форума"
    return f"{forum}: {question.text}"


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(_normalize(text)))


def _normalize(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _can_skip_llm_judge(state: BotState, confidence: float) -> bool:
    if state.get("generator_model") != "source_chunk":
        return _can_skip_official_llm_judge(state, confidence)
    if confidence >= get_settings().reranker_threshold_high:
        return True
    return _can_skip_low_confidence_technical_source_chunk(state)


def _can_skip_low_confidence_technical_source_chunk(state: BotState) -> bool:
    """Allow deterministic official tech-support fallbacks without an LLM judge.

    These chunks are intentionally generic and often receive a low reranker score, but
    they are still safe when the answer is directly copied from the cited official
    source and the user question overlaps with the chunk's examples/topic.
    """
    response = str(state.get("generated_response") or "")
    cited_sources = set(state.get("cited_sources") or SOURCE_RE.findall(response))
    if not response or not cited_sources:
        return False

    chunks = state.get("reranked_chunks", [])
    cited_chunks = [chunk for chunk in chunks if chunk.chunk_id in cited_sources]
    if len(cited_chunks) != len(cited_sources):
        return False
    if not cited_chunks or not all(_is_technical_support_chunk(chunk) for chunk in cited_chunks):
        return False
    if not _response_supported_by_sources(response, cited_chunks):
        return False

    message = _state_message_for_search(state)
    if not message:
        return True
    question = Question(
        text=message,
        category=getattr(state.get("analysis"), "category", None),
    )
    return _question_has_source_coverage(question, cited_chunks)


def _can_skip_official_llm_judge(state: BotState, confidence: float) -> bool:
    if confidence < get_settings().reranker_threshold_high:
        return False
    model = state.get("generator_model")
    if not model or model in {"source_chunk", "source_only"}:
        return False
    cited_sources = set(state.get("cited_sources") or SOURCE_RE.findall(
        str(state.get("generated_response") or "")
    ))
    if not cited_sources:
        return False
    chunks = state.get("reranked_chunks", [])
    cited_chunks = [chunk for chunk in chunks if chunk.chunk_id in cited_sources]
    if len(cited_chunks) != len(cited_sources):
        return False
    return all(_source_type(chunk) in OFFICIAL_SOURCE_TYPES for chunk in cited_chunks)


def _requires_source_citations(state: BotState) -> bool:
    model = state.get("generator_model")
    return bool(model and model not in {"source_chunk", "source_only"})


def _source_type(chunk: ScoredChunk) -> str:
    return str((chunk.metadata or {}).get("source_type") or "").strip().casefold()


def _is_technical_support_chunk(chunk: ScoredChunk) -> bool:
    metadata = chunk.metadata or {}
    haystack = _normalize(
        " ".join(
            str(value or "")
            for value in (
                chunk.chunk_id,
                metadata.get("category"),
                metadata.get("topic"),
                metadata.get("intent_name"),
                metadata.get("source_category"),
            )
        )
    )
    return (
        _source_type(chunk) in OFFICIAL_SOURCE_TYPES
        and (
            "техподдерж" in haystack
            or "техническ" in haystack
            or "tehnichesk" in haystack
            or "technical" in haystack
        )
    )


def _response_supported_by_sources(response: str, chunks: list[ScoredChunk]) -> bool:
    response_tokens = _tokens(SOURCE_RE.sub(" ", response))
    if not response_tokens:
        return False
    source_tokens = _tokens(" ".join(chunk.text for chunk in chunks))
    if not source_tokens:
        return False
    overlap = response_tokens & source_tokens
    return len(overlap) / len(response_tokens) >= 0.7
