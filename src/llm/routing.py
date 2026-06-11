from __future__ import annotations

import re
from dataclasses import dataclass

from src.models import Complexity


@dataclass(frozen=True)
class RoutingHint:
    complexity: Complexity
    reason: str
    confidence: float

    def model_dump(self) -> dict[str, str | float]:
        return {
            "complexity": self.complexity.value,
            "reason": self.reason,
            "confidence": self.confidence,
        }


_SIMPLE_PATTERNS = (
    (re.compile(r"\bрегистрац\w*\b"), "registration_faq"),
    (re.compile(r"\bзарегистрир\w*\b"), "registration_faq"),
    (re.compile(r"\bподать\s+заявк\w*\b"), "application_faq"),
    (re.compile(r"\bсрок\w*\s+регистрац\w*\b"), "registration_deadline_faq"),
    (re.compile(r"\bгде\s+(?:зарегистрироваться|подать)\b"), "where_to_register_faq"),
)

_COMPLEX_PATTERNS = (
    (re.compile(r"\bесли\b"), "conditional_query"),
    (re.compile(r"\bа\s+если\b"), "conditional_query"),
    (re.compile(r"\bмне\s+\d{1,2}\b"), "personal_condition"),
    (re.compile(r"\bможно\s+ли\b.*\bесли\b"), "conditional_query"),
    (re.compile(r"\bпри\s+этом\b"), "compound_query"),
    (re.compile(r"\bодновременно\b"), "compound_query"),
    (re.compile(r"\bнесколько\b"), "compound_query"),
    (re.compile(r"\bсравни\w*\b"), "comparison_query"),
    (re.compile(r"\bподходит\s+ли\b"), "eligibility_query"),
)


def estimate_routing_hint(message: str) -> RoutingHint:
    normalized = re.sub(r"\s+", " ", message.lower()).strip()
    if not normalized:
        return RoutingHint(Complexity.COMPLEX, "empty_query", 0.0)

    words = re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
    if len(normalized) > 180 or len(words) > 22:
        return RoutingHint(Complexity.COMPLEX, "long_or_detailed_query", 0.8)

    if normalized.count("?") > 1:
        return RoutingHint(Complexity.COMPLEX, "multi_question", 0.85)

    for pattern, reason in _COMPLEX_PATTERNS:
        if pattern.search(normalized):
            return RoutingHint(Complexity.COMPLEX, reason, 0.85)

    question_markers = sum(
        marker in normalized
        for marker in ("кто ", "что ", "где ", "когда ", "как ", "можно ли", "почему ")
    )
    if question_markers > 1:
        return RoutingHint(Complexity.COMPLEX, "multiple_question_markers", 0.75)

    for pattern, reason in _SIMPLE_PATTERNS:
        if pattern.search(normalized) and len(words) <= 12:
            return RoutingHint(Complexity.SIMPLE, reason, 0.8)

    return RoutingHint(Complexity.COMPLEX, "default_conservative", 0.55)
