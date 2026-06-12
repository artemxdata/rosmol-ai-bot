from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState
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
        analysis = QueryAnalysis.model_validate(_coerce_analysis_payload(parse_llm_json(content)))
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
    normalized["topics"] = _coerce_string_list(normalized.get("topics"))
    return normalized


def _coerce_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]

    result: list[str] = []
    for item in value:
        if isinstance(item, str):
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
