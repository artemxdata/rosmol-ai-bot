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
from src.security.operator_request import is_operator_request, operator_review_reason


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
        _ensure_deterministic_questions(payload, masked_message)
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
    is_generic_help = _is_generic_help_request(masked_message)
    if is_generic_help and not category:
        category = "общее"
    needs_forum_context = _needs_forum_context_clarification(masked_message)
    if needs_forum_context and not category:
        category = "форумы"
    complexity = _complexity_from_routing_hint(routing_hint)
    if _has_feedback_context(masked_message):
        complexity = Complexity.SIMPLE
    if _is_exact_fallback_intent_message(masked_message):
        complexity = Complexity.SIMPLE
    needs_application_context = _needs_application_context_clarification(masked_message)
    needs_clarification = (
        is_offtopic or is_generic_help or needs_application_context or needs_forum_context
    )
    clarification_question = (
        _build_clarification_question(
            is_generic_help=is_generic_help,
            needs_application_context=needs_application_context,
            needs_forum_context=needs_forum_context,
        )
        if needs_clarification and not is_offtopic
        else None
    )
    if needs_clarification:
        complexity = Complexity.SIMPLE
    if _should_force_simple_support_query(category, masked_message):
        complexity = Complexity.SIMPLE
    review_reason = operator_review_reason(masked_message)
    should_escalate = review_reason is not None
    if is_offtopic:
        should_escalate = False
        review_reason = None
    if should_escalate and not category:
        category = "техподдержка" if review_reason == "technical_issue" else "навигация"
    payload = {
        "category": category,
        "complexity": complexity.value,
        "questions": [],
        "needs_clarification": needs_clarification,
        "clarification_question": clarification_question,
        "is_offtopic": is_offtopic,
        "should_escalate": should_escalate,
        "escalation_reason": review_reason if should_escalate else None,
    }
    payload = _coerce_analysis_payload(payload)
    _apply_deterministic_forum(payload, original_message)
    _apply_forum_category_guardrail(payload, original_message)
    _ensure_deterministic_questions(payload, masked_message)
    analysis = QueryAnalysis.model_validate(payload)
    analysis = apply_session_context(analysis, masked_message, session)
    if not analysis.category and not analysis.forum_normalized:
        return None
    return analysis


def _is_operator_request(message: str) -> bool:
    return is_operator_request(message)


def _is_safe_offtopic(message: str) -> bool:
    normalized = message.casefold().replace("ё", "е")
    if ("контрольн" in normalized and "точк" in normalized) or any(
        marker in normalized for marker in ("окно отчета", "окно отчёта")
    ):
        return False
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
        "починить экран",
        "чинить телефон",
        "ремонт телефон",
        "сломался телефон",
        "починить айфон",
        "ремонт айфон",
        "закажи мне такси",
        "заказать такси",
        "такси до",
        "роллы",
        "пицц",
        "доставка еды",
        "исковое заявление",
        "в суд",
        "математик",
        "задачу по математике",
        "билет на матч",
        "билеты на матч",
        "матчи сборной",
        "матч сборной",
        "сборной россии",
        "футбольный матч",
        "хоккейный матч",
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


def _is_generic_help_request(message: str) -> bool:
    if _is_operator_request(message):
        return False
    normalized = message.casefold().replace("ё", "е").strip()
    normalized = re.sub(r"[^\w\s-]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return False
    words = re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
    exact_phrases = {
        "помогите",
        "помогите пожалуйста",
        "нужна помощь",
        "нужна консультация",
        "есть вопрос",
        "подскажите",
        "добрый день помогите",
        "здравствуйте помогите",
    }
    if normalized in exact_phrases:
        return True
    if len(words) <= 4 and any(
        marker in normalized
        for marker in (
            "помогите",
            "нужна помощь",
            "есть вопрос",
            "подскажите",
        )
    ):
        return True
    return False


def _build_clarification_question(
    *,
    is_generic_help: bool,
    needs_application_context: bool,
    needs_forum_context: bool,
) -> str:
    if needs_application_context:
        return (
            "Уточни, пожалуйста, о какой заявке речь: на конкретный форум/мероприятие "
            "или на грантовый конкурс?"
        )
    if needs_forum_context:
        return (
            "Уточни, пожалуйста, о каком форуме или мероприятии речь? "
            "У разных событий условия могут отличаться."
        )
    if is_generic_help:
        return (
            "Уточни, пожалуйста, вопрос: это про форум, мероприятие, ФГАИС "
            "«Молодёжь России» или грантовый конкурс?"
        )
    return "Уточни, пожалуйста, речь о форуме, мероприятии или грантовом конкурсе?"


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
    if detect_forums_from_text(message):
        return False
    has_application_request = "заяв" in normalized and any(
        marker in normalized
        for marker in (
            "как подать",
            "подать",
            "подач",
            "отправить",
            "оформить",
            "создать",
            "заполнить",
        )
    )
    has_cancel_request = "заяв" in normalized and any(
        marker in normalized
        for marker in ("отмен", "отозв", "удал")
    )
    if not (has_application_request or has_cancel_request):
        return False
    if any(
        marker in normalized
        for marker in (
            "грант",
            "проект",
            "фгаис",
            "росмолод",
        )
    ):
        return False
    return True


def _needs_forum_context_clarification(message: str) -> bool:
    normalized = message.casefold().replace("ё", "е")
    if detect_forums_from_text(message):
        return False
    if any(marker in normalized for marker in ("грант", "фгаис", "росмолод")):
        return False
    if _needs_participant_event_context_clarification(normalized):
        return True
    markers = (
        "фельдшер",
        "медпункт",
        "медицин",
        "питани",
        "прожив",
        "трансфер",
        "письмо-вызов",
        "письмо вызов",
        "сертификат",
        "справк",
        "памятк",
        "положение",
        "программа форума",
        "чат участников",
    )
    return any(marker in normalized for marker in markers)


def _needs_participant_event_context_clarification(normalized: str) -> bool:
    if "участ" not in normalized:
        return False
    participant_markers = (
        "я участник",
        "я участница",
        "стал участник",
        "стала участниц",
        "прошел отбор",
        "прошёл отбор",
        "прошла отбор",
        "подтвердил участие",
        "подтвердила участие",
    )
    next_step_markers = (
        "что дальше",
        "дальше что",
        "что теперь",
        "следующ",
        "какие дальнейшие",
        "что делать",
    )
    return any(marker in normalized for marker in participant_markers) and any(
        marker in normalized for marker in next_step_markers
    )


def _has_grant_project_context(normalized: str) -> bool:
    if "грант" in normalized:
        return True
    if "проект" not in normalized:
        return False
    if any(
        marker in normalized
        for marker in (
            "конкурс",
            "смет",
            "эксперт",
            "оцен",
            "номинац",
            "массов",
            "финанс",
            "средств",
            "соглашен",
            "отчет",
            "отчёт",
            "реализац",
            "поддержк",
        )
    ):
        return True
    if any(marker in normalized for marker in ("подать", "заявк", "отправ")):
        return not any(
            marker in normalized for marker in ("форум", "фестивал", "мероприят")
        )
    return False


def _has_ui_failure_context(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "не могу выбрать",
            "не могу отправить",
            "не могу сохранить",
            "не могу заполнить",
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
            "не выпада",
            "не отображ",
            "ошиб",
            "баг",
            "поле",
            "кнопк",
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
    if _has_grant_project_context(normalized):
        if _has_ui_failure_context(normalized):
            return "техподдержка"
        return "гранты"
    if _needs_application_context_clarification(normalized):
        return "форумы"
    if _needs_forum_context_clarification(normalized):
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
        and not any(
            marker in normalized
            for marker in (
                "не могу выбрать",
                "не могу отправить",
                "не могу сохранить",
                "не могу заполнить",
            )
        )
    ):
        return "гранты"
    if any(
        marker in normalized
        for marker in (
            "не могу выбрать",
            "не могу отправить",
            "не могу сохранить",
            "не могу заполнить",
        )
    ):
        return "техподдержка"
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
            "не могу выбрать",
            "не могу отправить",
            "не могу сохранить",
            "не могу заполнить",
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


def _ensure_deterministic_questions(payload: dict, message: str) -> None:
    deterministic_questions = _build_deterministic_questions(payload, message)
    if not deterministic_questions:
        return

    existing_questions = list(payload.get("questions") or [])
    if not existing_questions:
        payload["questions"] = deterministic_questions
        _propagate_question_defaults(payload, override_forum=True, override_category=True)
        return

    seen_topics = {
        str(question.get("topic") or "").strip()
        for question in existing_questions
        if isinstance(question, dict)
    }
    seen_texts = {
        str(question.get("text") or "").strip().casefold().replace("ё", "е")
        for question in existing_questions
        if isinstance(question, dict)
    }
    for question in deterministic_questions:
        topic = str(question.get("topic") or "").strip()
        text = str(question.get("text") or "").strip()
        normalized_text = text.casefold().replace("ё", "е")
        if topic and topic in seen_topics:
            continue
        if normalized_text and normalized_text in seen_texts:
            continue
        existing_questions.append(question)
        if topic:
            seen_topics.add(topic)
        if normalized_text:
            seen_texts.add(normalized_text)

    payload["questions"] = existing_questions
    _propagate_question_defaults(payload, override_forum=True, override_category=True)


def _build_deterministic_questions(payload: dict, message: str) -> list[dict]:
    category = payload.get("category")
    forum = payload.get("forum_normalized") or payload.get("forum")
    normalized = message.casefold().replace("ё", "е")
    if not forum and category not in {"гранты", "платформа_фгаис", "техподдержка"}:
        return []
    if category == "гранты" and _is_general_grant_info_query(normalized):
        return [
            {
                "text": "Какие условия участия в грантовом конкурсе?",
                "topic": "usloviya_i_sroki_uchastiya",
                "category": category,
                "forum_normalized": forum,
            }
        ]

    candidates = [
        (
            "o_meropriyatii",
            "Что это за форум?",
            (
                "в чем суть",
                "в чём суть",
                "суть форума",
                "о форуме",
                "про форум",
                "что за форум",
                "что такое",
                "тематика",
            ),
        ),
        (
            "kak_zaregistrirovatsya_na_fgais",
            "Как подать заявку или зарегистрироваться?",
            ("регистрац", "зарегистр"),
        ),
        (
            "podacha_zayavki_na_proekt",
            "Как подать заявку?",
            ("подать заяв", "подача заяв", "подать проект"),
        ),
        (
            "grant_reporting",
            "Как оформить отчётность по гранту?",
            ("отчет", "отчетност", "отчёт", "отчётност"),
        ),
        ("oplata_proezda", "Оплачивается ли проезд?", ("проезд", "дорог", "билет")),
        (
            "usloviya_prozhivaniya",
            "Какие условия проживания?",
            ("прожив", "размещен", "жить", "жиль"),
        ),
        (
            "otkaz_ot_uchastiya",
            "Что делать, если не получается поехать?",
            (
                "не могу поехать",
                "не смогу поехать",
                "отказ",
                "отказаться",
                "отменить участие",
                "потом отказаться",
            ),
        ),
        (
            "vnesti_izmeneniya_v_zayavku",
            "Можно ли внести изменения в заявку?",
            ("изменить заявку", "изменить заявк", "внести изменения в заявк", "поменять заявк"),
        ),
        ("vozrastnye_ogranicheniya", "Какие возрастные ограничения?", ("возраст", "лет")),
        (
            "transfer_do_mesta_provedeniya_meropriyatiya",
            "Будет ли трансфер?",
            ("трансфер", "шаттл"),
        ),
        ("pismo_vyzov", "Где получить письмо-вызов?", ("письмо-вызов", "письмо вызов")),
        ("kogda_budet_sertifikat", "Когда будет сертификат?", ("сертификат",)),
        (
            "spisok_veschey_i_dokumentov",
            "Какие документы или вещи нужно взять с собой?",
            (
                "какие документы",
                "документы нужны",
                "документы брать",
                "документы взять",
                "список документов",
                "список вещей",
                "вещ",
                "памятк",
                "одежд",
                "снаряж",
                "взять с собой",
                "рюкзак",
                "гигиен",
            ),
        ),
        (
            "dokumenty_meropriyatiya",
            "Где найти положение или документы мероприятия?",
            (
                "положен",
                "регламент",
                "где документы",
                "где документ",
                "документы мероприятия",
                "документы форума",
                "документ о мероприятии",
                "документ по конкурсу",
            ),
        ),
        ("rezultaty_rm", "Где посмотреть результаты отбора?", ("результат", "отбор", "списк")),
        (
            "informaciya_o_ploschadke_pitanie_pite",
            "Как организовано питание?",
            ("питани", "питат", "покорм", "еда", "пить", "вода", "меню"),
        ),
        (
            "informaciya_o_ploschadke_medicina",
            "Есть ли медицинская помощь?",
            ("медицин", "медпункт", "здоров"),
        ),
        ("uchastniki_s_ovz", "Можно ли участвовать с ОВЗ?", ("овз", "ограниченн")),
        (
            "inostrannye_grazhdane",
            "Могут ли участвовать иностранные граждане?",
            ("иностран", "граждан"),
        ),
        ("rosmolodezh_granty", "Есть ли грантовый конкурс?", ("грант", "грантов")),
        ("trebovaniya_po_dress_kodu", "Есть ли требования по дресс-коду?", ("дресс", "одежд")),
        (
            "poseschenie_festivalya_s_detmi",
            "Можно ли прийти с ребёнком или детьми?",
            ("ребен", "ребён", "деть", "дети", "детьми"),
        ),
        (
            "programma_i_artisty",
            "Где посмотреть программу и артистов?",
            ("программ", "артист"),
        ),
        ("programma_foruma", "Где посмотреть программу?", ("программ", "расписан")),
        (
            "daty_nachala_meropriyatiya",
            "Когда начинается мероприятие?",
            (
                "когда",
                "дата",
                "даты",
                "срок",
                "начина",
                "место проведения",
                "где проходит",
                "где пройдет",
                "где пройдёт",
                "где будет проходить",
                "где проводится",
                "адрес площадки",
                "локац",
            ),
        ),
        ("dobavlenie_v_chat_meropriyatiya", "Когда добавят в чат мероприятия?", ("добав", "чат")),
        (
            "podtverzhdenie_uchastiya_i_org_momenty",
            "Что с подтверждением участия?",
            ("подтвержд", "подтвердил"),
        ),
        ("cifrovaya_nedelya", "Что такое цифровая неделя?", ("цифровая неделя",)),
        ("gde_nayti_id_profilya", "Где найти ID профиля?", ("id проф", "айди проф", "ид проф")),
        ("vernut_denezhnye_sredstva", "Как вернуть грантовые средства?", ("вернуть", "средств")),
    ]
    questions: list[dict] = []
    seen_topics: set[str] = set()
    for topic, text, markers in candidates:
        if topic in seen_topics:
            continue
        if topic == "rosmolodezh_granty" and category == "гранты":
            continue
        if topic == "daty_nachala_meropriyatiya" and _has_personal_date_without_event_context(
            normalized
        ):
            continue
        if topic == "podtverzhdenie_uchastiya_i_org_momenty" and _has_decline_context(
            normalized
        ):
            continue
        if any(marker in normalized for marker in markers):
            questions.append(
                {
                    "text": text,
                    "topic": topic,
                    "category": category,
                    "forum_normalized": forum,
                }
            )
            seen_topics.add(topic)
    if questions:
        return questions
    if _is_general_forum_info_query(message, str(forum or "")):
        return [
            {
                "text": "Что это за форум?",
                "topic": "o_meropriyatii",
                "category": category,
                "forum_normalized": forum,
            }
        ]
    return [
        {
            "text": message,
            "topic": None,
            "category": category,
            "forum_normalized": forum,
        }
    ]


def _is_general_forum_info_query(message: str, forum: str) -> bool:
    if not forum:
        return False
    normalized = _normalize_forum_info_match_text(message)
    explicit_markers = (
        "расскажи",
        "что такое",
        "что за",
        "подробнее",
        "информация",
        "о форуме",
        "про форум",
        "суть форум",
        "тематика",
    )
    if any(marker in normalized for marker in explicit_markers):
        return True

    words = re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
    forum_words = set(
        re.findall(r"[\w-]+", _normalize_forum_info_match_text(forum), flags=re.UNICODE)
    )
    filler_words = {
        "форум",
        "про",
        "о",
        "об",
        "это",
        "что",
        "такое",
        "за",
        "расскажи",
        "подскажи",
        "пожалуйста",
    }
    meaningful_words = [
        word for word in words if word not in forum_words and word not in filler_words
    ]
    return len(words) <= 5 and not meaningful_words


def _normalize_forum_info_match_text(value: str) -> str:
    latin_to_cyrillic_lookalikes = str.maketrans(
        {
            "a": "а",
            "e": "е",
            "o": "о",
            "p": "р",
            "c": "с",
            "x": "х",
            "y": "у",
            "k": "к",
            "m": "м",
            "h": "н",
            "t": "т",
            "b": "в",
        }
    )
    return str(value or "").casefold().replace("ё", "е").translate(
        latin_to_cyrillic_lookalikes
    )


def _is_general_grant_info_query(normalized: str) -> bool:
    if "грант" not in normalized:
        return False
    if any(
        marker in normalized
        for marker in (
            "отчет",
            "отчёт",
            "соглашени",
            "возврат",
            "вернуть",
            "отклони",
            "обратн",
            "результат",
            "заявк",
            "проект",
            "смет",
        )
    ):
        return False
    return any(
        marker in normalized
        for marker in (
            "физлиц",
            "физических лиц",
            "услов",
            "срок",
            "участв",
            "кто может",
            "для кого",
        )
    )


def _has_personal_date_without_event_context(normalized: str) -> bool:
    if "дата рождения" not in normalized and "[дата]" not in normalized:
        return False
    return not any(
        marker in normalized
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


def _has_decline_context(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "отказ",
            "отказаться",
            "отозвать",
            "отменить участие",
            "не могу поехать",
            "не смогу поехать",
            "не могу приехать",
            "не смогу приехать",
            "не могу посетить",
            "не смогу посетить",
            "подтвердил участие",
            "подтвердила участие",
        )
    )


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
