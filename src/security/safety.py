from __future__ import annotations

import re

SAFETY_PATTERNS: dict[str, re.Pattern[str]] = {
    "self_harm": re.compile(
        r"\b(?:суицид|самоубийств|покончу с собой|не хочу жить)\b",
        re.IGNORECASE,
    ),
    "threat": re.compile(r"\b(?:убью|взорву|угрожаю|расправлюсь)\b", re.IGNORECASE),
}


def check(text: str) -> tuple[bool, str | None]:
    for reason, pattern in SAFETY_PATTERNS.items():
        if pattern.search(text):
            return False, reason
    return True, None
