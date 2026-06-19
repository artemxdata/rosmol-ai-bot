from __future__ import annotations

import json
import re
from pathlib import Path

MANUAL_CASES_PATH = Path("data/manual_complex_queries.json")
PII_RE = re.compile(
    r"(\+?\d[\d\s().-]{8,}\d|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
    flags=re.IGNORECASE,
)
REQUIRED_TAGS = {
    "ambiguous_forum",
    "cancel",
    "compare",
    "documents",
    "equipment",
    "grant",
    "platform",
    "status",
    "technical",
    "transfer",
}


def test_manual_complex_queries_are_valid_stress_cases() -> None:
    cases = json.loads(MANUAL_CASES_PATH.read_text(encoding="utf-8"))

    assert isinstance(cases, list)
    assert len(cases) >= 15
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))

    all_tags: set[str] = set()
    for case in cases:
        assert case["id"].strip()
        assert len(case["query"].split()) >= 8
        assert "manual" in case["tags"]
        assert "complex" in case["tags"]
        assert not PII_RE.search(case["query"])
        all_tags.update(case["tags"])

    assert REQUIRED_TAGS <= all_tags
