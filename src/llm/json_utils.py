from __future__ import annotations

import json
from typing import Any


def parse_llm_json(content: str) -> dict[str, Any]:
    """Parse a JSON object from an LLM response.

    Models sometimes wrap JSON in markdown fences or add a short preface.
    This function accepts those cases but still fails closed if there is no
    valid JSON object.
    """
    text = content.strip()
    if not text:
        raise ValueError("empty LLM response")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = _parse_first_json_object(text)

    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")
    return parsed


def _parse_first_json_object(text: str) -> Any:
    fenced = _extract_fenced_json(text)
    if fenced is not None:
        try:
            return json.loads(fenced)
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start < 0:
        raise ValueError("LLM response does not contain a JSON object")

    in_string = False
    escaped = False
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : index + 1])

    raise ValueError("LLM response contains an unfinished JSON object")


def _extract_fenced_json(text: str) -> str | None:
    fence_start = text.find("```")
    if fence_start < 0:
        return None

    content_start = text.find("\n", fence_start + 3)
    if content_start < 0:
        return None
    content_start += 1

    fence_end = text.find("```", content_start)
    if fence_end < 0:
        return None

    return text[content_start:fence_end].strip()
