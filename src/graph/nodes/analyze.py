from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState
from src.kb.forum_registry import detect_forum_from_text
from src.llm.cascade import select_analyzer_model
from src.llm.json_utils import parse_llm_json
from src.llm.prompts import QUERY_ANALYZER_SYSTEM, build_analyzer_user
from src.models import QueryAnalysis


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
        _apply_deterministic_forum(payload, state["message_masked"])
        analysis = QueryAnalysis.model_validate(payload)
        if tracer:
            tracer.add("analyze", int((perf_counter() - started_at) * 1000), model=model)
        return {"analysis": analysis}
    except Exception as exc:
        if tracer:
            tracer.add_error("analyze", int((perf_counter() - started_at) * 1000), exc)
        return {
            "should_escalate": True,
            "escalation_reason": "analyzer_failed",
            "error": str(exc),
        }


def _coerce_analysis_payload(payload: dict) -> dict:
    normalized = dict(payload)
    normalized["forum"] = _coerce_optional_string(normalized.get("forum"))
    normalized["forum_normalized"] = _coerce_optional_string(normalized.get("forum_normalized"))
    normalized["topics"] = _coerce_string_list(normalized.get("topics"))
    normalized["category"] = _normalize_category(normalized.get("category"))
    if normalized.get("forum") and not normalized.get("forum_normalized"):
        normalized["forum_normalized"] = normalized["forum"]
    normalized["questions"] = _coerce_questions(normalized.get("questions"))
    _propagate_question_defaults(normalized)
    return normalized


def _apply_deterministic_forum(payload: dict, message: str) -> None:
    detected_forum = detect_forum_from_text(message)
    if not detected_forum:
        return

    if not payload.get("forum"):
        payload["forum"] = detected_forum
    if not payload.get("forum_normalized"):
        payload["forum_normalized"] = detected_forum
    if not payload.get("category"):
        payload["category"] = "форумы"
    _propagate_question_defaults(payload)


def _propagate_question_defaults(payload: dict) -> None:
    forum = payload.get("forum_normalized")
    category = payload.get("category")
    questions = payload.get("questions") or []
    for question in questions:
        if forum and not question.get("forum_normalized"):
            question["forum_normalized"] = forum
        if category and not question.get("category"):
            question["category"] = category


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
