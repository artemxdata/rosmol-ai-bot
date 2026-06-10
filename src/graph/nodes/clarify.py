from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState


async def clarify(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    analysis = state.get("analysis")
    text = (
        analysis.clarification_question
        if analysis and analysis.clarification_question
        else "Уточни, пожалуйста, название форума или тему вопроса."
    )
    if tracer:
        tracer.add("clarify", int((perf_counter() - started_at) * 1000))
    return {"final_response": text}
