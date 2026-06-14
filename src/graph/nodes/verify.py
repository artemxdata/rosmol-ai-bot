from __future__ import annotations

import re
from time import perf_counter

from src.config import get_settings
from src.graph.state import BotState
from src.llm.cascade import select_judge_model
from src.llm.json_utils import parse_llm_json
from src.llm.prompts import LLM_JUDGE_SYSTEM, build_judge_user
from src.models import VerificationResult

SOURCE_RE = re.compile(r"\[src:([^\]]+)\]")
NO_QUESTION_RE = re.compile(
    r"(пока\s+нет\s+вопрос|задайте\s+(?:ваш\s+)?вопрос|готов\s+помочь.*задайте)",
    flags=re.IGNORECASE,
)


async def verify(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    response = state.get("generated_response") or ""
    chunks = state.get("reranked_chunks", [])
    if _contradicts_present_question(response, state):
        result = VerificationResult(
            has_hallucination=True,
            confidence=0.0,
            details="Response asks for a question although the user already asked one.",
        )
        if tracer:
            tracer.add("verify", int((perf_counter() - started_at) * 1000), guard=True)
        return {"verification": result, "verifier_triggered": False}

    cited = set(SOURCE_RE.findall(response))
    known = {chunk.chunk_id for chunk in chunks}
    unknown_sources = cited - known
    if unknown_sources:
        result = VerificationResult(
            has_hallucination=True,
            confidence=0.0,
            details=f"Unknown source markers: {sorted(unknown_sources)}",
        )
        return {"verification": result, "verifier_triggered": False}

    confidence = float(state.get("max_confidence") or 0)
    if confidence >= get_settings().reranker_threshold_high:
        result = VerificationResult(has_hallucination=False, confidence=confidence)
        if tracer:
            tracer.add("verify", int((perf_counter() - started_at) * 1000), judge=False)
        return {"verification": result, "verifier_triggered": False}

    try:
        model = select_judge_model()
        judge_raw = await state["llm_client"].generate(
            model=model,
            system=LLM_JUDGE_SYSTEM,
            user=build_judge_user(response, chunks),
            response_format="json",
            max_tokens=500,
        )
        data = parse_llm_json(judge_raw)
        result = VerificationResult.model_validate({**data, "triggered_llm_judge": True})
    except Exception as exc:
        result = VerificationResult(
            has_hallucination=True,
            confidence=0.0,
            details=f"Judge failed: {exc}",
            triggered_llm_judge=True,
        )

    if tracer:
        tracer.add("verify", int((perf_counter() - started_at) * 1000), judge=True)
    return {"verification": result, "verifier_triggered": True}


def _contradicts_present_question(response: str, state: BotState) -> bool:
    if not response or not NO_QUESTION_RE.search(response):
        return False
    message = str(state.get("message_masked") or state.get("message") or "").strip()
    if "?" in message:
        return True
    analysis = state.get("analysis")
    return bool(getattr(analysis, "questions", None))
