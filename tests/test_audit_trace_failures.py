from __future__ import annotations

from eval.audit_trace_failures import build_trace_failure_report


def test_build_trace_failure_report_marks_rerank_loss_without_raw_text() -> None:
    metrics = {
        "target": "http://test/ask",
        "cases_path": "cases.json",
        "cases_total": 1,
        "results": [
            {
                "id": "case-1",
                "request_id": "11111111-1111-1111-1111-111111111111",
                "failure_reasons": ["expected_chunk_not_cited"],
                "expected_chunk_ids": ["expected"],
                "expected_cited_chunk_ids": ["expected"],
                "observed_chunk_ids": ["expected", "neighbor"],
                "cited_source_ids": ["neighbor"],
                "query": "SECRET QUERY",
                "response": "SECRET RESPONSE",
            }
        ],
    }
    traces = {
        "11111111-1111-1111-1111-111111111111": {
            "query_analysis": {
                "category": "platform",
                "forum_normalized": "Forum A",
                "complexity": "simple",
                "questions": [{"text": "SECRET QUESTION"}],
            },
            "metadata_filter": {"category": "platform", "forum_normalized": "Forum A"},
            "reranker_scores": [
                {"chunk_id": "neighbor", "text": "SECRET CHUNK TEXT"},
            ],
            "cited_sources": ["neighbor"],
            "generator_model": "source_chunk",
        }
    }

    report = build_trace_failure_report(metrics=metrics, traces=traces)

    assert report["loss_stage_counts"] == {"rerank": 1}
    assert report["rows"][0]["reranked_top_ids"] == ["neighbor"]
    assert report["rows"][0]["analysis"] == {
        "category": "platform",
        "forum_normalized": "Forum A",
        "complexity": "simple",
        "questions_count": 1,
        "needs_clarification": False,
        "should_escalate": False,
    }
    assert "SECRET" not in str(report)


def test_build_trace_failure_report_marks_generate_loss() -> None:
    metrics = {
        "results": [
            {
                "id": "case-1",
                "request_id": "11111111-1111-1111-1111-111111111111",
                "failure_reasons": ["expected_chunk_not_cited"],
                "expected_chunk_ids": ["expected"],
                "observed_chunk_ids": ["expected"],
                "cited_source_ids": ["neighbor"],
            }
        ],
    }
    traces = {
        "11111111-1111-1111-1111-111111111111": {
            "query_analysis": {},
            "reranker_scores": [{"chunk_id": "expected"}, {"chunk_id": "neighbor"}],
            "cited_sources": ["neighbor"],
        }
    }

    report = build_trace_failure_report(metrics=metrics, traces=traces)

    assert report["loss_stage_counts"] == {"generate_or_verify": 1}


def test_build_trace_failure_report_accepts_missing_query_analysis() -> None:
    metrics = {
        "results": [
            {
                "id": "case-1",
                "request_id": "11111111-1111-1111-1111-111111111111",
                "failure_reasons": ["expected_chunk_not_cited"],
                "expected_chunk_ids": ["expected"],
                "observed_chunk_ids": ["expected"],
                "cited_source_ids": ["neighbor"],
            }
        ],
    }
    traces = {
        "11111111-1111-1111-1111-111111111111": {
            "query_analysis": None,
            "metadata_filter": None,
            "reranker_scores": [{"chunk_id": "expected"}],
            "cited_sources": ["neighbor"],
        }
    }

    report = build_trace_failure_report(metrics=metrics, traces=traces)

    assert report["rows"][0]["analysis"]["category"] == ""
    assert report["rows"][0]["metadata_filter"] == {}
    assert report["loss_stage_counts"] == {"generate_or_verify": 1}


def test_build_trace_failure_report_marks_retrieval_loss() -> None:
    metrics = {
        "results": [
            {
                "id": "case-1",
                "request_id": "11111111-1111-1111-1111-111111111111",
                "failure_reasons": ["expected_chunk_not_observed"],
                "expected_chunk_ids": ["expected"],
                "observed_chunk_ids": ["neighbor"],
                "cited_source_ids": ["neighbor"],
            }
        ],
    }

    report = build_trace_failure_report(metrics=metrics, traces={})

    assert report["loss_stage_counts"] == {"retrieval": 1}
