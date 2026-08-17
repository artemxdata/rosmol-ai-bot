from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scripts.check_fact_pipeline_qdrant import (
    ForbiddenLLM,
    build_owner_preview,
    build_summary,
)
from scripts.qdrant_readonly_proxy import ALLOWED_POST_PATHS, _upstream_api_key

CANDIDATE_SHA = "a" * 40
MANIFEST_SHA = "b" * 64
CASES_SHA = "c" * 64
ROOT = Path(__file__).resolve().parents[1]


def _scored(*, passed: bool = True, retrieval: bool = True) -> dict[str, object]:
    return {
        "passed": passed,
        "was_escalated": False,
        "expected_or_equivalent_chunk_hit": retrieval,
        "expected_cited_or_equivalent_chunk_hit": True,
        "failure_reasons": [] if passed else ["answer_contains_match"],
    }


def _summary(
    *,
    failed: int = 1,
    retrieval_complete: bool = True,
    llm_calls: int = 0,
) -> dict[str, object]:
    rows = [_scored() for _ in range(50)]
    for index in range(failed):
        rows[-(index + 1)] = _scored(passed=False)
    if not retrieval_complete:
        rows[0] = _scored(passed=False, retrieval=False)
    return build_summary(
        candidate_sha=CANDIDATE_SHA,
        manifest_sha256=MANIFEST_SHA,
        cases_sha256=CASES_SHA,
        scored_cases=rows,
        groups=["typical"] * 25 + ["atypical"] * 25,
        llm_calls=llm_calls,
    )


def test_summary_requires_49_of_50_complete_retrieval_and_zero_llm() -> None:
    summary = _summary()
    assert summary["status"] == "GO"
    assert summary["classification"] == "calibration_only"
    assert summary["counts"] == {
        "total": 50,
        "passed": 49,
        "typical_passed": 25,
        "atypical_passed": 24,
        "no_operator": 50,
        "typical_no_operator": 25,
        "atypical_no_operator": 25,
        "retrieval_complete": 50,
        "citation_complete": 50,
        "llm_calls": 0,
    }
    assert summary["failures"] == [
        {"ordinal": 50, "reasons": ["answer_contains_match"]}
    ]


@pytest.mark.parametrize(
    "summary",
    [
        _summary(failed=2),
        _summary(retrieval_complete=False),
        _summary(llm_calls=1),
    ],
)
def test_summary_stops_on_quality_retrieval_or_llm_regression(
    summary: dict[str, object],
) -> None:
    assert summary["status"] == "STOP"


def test_forbidden_llm_fails_closed_and_counts_attempt() -> None:
    llm = ForbiddenLLM()
    with pytest.raises(AssertionError, match="must not call an LLM"):
        asyncio.run(llm.generate(prompt="forbidden"))
    assert llm.calls == 1


def test_owner_preview_is_bounded_and_reports_all_response_lengths() -> None:
    rows = []
    for ordinal in range(1, 51):
        rows.append(
            {
                **_scored(passed=ordinal != 23),
                "query": f"Вопрос {ordinal}",
                "response": "Короткий подтверждённый ответ. " + ("x" * ordinal),
            }
        )
    preview = build_owner_preview(
        candidate_sha=CANDIDATE_SHA,
        manifest_sha256=MANIFEST_SHA,
        cases_sha256=CASES_SHA,
        scored_cases=rows,
        groups=["typical"] * 25 + ["atypical"] * 25,
        llm_calls=0,
    )

    assert preview["schema_version"] == "fact-core-qdrant-owner-preview-v1"
    assert preview["counts"] == {
        "total": 50,
        "passed": 49,
        "no_operator": 50,
        "llm_calls": 0,
    }
    assert preview["preview_ordinals"] == [1, 9, 26, 44, 48]
    assert [row["ordinal"] for row in preview["previews"]] == [1, 9, 26, 44, 48]
    assert preview["response_shape"]["empty_responses"] == 0
    assert preview["response_shape"]["max_links"] == 0
    assert preview["response_shape"]["min_chars"] < preview["response_shape"]["max_chars"]


def test_diagnostic_scores_the_post_respond_final_response() -> None:
    source = (ROOT / "scripts/check_fact_pipeline_qdrant.py").read_text(
        encoding="utf-8"
    )

    assert "responded = await respond" in source
    assert '"response": result.get("final_response", "")' in source
    assert 'result.get("generated_response", "")' not in source


def test_proxy_allows_only_qdrant_query_and_scroll_paths() -> None:
    assert ALLOWED_POST_PATHS == {
        "/collections/knowledge_base/points/query",
        "/collections/knowledge_base/points/scroll",
    }
    assert all(
        forbidden not in path
        for path in ALLOWED_POST_PATHS
        for forbidden in ("upsert", "delete", "update", "create")
    )


def test_proxy_requires_exact_server_local_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QDRANT_UPSTREAM_URL", "http://qdrant:6333")
    monkeypatch.setenv("QDRANT_UPSTREAM_API_KEY", "test-only-key")
    assert _upstream_api_key() == "test-only-key"
    monkeypatch.setenv("QDRANT_UPSTREAM_URL", "https://example.invalid")
    with pytest.raises(RuntimeError, match="upstream is invalid"):
        _upstream_api_key()
