from __future__ import annotations

from types import SimpleNamespace

from src.llm.usage import build_llm_usage_event, summarize_llm_usage


def test_build_llm_usage_event_estimates_complex_model_cost(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.llm.usage.get_settings",
        lambda: SimpleNamespace(
            cloud_ru_model="ai-sage/GigaChat3-10B-A1.8B",
            cloud_ru_model_simple="ai-sage/GigaChat3-10B-A1.8B",
            cloud_ru_model_complex="GigaChat/GigaChat-2-Max",
            cloud_ru_model_simple_input_price_rub_per_million=0.0,
            cloud_ru_model_simple_output_price_rub_per_million=0.0,
            cloud_ru_model_complex_input_price_rub_per_million=569.34,
            cloud_ru_model_complex_output_price_rub_per_million=569.34,
        ),
    )

    event = build_llm_usage_event(
        "GigaChat/GigaChat-2-Max",
        latency_ms=100,
        usage={"prompt_tokens": 1_000, "completion_tokens": 500, "total_tokens": 1_500},
    )

    assert event["prompt_tokens"] == 1_000
    assert event["completion_tokens"] == 500
    assert event["total_tokens"] == 1_500
    assert event["priced"] is True
    assert event["estimated_cost_rub"] == 0.85401


def test_summarize_llm_usage_totals_events() -> None:
    summary = summarize_llm_usage(
        [
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "estimated_cost_rub": 0.01,
            },
            {
                "prompt_tokens": 20,
                "completion_tokens": 7,
                "total_tokens": 27,
                "estimated_cost_rub": 0.02,
            },
        ]
    )

    assert summary["llm_prompt_tokens"] == 30
    assert summary["llm_completion_tokens"] == 12
    assert summary["llm_total_tokens"] == 42
    assert summary["llm_estimated_cost_rub"] == 0.03
