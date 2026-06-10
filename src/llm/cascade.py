from __future__ import annotations

from src.models import Complexity

ANALYZER_MODEL = "GigaChat-2-Max"
JUDGE_MODEL = "GigaChat3-10B"
GENERATOR_MODEL_SIMPLE = "GigaChat3-10B"
GENERATOR_MODEL_COMPLEX = "GigaChat-2-Max"


def select_generator_model(complexity: str | Complexity) -> str:
    if complexity == Complexity.COMPLEX or complexity == "complex":
        return GENERATOR_MODEL_COMPLEX
    return GENERATOR_MODEL_SIMPLE
