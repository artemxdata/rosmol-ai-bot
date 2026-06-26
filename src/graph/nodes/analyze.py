from __future__ import annotations

import re
from time import perf_counter

from src.graph.context import apply_session_context, build_contextual_message
from src.graph.state import BotState
from src.kb.forum_registry import detect_forums_from_text
from src.llm.cascade import select_analyzer_model
from src.llm.json_utils import parse_llm_json
from src.llm.prompts import QUERY_ANALYZER_SYSTEM, build_analyzer_user
from src.models import Complexity, QueryAnalysis
from src.security.operator_request import is_operator_request


async def analyze_query(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    original_message = state.get("message") or state["message_masked"]
    masked_message = state["message_masked"]
    routing_hint = state.get("routing_hint")

    deterministic = _deterministic_analysis(
        original_message,
        masked_message,
        routing_hint,
        state.get("session"),
    )
    if deterministic is not None:
        if tracer:
            tracer.add(
                "analyze",
                int((perf_counter() - started_at) * 1000),
                mode="deterministic",
            )
        result = {
            "analysis": deterministic,
            "analyzer_mode": "deterministic",
            "contextual_message": build_contextual_message(
                masked_message,
                state.get("session"),
                deterministic,
            ),
        }
        if deterministic.should_escalate:
            result["should_escalate"] = True
            result["escalation_reason"] = deterministic.escalation_reason or "needs_operator"
        return result

    try:
        llm = state["llm_client"]
        model = select_analyzer_model(routing_hint)
        content = await llm.generate(
            model=model,
            system=QUERY_ANALYZER_SYSTEM,
            user=build_analyzer_user(
                masked_message,
                state.get("session"),
                None,
                routing_hint,
            ),
            response_format="json",
        )
        payload = _coerce_analysis_payload(parse_llm_json(content))
        _apply_deterministic_forum(
            payload,
            original_message,
        )
        _apply_forum_category_guardrail(payload, original_message)
        analysis = QueryAnalysis.model_validate(payload)
        analysis = apply_session_context(analysis, masked_message, state.get("session"))
        if tracer:
            tracer.add("analyze", int((perf_counter() - started_at) * 1000), model=model)
        result = {
            "analysis": analysis,
            "contextual_message": build_contextual_message(
                masked_message,
                state.get("session"),
                analysis,
            ),
        }
        if analysis.should_escalate:
            result["should_escalate"] = True
            result["escalation_reason"] = analysis.escalation_reason or "needs_operator"
        return result
    except Exception as exc:
        fallback = _fallback_analysis(
            original_message,
            masked_message,
            routing_hint,
            state.get("session"),
        )
        if fallback is not None:
            if tracer:
                tracer.add_error("analyze_llm", int((perf_counter() - started_at) * 1000), exc)
                tracer.add(
                    "analyze",
                    int((perf_counter() - started_at) * 1000),
                    fallback=True,
                    reason="deterministic_fallback",
                )
            return {
                "analysis": fallback,
                "analyzer_fallback": True,
                "contextual_message": build_contextual_message(
                    masked_message,
                    state.get("session"),
                    fallback,
                ),
            }
        if tracer:
            tracer.add_error("analyze", int((perf_counter() - started_at) * 1000), exc)
        return {
            "should_escalate": True,
            "escalation_reason": "analyzer_failed",
            "error": str(exc),
        }


def _deterministic_analysis(
    original_message: str,
    masked_message: str,
    routing_hint: object,
    session: object | None = None,
) -> QueryAnalysis | None:
    return _fallback_analysis(original_message, masked_message, routing_hint, session)


def _fallback_analysis(
    original_message: str,
    masked_message: str,
    routing_hint: object,
    session: object | None = None,
) -> QueryAnalysis | None:
    category = _infer_category_from_message(masked_message)
    is_offtopic = _is_safe_offtopic(masked_message)
    if is_offtopic:
        category = "offtopic"
    complexity = _complexity_from_routing_hint(routing_hint)
    if _has_feedback_context(masked_message):
        complexity = Complexity.SIMPLE
    if _is_exact_fallback_intent_message(masked_message):
        complexity = Complexity.SIMPLE
    needs_clarification = is_offtopic or _needs_application_context_clarification(
        masked_message
    )
    clarification_question = (
        "Уточни, пожалуйста, речь о форуме, мероприятии или грантовом конкурсе?"
        if needs_clarification and not is_offtopic
        else None
    )
    if needs_clarification:
        complexity = Complexity.SIMPLE
    if _should_force_simple_support_query(category, masked_message):
        complexity = Complexity.SIMPLE
    should_escalate = _is_operator_request(masked_message)
    if is_offtopic:
        should_escalate = False
    if should_escalate and not category:
        category = "навигация"
    payload = {
        "category": category,
        "complexity": complexity.value,
        "questions": [],
        "needs_clarification": needs_clarification,
        "clarification_question": clarification_question,
        "is_offtopic": is_offtopic,
        "should_escalate": should_escalate,
        "escalation_reason": "operator_requested" if should_escalate else None,
    }
    payload = _coerce_analysis_payload(payload)
    _apply_deterministic_forum(payload, original_message)
    _apply_forum_category_guardrail(payload, original_message)
    analysis = QueryAnalysis.model_validate(payload)
    analysis = apply_session_context(analysis, masked_message, session)
    if not analysis.category and not analysis.forum_normalized:
        return None
    return analysis


def _is_operator_request(message: str) -> bool:
    return is_operator_request(message)


def _is_safe_offtopic(message: str) -> bool:
    normalized = message.casefold().replace("ё", "е")
    in_scope_markers = (
        "форум",
        "мероприят",
        "фестивал",
        "грант",
        "фгаис",
        "молодежь россии",
        "молодёжь россии",
        "росмолод",
        "заявк",
        "личн",
        "кабинет",
        "профил",
    )
    if any(marker in normalized for marker in in_scope_markers):
        return False

    offtopic_markers = (
        "погода",
        "температура",
        "курс валют",
        "курс доллара",
        "курс евро",
        "новости",
        "гороскоп",
        "анекдот",
        "шутк",
        "рецепт",
        "приготовить",
        "домашн",
        "контрольн",
        "реферат",
        "сочинение",
        "реши задачу",
        "переведи",
        "фильм",
        "сериал",
        "починить телефон",
        "чинить телефон",
        "ремонт телефон",
        "сломался телефон",
        "починить айфон",
        "ремонт айфон",
    )
    return any(marker in normalized for marker in offtopic_markers)


def _should_force_simple_support_query(category: str | None, message: str) -> bool:
    if category not in {"техподдержка", "платформа_фгаис"}:
        return False
    normalized = message.casefold().replace("ё", "е")
    words = re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
    if len(words) > 12:
        return False
    if any(marker in normalized for marker in ("если", "сравни", "одновременно", "несколько")):
        return False
    return True


def _is_exact_fallback_intent_message(message: str) -> bool:
    normalized = message.casefold().replace("ё", "е").strip()
    return normalized.startswith(("технические вопросы.", "рекомендации.")) or any(
        marker in normalized
        for marker in (
            "предложение о сотрудничестве",
            "предложение сотрудничества",
            "возможности бота",
            "abilities",
            "что такое росмолод",
            "обратную связь о сотрудн",
            "обратная связь о сотрудн",
        )
    )


def _needs_application_context_clarification(message: str) -> bool:
    normalized = message.casefold().replace("ё", "е")
    if not ("подать" in normalized and "заяв" in normalized and "участ" in normalized):
        return False
    return not any(
        marker in normalized
        for marker in (
            "грант",
            "проект",
            "форум",
            "мероприят",
            "фестивал",
            "фгаис",
            "росмолод",
        )
    )


def _infer_category_from_message(message: str) -> str | None:
    normalized = message.casefold().replace("ё", "е")
    if _has_staff_feedback_context(normalized):
        return "навигация"
    if normalized.startswith("технические вопросы.") or "технические вопросы" in normalized:
        return "техподдержка"
    if normalized.startswith("рекомендации.") or "рекомендации" in normalized:
        return "общее"
    if any(marker in normalized for marker in ("сотруднич", "партнерств", "партнёрств")):
        return "общее"
    if any(marker in normalized for marker in ("возможности бота", "abilities", "что умеешь")):
        return "общее"
    if "что такое росмолод" in normalized or "кто такие росмолод" in normalized:
        return "платформа_фгаис"
    if _needs_application_context_clarification(normalized):
        return "форумы"
    if _has_feedback_context(normalized):
        return "гранты"
    if "грант" in normalized:
        return "гранты"
    if any(
        marker in normalized
        for marker in (
            "отчет",
            "отчетност",
            "отчёт",
            "отчётност",
            "расход",
            "смет",
            "договор",
            "наклад",
            "закуп",
            "контрольн",
            "точк",
        )
    ):
        return "гранты"
    if (
        "проект" in normalized
        and any(marker in normalized for marker in ("подать", "заявк", "отправ"))
        and not any(marker in normalized for marker in ("форум", "фестивал", "мероприят"))
    ):
        return "гранты"
    if any(
        word in normalized
        for word in (
            "отвяз",
            "госуслуг",
            "есиа",
            "верифицировать другой",
            "верификац другого",
            "двойное граждан",
            "два граждан",
            "почта физ",
            "почта юр",
            "ответственное лицо",
            "ответственного лица",
            "ошиб",
            "баг",
            "не работает",
            "не получается войти",
            "не получается выбрать",
            "не получается отправить",
            "не получается сохранить",
            "не получается заполнить",
            "не удается выбрать",
            "не удаётся выбрать",
            "не удается отправить",
            "не удаётся отправить",
            "не удается сохранить",
            "не удаётся сохранить",
            "не удается заполнить",
            "не удаётся заполнить",
            "техподдерж",
            "id не",
            "id проф",
            "айди",
            "ид проф",
        )
    ):
        return "техподдержка"
    if any(
        word in normalized
        for word in (
            "фгаис",
            "личн",
            "кабинет",
            "парол",
            "верификац",
            "регистрац",
            "зарегистр",
        )
    ):
        return "платформа_фгаис"
    if any(word in normalized for word in ("форум", "мероприят", "фестивал")):
        return "форумы"
    return None


def _has_staff_feedback_context(message: str) -> bool:
    normalized = str(message or "").casefold().replace("ё", "е")
    return "обратн" in normalized and any(
        marker in normalized for marker in ("сотрудн", "специалист", "оператор")
    )


def _has_feedback_context(message: str) -> bool:
    normalized = str(message or "").casefold().replace("ё", "е")
    if "обратн" not in normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "заявк",
            "проект",
            "грант",
            "эксперт",
            "оценк",
            "куратор",
            "балл",
            "остав",
            "поделит",
            "впечатл",
        )
    )


def _complexity_from_routing_hint(routing_hint: object) -> Complexity:
    if isinstance(routing_hint, dict):
        value = routing_hint.get("complexity")
        if isinstance(value, str):
            try:
                return Complexity(value)
            except ValueError:
                return Complexity.COMPLEX
    if isinstance(routing_hint, Complexity):
        return routing_hint
    if hasattr(routing_hint, "complexity"):
        value = routing_hint.complexity
        if isinstance(value, Complexity):
            return value
    return Complexity.COMPLEX


def _coerce_analysis_payload(payload: dict) -> dict:
    normalized = dict(payload)
    normalized["forum"] = _normalize_forum_alias(_coerce_optional_string(normalized.get("forum")))
    normalized["forum_normalized"] = _normalize_forum_alias(
        _coerce_optional_string(normalized.get("forum_normalized"))
    )
    normalized["topics"] = _coerce_string_list(normalized.get("topics"))
    normalized["category"] = _normalize_category(normalized.get("category"))
    _drop_pseudo_forum_for_category(normalized)
    if not isinstance(normalized.get("extracted_params"), dict):
        normalized["extracted_params"] = {}
    if bool(normalized.get("is_offtopic")) or normalized.get("category") == "offtopic":
        normalized["category"] = "offtopic"
        normalized["is_offtopic"] = True
        normalized["needs_clarification"] = True
        normalized["should_escalate"] = False
        normalized["escalation_reason"] = None
    if normalized.get("forum") and not normalized.get("forum_normalized"):
        normalized["forum_normalized"] = normalized["forum"]
    normalized["questions"] = _coerce_questions(normalized.get("questions"))
    _propagate_question_defaults(normalized)
    _drop_pseudo_forums(normalized)
    return normalized


def _apply_deterministic_forum(payload: dict, message: str) -> None:
    detected_forums = detect_forums_from_text(message)
    grant_pseudo_detected = any(_is_grant_pseudo_forum(forum) for forum in detected_forums)
    detected_forums = [
        forum for forum in detected_forums if not _is_grant_pseudo_forum(forum)
    ]
    if grant_pseudo_detected and not detected_forums:
        payload["category"] = "гранты"
        _propagate_question_defaults(payload, override_category=True)
        _drop_pseudo_forums(payload)
        return

    if len(detected_forums) > 1:
        extracted_params = payload.get("extracted_params")
        if not isinstance(extracted_params, dict):
            extracted_params = {}
        extracted_params["detected_forums"] = detected_forums
        payload["extracted_params"] = extracted_params
        if not payload.get("category"):
            payload["category"] = "форумы"
        return

    detected_forum = detected_forums[0] if detected_forums else None
    if not detected_forum:
        return

    payload["forum"] = detected_forum
    payload["forum_normalized"] = detected_forum
    force_forum_category = _should_force_forum_category(detected_forum, message)
    if force_forum_category:
        payload["category"] = "форумы"
    if not payload.get("category"):
        payload["category"] = "форумы"
    _propagate_question_defaults(
        payload,
        override_forum=True,
        override_category=force_forum_category,
    )


def _apply_forum_category_guardrail(payload: dict, message: str) -> None:
    forum = str(payload.get("forum_normalized") or payload.get("forum") or "").strip()
    if not forum or _is_grant_pseudo_forum(forum):
        return
    if _has_forum_technical_marker(message):
        return
    if _should_force_forum_category(forum, message) or _should_override_llm_category_for_forum(
        payload.get("category"),
        message,
    ):
        payload["category"] = "форумы"
        _propagate_question_defaults(payload, override_category=True)


def _should_override_llm_category_for_forum(category: object, message: str) -> bool:
    if category not in {"платформа_фгаис", "навигация", "общее", None}:
        return False
    return not _has_forum_technical_marker(message)


def _has_forum_technical_marker(message: str) -> bool:
    normalized = message.casefold().replace("ё", "е")
    technical_markers = (
        "ошиб",
        "не работает",
        "не приходит письмо",
        "парол",
        "id проф",
        "айди",
        "ид проф",
        "верификац",
        "техподдерж",
    )
    return any(marker in normalized for marker in technical_markers)


def _propagate_question_defaults(
    payload: dict,
    *,
    override_forum: bool = False,
    override_category: bool = False,
) -> None:
    forum = payload.get("forum_normalized")
    category = payload.get("category")
    questions = payload.get("questions") or []
    for question in questions:
        if forum and (override_forum or not question.get("forum_normalized")):
            question["forum_normalized"] = forum
        if category and (override_category or not question.get("category")):
            question["category"] = category


def _should_force_forum_category(detected_forum: str, message: str) -> bool:
    if "грант" in detected_forum.casefold():
        return False
    normalized = message.casefold().replace("ё", "е")
    markers = (
        "положение",
        "документ",
        "трансфер",
        "питани",
        "возраст",
        "проезд",
        "прожив",
        "сертификат",
        "чат",
        "куратор",
        "заявк",
        "резерв",
        "отбор",
        "даты",
        "место",
        "программ",
        "участ",
    )
    return any(marker in normalized for marker in markers)


def _coerce_questions(value: object) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]

    result: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            item = {"text": str(item)}
        question = dict(item)
        question["topic"] = _coerce_optional_string(question.get("topic"))
        question["forum"] = _coerce_optional_string(question.get("forum"))
        question["forum"] = _normalize_forum_alias(_coerce_optional_string(question.get("forum")))
        question["forum_normalized"] = _normalize_forum_alias(
            _coerce_optional_string(question.get("forum_normalized"))
        )
        question["category"] = _normalize_category(question.get("category"))
        if question.get("forum_normalized") is None and question.get("forum"):
            question["forum_normalized"] = question["forum"]
        _drop_pseudo_forum_for_category(question)
        result.append(question)
    return result


def _normalize_category(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.casefold().replace("ё", "е").replace("_", " ")

    if any(
        word in normalized
        for word in (
            "offtopic",
            "оффтоп",
            "не по теме",
            "вне темы",
            "погода",
            "курс валют",
            "гороскоп",
        )
    ):
        return "offtopic"
    if any(word in normalized for word in ("форум", "мероприят", "событи")):
        return "форумы"
    if "грант" in normalized:
        return "гранты"
    if any(word in normalized for word in ("средств", "финанс", "отчет", "отчетност")):
        return "гранты"
    if "проект" in normalized and any(
        word in normalized for word in ("реализац", "эксперт", "оцен", "поддерж")
    ):
        return "гранты"
    if any(word in normalized for word in ("тех", "ошиб", "баг", "поддерж")):
        return "техподдержка"
    if any(word in normalized for word in ("фгаис", "платформ", "аккаунт", "кабинет", "регистрац")):
        return "платформа_фгаис"
    if any(
        word in normalized
        for word in ("навигац", "оператор", "обратн", "жалоб", "привет", "прощ")
    ):
        return "навигация"
    if any(word in normalized for word in ("общ", "другое", "прочее")):
        return "общее"
    return text


def _drop_pseudo_forum_for_category(payload: dict) -> None:
    category = payload.get("category")
    forum = str(payload.get("forum_normalized") or payload.get("forum") or "")
    if _is_platform_pseudo_forum(forum):
        payload["forum"] = None
        payload["forum_normalized"] = None
        if category in {None, "форумы"}:
            payload["category"] = "платформа_фгаис"
        return
    if category != "гранты" or not _is_grant_pseudo_forum(forum):
        return
    payload["forum"] = None
    payload["forum_normalized"] = None


def _drop_pseudo_forums(payload: dict) -> None:
    _drop_pseudo_forum_for_category(payload)
    for question in payload.get("questions") or []:
        if isinstance(question, dict):
            _drop_pseudo_forum_for_category(question)


def _is_grant_pseudo_forum(value: str | None) -> bool:
    normalized = str(value or "").casefold().replace("ё", "е")
    return normalized in {"грант", "гранты"} or "грант" in normalized and (
        "физичес" in normalized or "росмолод" in normalized
    )


def _is_platform_pseudo_forum(value: str | None) -> bool:
    normalized = str(value or "").casefold().replace("ё", "е").strip()
    if not normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "myrosmol.ru",
            "admin.myrosmol",
            "фгаис",
            "личный кабинет",
            "личн кабинет",
        )
    )


def _normalize_forum_alias(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    match_key = normalized.casefold().replace("ё", "е").replace("i", "и")
    if match_key in {"иволга", "иволга 2025", "иволге", "иволгу"}:
        return "Иволга"
    return normalized


def _coerce_optional_string(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _coerce_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]

    result: list[str] = []
    for item in value:
        if isinstance(item, bool):
            text = ""
        elif isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = next(
                (
                    str(item[key])
                    for key in ("text", "title", "topic", "name")
                    if item.get(key)
                ),
                "",
            )
        else:
            text = str(item)
        text = text.strip()
        if text:
            result.append(text)
    return result
