from __future__ import annotations

import re
from time import perf_counter

from src.config import get_settings
from src.graph.provenance import (
    MAX_PROVENANCE_SOURCE_IDS,
    PROVENANCE_SCHEMA_VERSION,
    bounded_id_sequence,
    safe_reason,
    truncation_counts,
)
from src.graph.question_utils import (
    ADDITIONAL_FALLBACK_QUESTION_MARKERS,
    FALLBACK_QUESTION_MARKERS,
    build_effective_questions,
)
from src.graph.response_profiles import (
    LEGACY_EVENT_DATE_TOPICS,
    chunk_has_event_date_evidence,
)
from src.graph.response_profiles import (
    asks_event_dates as asks_profile_event_dates,
)
from src.graph.state import BotState
from src.llm.cascade import select_judge_model
from src.llm.json_utils import parse_llm_json
from src.llm.prompts import LLM_JUDGE_SYSTEM, build_judge_user
from src.models import Question, ScoredChunk, VerificationResult
from src.response_contract import get_response_contract

SOURCE_RE = re.compile(r"\[src:([^\]]+)\]")
TOKEN_RE = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)
_RESPONSE_CONTRACT = get_response_contract()
FACTUAL_SOURCE_TYPE = _RESPONSE_CONTRACT.fact_policy.source_type
UNKNOWN_FORUM_RESPONSE = _RESPONSE_CONTRACT.message("unknown_forum").select_text()
NO_QUESTION_RE = re.compile(
    r"(пока\s+нет\s+вопрос|задайте\s+(?:ваш\s+)?вопрос|готов\s+помочь.*задайте)",
    flags=re.IGNORECASE,
)
COVERAGE_MARKER_GROUPS: tuple[tuple[str, ...], ...] = (
    ("регистрац", "подать заяв", "подать проект", "заявк", "поучаств", "участв", "акци"),
    (
        "проезд",
        "дорог",
        "билет",
        "чартер",
        "доезд",
        "доехать",
        "добраться",
        "ехать",
        "поехать",
        "поездк",
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
DATE_TOPIC_ALIASES = LEGACY_EVENT_DATE_TOPICS
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
    "заявк",
    "обучен",
    "смен",
    "концерт",
    "праздник",
    "ребен",
    "ребён",
    "дет",
    "расписан",
    "программ",
    "афиш",
    "артист",
    "выступ",
    "резерв",
    "отбор",
    "одобр",
    "статус",
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
SENSITIVE_DATA_REQUEST_RE = re.compile(
    r"(?:"
    r"(?:пришли|пришлите|отправь|отправьте|сообщи|сообщите|напиши|напишите|"
    r"укажи|укажите|предоставь|предоставьте|скинь|скиньте)"
    r"[^.!?\n]{0,80}"
    r"(?:логин|парол|снилс|паспортн\w*\s+данн|сери\w*\s+паспорт|номер\w*\s+паспорт)"
    r"|"
    r"(?:логин|парол|снилс|паспортн\w*\s+данн|сери\w*\s+паспорт|номер\w*\s+паспорт)"
    r"[^.!?\n]{0,80}"
    r"(?:пришли|пришлите|отправь|отправьте|сообщи|сообщите|напиши|напишите|"
    r"укажи|укажите|предоставь|предоставьте|скинь|скиньте)"
    r")",
    flags=re.IGNORECASE,
)


async def verify(state: BotState) -> dict:
    started_at = perf_counter()
    result = await _verify_core(state)
    _trace_verify_decision(
        state,
        result,
        latency_ms=int((perf_counter() - started_at) * 1000),
    )
    return result


async def _verify_core(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    response = state.get("generated_response") or ""
    chunks = state.get("reranked_chunks", [])
    if state.get("should_escalate") and not response.strip():
        upstream_reason = str(state.get("escalation_reason") or "upstream_escalation")
        result = VerificationResult(
            has_hallucination=False,
            confidence=1.0,
            details=f"Verification skipped after upstream escalation: {upstream_reason}.",
        )
        if tracer:
            tracer.add(
                "verify",
                int((perf_counter() - started_at) * 1000),
                skipped=True,
                reason=upstream_reason,
            )
        return {"verification": result, "verifier_triggered": False}

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

    referenced_sources = set(state.get("cited_sources") or []) | cited
    if not referenced_sources and state.get("generator_model") in {
        "source_chunk",
        "source_only",
    }:
        referenced_sources = known
    unknown_referenced_sources = referenced_sources - known
    if unknown_referenced_sources:
        result = VerificationResult(
            has_hallucination=True,
            confidence=0.0,
            details=f"Unknown cited sources: {sorted(unknown_referenced_sources)}",
        )
        return {
            "verification": result,
            "verifier_triggered": False,
            "should_escalate": True,
            "escalation_reason": "unknown_source_citation",
        }

    non_yonote_sources = sorted(
        chunk.chunk_id
        for chunk in chunks
        if chunk.chunk_id in referenced_sources
        and _source_type(chunk) != FACTUAL_SOURCE_TYPE
    )
    if non_yonote_sources:
        result = VerificationResult(
            has_hallucination=True,
            confidence=0.0,
            details="Non-Yonote factual sources: " + ", ".join(non_yonote_sources),
        )
        if tracer:
            tracer.add(
                "verify",
                int((perf_counter() - started_at) * 1000),
                guard=True,
                reason="non_yonote_source",
                rejected_sources=non_yonote_sources,
            )
        return {
            "verification": result,
            "verifier_triggered": False,
            "should_escalate": True,
            "escalation_reason": "non_yonote_source",
        }

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

    sensitive_data_request = _sensitive_data_request(response)
    if sensitive_data_request:
        result = VerificationResult(
            has_hallucination=True,
            confidence=0.0,
            details="Response asks the user to transmit sensitive credentials or documents.",
        )
        if tracer:
            tracer.add(
                "verify",
                int((perf_counter() - started_at) * 1000),
                guard=True,
                sensitive_data_request=True,
            )
        return {
            "verification": result,
            "verifier_triggered": False,
            "should_escalate": True,
            "escalation_reason": "unsafe_sensitive_data_request",
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
            "generated_response": UNKNOWN_FORUM_RESPONSE,
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


def _trace_verify_decision(
    state: BotState,
    result: dict,
    *,
    latency_ms: int,
) -> None:
    tracer = state.get("trace")
    if not tracer:
        return
    verification = result.get("verification")
    has_hallucination = bool(getattr(verification, "has_hallucination", False))
    if "should_escalate" in result:
        should_escalate = bool(result.get("should_escalate"))
    else:
        should_escalate = bool(state.get("should_escalate"))
    partial = bool(
        result.get("partial_source_missing_coverage")
        or state.get("partial_source_missing_coverage")
    )
    if has_hallucination:
        decision = "reject"
        reason = safe_reason(
            result.get("escalation_reason"),
            default="hallucination_detected",
        )
    elif should_escalate:
        decision = "escalate"
        reason = safe_reason(
            result.get("escalation_reason") or state.get("escalation_reason"),
            default="upstream_escalation",
        )
    elif partial:
        decision = "partial"
        reason = "partial_source_coverage"
    else:
        decision = "pass"
        reason = "passed"

    chunks = state.get("reranked_chunks", [])
    known_source_ids = {str(chunk.chunk_id) for chunk in chunks if chunk.chunk_id}
    original_response = str(state.get("generated_response") or "")
    result_has_response = "generated_response" in result
    response = str(
        result.get("generated_response") if result_has_response else original_response
    )
    response_changed = result_has_response and response != original_response
    inline_candidates = SOURCE_RE.findall(response)
    explicit_candidates = [
        chunk_id
        for chunk_id in inline_candidates
        if chunk_id in known_source_ids
    ]
    if inline_candidates:
        candidates = explicit_candidates
        reference_scope = (
            "actual_response_explicit"
            if explicit_candidates
            else "actual_response_unknown_reference"
        )
    elif not response_changed:
        candidates = [
            str(value)
            for value in state.get("cited_sources", [])
            if str(value) in known_source_ids
        ]
        reference_scope = (
            "inherited_state_coarse" if candidates else "actual_response_unreferenced"
        )
    else:
        candidates = []
        reference_scope = "actual_response_unreferenced"
    referenced_source_ids, referenced_total = bounded_id_sequence(
        candidates,
        limit=MAX_PROVENANCE_SOURCE_IDS,
    )

    candidate_uncovered_question_ids: list[str] = []
    for event in reversed(tracer.events):
        if event.node != "generate_selection":
            continue
        value = event.metadata.get("candidate_uncovered_question_ids")
        if isinstance(value, list):
            candidate_uncovered_question_ids = [
                item
                for item in value
                if isinstance(item, str) and re.fullmatch(r"q[1-9][0-9]*", item)
            ]
        break
    tracer.add(
        "verify_decision",
        latency_ms,
        schema_version=PROVENANCE_SCHEMA_VERSION,
        decision=decision,
        reason=reason,
        referenced_source_ids=referenced_source_ids,
        **truncation_counts(
            total=referenced_total,
            recorded=len(referenced_source_ids),
            label="referenced_source_ids",
        ),
        reference_scope=reference_scope,
        candidate_uncovered_question_ids=candidate_uncovered_question_ids,
        verifier_triggered=bool(result.get("verifier_triggered")),
    )


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


def _sensitive_data_request(response: str) -> bool:
    if not response:
        return False
    normalized = SOURCE_RE.sub(" ", response)
    return bool(SENSITIVE_DATA_REQUEST_RE.search(normalized))


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
    if category == "гранты":
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
    return any(marker in message for marker in FORUM_SPECIFIC_MARKERS)


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

    source_chunks = _source_chunks_for_coverage(state, chunks)
    missing: list[str] = []
    for question in questions:
        if question.forum_normalized not in detected_forums:
            continue
        if not _question_has_source_coverage(question, source_chunks):
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
    cited_sources = set(
        state.get("cited_sources")
        or SOURCE_RE.findall(str(state.get("generated_response") or ""))
    )
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
    if asks_profile_event_dates(question.text):
        return any(
            chunk_has_event_date_evidence(chunk.text, chunk.metadata)
            for chunk in relevant_chunks
        )

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
    if state.get("generator_model") not in {"source_chunk", "source_only"}:
        # Retrieval confidence measures source relevance, not whether every generated
        # date, URL and number is entailed by that source. LLM synthesis must therefore
        # always pass the judge, including answers citing official chunks.
        return False
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
        _source_type(chunk) == FACTUAL_SOURCE_TYPE
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
