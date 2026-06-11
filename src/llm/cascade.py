from __future__ import annotations

from typing import Any

from src.config import get_settings
from src.models import Complexity

DEFAULT_CLOUD_RU_SIMPLE_MODEL = "ai-sage/GigaChat3-10B-A1.8B"


def _configured(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _simple_model() -> str:
    settings = get_settings()
    legacy_model = getattr(settings, "cloud_ru_model", "")
    return (
        _configured(settings.cloud_ru_model_simple)
        or _configured(legacy_model)
        or DEFAULT_CLOUD_RU_SIMPLE_MODEL
    )


def _complex_model() -> str:
    settings = get_settings()
    return _configured(settings.cloud_ru_model_complex) or _simple_model()


def _complexity_from_hint(complexity_hint: Any | None) -> Complexity | None:
    if isinstance(complexity_hint, Complexity):
        return complexity_hint
    if isinstance(complexity_hint, str):
        try:
            return Complexity(complexity_hint)
        except ValueError:
            return None
    if isinstance(complexity_hint, dict):
        return _complexity_from_hint(complexity_hint.get("complexity"))
    if hasattr(complexity_hint, "complexity"):
        return _complexity_from_hint(complexity_hint.complexity)
    return None


def select_analyzer_model(complexity_hint: Any | None = None) -> str:
    settings = get_settings()
    if configured_model := _configured(settings.cloud_ru_model_analyzer):
        return configured_model
    if _complexity_from_hint(complexity_hint) == Complexity.SIMPLE:
        return _simple_model()
    return _complex_model()


def select_generator_model(complexity: str | Complexity) -> str:
    if complexity == Complexity.COMPLEX or complexity == "complex":
        return _complex_model()
    return _simple_model()


def select_judge_model() -> str:
    settings = get_settings()
    return _configured(settings.cloud_ru_model_judge) or _simple_model()
