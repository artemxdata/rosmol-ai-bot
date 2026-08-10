from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import analyze_phase0_escalations as analyzer
from scripts.analyze_phase0_escalations import (
    EXPECTED_REASON_COUNTS,
    classify_escalation_reason,
    decision_from_bounds,
    main,
    prepare_review,
    summarize_review,
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_review(path: Path, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if row["escalation_type"] in {"threshold", "source_gap"}:
            row["human_review"]["reviewer_alias"] = "qa-reviewer"
            row["human_review"]["reviewed_at"] = "2026-08-10T12:00:00+07:00"
    _write_jsonl(path, rows)


def _retrieve_trace(source_id: str) -> dict[str, Any]:
    provenance_version = analyzer.trace_exporter.PROVENANCE_SCHEMA_VERSION
    return {
        "retrieval_method": "hybrid",
        "metadata_lookup_attempted": False,
        "metadata_lookup_succeeded": False,
        "metadata_lookup_result_count": 0,
        "hybrid_candidates_present": True,
        "question_provenance": [
            {
                "schema_version": provenance_version,
                "question_id": "q1",
                "attempts": [
                    {
                        "attempt_no": 1,
                        "scope": "strict",
                        "filters": {"source_type": "yonote"},
                        "top_k": 10,
                        "retrieval_method": "hybrid",
                        "metadata_lookup_attempted": False,
                        "metadata_lookup_succeeded": False,
                        "metadata_lookup_result_count": 0,
                        "hybrid_candidates_present": True,
                        "candidates": [
                            {
                                "chunk_id": source_id,
                                "method": "hybrid",
                                "score": 0.4,
                            }
                        ],
                        "candidates_total": 1,
                        "candidates_recorded": 1,
                        "candidates_truncated_count": 0,
                    }
                ],
                "attempts_total": 1,
                "attempts_recorded": 1,
                "attempts_truncated_count": 0,
                "retrieved_chunk_ids": [source_id],
                "retrieved_chunk_ids_total": 1,
                "retrieved_chunk_ids_recorded": 1,
                "retrieved_chunk_ids_truncated_count": 0,
                "questions_total": 1,
                "questions_recorded": 1,
                "questions_truncated_count": 0,
                "attributable_questions_total": 1,
            }
        ],
    }


def _reranker_trace(source_id: str) -> dict[str, Any]:
    provenance_version = analyzer.trace_exporter.PROVENANCE_SCHEMA_VERSION
    return {
        "max_confidence": 0.5,
        "confidence_source": "reranker",
        "reranker_invoked": True,
        "raw_reranker_scores": [0.5],
        "raw_reranker_scores_total": 1,
        "raw_reranker_scores_recorded": 1,
        "raw_reranker_scores_truncated_count": 0,
        "raw_reranker_max": 0.5,
        "score_origin": "reranker",
        "synthetic_score_applied": False,
        "synthetic_high_score_applied": False,
        "floor_applied": False,
        "confidence_components": {
            "raw_reranker_max": 0.5,
            "reranked_output_max": 0.5,
            "retrieval_exact_filter_floor": 0.0,
            "decision_confidence": 0.5,
        },
        "question_provenance": [
            {
                "schema_version": provenance_version,
                "question_id": "q1",
                "input_chunk_ids": [source_id],
                "input_chunk_ids_total": 1,
                "input_chunk_ids_recorded": 1,
                "input_chunk_ids_truncated_count": 0,
                "output_chunks": [{"chunk_id": source_id, "score": 0.5}],
                "dropped_chunk_ids": [],
                "questions_total": 1,
                "questions_recorded": 1,
                "questions_truncated_count": 0,
            }
        ],
    }


def _failed_generation_trace(source_id: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": analyzer.trace_exporter.PROVENANCE_SCHEMA_VERSION,
        "mode": "llm",
        "generator_path": "llm",
        "source_chunk_applied": False,
        "selected_source_ids": [source_id],
        "selected_source_ids_total": 1,
        "selected_source_ids_recorded": 1,
        "selected_source_ids_truncated_count": 0,
        "cited_source_ids": [],
        "cited_source_ids_total": 0,
        "cited_source_ids_recorded": 0,
        "cited_source_ids_truncated_count": 0,
        "selection_binding_scope": "global_exact_question_unattributed",
        "question_source_overlaps": [
            {
                "question_id": "q1",
                "binding_scope": "candidate_overlap_coarse_unattributed",
                "candidate_overlap_source_ids": [source_id],
            }
        ],
        "candidate_uncovered_question_ids": [],
        "question_overlaps_total": 1,
        "question_overlaps_recorded": 1,
        "question_overlaps_truncated_count": 0,
        "contract_status": "failed",
        "reason": reason,
    }


@pytest.fixture
def phase0_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    private = tmp_path / "data" / "private" / "phase0"
    cases_path = private / "cases.json"
    manifest_path = private / "manifest.json"
    traces_path = private / "traces.json"
    review_path = private / "review.jsonl"
    result_path = private / "result.json"
    seed_path = tmp_path / "data" / "knowledge_base_seed.json"

    cases = [
        {
            "id": f"case-{index:02d}",
            "query": f"PRIVATE QUERY {index:02d}",
            "privacy_class": "private_ticket_derived",
        }
        for index in range(30)
    ]
    _write_json(cases_path, cases)
    cases_hash = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "phase0-test-v1",
        "cases": [{"id": case["id"]} for case in cases],
        "integrity": {"cases_file_sha256": cases_hash},
    }
    _write_json(manifest_path, manifest)
    seed = [
        {
            "chunk_id": f"chunk-{index:02d}",
            "text_clean": f"PRIVATE QUERY {index:02d} PUBLISHED FACT {index:02d}",
            "status": "published",
            "source_type": "yonote",
        }
        for index in range(30)
    ] + [
        {
            "chunk_id": "unpublished-yonote",
            "text_clean": "PRIVATE QUERY",
            "status": "draft",
            "source_type": "yonote",
        },
        {
            "chunk_id": "published-xlsx",
            "text_clean": "PRIVATE QUERY",
            "status": "published",
            "source_type": "xlsx",
        },
    ]
    _write_json(seed_path, seed)

    reasons = (
        ["insufficient_sources"] * 6
        + ["low_confidence"] * 6
        + ["llm_response_profile_failed"] * 4
        + ["llm_response_too_long"] * 3
        + ["operator_requested"]
    )
    traces: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        reason = reasons[index] if index < len(reasons) else None
        source_id = f"chunk-{index:02d}"
        timestamp = analyzer.PHASE0_RUN_STARTED_AT + timedelta(seconds=index + 1)
        trace: dict[str, Any] = {
            "schema_version": analyzer.TRACE_ROW_SCHEMA_VERSION,
            "request_id": f"00000000-0000-0000-0000-{index:012d}",
            "eval_run_id": "phase0-run",
            "eval_case_id": case["id"],
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "was_escalated": reason is not None,
            "escalation_reason": reason,
            "max_reranker_score": 0.5,
            "query_analysis": {
                "category": "forums",
                "questions_count": 1,
                "topics_count": 2,
            },
            "retrieved_chunks": [
                {
                    "chunk_id": source_id,
                    "retrieval_score": 0.4,
                }
            ],
            "retrieve_trace": _retrieve_trace(source_id),
            "reranked_chunks": [
                {
                    "chunk_id": source_id,
                    "reranker_score": 0.5,
                    "retrieval_score": 0.4,
                }
            ],
            "reranker_trace": _reranker_trace(source_id),
            "generation_trace": {},
            "selected_source_ids": [],
            "generate_retries": [],
            "generator_model": (
                "model-a"
                if reason and reason.startswith("llm_")
                else None
            ),
            "cited_source_ids": [],
            "verify_trace": {},
            "verification_source_ids": [],
            "trace_timeline": [
                {"node": "analyze", "latency_ms": 1, "error_present": False},
                {"node": "retrieve", "latency_ms": 1, "error_present": False},
                {"node": "rerank", "latency_ms": 1, "error_present": False},
            ],
            "cache_hit": False,
            "verifier_triggered": False,
            "verifier_result": None,
            "ticket_outcome": "escalated" if reason else "answered",
            "llm_usage": [],
            "llm_prompt_tokens": 0,
            "llm_completion_tokens": 0,
            "llm_total_tokens": 0,
            "llm_estimated_cost_rub": 0.0,
            "total_latency_ms": 100,
            "prompt_version": "v1",
            "error_present": False,
            "error_code": None,
        }
        if reason and reason.startswith("llm_"):
            trace["generation_trace"] = _failed_generation_trace(source_id, reason)
            trace["selected_source_ids"] = [source_id]
            trace["generate_retries"] = [
                {"latency_ms": 1, "reason": "first_attempt_failed", "chunks": 1}
            ]
            trace["trace_timeline"].append(
                {"node": "generate", "latency_ms": 1, "error_present": False}
            )
        elif reason == "insufficient_sources":
            trace["trace_timeline"].append(
                {"node": "generate", "latency_ms": 1, "error_present": False}
            )
        traces.append(trace)
    private.mkdir(parents=True, exist_ok=True)
    _write_json(
        traces_path,
        {
            "schema_version": analyzer.TRACE_EXPORT_SCHEMA_VERSION,
            "eval_run_id": "phase0-run",
            "run_window": {
                "started_at": analyzer.PHASE0_RUN_STARTED_AT.isoformat().replace(
                    "+00:00", "Z"
                ),
                "completed_at": analyzer.PHASE0_RUN_COMPLETED_AT.isoformat().replace(
                    "+00:00", "Z"
                ),
            },
            "bindings": {
                "cases_file_sha256": cases_hash,
                "manifest_file_sha256": hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
            },
            "cases_total": 30,
            "rows": traces,
        },
    )
    monkeypatch.setattr(analyzer, "PRIVATE_DATA_ROOT", (tmp_path / "data" / "private").resolve())
    monkeypatch.setattr(analyzer, "PHASE0_EVAL_RUN_ID", "phase0-run")
    monkeypatch.setattr(analyzer, "PHASE0_CASES_SHA256", cases_hash)
    monkeypatch.setattr(
        analyzer,
        "PHASE0_MANIFEST_SHA256",
        hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        analyzer,
        "FROZEN_KB_SEED_SHA256",
        hashlib.sha256(seed_path.read_bytes()).hexdigest(),
    )
    return {
        "cases": cases_path,
        "manifest": manifest_path,
        "traces": traces_path,
        "seed": seed_path,
        "review": review_path,
        "result": result_path,
    }


def _prepare(paths: dict[str, Path]) -> list[dict[str, Any]]:
    return prepare_review(
        cases_path=paths["cases"],
        manifest_path=paths["manifest"],
        traces_path=paths["traces"],
        kb_seed_path=paths["seed"],
        review_output_path=paths["review"],
    )


def _summarize(paths: dict[str, Path]) -> dict[str, Any]:
    return summarize_review(
        cases_path=paths["cases"],
        manifest_path=paths["manifest"],
        traces_path=paths["traces"],
        kb_seed_path=paths["seed"],
        review_path=paths["review"],
        result_output_path=paths["result"],
    )


def test_reason_mapping_is_explicit_and_rejects_unknown() -> None:
    assert classify_escalation_reason("operator_requested") == "requested"
    assert classify_escalation_reason("low_confidence") == "threshold"
    assert classify_escalation_reason("llm_response_too_long") == "output"
    assert classify_escalation_reason("insufficient_sources") == "source_gap"
    with pytest.raises(ValueError, match="unsupported final escalation reason"):
        classify_escalation_reason("made_up")


def test_prepare_joins_seed_sets_enums_and_binds_provenance(
    phase0_files: dict[str, Path],
) -> None:
    rows = _prepare(phase0_files)

    assert len(rows) == 20
    assert Counter(row["escalation_reason"] for row in rows) == EXPECTED_REASON_COUNTS
    assert rows[0]["escalation_type"] == "source_gap"
    assert rows[0]["top1"] == {
        "chunk_id": "chunk-00",
        "reranker_score": 0.5,
        "status": "published",
        "source_type": "yonote",
        "text": "PRIVATE QUERY 00 PUBLISHED FACT 00",
        "text_sha256": hashlib.sha256(
            b"PRIVATE QUERY 00 PUBLISHED FACT 00"
        ).hexdigest(),
    }
    threshold = next(row for row in rows if row["escalation_type"] == "threshold")
    assert threshold["human_review"] == {
        "top1_answerability": "uncertain",
        "frozen_seed_answerability": "n/a",
        "rejected_candidate_correctness": "n/a",
        "reviewer_alias": "",
        "reviewed_at": "",
        "notes": "",
    }
    output_rows = [row for row in rows if row["escalation_type"] == "output"]
    assert len(output_rows) == 7
    assert all(row["rejected_candidate_available"] is False for row in output_rows)
    assert all(
        row["human_review"]["rejected_candidate_correctness"] == "unavailable"
        for row in output_rows
    )
    provenance = rows[0]["provenance"]
    assert all(row["provenance"] == provenance for row in rows)
    assert set(provenance) == {
        "cases_sha256",
        "manifest_sha256",
        "trace_export_sha256",
        "knowledge_base_seed_sha256",
        "eval_run_id",
        "published_yonote_universe_count",
    }
    assert provenance["published_yonote_universe_count"] == 30
    assert rows[0]["source_gap_full_seed_evidence"]["universe_count"] == 30
    candidate_ids = {
        candidate["chunk_id"]
        for candidate in rows[0]["source_gap_full_seed_evidence"]["candidates"]
    }
    assert "unpublished-yonote" not in candidate_ids
    assert "published-xlsx" not in candidate_ids
    assert rows[0]["evidence_path"]["query_analysis"] == {
        "category": "forums",
        "questions_count": 1,
        "topics_count": 2,
    }
    trace_export = json.loads(phase0_files["traces"].read_text(encoding="utf-8"))
    assert rows[0]["evidence_path"]["retrieve_trace"] == trace_export["rows"][0][
        "retrieve_trace"
    ]
    assert rows[0]["evidence_path"]["reranker_trace"] == trace_export["rows"][0][
        "reranker_trace"
    ]
    assert rows[0]["evidence_path"]["generation_execution"] == "attempted"
    assert threshold["evidence_path"]["generation_execution"] == "not_run"
    assert output_rows[0]["evidence_path"]["generation_execution"] == "attempted"
    assert analyzer._generation_execution({}) == "missing_telemetry"
    assert analyzer._generation_execution(
        {"trace_timeline": [{"node": "clarify"}]}
    ) == "not_run"
    assert analyzer._generation_execution(
        {
            "generator_model": None,
            "escalation_reason": "low_confidence",
            "trace_timeline": [
                {"node": "analyze"},
                {"node": "retrieve"},
                {"node": "rerank"},
                {"node": "escalate"},
            ],
        }
    ) == "not_run"
    assert analyzer._generation_execution(
        {"trace_timeline": [{"node": "respond"}]}
    ) == "attempted"
    assert analyzer._generation_execution(
        {
            "generator_model": "not_run",
            "trace_timeline": [{"node": "verify_decision"}],
        }
    ) == "attempted"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("top1_answerability", "maybe"),
        ("frozen_seed_answerability", "missing"),
        ("rejected_candidate_correctness", "mostly"),
    ],
)
def test_summarize_rejects_unknown_human_enums(
    phase0_files: dict[str, Path], field: str, value: str
) -> None:
    rows = _prepare(phase0_files)
    rows[0]["human_review"][field] = value
    _write_review(phase0_files["review"], rows)

    with pytest.raises(ValueError, match=field):
        _summarize(phase0_files)
    assert not phase0_files["result"].exists()


def test_summarize_enforces_source_specific_verdict_columns(
    phase0_files: dict[str, Path],
) -> None:
    rows = _prepare(phase0_files)
    source_gap = next(row for row in rows if row["escalation_type"] == "source_gap")
    source_gap["human_review"]["top1_answerability"] = "full"
    _write_review(phase0_files["review"], rows)

    with pytest.raises(ValueError, match="only for threshold failures"):
        _summarize(phase0_files)


def test_lost_generation_draft_cannot_be_declared_correct(
    phase0_files: dict[str, Path],
) -> None:
    rows = _prepare(phase0_files)
    output = next(row for row in rows if row["escalation_type"] == "output")
    output["human_review"]["rejected_candidate_correctness"] = "correct"
    _write_review(phase0_files["review"], rows)

    with pytest.raises(ValueError, match="require unavailable"):
        _summarize(phase0_files)


def test_summarize_rejects_provenance_change(
    phase0_files: dict[str, Path],
) -> None:
    rows = _prepare(phase0_files)
    rows[0]["provenance"]["knowledge_base_seed_sha256"] = "0" * 64
    _write_review(phase0_files["review"], rows)

    with pytest.raises(ValueError, match="mechanical evidence"):
        _summarize(phase0_files)


def test_six_confirmed_thresholds_and_seven_lost_drafts_cannot_confirm(
    phase0_files: dict[str, Path],
) -> None:
    rows = _prepare(phase0_files)
    for row in rows:
        if row["escalation_type"] == "threshold":
            row["human_review"]["top1_answerability"] = "full"
        elif row["escalation_type"] == "source_gap":
            row["human_review"]["frozen_seed_answerability"] = "absent"
    _write_review(phase0_files["review"], rows)

    result = _summarize(phase0_files)

    assert result["denominator"] == 20
    assert result["reason_total"] == 20
    assert result["confirmed_fixable"] == {
        "numerator": 6,
        "threshold_top1_full": 6,
        "output_correct_available": 0,
    }
    assert result["bounds"] == {
        "lower_numerator": 6,
        "upper_numerator": 13,
        "lower_rate": 0.3,
        "upper_rate": 0.65,
    }
    assert result["lost_generation_drafts"] == 7
    assert result["phase0_evidence_can_confirm"] is False
    assert result["status"] == "INCONCLUSIVE_STOP"
    assert result["disposition_counts"] == {
        "confirmed_threshold": 6,
        "justified_source_gap": 6,
        "output_unavailable": 7,
        "requested": 1,
    }
    assert result["disposition_total"] == 20
    assert sum(row["total"] for row in result["result_table"]) == 20
    output_table = [
        row for row in result["result_table"] if row["escalation_type"] == "output"
    ]
    assert sum(row["verdict_counts"]["unavailable"] for row in output_table) == 7
    assert result["historical_operator_replies_used_as_facts"] is False
    assert result["provenance"]["review_sha256"] == hashlib.sha256(
        phase0_files["review"].read_bytes()
    ).hexdigest()


def test_upper_bound_below_ten_refutes_and_stops(
    phase0_files: dict[str, Path],
) -> None:
    rows = _prepare(phase0_files)
    for row in rows:
        if row["escalation_type"] == "threshold":
            row["human_review"]["top1_answerability"] = "none"
        elif row["escalation_type"] == "source_gap":
            row["human_review"]["frozen_seed_answerability"] = "absent"
    _write_review(phase0_files["review"], rows)

    result = _summarize(phase0_files)

    assert result["bounds"]["upper_numerator"] == 7
    assert result["status"] == "REFUTED_STOP"


def test_stop_criterion_boundaries_are_exactly_nine_and_ten() -> None:
    assert decision_from_bounds(9, 9) == "REFUTED_STOP"
    assert decision_from_bounds(9, 10) == "INCONCLUSIVE_STOP"
    assert decision_from_bounds(10, 10) == "CONFIRMED"


@pytest.mark.parametrize(
    ("reviewer_alias", "reviewed_at", "error"),
    [
        ("", "2026-08-10T12:00:00+07:00", "reviewer_alias"),
        ("qa", "not-a-timestamp", "reviewed_at"),
        ("qa", "2026-08-10T12:00:00", "timezone"),
    ],
)
def test_human_scored_rows_require_alias_and_zoned_timestamp(
    phase0_files: dict[str, Path],
    reviewer_alias: str,
    reviewed_at: str,
    error: str,
) -> None:
    rows = _prepare(phase0_files)
    rows[0]["human_review"].update(
        {
            "frozen_seed_answerability": "absent",
            "reviewer_alias": reviewer_alias,
            "reviewed_at": reviewed_at,
        }
    )
    _write_jsonl(phase0_files["review"], rows)

    with pytest.raises(ValueError, match=error):
        _summarize(phase0_files)


def test_summarize_rejects_tampered_mechanical_evidence(
    phase0_files: dict[str, Path],
) -> None:
    rows = _prepare(phase0_files)
    _write_review(phase0_files["review"], rows)
    rows[0]["evidence_path"]["retrieve_trace"]["retrieval_method"] = "tampered"
    _write_jsonl(phase0_files["review"], rows)

    with pytest.raises(ValueError, match="mechanical evidence"):
        _summarize(phase0_files)


@pytest.mark.parametrize(
    "constant",
    ["PHASE0_CASES_SHA256", "PHASE0_MANIFEST_SHA256", "FROZEN_KB_SEED_SHA256"],
)
def test_prepare_rejects_any_unapproved_frozen_hash(
    phase0_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
) -> None:
    monkeypatch.setattr(analyzer, constant, "0" * 64)

    with pytest.raises(ValueError, match="SHA-256"):
        _prepare(phase0_files)


@pytest.mark.parametrize("mutation", ["wrong_run", "legacy_array"])
def test_prepare_rejects_wrong_run_or_unbound_legacy_export(
    phase0_files: dict[str, Path], mutation: str
) -> None:
    report = json.loads(phase0_files["traces"].read_text(encoding="utf-8"))
    if mutation == "wrong_run":
        report["eval_run_id"] = "another-run"
    else:
        report = report["rows"]
    _write_json(phase0_files["traces"], report)

    with pytest.raises(ValueError, match="bound JSON export|eval_run_id"):
        _prepare(phase0_files)


@pytest.mark.parametrize(
    "mutation",
    [
        "top_schema",
        "top_missing",
        "top_extra",
        "row_schema",
        "row_missing",
        "row_extra",
        "cases_total",
        "run_window",
        "cache_hit",
        "request_id",
        "timestamp",
        "raw_questions",
        "bad_question_count",
    ],
)
def test_prepare_requires_exact_typed_exporter_contract(
    phase0_files: dict[str, Path], mutation: str
) -> None:
    report = json.loads(phase0_files["traces"].read_text(encoding="utf-8"))
    row = report["rows"][0]
    if mutation == "top_schema":
        report["schema_version"] = "wrong"
    elif mutation == "top_missing":
        report.pop("cases_total")
    elif mutation == "top_extra":
        report["unexpected"] = True
    elif mutation == "row_schema":
        row["schema_version"] = "wrong"
    elif mutation == "row_missing":
        row.pop("prompt_version")
    elif mutation == "row_extra":
        row["unexpected"] = True
    elif mutation == "cases_total":
        report["cases_total"] = 29
    elif mutation == "run_window":
        report["run_window"]["started_at"] = "2026-08-06T00:00:00Z"
    elif mutation == "cache_hit":
        row["cache_hit"] = True
    elif mutation == "request_id":
        row["request_id"] = "not-a-uuid"
    elif mutation == "timestamp":
        row["timestamp"] = "2026-08-07T00:00:00Z"
    elif mutation == "raw_questions":
        row["query_analysis"]["questions"] = [{"text": "forbidden"}]
    else:
        row["query_analysis"]["questions_count"] = True
    _write_json(phase0_files["traces"], report)

    with pytest.raises(ValueError):
        _prepare(phase0_files)


def test_input_snapshots_are_read_once_for_hash_parse_and_provenance(
    phase0_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = analyzer._read_bounded_regular_bytes
    counts: Counter[Path] = Counter()
    input_paths = {
        phase0_files[key].resolve() for key in ("cases", "manifest", "traces", "seed")
    }

    def read_then_swap(
        path: Path,
        *,
        label: str,
        max_bytes: int,
    ) -> bytes:
        payload = original(path, label=label, max_bytes=max_bytes)
        resolved = path.resolve()
        counts[resolved] += 1
        if resolved == phase0_files["cases"].resolve() and counts[resolved] == 1:
            path.write_text("[]\n", encoding="utf-8")
        return payload

    monkeypatch.setattr(analyzer, "_read_bounded_regular_bytes", read_then_swap)

    rows = _prepare(phase0_files)

    assert len(rows) == 20
    assert {path: counts[path] for path in input_paths} == {
        path: 1 for path in input_paths
    }
    assert rows[0]["provenance"]["cases_sha256"] == analyzer.PHASE0_CASES_SHA256


def test_trace_chunk_outside_published_yonote_universe_fails_closed(
    phase0_files: dict[str, Path],
) -> None:
    report = json.loads(phase0_files["traces"].read_text(encoding="utf-8"))
    report["rows"][0]["reranked_chunks"][0]["chunk_id"] = "published-xlsx"
    _write_json(phase0_files["traces"], report)

    with pytest.raises(ValueError, match="published-Yonote universe"):
        _prepare(phase0_files)


def test_private_path_must_be_under_configured_project_root(
    phase0_files: dict[str, Path], tmp_path: Path
) -> None:
    decoy = tmp_path / "decoy" / "data" / "private" / "review.jsonl"

    with pytest.raises(ValueError, match="data/private"):
        prepare_review(
            cases_path=phase0_files["cases"],
            manifest_path=phase0_files["manifest"],
            traces_path=phase0_files["traces"],
            kb_seed_path=phase0_files["seed"],
            review_output_path=decoy,
        )
    outside_trace = tmp_path / "trace-export.json"
    outside_trace.write_bytes(phase0_files["traces"].read_bytes())
    with pytest.raises(ValueError, match="data/private"):
        prepare_review(
            cases_path=phase0_files["cases"],
            manifest_path=phase0_files["manifest"],
            traces_path=outside_trace,
            kb_seed_path=phase0_files["seed"],
            review_output_path=phase0_files["review"],
        )


def test_atomic_publish_rejects_parent_symlink_swap(
    phase0_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = analyzer.PRIVATE_DATA_ROOT
    first = private_root / "publish-a"
    second = private_root / "publish-b"
    alias = private_root / "publish-link"
    first.mkdir()
    second.mkdir()
    try:
        alias.symlink_to(first, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")
    output = alias / "review.jsonl"
    original_link = analyzer.os.link

    def swap_then_link(
        source: Path,
        destination: Path,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        alias.unlink()
        alias.symlink_to(second, target_is_directory=True)
        original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(analyzer.os, "link", swap_then_link)

    with pytest.raises(ValueError, match="parent changed"):
        prepare_review(
            cases_path=phase0_files["cases"],
            manifest_path=phase0_files["manifest"],
            traces_path=phase0_files["traces"],
            kb_seed_path=phase0_files["seed"],
            review_output_path=output,
        )
    assert not (first / "review.jsonl").exists()
    assert not (second / "review.jsonl").exists()


def test_atomic_publish_cleans_up_when_canonical_parent_is_renamed(
    phase0_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = analyzer.PRIVATE_DATA_ROOT
    canonical_parent = private_root / "canonical-parent"
    moved_parent = private_root / "canonical-parent-moved"
    canonical_parent.mkdir()
    output = canonical_parent / "review.jsonl"
    original_link = analyzer.os.link

    def link_then_rename_parent(
        source: Path,
        destination: Path,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        original_link(source, destination, *args, **kwargs)
        canonical_parent.rename(moved_parent)
        canonical_parent.mkdir()

    monkeypatch.setattr(analyzer.os, "link", link_then_rename_parent)

    with pytest.raises((OSError, ValueError)):
        analyzer._write_bytes_exclusive(output, b"private-review-payload")
    assert not output.exists()
    assert not (moved_parent / output.name).exists()
    for parent in (canonical_parent, moved_parent):
        if parent.exists():
            assert not list(parent.glob(f".{output.name}.*.tmp"))


def test_atomic_publish_requires_preexisting_parent(
    phase0_files: dict[str, Path],
) -> None:
    missing_parent = analyzer.PRIVATE_DATA_ROOT / "missing-parent"
    output = missing_parent / "review.jsonl"

    with pytest.raises(ValueError, match="cannot be safely resolved"):
        analyzer._write_bytes_exclusive(output, b"private-review-payload")

    assert not missing_parent.exists()


def test_partial_threshold_is_not_in_upper_bound_and_dispositions_are_exact(
    phase0_files: dict[str, Path],
) -> None:
    rows = _prepare(phase0_files)
    source_verdicts = ["full", "partial", "absent", "uncertain", "full", "partial"]
    source_index = 0
    for row in rows:
        if row["escalation_type"] == "threshold":
            row["human_review"]["top1_answerability"] = "partial"
        elif row["escalation_type"] == "source_gap":
            row["human_review"]["frozen_seed_answerability"] = source_verdicts[
                source_index
            ]
            source_index += 1
    _write_review(phase0_files["review"], rows)

    result = _summarize(phase0_files)

    assert result["bounds"]["upper_numerator"] == 7
    assert result["disposition_counts"] == {
        "justified_source_gap": 1,
        "not_confirmed_threshold": 6,
        "output_unavailable": 7,
        "requested": 1,
        "retrieval_or_coverage_failure": 4,
        "uncertain_source_gap": 1,
    }


def test_private_atomic_outputs_refuse_overwrite(
    phase0_files: dict[str, Path],
) -> None:
    _prepare(phase0_files)
    original = phase0_files["review"].read_bytes()

    with pytest.raises(FileExistsError, match="already exists"):
        _prepare(phase0_files)
    assert phase0_files["review"].read_bytes() == original


def test_cli_stdout_contains_only_status_path_and_hash(
    phase0_files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "prepare",
            "--cases",
            str(phase0_files["cases"]),
            "--manifest",
            str(phase0_files["manifest"]),
            "--traces",
            str(phase0_files["traces"]),
            "--kb-seed",
            str(phase0_files["seed"]),
            "--review-output",
            str(phase0_files["review"]),
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out.splitlines()
    assert [line.split("=", 1)[0] for line in output] == [
        "STATUS",
        "OUTPUT",
        "SHA256",
    ]
    assert "PRIVATE QUERY" not in "\n".join(output)
    assert "low_confidence" not in "\n".join(output)
