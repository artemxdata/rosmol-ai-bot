from __future__ import annotations

import re

PROFANITY_RE = re.compile(
    r"\b(?:бля\w*|сука\w*|хуй\w*|пизд\w*|еба\w*|ёба\w*|мудак\w*)\b",
    re.IGNORECASE,
)


def check(text: str) -> bool:
    return bool(PROFANITY_RE.search(text))
