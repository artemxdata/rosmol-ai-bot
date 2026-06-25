from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState

OFFTOPIC_SCOPE_NOTE = (
    "Я отвечаю на вопросы по мероприятиям, форумам, ФГАИС «Молодёжь России» "
    "и грантам Росмолодёжи. Задай, пожалуйста, вопрос по этим темам."
)


async def clarify(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    analysis = state.get("analysis")
    if analysis and analysis.is_offtopic:
        text = OFFTOPIC_SCOPE_NOTE
    else:
        text = (
            analysis.clarification_question
            if analysis and analysis.clarification_question
            else "Уточни, пожалуйста, название форума или тему вопроса."
        )
    if tracer:
        tracer.add(
            "clarify",
            int((perf_counter() - started_at) * 1000),
            offtopic=bool(analysis and analysis.is_offtopic),
        )
    return {
        "final_response": text,
        "should_escalate": (
            False if analysis and analysis.is_offtopic else state.get("should_escalate", False)
        ),
        "escalation_reason": (
            None if analysis and analysis.is_offtopic else state.get("escalation_reason")
        ),
    }
