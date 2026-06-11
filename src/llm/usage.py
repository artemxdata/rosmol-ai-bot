from __future__ import annotations

from contextvars import ContextVar, Token
from decimal import Decimal
from typing import Any

from src.config import get_settings

_collector: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "llm_usage_collector",
    default=None,
)


def start_llm_usage_collection() -> tuple[list[dict[str, Any]], Token[list[dict[str, Any]] | None]]:
    events: list[dict[str, Any]] = []
    token = _collector.set(events)
    return events, token


def reset_llm_usage_collection(token: Token[list[dict[str, Any]] | None]) -> None:
    _collector.reset(token)


def record_llm_usage(model: str, latency_ms: int, usage: dict[str, Any] | None) -> None:
    collector = _collector.get()
    if collector is None:
        return
    collector.append(build_llm_usage_event(model, latency_ms, usage))


def build_llm_usage_event(
    model: str,
    latency_ms: int,
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    usage = usage or {}
    prompt_tokens = _int_or_zero(usage.get("prompt_tokens"))
    completion_tokens = _int_or_zero(usage.get("completion_tokens"))
    total_tokens = _int_or_zero(usage.get("total_tokens")) or prompt_tokens + completion_tokens
    estimated_cost_rub = estimate_llm_cost_rub(model, prompt_tokens, completion_tokens)
    return {
        "model": model,
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_rub": float(estimated_cost_rub),
        "priced": estimated_cost_rub > 0,
    }


def summarize_llm_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    estimated_cost = sum(Decimal(str(event.get("estimated_cost_rub") or 0)) for event in events)
    return {
        "llm_usage": events,
        "llm_prompt_tokens": sum(_int_or_zero(event.get("prompt_tokens")) for event in events),
        "llm_completion_tokens": sum(
            _int_or_zero(event.get("completion_tokens")) for event in events
        ),
        "llm_total_tokens": sum(_int_or_zero(event.get("total_tokens")) for event in events),
        "llm_estimated_cost_rub": float(estimated_cost),
    }


def estimate_llm_cost_rub(model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
    settings = get_settings()
    if model == settings.cloud_ru_model_complex:
        input_price = Decimal(str(settings.cloud_ru_model_complex_input_price_rub_per_million))
        output_price = Decimal(str(settings.cloud_ru_model_complex_output_price_rub_per_million))
    else:
        legacy_simple_model = (settings.cloud_ru_model or "").strip()
        simple_models = {settings.cloud_ru_model_simple, legacy_simple_model}
        if model not in simple_models:
            return Decimal("0")
        input_price = Decimal(str(settings.cloud_ru_model_simple_input_price_rub_per_million))
        output_price = Decimal(str(settings.cloud_ru_model_simple_output_price_rub_per_million))

    return (
        Decimal(prompt_tokens) * input_price / Decimal(1_000_000)
        + Decimal(completion_tokens) * output_price / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"))


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
