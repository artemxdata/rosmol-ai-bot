from __future__ import annotations

import pytest

from src.llm.json_utils import parse_llm_json


def test_parse_llm_json_accepts_plain_object() -> None:
    assert parse_llm_json('{"complexity": "simple"}') == {"complexity": "simple"}


def test_parse_llm_json_accepts_markdown_fence() -> None:
    content = """```json
{"has_hallucination": false, "confidence": 0.91}
```"""

    assert parse_llm_json(content) == {"has_hallucination": False, "confidence": 0.91}


def test_parse_llm_json_extracts_object_from_text() -> None:
    content = 'Ответ ниже:\n{"forum": "Машук", "nested": {"ok": true}}\nГотово.'

    assert parse_llm_json(content) == {"forum": "Машук", "nested": {"ok": True}}


def test_parse_llm_json_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        parse_llm_json('[{"forum": "Машук"}]')
