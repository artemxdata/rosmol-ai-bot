from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState
from src.kb.forum_registry import detect_forums_from_text
from src.llm.cascade import select_analyzer_model
from src.llm.json_utils import parse_llm_json
from src.llm.prompts import QUERY_ANALYZER_SYSTEM, build_analyzer_user
from src.models import Complexity, QueryAnalysis


async def analyze_query(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    try:
        llm = state["llm_client"]
        routing_hint = state.get("routing_hint")
        model = select_analyzer_model(routing_hint)
        content = await llm.generate(
            model=model,
            system=QUERY_ANALYZER_SYSTEM,
            user=build_analyzer_user(
                state["message_masked"],
                state.get("session"),
                None,
                routing_hint,
            ),
            response_format="json",
        )
        payload = _coerce_analysis_payload(parse_llm_json(content))
        _apply_deterministic_forum(
            payload,
            state.get("message") or state["message_masked"],
        )
        analysis = QueryAnalysis.model_validate(payload)
        if tracer:
            tracer.add("analyze", int((perf_counter() - started_at) * 1000), model=model)
        return {"analysis": analysis}
    except Exception as exc:
        fallback = _fallback_analysis(
            state.get("message") or state["message_masked"],
            state["message_masked"],
            state.get("routing_hint"),
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
            return {"analysis": fallback, "analyzer_fallback": True}
        if tracer:
            tracer.add_error("analyze", int((perf_counter() - started_at) * 1000), exc)
        return {
            "should_escalate": True,
            "escalation_reason": "analyzer_failed",
            "error": str(exc),
        }


def _fallback_analysis(
    original_message: str,
    masked_message: str,
    routing_hint: object,
) -> QueryAnalysis | None:
    category = _infer_category_from_message(masked_message)
    payload = {
        "category": category,
        "complexity": _complexity_from_routing_hint(routing_hint).value,
        "questions": [
            {
                "text": masked_message,
                "category": category,
            }
        ],
    }
    payload = _coerce_analysis_payload(payload)
    _apply_deterministic_forum(payload, original_message)
    if not payload.get("category") and not payload.get("forum_normalized"):
        return None
    return QueryAnalysis.model_validate(payload)


def _infer_category_from_message(message: str) -> str | None:
    normalized = message.casefold().replace("ё", "е")
    if "грант" in normalized:
        return "гранты"
    if any(word in normalized for word in ("фгаис", "личн", "кабинет", "парол", "верификац")):
        return "платформа_фгаис"
    if any(word in normalized for word in ("ошиб", "баг", "не работает", "техподдерж")):
        return "техподдержка"
    if any(word in normalized for word in ("форум", "мероприят", "фестивал")):
        return "форумы"
    return None


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
    normalized["forum"] = _coerce_optional_string(normalized.get("forum"))
    normalized["forum_normalized"] = _coerce_optional_string(normalized.get("forum_normalized"))
    normalized["topics"] = _coerce_string_list(normalized.get("topics"))
    normalized["category"] = _normalize_category(normalized.get("category"))
    _drop_pseudo_forum_for_category(normalized)
    if not isinstance(normalized.get("extracted_params"), dict):
        normalized["extracted_params"] = {}
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
        question["forum_normalized"] = _coerce_optional_string(question.get("forum_normalized"))
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
    return "грант" in normalized and "физичес" in normalized


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
