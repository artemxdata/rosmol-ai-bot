from __future__ import annotations

import re
from time import perf_counter

from src.graph.context import (
    RECENT_CONTEXT_TURNS,
    apply_session_context,
    build_contextual_message,
)
from src.graph.state import BotState
from src.kb.forum_registry import detect_forums_from_text
from src.llm.cascade import select_analyzer_model
from src.llm.json_utils import parse_llm_json
from src.llm.prompts import QUERY_ANALYZER_SYSTEM, build_analyzer_user
from src.models import Complexity, QueryAnalysis
from src.security.operator_request import (
    is_operator_request,
    operator_review_reason,
)

BOT_CAPABILITIES_RESPONSE = (
    "Я ИИ-помощник Росмолодёжи. Отвечаю по подтверждённой базе знаний о форумах, "
    "мероприятиях, ФГАИС «Молодёжь России» и грантах. Если вопрос вне этих тем, "
    "я прямо скажу об этом, а опасную ситуацию или явный запрос оператора передам "
    "специалисту."
)
GREETING_RESPONSE = (
    "Привет! Я помогу разобраться с форумами и мероприятиями Росмолодёжи, "
    "ФГАИС «Молодёжь России» и грантами. Напиши, что именно тебя интересует."
)
FEEDBACK_RESPONSE = (
    "Расскажи, пожалуйста, что именно хочешь оценить: ответ бота, работу сервиса, "
    "мероприятие или сотрудника. Опиши ситуацию без персональных данных — я передам "
    "обратную связь по назначению."
)
APPLICATION_SUCCESS_RESPONSE = (
    "Отлично, заявка подана! Если понадобится, я помогу разобраться с дальнейшими "
    "шагами по конкретному форуму, мероприятию или грантовому конкурсу."
)
ACCOUNT_CHECK_RESPONSE = (
    "Проверь вход на https://myrosmol.ru/auth/login. Если пароль не помнишь, нажми "
    "«Восстановить пароль» и укажи электронную почту, которую мог использовать при "
    "регистрации. Письмо для восстановления покажет, что к этой почте привязан аккаунт. "
    "Если письма нет, проверь папку «Спам» и правильность адреса."
)
SOURCE_BOUNDARY_RESPONSE = (
    "Я отвечаю только по подтверждённым данным из базы Росмолодёжи. Если уточнишь, "
    "какие именно условия или какой шаг тебя интересует, я проверю это точнее."
)
GRANT_CONTEXT_RESPONSE = (
    "Понял, речь о грантовом конкурсе Росмолодёжи. Я могу подсказать условия участия, "
    "подачу заявки, требования к проекту, команде, смете и отчётности. Напиши, какой "
    "именно этап тебя интересует."
)


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
            allow_unknown_clarification=True,
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
    *,
    allow_unknown_clarification: bool = False,
) -> QueryAnalysis | None:
    category = _infer_category_from_message(original_message)
    exact_grant_questions = _build_exact_grant_questions(
        _normalize_for_session(original_message)
    )
    is_collaboration = _is_collaboration_request(
        _normalize_for_session(original_message)
    )
    if exact_grant_questions:
        category = "гранты"
    elif is_collaboration:
        category = "общее"
    if (
        not category
        and _is_project_team_question(_normalize_for_session(original_message))
        and _session_mentions(session, ("идея проекта", "команда проекта", "грант"))
    ):
        category = "гранты"
    # Names can be masked before analysis (for example, politicians). Scope routing is
    # local and deterministic, so use the original text here without exposing it to LLMs.
    is_offtopic = is_safe_offtopic_message(original_message)
    if is_offtopic:
        category = "offtopic"
    interaction_response = None if is_offtopic else _bot_interaction_response(original_message)
    ambiguous_response = (
        None
        if is_offtopic or interaction_response
        else _ambiguous_short_request_response(original_message, session)
    )
    if interaction_response and not category:
        category = "общее"
    if ambiguous_response and not category:
        category = "общее"
    is_generic_help = _is_generic_help_request(original_message)
    if is_generic_help and not category:
        category = "общее"
    needs_forum_context = _needs_forum_context_clarification(original_message)
    if needs_forum_context and not category:
        category = "форумы"
    complexity = _complexity_from_routing_hint(routing_hint)
    if _has_feedback_context(original_message):
        complexity = Complexity.SIMPLE
    if _is_exact_fallback_intent_message(original_message):
        complexity = Complexity.SIMPLE
    needs_application_context = _needs_application_context_clarification(original_message)
    needs_clarification = bool(
        is_offtopic
        or interaction_response
        or ambiguous_response
        or is_generic_help
        or needs_application_context
        or needs_forum_context
    )
    clarification_question = None
    if needs_clarification and not is_offtopic:
        clarification_question = interaction_response or ambiguous_response or (
            _build_clarification_question(
                is_generic_help=is_generic_help,
                needs_application_context=needs_application_context,
                needs_forum_context=needs_forum_context,
            )
        )
    if needs_clarification:
        complexity = Complexity.SIMPLE
    if _should_force_simple_support_query(category, original_message):
        complexity = Complexity.SIMPLE
    review_reason = operator_review_reason(original_message) or _session_followup_review_reason(
        original_message,
        session,
    )
    if exact_grant_questions and review_reason == "technical_issue":
        review_reason = None
    should_escalate = review_reason is not None
    if is_offtopic:
        should_escalate = False
        review_reason = None
    if should_escalate and not category:
        category = "техподдержка" if review_reason == "technical_issue" else "навигация"
    if (
        allow_unknown_clarification
        and not category
        and not detect_forums_from_text(original_message)
        and not should_escalate
    ):
        category = "общее"
        complexity = Complexity.SIMPLE
        needs_clarification = True
        clarification_question = _build_clarification_question(
            is_generic_help=True,
            needs_application_context=False,
            needs_forum_context=False,
        )
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
    _ensure_deterministic_questions(payload, original_message)
    analysis = QueryAnalysis.model_validate(payload)
    analysis = apply_session_context(analysis, masked_message, session)
    if not analysis.category and not analysis.forum_normalized:
        return None
    return analysis


def _is_operator_request(message: str) -> bool:
    return is_operator_request(message)


def is_safe_offtopic_message(message: str) -> bool:
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
    has_known_forum = bool(detect_forums_from_text(message))
    has_in_scope_context = has_known_forum or any(
        marker in normalized for marker in in_scope_markers
    )
    if has_in_scope_context and _has_actionable_support_request(normalized):
        return False

    conversational_offtopic_markers = (
        "путин",
        "трамп",
        "трам ",
        "нетаньяху",
        "израиль или иран",
        "иран или израиль",
        "чей крым",
        "за путина",
        "против путина",
        "политическ",
        "выборы президента",
        "какой президент",
        "ахмат сила",
        "деньги пилите",
        "шарага",
        "ты туп",
        "ты дурак",
        "такой дурак",
        "такой тупой",
        "дурацкий бот",
        "тупой бот",
        "глупый бот",
        "ты глуп",
        "ты бесполез",
        "ничего не умеешь",
        "чат gpt лучше",
        "чатгпт лучше",
        "chatgpt лучше",
        "чат gpt и то",
        "лучше поспрашиваю алису",
        "зачем нужен такой",
        "сосал",
        "дом труба шатал",
        "поговори со мной",
        "я тебя люблю",
        "кто виноват",
        "доколе",
        "мистер поттер",
        "златоцвет",
        "настойку полыни",
    )
    if any(marker in normalized for marker in conversational_offtopic_markers):
        return True

    if has_in_scope_context:
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
        "домашк",
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
        "проблемы с работодателем",
        "жилье молодому специалисту",
        "жильё молодому специалисту",
        "меня отчисляют",
        "материальная помощь",
        "льгот",
        "выплат",
        "роскомнадзор",
        "заблокировать телеграм",
        "жалоба на телеграм канал",
        "пожаловаться на телеграм канал",
    )
    return any(marker in normalized for marker in offtopic_markers)


def _has_actionable_support_request(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "как ",
            "как?",
            "где ",
            "когда ",
            "куда ",
            "можно ли",
            "нужно ли",
            "что нужно",
            "что делать",
            "подскаж",
            "расскаж",
            "помог",
            "хочу попасть",
            "хочу участвовать",
            "хочу поучаствовать",
            "хочу подать",
            "зарегистр",
            "регистрац",
            "подать заяв",
            "не работает",
            "не получается",
            "не могу",
            "не груз",
            "не откры",
            "не пуска",
            "не приш",
            "не вижу",
            "пропал",
            "вылет",
            "завис",
            "висит",
            "ошиб",
        )
    )


def _bot_interaction_response(message: str) -> str | None:
    normalized = message.casefold().replace("ё", "е")
    normalized = re.sub(r"[^\w\s-]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return None

    greeting_phrases = {
        "привет",
        "здравствуйте",
        "добрый день",
        "добрый вечер",
        "как дела",
        "привет можете помочь",
        "привет можешь помочь",
    }
    if normalized in greeting_phrases:
        return GREETING_RESPONSE
    if normalized in {"пока", "до свидания", "до встречи"}:
        return "До встречи! Если появится вопрос по Росмолодёжи, я помогу."

    bot_markers = (
        "почему не можешь помочь",
        "что ты умеешь",
        "что ты вообще умеешь",
        "что умеешь",
        "что вы можете подсказать",
        "что вы вообще можете подсказать",
        "ты нейросеть",
        "ты ии",
        "ты искусственный интеллект",
        "ты исскуственный интеллект",
        "ты исскусственный интеллект",
        "ты чат gpt",
        "ты чат джипити",
        "ты учишься",
        "ты умеешь обучаться",
        "какие вопросы ты понимаешь",
        "как задавать тебе вопросы",
        "ты полезный",
        "какой в тебе смысл",
        "ты хоть что-то можешь",
        "вы вообще отвечаете людям",
        "зачем вы тогда нужны",
        "ты ответишь",
    )
    if any(marker in normalized for marker in bot_markers):
        return BOT_CAPABILITIES_RESPONSE
    if any(
        marker in normalized
        for marker in (
            "а ты сам не знаешь",
            "ты сам не знаешь",
            "сам не знаешь",
        )
    ):
        return SOURCE_BOUNDARY_RESPONSE
    if any(
        marker in normalized
        for marker in (
            "оставить обратную связь",
            "хочу дать фидбек",
            "дать фидбек",
        )
    ):
        return FEEDBACK_RESPONSE
    return None


def _ambiguous_short_request_response(message: str, session: object | None) -> str | None:
    normalized = message.casefold().replace("ё", "е")
    normalized = re.sub(r"[^\w\s-]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not _has_session_context(session) and normalized in {
        "старт",
        "start",
        "назад",
        "регистрация",
        "регистрациях",
        "расписание",
    }:
        return (
            "Уточни, пожалуйста, что именно тебя интересует и о каком форуме, "
            "мероприятии, разделе ФГАИС или грантовом конкурсе речь."
        )
    if _is_application_success(normalized):
        return APPLICATION_SUCCESS_RESPONSE
    if "это не форум а грант" in normalized:
        return GRANT_CONTEXT_RESPONSE
    if "есть ли у меня аккаунт" in normalized:
        return ACCOUNT_CHECK_RESPONSE
    if any(
        marker in normalized
        for marker in (
            "это не форум а программа",
            "это не на грант заявка",
            "это не грант",
        )
    ):
        return (
            "Понял, предыдущая тема не подходит. Уточни точное название программы или "
            "мероприятия и что произошло с заявкой — я проверю нужный раздел базы."
        )
    if "точно такой грант есть" in normalized:
        return (
            "Не нахожу подтверждения гранта с таким точным названием. Уточни официальное "
            "название конкурса или проверь написание — я не буду подменять его похожим."
        )
    if "где там про условия" in normalized:
        return (
            "Уточни, пожалуйста, какие условия тебя интересуют: возраст и отбор, проезд, "
            "проживание, питание или документы?"
        )
    if normalized in {"нет это от вас письмо", "это от вас письмо"}:
        return (
            "Понял. Уточни, пожалуйста, тему письма и что в нём просят сделать, без "
            "персональных данных и номера заявки. Тогда я проверю нужную инструкцию."
        )
    if any(
        marker in normalized
        for marker in ("лк на вашем сайте", "личный кабинет на вашем сайте")
    ):
        return (
            "Уточни, пожалуйста, какой сайт открыт: ФГАИС «Молодёжь России» "
            "(myrosmol.ru), Добро.рф или другой сервис, и на каком шаге возникает проблема."
        )
    if any(marker in normalized for marker in ("просто тупит", "тупит и неудоб", "просто неудоб")):
        if _session_mentions(session, ("кабинет", "личный кабинет", "лк", "сайт")):
            return (
                "Уточни, пожалуйста, что именно не работает в личном кабинете: вход, "
                "профиль, заявка или карточка мероприятия, и что происходит после нажатия."
            )
    if _looks_like_unknown_name_after_clarification(normalized, session):
        return (
            "Не нахожу точного подтверждённого названия. Проверь написание или уточни, "
            "это форум, программа, мероприятие или грантовый конкурс."
        )
    if _has_session_context(session):
        return None
    if "пришло" in normalized and "письм" in normalized and any(
        marker in normalized for marker in ("не понимаю", "неясно", "что делать")
    ):
        return (
            "Уточни, пожалуйста, от кого пришло письмо и что именно в нём непонятно. "
            "Не отправляй сюда персональные данные, номер заявки или скриншот с ними."
        )
    if "вложен" in normalized and "контекст" in normalized:
        return (
            "Уточни, пожалуйста, текстом: что изображено во вложении "
            "и с чем нужна помощь."
        )
    if (
        len(normalized.split()) <= 16
        and "анкет" in normalized
        and any(
        marker in normalized for marker in ("фигн", "проблем", "непонят")
        )
    ):
        return (
            "Уточни, пожалуйста, о какой анкете речь и на каком шаге возникла проблема: "
            "вход, заполнение, сохранение или отправка?"
        )
    if "кабинет" in normalized and any(
        marker in normalized for marker in ("задолб", "тупит", "неудоб", "фигн")
    ):
        return (
            "Уточни, пожалуйста, что именно происходит в личном кабинете и на каком "
            "шаге: вход, заполнение профиля, заявка или карточка мероприятия?"
        )
    if "участвовал раньше" in normalized and "заново регистр" in normalized:
        return (
            "Уточни, пожалуйста, речь о повторной регистрации аккаунта ФГАИС или о "
            "подаче новой заявки на конкретное мероприятие?"
        )
    if (
        not detect_forums_from_text(message)
        and re.search(r"\b\d{2}\b", normalized)
        and any(
        marker in normalized for marker in ("не поздно", "ваших программ", "участвовать")
        )
    ):
        return (
            "Для разных программ действуют разные возрастные ограничения. Уточни, "
            "пожалуйста, название форума или мероприятия — я проверю точные условия."
        )
    technical_phrases = {
        "ошибка",
        "не работает",
        "не пускает",
        "не проходит",
        "не прикрепляется",
        "не отображается",
        "там ошибка",
        "у вас баг",
        "там ошибка но я не понял какая",
        "я что-то нажал и теперь непонятно что дальше",
    }
    if normalized in technical_phrases:
        return (
            "Уточни, пожалуйста, где именно возникла проблема и что видишь на экране: "
            "вход в ФГАИС, профиль, заявка или карточка мероприятия?"
        )
    if normalized in {"что делать", "а дальше что", "что дальше"}:
        return (
            "Уточни, пожалуйста, что произошло и о каком форуме, мероприятии, "
            "гранте или разделе ФГАИС идёт речь."
        )
    return None


def _is_application_success(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "получилось подать заявку",
            "получилось отправить заявку",
            "ура я подал заявку",
            "ура я подала заявку",
            "заявку успешно подал",
            "заявку успешно подала",
        )
    )


def _looks_like_unknown_name_after_clarification(
    normalized: str,
    session: object | None,
) -> bool:
    if not normalized or detect_forums_from_text(normalized):
        return False
    words = re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
    if not 1 <= len(words) <= 4:
        return False
    if all(word.isdigit() for word in words):
        return False
    last_bot = _last_session_message(session, "bot")
    return any(
        marker in last_bot
        for marker in (
            "уточни",
            "точное название",
            "название форума",
            "название программы",
            "о какой заявке",
        )
    )


def _session_followup_review_reason(message: str, session: object | None) -> str | None:
    normalized = _normalize_for_session(message)
    if not (
        "заяв" in normalized
        and any(marker in normalized for marker in ("что по", "что с", "а по"))
    ):
        return None
    if _session_mentions(
        session,
        (
            "не понимаю прошел ли",
            "не понимаю прошёл ли",
            "прошел ли я",
            "прошёл ли я",
            "проверить статус",
            "статус заявки",
        ),
    ):
        return "personal_status"
    return None


def _session_mentions(session: object | None, markers: tuple[str, ...]) -> bool:
    for field in ("user", "bot"):
        if any(marker in _last_session_message(session, field) for marker in markers):
            return True
    return False


def _last_session_message(session: object | None, field: str) -> str:
    messages = getattr(session, "last_messages", None) or []
    for item in reversed(messages[-RECENT_CONTEXT_TURNS:]):
        value = _normalize_for_session(str(item.get(field) or ""))
        if value:
            return value
    return ""


def _normalize_for_session(value: str) -> str:
    normalized = str(value or "").casefold().replace("ё", "е")
    normalized = re.sub(r"[^\w\s-]+", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def _has_session_context(session: object | None) -> bool:
    if session is None:
        return False
    return bool(
        getattr(session, "forum_context", None)
        or getattr(session, "last_messages", None)
        or getattr(session, "pending_clarification", None)
    )


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
        "у меня вопрос но не знаю к вам ли это",
        "я запутался куда обратиться",
        "я запутался куда мне обратиться",
        "не понимаю с чего начать",
        "мне нужна помощь по молодежной теме",
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
    has_vague_application_problem = "заяв" in normalized and any(
        marker in normalized
        for marker in (
            "проблем",
            "какая-то",
            "какая то",
            "что-то не так",
            "что то не так",
            "непонят",
            "вопрос по поводу",
            "вопрос по заяв",
            "по поводу заяв",
        )
    )
    has_application_request = "заяв" in normalized and any(
        marker in normalized
        for marker in (
            "как подать",
            "подать",
            "подач",
            "отправить",
            "не отправ",
            "оформить",
            "создать",
            "заполнить",
            "исправить",
            "изменить",
            "поменять",
            "редактировать",
        )
    )
    has_cancel_request = "заяв" in normalized and any(
        marker in normalized
        for marker in ("отмен", "отозв", "удал")
    )
    has_unspecified_selection_problem = any(
        marker in normalized
        for marker in (
            "мне отказали",
            "не прошел отбор",
            "не прошёл отбор",
            "не прошла отбор",
        )
    )
    if not (
        has_application_request
        or has_cancel_request
        or has_vague_application_problem
        or has_unspecified_selection_problem
    ):
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


def _is_about_rosmolodezh_query(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "что такое росмолод",
            "кто такие росмолод",
            "чем занимается росмолод",
            "какие задачи у росмолод",
        )
    )


def _is_application_status_explanation_query(normalized: str) -> bool:
    if "заяв" not in normalized or "статус" not in normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "что означает",
            "что означают",
            "что значит",
            "что значат",
            "расшифр",
            "какие бывают",
            "список статус",
        )
    )


def _is_participation_confirmation_workflow_query(normalized: str) -> bool:
    if "участи" not in normalized or "подтверд" not in normalized:
        return False
    if any(
        marker in normalized
        for marker in (
            "не могу поехать",
            "не смогу поехать",
            "отказаться от участия",
            "отменить участие",
        )
    ):
        return False
    return any(
        marker in normalized
        for marker in (
            "как подтверд",
            "где подтверд",
            "нужно подтверд",
            "необходимо подтверд",
            "подтвердить участие",
        )
    )


def _is_generic_platform_workflow_query(normalized: str) -> bool:
    return _is_application_status_explanation_query(
        normalized
    ) or _is_participation_confirmation_workflow_query(normalized)


def _is_account_deletion_information_query(normalized: str) -> bool:
    if "аккаунт" not in normalized or "удал" not in normalized:
        return False
    if any(
        marker in normalized
        for marker in (
            "не могу",
            "не получается",
            "не удаляется",
            "ошиб",
            "кнопка не работает",
        )
    ):
        return False
    return True


def _is_account_merge_query(normalized: str) -> bool:
    has_account_context = "аккаунт" in normalized or "учетн" in normalized
    has_transfer_context = any(
        marker in normalized
        for marker in (
            "объедин",
            "обьедин",
            "перенести данные",
            "перенос данных",
            "старой почт",
            "старому аккаунт",
            "новый аккаунт",
        )
    )
    return has_account_context and has_transfer_context


def _needs_forum_context_clarification(message: str) -> bool:
    normalized = message.casefold().replace("ё", "е")
    if _is_generic_platform_workflow_query(normalized):
        return False
    if detect_forums_from_text(message):
        return False
    if any(marker in normalized for marker in ("грант", "фгаис", "росмолод")):
        return False
    has_generic_event = any(
        marker in normalized for marker in ("форум", "мероприят", "фестивал")
    )
    asks_event_details = any(
        marker in normalized
        for marker in (
            "когда",
            "где",
            "дат",
            "срок",
            "программ",
            "афиш",
            "артист",
            "исполнител",
            "кто выступ",
            "выступлен",
        )
    )
    if has_generic_event and asks_event_details:
        return True
    has_unanchored_age_or_family_question = any(
        marker in normalized
        for marker in (
            "до какого возраста",
            "возрастные огранич",
            "во сколько лет",
            "можно с ребен",
            "можно с ребён",
            "взять ребен",
            "взять ребён",
            "прийти с ребен",
            "прийти с ребён",
            "регистрировать ребен",
            "регистрировать ребён",
            "регистрировать дет",
            "регистрация для детей",
            "билет для детей",
            "билет ребенку",
            "билет ребёнку",
        )
    )
    if has_unanchored_age_or_family_question:
        return True
    has_unanchored_application_state = "заяв" in normalized and any(
        marker in normalized
        for marker in (
            "одобр",
            "подтверд",
            "рассмотр",
            "резерв",
            "статус",
            "прошел",
            "прошёл",
            "прошла",
        )
    )
    if has_unanchored_application_state:
        return True
    if "обучен" in normalized and any(
        marker in normalized
        for marker in ("как зайти", "как попасть", "зарегистр", "не могу", "не получается")
    ):
        return True
    if _needs_participant_event_context_clarification(normalized):
        return True
    if any(
        marker in normalized
        for marker in (
            "хочу попасть на форум",
            "хочу попасть на мероприятие",
            "как попасть на форум",
            "как туда попасть",
            "хочу на форум",
            "хочу на мероприятие",
            "вписаться в движ",
            "залететь на форум",
            "залететь на программу",
            "стать участником форума",
            "стать участницей форума",
        )
    ):
        return True
    markers = (
        "фельдшер",
        "медпункт",
        "медицин",
        "питани",
        "прожив",
        "трансфер",
        "проезд",
        "письмо-вызов",
        "письмо вызов",
        "сертификат",
        "справк",
        "памятк",
        "положение",
        "программа форума",
        "афиш",
        "артист",
        "исполнител",
        "кто выступ",
        "выступлен",
        "чат участников",
        "отменить участие",
        "отказаться от участия",
        "отозвать заявку",
        "аккредитац",
        "съем",
        "съём",
        "сми",
        "пресс",
        "видео",
        "билет",
        "max",
        "макс",
    )
    return any(marker in normalized for marker in markers)


def _needs_participant_event_context_clarification(normalized: str) -> bool:
    if not any(marker in normalized for marker in ("участ", "подал", "подалась", "подался")):
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
        "я подался",
        "я подалась",
        "я подал заявку",
        "я подала заявку",
    )
    next_step_markers = (
        "что дальше",
        "дальше что",
        "дальше че",
        "дальше чо",
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
            "команд",
            "идея проект",
            "идея для проект",
            "нет опыта",
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
    if _is_about_rosmolodezh_query(normalized):
        return "общее"
    if _is_generic_platform_workflow_query(normalized):
        return "платформа_фгаис"
    if _is_account_deletion_information_query(normalized):
        return "платформа_фгаис"
    if _is_account_merge_query(normalized):
        return "платформа_фгаис"
    if _is_sport_recommendation_request(normalized):
        return "платформа_фгаис"
    if _has_staff_feedback_context(normalized):
        return "навигация"
    if normalized.startswith("технические вопросы.") or "технические вопросы" in normalized:
        return "техподдержка"
    if normalized.startswith("рекомендации.") or "рекомендации" in normalized:
        return "общее"
    if _is_forum_discovery_request(normalized):
        return "общее"
    if any(marker in normalized for marker in ("сотруднич", "партнерств", "партнёрств")):
        return "общее"
    if any(marker in normalized for marker in ("возможности бота", "abilities", "что умеешь")):
        return "общее"
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


def _is_forum_discovery_request(message: str) -> bool:
    normalized = message.casefold().replace("ё", "е")
    if any(
        marker in normalized
        for marker in (
            "какие у меня вообще есть возможности",
            "куда вообще можно податься",
            "хочу развиваться",
            "не понимаю куда двигаться",
            "есть ли у вас что-то полезное",
            "куда-то подаваться",
            "начать участвовать",
            "молодежной жизни",
            "молодёжной жизни",
            "нет опыта и портфолио",
            "нет сильного опыта и портфолио",
            "не знаю что мне интересно",
            "не знаю, что мне интересно",
            "для моего региона",
            "куда мне пойти участвовать",
            "куда пойти участвовать",
            "нет возможности надолго уезжать",
            "нет возможности уезжать",
        )
    ):
        return True
    if any(
        marker in normalized
        for marker in (
            "есть что-то",
            "есть что то",
            "что-то проводится",
            "что то проводится",
        )
    ) and any(marker in normalized for marker in ("для ", "в адыге", "в анадыр")):
        return True
    has_forum_context = "форум" in normalized or "мероприят" in normalized
    return has_forum_context and any(
        marker in normalized
        for marker in (
            "какие есть",
            "что есть",
            "что сейчас есть",
            "че по форум",
            "чё по форум",
            "список форум",
            "все форум",
            "выбрать форум",
        )
    )


def _is_sport_recommendation_request(normalized: str) -> bool:
    if not any(marker in normalized for marker in ("спортсмен", "спортсменка", "спорт")):
        return False
    return any(
        marker in normalized
        for marker in (
            "где я могу поучаствовать",
            "где можно поучаствовать",
            "куда податься",
            "что подойдет",
            "что подойдёт",
        )
    )


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
    if _is_collaboration_request(message.casefold().replace("ё", "е")):
        return
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
    if _is_collaboration_request(message.casefold().replace("ё", "е")):
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
    if _is_about_rosmolodezh_query(normalized):
        return [
            {
                "text": "Что такое Росмолодёжь и чем она занимается?",
                "topic": "chto_takoe_rosmolodezh",
                "category": "общее",
                "forum_normalized": None,
            }
        ]
    if _is_account_deletion_information_query(normalized):
        return [
            {
                "text": "Кто и как может удалить аккаунт ФГАИС?",
                "topic": "udalenie_akkaunta",
                "category": "платформа_фгаис",
                "forum_normalized": None,
            }
        ]
    if _is_account_merge_query(normalized):
        return [
            {
                "text": "Как объединить старый и новый аккаунты ФГАИС?",
                "topic": "obedinenie_akkauntov",
                "category": "платформа_фгаис",
                "forum_normalized": None,
            }
        ]
    if _is_application_status_explanation_query(normalized):
        return [
            {
                "text": "Что означают статусы заявки в ФГАИС?",
                "topic": "statusy_zayavok",
                "category": "платформа_фгаис",
                "forum_normalized": None,
            }
        ]
    if _is_participation_confirmation_workflow_query(normalized):
        return [
            {
                "text": "Как подтвердить участие после одобрения заявки на форум?",
                "topic": "podtverzhdenie_uchastiya_v_forume",
                "category": "платформа_фгаис",
                "forum_normalized": None,
            }
        ]
    grant_questions = _build_exact_grant_questions(normalized)
    if grant_questions:
        return grant_questions
    if category == "гранты" and _is_grant_report_review_timing_query(normalized):
        return [
            {
                "text": "Сколько времени проверяют грантовый отчёт?",
                "topic": "proverka_otcheta",
                "category": "гранты",
                "forum_normalized": None,
            }
        ]
    if _is_sport_recommendation_request(normalized):
        return [
            {
                "text": "Какие мероприятия подойдут любителям спорта?",
                "topic": "rekomendacii_sport",
                "category": "платформа_фгаис",
                "forum_normalized": None,
            }
        ]
    if _is_project_team_question(normalized):
        return [
            {
                "text": "Обязательна ли команда проекта и можно ли участвовать одному?",
                "topic": "komanda_proekta",
                "category": "гранты",
                "forum_normalized": None,
            }
        ]
    if category == "гранты" and "ссылк" in normalized and "грант" in normalized:
        return [
            {
                "text": "Как подать заявку на участие в грантовом конкурсе?",
                "topic": "podat_zayavku_na_uchastie",
                "category": "гранты",
                "forum_normalized": None,
            }
        ]
    if _is_forum_discovery_request(normalized):
        return [
            {
                "text": "Какие форумы и мероприятия сейчас доступны?",
                "topic": "rekomendacii_obschie",
                "category": "общее",
                "forum_normalized": None,
            }
        ]
    if _is_collaboration_request(normalized):
        return [
            {
                "text": "Куда направить предложение о сотрудничестве?",
                "topic": "predlozhenie_sotrudnichestva",
                "category": "общее",
                "forum_normalized": None,
            }
        ]
    if not forum and category not in {"гранты", "платформа_фгаис", "техподдержка"}:
        return []
    ticket_questions = _build_exact_ticket_questions(normalized, str(forum or ""))
    if ticket_questions:
        return ticket_questions
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
            (
                "подать заяв",
                "подача заяв",
                "подать проект",
                "хочу попасть на",
                "как попасть на форум",
                "как туда попасть",
                "что нужно сделать чтобы попасть",
                "что нужно сделать, чтобы попасть",
                "как стать участником",
                "как стать участницей",
                "хочу стать участником",
                "хочу стать участницей",
                "хочу поучаствовать",
                "хочу участвовать",
                "хочу на форум",
                "хочу на мероприятие",
                "вписаться в движ",
                "залететь на форум",
                "залететь на программу",
                "присоединиться к форуму",
            ),
        ),
        (
            "grant_reporting",
            "Как оформить отчётность по гранту?",
            ("отчет", "отчетност", "отчёт", "отчётност"),
        ),
        ("oplata_proezda", "Оплачивается ли проезд?", ("проезд", "дорог")),
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
        (
            "vozrastnye_ogranicheniya",
            "Какие возрастные ограничения?",
            ("возраст", "сколько лет", "до 35", "от 14", "от 18"),
        ),
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
            ("программ", "артист", "выступ"),
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
    forum_only_topics = {
        "o_meropriyatii",
        "oplata_proezda",
        "usloviya_prozhivaniya",
        "otkaz_ot_uchastiya",
        "vnesti_izmeneniya_v_zayavku",
        "vozrastnye_ogranicheniya",
        "transfer_do_mesta_provedeniya_meropriyatiya",
        "pismo_vyzov",
        "kogda_budet_sertifikat",
        "spisok_veschey_i_dokumentov",
        "dokumenty_meropriyatiya",
        "rezultaty_rm",
        "informaciya_o_ploschadke_pitanie_pite",
        "informaciya_o_ploschadke_medicina",
        "uchastniki_s_ovz",
        "inostrannye_grazhdane",
        "rosmolodezh_granty",
        "trebovaniya_po_dress_kodu",
        "poseschenie_festivalya_s_detmi",
        "programma_i_artisty",
        "programma_foruma",
        "daty_nachala_meropriyatiya",
        "dobavlenie_v_chat_meropriyatiya",
        "podtverzhdenie_uchastiya_i_org_momenty",
        "cifrovaya_nedelya",
    }
    for topic, text, markers in candidates:
        if topic in seen_topics:
            continue
        if topic in forum_only_topics and category != "форумы":
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


def _build_exact_grant_questions(normalized: str) -> list[dict]:
    questions: list[dict] = []
    if "соглашен" in normalized:
        topic = (
            "proverka_proekta_grantovogo_soglasheniya"
            if "провер" in normalized
            else "poryadok_zaklyucheniya_soglasheniya"
        )
        questions.append(
            {
                "text": "Как заключить и проверить грантовое соглашение?",
                "topic": topic,
                "category": "гранты",
                "forum_normalized": None,
            }
        )
    if (
        any(marker in normalized for marker in ("оплат", "оплач", "закуп"))
        and any(marker in normalized for marker in ("товар", "услуг", "расход"))
        and ("грант" in normalized or "проект" in normalized)
    ):
        questions.append(
            {
                "text": "Как оплачивать товары и услуги за счёт гранта?",
                "topic": "oplata_tovarov_i_uslug",
                "category": "гранты",
                "forum_normalized": None,
            }
        )
    if (
        any(marker in normalized for marker in ("победител", "приказ"))
        and any(marker in normalized for marker in ("грант", "конкурс", "результат"))
    ):
        questions.append(
            {
                "text": "Где публикуют приказ и список победителей грантового конкурса?",
                "topic": "publikaciya_prikaza",
                "category": "гранты",
                "forum_normalized": None,
            }
        )
    project_selector_markers = (
        "выберите проект",
        "выбрать проект",
        "проект не отображ",
        "проект не высвеч",
        "проекты не отображ",
        "проекты не высвеч",
        "не отображается проект",
        "не отображаются проекты",
        "не высвечивается проект",
        "не высвечиваются проекты",
    )
    if "грант" in normalized and any(
        marker in normalized for marker in project_selector_markers
    ):
        questions.append(
            {
                "text": "Почему проект не отображается при подаче грантовой заявки?",
                "topic": "2_zapolnenie_vkladok_proekta",
                "category": "гранты",
                "forum_normalized": None,
            }
        )
    return questions


def _is_collaboration_request(normalized: str) -> bool:
    collaboration_markers = (
        "предложить сотрудничество",
        "предлагаю сотрудничество",
        "предлагаем сотрудничество",
        "предложение о сотрудничестве",
        "предложение сотрудничества",
        "коммерческое предложение",
        "стать партнером",
        "стать партнёром",
        "в качестве партнера",
        "в качестве партнёра",
        "в качестве спикера",
        "выступить спикером",
        "предложить свои услуги",
        "по вопросам рекламы",
        "сотрудничество с росмолод",
    )
    return any(marker in normalized for marker in collaboration_markers)


def _build_exact_ticket_questions(normalized: str, forum: str) -> list[dict]:
    if forum.casefold().replace("ё", "е") != "день молодежи":
        return []
    if not any(marker in normalized for marker in ("билет", "пропуск", "код")):
        return []

    questions: list[dict] = []
    if any(marker in normalized for marker in ("исправ", "измен", "сменить", "неверн")):
        questions.append(
            _ticket_question("ispravlenie_dannyh_v_bilete", "Как исправить данные в билете?")
        )
    if any(
        marker in normalized
        for marker in ("не приш", "не могу найти", "не получается найти", "повторно", "почт")
    ):
        questions.append(
            _ticket_question(
                "bilet_ne_prishel_povtornoe_poluchenie",
                "Что делать, если билет не пришёл или потерялся?",
            )
        )
    if any(
        marker in normalized
        for marker in (
            "другого",
            "другому",
            "муж",
            "жен",
            "друг",
            "доч",
            "сын",
            "ребен",
            "ребён",
            "дет",
            "несколько билет",
            "два билет",
            "2 билет",
        )
    ):
        questions.append(
            _ticket_question(
                "registraciya_drugogo_cheloveka",
                "Можно ли зарегистрировать другого человека и получить несколько билетов?",
            )
        )
    if any(marker in normalized for marker in ("отмен", "отказаться", "не смогу прийти")):
        questions.append(
            _ticket_question(
                "kolichestvo_person_otmena_registracii",
                "Можно ли отменить билет или регистрацию?",
            )
        )
    if questions:
        return questions
    return [
        _ticket_question(
            "poluchenie_i_naznachenie_bileta",
            "Как получить билет и для чего он нужен?",
        )
    ]


def _ticket_question(topic: str, text: str) -> dict:
    return {
        "text": text,
        "topic": topic,
        "category": "форумы",
        "forum_normalized": "День молодёжи",
    }


def _is_grant_report_review_timing_query(normalized: str) -> bool:
    has_report = "отчет" in normalized or "отчетност" in normalized
    has_review = "провер" in normalized or "рассматрива" in normalized
    has_timing = any(
        marker in normalized
        for marker in ("срок", "сколько", "долго", "время", "дней", "когда")
    )
    return has_report and has_review and has_timing


def _is_project_team_question(normalized: str) -> bool:
    if any(
        marker in normalized
        for marker in (
            "нет команды",
            "без команды",
            "команда проекта",
            "одному можно участвовать",
            "можно участвовать одному",
            "одному можно подать",
            "самому можно участвовать",
        )
    ):
        return True
    return "идея проект" in normalized and "команд" in normalized


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
