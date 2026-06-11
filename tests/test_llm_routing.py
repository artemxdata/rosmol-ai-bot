from __future__ import annotations

from src.llm.routing import estimate_routing_hint
from src.models import Complexity


def test_routing_marks_obvious_registration_question_as_simple() -> None:
    hint = estimate_routing_hint("Регистрация на форум")

    assert hint.complexity == Complexity.SIMPLE
    assert hint.reason == "registration_faq"


def test_routing_keeps_personal_conditional_question_complex() -> None:
    hint = estimate_routing_hint("Мне 17 лет, хочу на Машук, кто платит за дорогу?")

    assert hint.complexity == Complexity.COMPLEX
    assert hint.reason == "personal_condition"


def test_routing_keeps_unknown_short_query_complex_by_default() -> None:
    hint = estimate_routing_hint("Что с моей заявкой?")

    assert hint.complexity == Complexity.COMPLEX
    assert hint.reason == "default_conservative"
