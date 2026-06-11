from __future__ import annotations

from types import SimpleNamespace

from src.llm.cascade import (
    DEFAULT_CLOUD_RU_SIMPLE_MODEL,
    select_analyzer_model,
    select_generator_model,
    select_judge_model,
)
from src.models import Complexity


def _settings(**overrides):
    values = {
        "cloud_ru_model_simple": "",
        "cloud_ru_model_complex": "",
        "cloud_ru_model_analyzer": "",
        "cloud_ru_model_judge": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_llm_cascade_defaults_to_simple_model(monkeypatch) -> None:
    monkeypatch.setattr("src.llm.cascade.get_settings", lambda: _settings())

    assert select_generator_model(Complexity.SIMPLE) == DEFAULT_CLOUD_RU_SIMPLE_MODEL
    assert select_generator_model(Complexity.COMPLEX) == DEFAULT_CLOUD_RU_SIMPLE_MODEL
    assert select_analyzer_model() == DEFAULT_CLOUD_RU_SIMPLE_MODEL
    assert select_judge_model() == DEFAULT_CLOUD_RU_SIMPLE_MODEL


def test_llm_cascade_routes_simple_analyzer_and_judge_to_10b(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.llm.cascade.get_settings",
        lambda: _settings(
            cloud_ru_model_simple="ai-sage/GigaChat3-10B-A1.8B",
            cloud_ru_model_complex="cloud-ru-max-model",
        ),
    )

    assert select_analyzer_model({"complexity": "simple"}) == "ai-sage/GigaChat3-10B-A1.8B"
    assert select_generator_model("simple") == "ai-sage/GigaChat3-10B-A1.8B"
    assert select_judge_model() == "ai-sage/GigaChat3-10B-A1.8B"


def test_llm_cascade_routes_complex_and_analyzer_to_max(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.llm.cascade.get_settings",
        lambda: _settings(
            cloud_ru_model_simple="ai-sage/GigaChat3-10B-A1.8B",
            cloud_ru_model_complex="cloud-ru-max-model",
        ),
    )

    assert select_generator_model("complex") == "cloud-ru-max-model"
    assert select_analyzer_model({"complexity": "complex"}) == "cloud-ru-max-model"


def test_llm_cascade_allows_explicit_analyzer_and_judge_models(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.llm.cascade.get_settings",
        lambda: _settings(
            cloud_ru_model_simple="simple-model",
            cloud_ru_model_complex="complex-model",
            cloud_ru_model_analyzer="analyzer-model",
            cloud_ru_model_judge="judge-model",
        ),
    )

    assert select_analyzer_model() == "analyzer-model"
    assert select_judge_model() == "judge-model"
