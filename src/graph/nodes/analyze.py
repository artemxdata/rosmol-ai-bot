from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState
from src.llm.cascade import ANALYZER_MODEL
from src.llm.json_utils import parse_llm_json
from src.llm.prompts import QUERY_ANALYZER_SYSTEM, build_analyzer_user
from src.models import QueryAnalysis


async def analyze_query(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    try:
        llm = state["llm_client"]
        content = await llm.generate(
            model=ANALYZER_MODEL,
            system=QUERY_ANALYZER_SYSTEM,
            user=build_analyzer_user(state["message_masked"], state.get("session"), None),
            response_format="json",
        )
        analysis = QueryAnalysis.model_validate(parse_llm_json(content))
        if tracer:
            tracer.add("analyze", int((perf_counter() - started_at) * 1000), model=ANALYZER_MODEL)
        return {"analysis": analysis}
    except Exception as exc:
        if tracer:
            tracer.add_error("analyze", int((perf_counter() - started_at) * 1000), exc)
        return {
            "should_escalate": True,
            "escalation_reason": "analyzer_failed",
            "error": str(exc),
        }
