from __future__ import annotations

import re
from time import perf_counter

from src.graph.state import BotState

SOURCE_RE = re.compile(r"[ \t]*\[src:[^\]]+\][ \t]*")
TRAILING_LINE_SPACE_RE = re.compile(r"[ \t]+\n")
LEADING_LINE_SPACE_RE = re.compile(r"\n[ \t]+")
EXCESSIVE_BLANK_LINES_RE = re.compile(r"\n{3,}")


async def respond(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    response = state.get("generated_response") or state.get("final_response") or ""
    final = _strip_source_markers(response)
    if tracer:
        tracer.add("respond", int((perf_counter() - started_at) * 1000))
    return {"final_response": final}


def _strip_source_markers(response: str) -> str:
    without_markers = SOURCE_RE.sub("", response)
    without_markers = TRAILING_LINE_SPACE_RE.sub("\n", without_markers)
    without_markers = LEADING_LINE_SPACE_RE.sub("\n", without_markers)
    without_markers = EXCESSIVE_BLANK_LINES_RE.sub("\n\n", without_markers)
    return "\n".join(line.rstrip() for line in without_markers.strip().splitlines())
