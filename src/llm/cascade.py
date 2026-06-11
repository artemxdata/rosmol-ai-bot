from __future__ import annotations

from src.config import get_settings
from src.models import Complexity

DEFAULT_CLOUD_RU_SIMPLE_MODEL = "ai-sage/GigaChat3-10B-A1.8B"


def _configured(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _simple_model() -> str:
    settings = get_settings()
    return _configured(settings.cloud_ru_model_simple) or DEFAULT_CLOUD_RU_SIMPLE_MODEL


def _complex_model() -> str:
    settings = get_settings()
    return _configured(settings.cloud_ru_model_complex) or _simple_model()


def select_analyzer_model() -> str:
    settings = get_settings()
    return _configured(settings.cloud_ru_model_analyzer) or _complex_model()


def select_generator_model(complexity: str | Complexity) -> str:
    if complexity == Complexity.COMPLEX or complexity == "complex":
        return _complex_model()
    return _simple_model()


def select_judge_model() -> str:
    settings = get_settings()
    return _configured(settings.cloud_ru_model_judge) or _simple_model()
