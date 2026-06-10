from __future__ import annotations

from time import perf_counter

from src.graph.state import BotState
from src.llm.cascade import select_generator_model
from src.llm.prompts import RESPONSE_GENERATOR_SYSTEM, build_generator_user


async def generate(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    analysis = state["analysis"]
    model = select_generator_model(analysis.complexity)
    chunks = state.get("reranked_chunks", [])
    try:
        response = await state["llm_client"].generate(
            model=model,
            system=RESPONSE_GENERATOR_SYSTEM,
            user=build_generator_user(
                analysis.questions,
                chunks,
                state.get("session"),
                analysis.extracted_params,
            ),
        )
        if tracer:
            tracer.add("generate", int((perf_counter() - started_at) * 1000), model=model)
        return {
            "generated_response": response,
            "generator_model": model,
            "cited_sources": [chunk.chunk_id for chunk in chunks],
        }
    except Exception as exc:
        if tracer:
            tracer.add_error("generate", int((perf_counter() - started_at) * 1000), exc)
        return {
            "should_escalate": True,
            "escalation_reason": "generation_failed",
            "error": str(exc),
        }
