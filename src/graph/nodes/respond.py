from __future__ import annotations

import re
from time import perf_counter

from src.graph.state import BotState

SOURCE_RE = re.compile(r"\s*\[src:[^\]]+\]\s*")


async def respond(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    response = state.get("generated_response") or state.get("final_response") or ""
    final = SOURCE_RE.sub(" ", response).strip()
    if tracer:
        tracer.add("respond", int((perf_counter() - started_at) * 1000))
    return {"final_response": final}
