from __future__ import annotations

from src.config import get_settings
from src.models import Complexity

DEFAULT_CLOUD_RU_MODEL = "ai-sage/GigaChat3-10B-A1.8B"
ANALYZER_MODEL = DEFAULT_CLOUD_RU_MODEL
JUDGE_MODEL = DEFAULT_CLOUD_RU_MODEL
GENERATOR_MODEL_SIMPLE = DEFAULT_CLOUD_RU_MODEL
GENERATOR_MODEL_COMPLEX = DEFAULT_CLOUD_RU_MODEL


def select_generator_model(complexity: str | Complexity) -> str:
    return get_settings().cloud_ru_model
