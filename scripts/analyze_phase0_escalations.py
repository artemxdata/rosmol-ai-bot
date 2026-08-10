from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

from scripts import export_phase0_trace_review as trace_exporter  # noqa: E402
from src.rag.seed_retriever import SeedRetriever  # noqa: E402

PRIVATE_DATA_ROOT = (PROJECT_ROOT / "data" / "private").resolve()
PHASE0_EVAL_RUN_ID = "ask-eval-61971dbd-75ff-44b0-8eef-0e64c5b27168"
PHASE0_CASES_SHA256 = "aff198bbc98d07894a3e1676e3457891e3a38f674315051505b681641fe9d02d"
PHASE0_MANIFEST_SHA256 = (
    "8cf9959aaf9caf8728b386214ebba826f7bb0eb349f27fd2737e2830eb353264"
)
FROZEN_KB_SEED_SHA256 = (
    "8586a7e6a3ffe1565e625d81ef8d4d6b7d37ee15eabf9be5200fd61c28af0b3d"
)
TRACE_EXPORT_SCHEMA_VERSION = trace_exporter.EXPORT_SCHEMA_VERSION
TRACE_ROW_SCHEMA_VERSION = trace_exporter.ROW_SCHEMA_VERSION
TRACE_ROW_FIELDS = trace_exporter.ROW_FIELDS
PHASE0_RUN_STARTED_AT = trace_exporter.PHASE0_RUN_STARTED_AT
PHASE0_RUN_COMPLETED_AT = trace_exporter.PHASE0_RUN_COMPLETED_AT
TRACE_EXPORT_FIELDS = frozenset(
    {"schema_version", "eval_run_id", "run_window", "bindings", "cases_total", "rows"}
)
TRACE_BINDING_FIELDS = frozenset({"cases_file_sha256", "manifest_file_sha256"})
MAX_FROZEN_INPUT_BYTES = 2 * 1024 * 1024
MAX_TRACE_EXPORT_BYTES = trace_exporter.MAX_EXPORT_BYTES
MAX_KB_SEED_BYTES = 16 * 1024 * 1024
MAX_REVIEW_BYTES = 32 * 1024 * 1024
_SUPPORTS_STABLE_PARENT_DIR_FD = os.name != "nt" and all(
    function in os.supports_dir_fd for function in (os.link, os.stat, os.unlink)
)

EXPECTED_CASES_TOTAL = 30
EXPECTED_ESCALATIONS_TOTAL = 20
FIXABLE_MAJORITY = 10
EXPECTED_REASON_COUNTS = {
    "insufficient_sources": 6,
    "low_confidence": 6,
    "llm_response_profile_failed": 4,
    "llm_response_too_long": 3,
    "operator_requested": 1,
}

TOP1_ANSWERABILITY = frozenset({"full", "partial", "none", "uncertain", "n/a"})
FROZEN_SEED_ANSWERABILITY = frozenset(
    {"full", "partial", "absent", "uncertain", "n/a"}
)
REJECTED_CANDIDATE_CORRECTNESS = frozenset(
    {"correct", "partial", "incorrect", "unavailable", "n/a"}
)

REASON_TYPE = {
    "operator_requested": "requested",
    "low_confidence": "threshold",
    "insufficient_sources": "source_gap",
    "no_relevant_chunks": "source_gap",
    "llm_response_profile_failed": "output",
    "llm_response_too_long": "output",
    "llm_response_contract_failed": "output",
    "llm_source_citation_failed": "output",
    "llm_source_fact_binding_failed": "output",
}
OUTPUT_REASONS = frozenset(
    reason for reason, reason_type in REASON_TYPE.items() if reason_type == "output"
)

REVIEW_SCHEMA_VERSION = "phase-a-escalation-review-v1"
RESULT_SCHEMA_VERSION = "phase-a-escalation-result-v1"
TRUTH_POLICY = "frozen_knowledge_base_seed_status_published_source_type_yonote_only"


def classify_escalation_reason(reason: str) -> str:
    """Map a final escalation reason to the Phase A review layer."""

    try:
        return REASON_TYPE[reason]
    except KeyError as exc:
        raise ValueError(f"unsupported final escalation reason: {reason!r}") from exc


def decision_from_bounds(lower: int, upper: int) -> str:
    """Apply the approved majority stop criterion to integer numerators."""

    if not 0 <= lower <= upper <= EXPECTED_ESCALATIONS_TOTAL:
        raise ValueError("bounds must satisfy 0 <= lower <= upper <= 20")
    if lower >= FIXABLE_MAJORITY:
        return "CONFIRMED"
    if upper < FIXABLE_MAJORITY:
        return "REFUTED_STOP"
    return "INCONCLUSIVE_STOP"


def prepare_review(
    *,
    cases_path: Path,
    manifest_path: Path,
    traces_path: Path,
    kb_seed_path: Path,
    review_output_path: Path,
) -> list[dict[str, Any]]:
    """Create the private, human-editable review sheet for the 20 escalations."""

    _require_private_path(review_output_path, label="review output")
    inputs = _load_inputs(
        cases_path=cases_path,
        manifest_path=manifest_path,
        traces_path=traces_path,
        kb_seed_path=kb_seed_path,
    )
    rows = _build_review_rows(inputs)

    if len(rows) != EXPECTED_ESCALATIONS_TOTAL:
        raise ValueError("review must contain exactly 20 escalations")
    _write_jsonl_exclusive(review_output_path, rows)
    return rows


def summarize_review(
    *,
    cases_path: Path,
    manifest_path: Path,
    traces_path: Path,
    kb_seed_path: Path,
    review_path: Path,
    result_output_path: Path,
) -> dict[str, Any]:
    """Validate human verdicts and write the private Phase A decision artifact."""

    _require_private_path(review_path, label="review input")
    _require_private_path(result_output_path, label="result output")
    inputs = _load_inputs(
        cases_path=cases_path,
        manifest_path=manifest_path,
        traces_path=traces_path,
        kb_seed_path=kb_seed_path,
    )
    review_bytes = _read_bounded_regular_bytes(
        review_path,
        label="review",
        max_bytes=MAX_REVIEW_BYTES,
    )
    rows = _decode_jsonl_bytes(review_bytes, label="review")
    _validate_review_rows(rows, inputs=inputs)

    reason_counts = Counter(str(row["escalation_reason"]) for row in rows)
    type_counts = Counter(str(row["escalation_type"]) for row in rows)
    if sum(reason_counts.values()) != EXPECTED_ESCALATIONS_TOTAL:
        raise ValueError("escalation reason totals must sum to 20")

    confirmed_threshold = sum(
        row["escalation_type"] == "threshold"
        and row["human_review"]["top1_answerability"] == "full"
        for row in rows
    )
    confirmed_output = sum(
        row["escalation_type"] == "output"
        and row["rejected_candidate_available"] is True
        and row["human_review"]["rejected_candidate_correctness"] == "correct"
        for row in rows
    )
    confirmed_fixable = confirmed_threshold + confirmed_output

    uncertain_threshold = sum(
        row["escalation_type"] == "threshold"
        and row["human_review"]["top1_answerability"]
        in {"uncertain", "n/a"}
        for row in rows
    )
    unavailable_output = sum(
        row["escalation_type"] == "output"
        and row["human_review"]["rejected_candidate_correctness"]
        == "unavailable"
        for row in rows
    )
    lower = confirmed_fixable
    upper = lower + uncertain_threshold + unavailable_output
    decision = decision_from_bounds(lower, upper)

    output_unavailable = sum(
        row["escalation_type"] == "output"
        and row["human_review"]["rejected_candidate_correctness"] == "unavailable"
        for row in rows
    )
    threshold_total = type_counts.get("threshold", 0)
    phase0_can_confirm = not (
        output_unavailable >= 7 and threshold_total < FIXABLE_MAJORITY
    )
    if not phase0_can_confirm and decision == "CONFIRMED":
        raise ValueError("lost Phase 0 drafts cannot support a confirmed decision")

    result_table = _build_result_table(rows)
    if sum(item["total"] for item in result_table) != EXPECTED_ESCALATIONS_TOTAL:
        raise ValueError("result table totals must sum to 20")
    if sum(item["confirmed_fixable"] for item in result_table) != lower:
        raise ValueError("result table lower bound differs from the decision")
    if sum(item["possibly_fixable"] for item in result_table) != upper:
        raise ValueError("result table upper bound differs from the decision")
    disposition_counts = Counter(_derived_disposition(row) for row in rows)
    if sum(disposition_counts.values()) != EXPECTED_ESCALATIONS_TOTAL:
        raise ValueError("derived disposition totals must sum to 20")

    provenance = dict(inputs["provenance"])
    provenance["review_sha256"] = hashlib.sha256(review_bytes).hexdigest()
    result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": decision,
        "denominator": EXPECTED_ESCALATIONS_TOTAL,
        "stop_criterion": {
            "majority_numerator": FIXABLE_MAJORITY,
            "confirmed_when": "lower_bound_numerator >= 10",
            "refuted_when": "upper_bound_numerator < 10",
            "otherwise": "INCONCLUSIVE_STOP",
        },
        "confirmed_fixable": {
            "numerator": confirmed_fixable,
            "threshold_top1_full": confirmed_threshold,
            "output_correct_available": confirmed_output,
        },
        "bounds": {
            "lower_numerator": lower,
            "upper_numerator": upper,
            "lower_rate": lower / EXPECTED_ESCALATIONS_TOTAL,
            "upper_rate": upper / EXPECTED_ESCALATIONS_TOTAL,
        },
        "phase0_evidence_can_confirm": phase0_can_confirm,
        "lost_generation_drafts": output_unavailable,
        "reason_counts": dict(sorted(reason_counts.items())),
        "reason_total": sum(reason_counts.values()),
        "type_counts": dict(sorted(type_counts.items())),
        "disposition_counts": dict(sorted(disposition_counts.items())),
        "disposition_total": sum(disposition_counts.values()),
        "result_table": result_table,
        "truth_policy": TRUTH_POLICY,
        "historical_operator_replies_used_as_facts": False,
        "provenance": provenance,
    }
    _write_json_exclusive(result_output_path, result)
    return result


def _build_result_table(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for reason in EXPECTED_REASON_COUNTS:
        group = [row for row in rows if row["escalation_reason"] == reason]
        reason_type = classify_escalation_reason(reason)
        if reason_type == "threshold":
            verdict_field = "top1_answerability"
            verdicts = Counter(
                row["human_review"][verdict_field] for row in group
            )
            confirmed = verdicts.get("full", 0)
            possible = confirmed + sum(
                verdicts.get(value, 0) for value in ("uncertain", "n/a")
            )
        elif reason_type == "output":
            verdict_field = "rejected_candidate_correctness"
            verdicts = Counter(
                row["human_review"][verdict_field] for row in group
            )
            confirmed = sum(
                row["rejected_candidate_available"] is True
                and row["human_review"][verdict_field] == "correct"
                for row in group
            )
            possible = confirmed + verdicts.get("unavailable", 0)
        elif reason_type == "source_gap":
            verdict_field = "frozen_seed_answerability"
            verdicts = Counter(
                row["human_review"][verdict_field] for row in group
            )
            confirmed = 0
            possible = 0
        else:
            verdict_field = "n/a"
            verdicts = Counter({"n/a": len(group)})
            confirmed = 0
            possible = 0
        result.append(
            {
                "escalation_reason": reason,
                "escalation_type": reason_type,
                "total": len(group),
                "verdict_field": verdict_field,
                "verdict_counts": dict(sorted(verdicts.items())),
                "confirmed_fixable": confirmed,
                "possibly_fixable": possible,
            }
        )
    return result


def _derived_disposition(row: Mapping[str, Any]) -> str:
    reason_type = row["escalation_type"]
    human = row["human_review"]
    if reason_type == "source_gap":
        verdict = human["frozen_seed_answerability"]
        if verdict == "absent":
            return "justified_source_gap"
        if verdict in {"full", "partial"}:
            return "retrieval_or_coverage_failure"
        return "uncertain_source_gap"
    if reason_type == "threshold":
        verdict = human["top1_answerability"]
        if verdict == "full":
            return "confirmed_threshold"
        if verdict in {"none", "partial"}:
            return "not_confirmed_threshold"
        return "uncertain_threshold"
    if reason_type == "output":
        return "output_unavailable"
    return "requested"


def _load_inputs(
    *,
    cases_path: Path,
    manifest_path: Path,
    traces_path: Path,
    kb_seed_path: Path,
) -> dict[str, Any]:
    _require_private_path(cases_path, label="frozen cases")
    _require_private_path(manifest_path, label="frozen manifest")
    _require_private_path(traces_path, label="trace export")
    cases_bytes = _read_bounded_regular_bytes(
        cases_path, label="frozen cases", max_bytes=MAX_FROZEN_INPUT_BYTES
    )
    manifest_bytes = _read_bounded_regular_bytes(
        manifest_path, label="frozen manifest", max_bytes=MAX_FROZEN_INPUT_BYTES
    )
    trace_bytes = _read_bounded_regular_bytes(
        traces_path, label="trace export", max_bytes=MAX_TRACE_EXPORT_BYTES
    )
    kb_seed_bytes = _read_bounded_regular_bytes(
        kb_seed_path,
        label="frozen knowledge base seed",
        max_bytes=MAX_KB_SEED_BYTES,
    )
    cases_sha256 = hashlib.sha256(cases_bytes).hexdigest()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    trace_sha256 = hashlib.sha256(trace_bytes).hexdigest()
    kb_seed_sha256 = hashlib.sha256(kb_seed_bytes).hexdigest()
    if cases_sha256 != PHASE0_CASES_SHA256:
        raise ValueError("cases SHA-256 differs from the approved Phase 0 input")
    if manifest_sha256 != PHASE0_MANIFEST_SHA256:
        raise ValueError("manifest SHA-256 differs from the approved Phase 0 input")
    if kb_seed_sha256 != FROZEN_KB_SEED_SHA256:
        raise ValueError("KB seed SHA-256 differs from the frozen Phase 0 truth set")
    cases = _decode_json_array(cases_bytes, label="frozen cases")
    manifest = _decode_json_object(manifest_bytes, label="frozen manifest")
    traces, trace_bindings = _decode_trace_export(trace_bytes)
    seed = _decode_json_array(kb_seed_bytes, label="frozen knowledge base seed")

    if len(cases) != EXPECTED_CASES_TOTAL:
        raise ValueError("frozen cases must contain exactly 30 cases")
    case_by_id = _unique_by_string_key(cases, "id", label="frozen cases")
    manifest_cases = _as_object_list(manifest.get("cases"))
    if len(manifest_cases) != EXPECTED_CASES_TOTAL:
        raise ValueError("frozen manifest must contain exactly 30 cases")
    manifest_ids = set(
        _unique_by_string_key(manifest_cases, "id", label="manifest cases")
    )
    if manifest_ids != set(case_by_id):
        raise ValueError("frozen cases and manifest case ids do not match")

    integrity = manifest.get("integrity")
    if isinstance(integrity, Mapping):
        expected_cases_hash = integrity.get("cases_file_sha256")
        if expected_cases_hash and expected_cases_hash != cases_sha256:
            raise ValueError("manifest cases_file_sha256 does not match frozen cases")
    if trace_bindings.get("cases_file_sha256") != PHASE0_CASES_SHA256:
        raise ValueError("trace export is not bound to the approved frozen cases")
    if trace_bindings.get("manifest_file_sha256") != PHASE0_MANIFEST_SHA256:
        raise ValueError("trace export is not bound to the approved frozen manifest")

    if len(traces) != EXPECTED_CASES_TOTAL:
        raise ValueError("trace export must contain exactly 30 traces")
    trace_by_case: dict[str, dict[str, Any]] = {}
    request_ids: set[str] = set()
    run_ids: set[str] = set()
    for trace in traces:
        case_id = _trace_case_id(trace)
        request_id = _nonempty_string(trace.get("request_id"), "request_id")
        run_id = _nonempty_string(trace.get("eval_run_id"), "eval_run_id")
        if case_id in trace_by_case:
            raise ValueError(f"duplicate trace eval_case_id: {case_id!r}")
        if request_id in request_ids:
            raise ValueError(f"duplicate trace request_id: {request_id!r}")
        trace_by_case[case_id] = trace
        request_ids.add(request_id)
        run_ids.add(run_id)
    if set(trace_by_case) != set(case_by_id):
        raise ValueError("trace export and frozen case ids do not match")
    if run_ids != {PHASE0_EVAL_RUN_ID}:
        raise ValueError("trace export eval_run_id differs from approved Phase 0")

    escalations = [trace for trace in traces if trace.get("was_escalated") is True]
    if len(escalations) != EXPECTED_ESCALATIONS_TOTAL:
        raise ValueError("trace export must contain exactly 20 final escalations")
    for trace in escalations:
        reason = _nonempty_string(trace.get("escalation_reason"), "escalation_reason")
        classify_escalation_reason(reason)
    observed_reason_counts = Counter(
        str(trace["escalation_reason"]) for trace in escalations
    )
    reason_total = sum(observed_reason_counts.values())
    if reason_total != EXPECTED_ESCALATIONS_TOTAL:
        raise ValueError("escalation reason totals must sum to 20")
    if dict(observed_reason_counts) != EXPECTED_REASON_COUNTS:
        raise ValueError("trace export does not match the frozen Phase 0 reason totals")

    published_yonote = [
        record
        for record in seed
        if record.get("status") == "published"
        and record.get("source_type") == "yonote"
    ]
    seed_by_id = _unique_by_string_key(
        published_yonote,
        "chunk_id",
        label="published Yonote KB truth universe",
    )
    if not seed_by_id:
        raise ValueError("published Yonote KB truth universe must not be empty")
    for trace in traces:
        missing_ids = sorted(_trace_chunk_ids(trace) - set(seed_by_id))
        if missing_ids:
            raise ValueError(
                "trace chunk IDs are absent from the frozen published-Yonote universe: "
                + ", ".join(missing_ids[:5])
            )

    provenance = {
        "cases_sha256": cases_sha256,
        "manifest_sha256": manifest_sha256,
        "trace_export_sha256": trace_sha256,
        "knowledge_base_seed_sha256": kb_seed_sha256,
        "eval_run_id": PHASE0_EVAL_RUN_ID,
        "published_yonote_universe_count": len(published_yonote),
    }
    return {
        "cases": cases,
        "case_by_id": case_by_id,
        "traces": traces,
        "trace_by_case": trace_by_case,
        "escalations": escalations,
        "provenance": provenance,
        "seed_by_id": seed_by_id,
        "seed_retriever": SeedRetriever(published_yonote),
    }


def _build_review_rows(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seed_by_id = inputs["seed_by_id"]
    retriever = inputs["seed_retriever"]
    for trace in inputs["escalations"]:
        case_id = _trace_case_id(trace)
        case = inputs["case_by_id"][case_id]
        query = _nonempty_string(case.get("query"), "case query")
        reason = _nonempty_string(trace.get("escalation_reason"), "escalation_reason")
        reason_type = classify_escalation_reason(reason)
        retrieved = [
            _review_chunk(item, seed_by_id=seed_by_id, score_kind="retrieval")
            for item in _as_object_list(trace.get("retrieved_chunks"))
        ]
        reranked = [
            _review_chunk(item, seed_by_id=seed_by_id, score_kind="reranker")
            for item in _as_object_list(
                trace.get("reranker_scores", trace.get("reranked_chunks"))
            )
        ]
        generation_trace = _mapping_copy(trace.get("generation_trace"))
        generation_retries = _as_object_list(trace.get("generate_retries"))
        verifier_trace = _mapping_copy(trace.get("verify_trace"))
        selected_ids = _string_list(trace.get("selected_source_ids"))
        cited_ids = _string_list(trace.get("cited_source_ids"))
        verification_ids = _string_list(trace.get("verification_source_ids"))
        source_gap_evidence = None
        if reason_type == "source_gap":
            candidates = retriever.retrieve(
                query,
                filters={"status": "published", "source_type": "yonote"},
                top_k=10,
            )
            source_gap_evidence = {
                "universe_count": len(seed_by_id),
                "candidate_limit": 10,
                "candidates": [
                    {
                        "chunk_id": candidate.chunk_id,
                        "lexical_score": candidate.score,
                        "text": candidate.text,
                        "text_sha256": hashlib.sha256(
                            candidate.text.encode("utf-8")
                        ).hexdigest(),
                        "status": candidate.metadata.get("status"),
                        "source_type": candidate.metadata.get("source_type"),
                    }
                    for candidate in candidates
                ],
            }
        rows.append(
            {
                "schema_version": REVIEW_SCHEMA_VERSION,
                "case_id": case_id,
                "request_id": _nonempty_string(trace.get("request_id"), "request_id"),
                "eval_run_id": PHASE0_EVAL_RUN_ID,
                "query": query,
                "escalation_reason": reason,
                "escalation_type": reason_type,
                "max_reranker_score": _optional_number(trace.get("max_reranker_score")),
                "top1": reranked[0] if reranked else None,
                "top_k": reranked,
                "evidence_path": {
                    "query_analysis": _safe_query_analysis(trace.get("query_analysis")),
                    "retrieved": retrieved,
                    "retrieve_trace": _mapping_copy(trace.get("retrieve_trace")),
                    "reranked": reranked,
                    "reranker_trace": _mapping_copy(trace.get("reranker_trace")),
                    "selected_source_ids": selected_ids,
                    "cited_source_ids": cited_ids,
                    "verification_source_ids": verification_ids,
                    "node_sequence": [
                        str(item["node"])
                        for item in _as_object_list(trace.get("trace_timeline"))
                        if isinstance(item.get("node"), str) and item["node"]
                    ],
                    "generator_model": str(trace.get("generator_model") or "unknown"),
                    "generation_execution": _generation_execution(trace),
                    "generation_trace": generation_trace,
                    "generation_retries": generation_retries,
                    "verifier_trace": verifier_trace,
                },
                "source_gap_full_seed_evidence": source_gap_evidence,
                "generation_contract_reason": _generation_contract_reason(trace),
                "generation_retry_reasons": _generation_retry_reasons(trace),
                "rejected_candidate_available": False,
                "human_review": {
                    "top1_answerability": (
                        "uncertain" if reason_type == "threshold" else "n/a"
                    ),
                    "frozen_seed_answerability": (
                        "uncertain" if reason_type == "source_gap" else "n/a"
                    ),
                    "rejected_candidate_correctness": (
                        "unavailable" if reason_type == "output" else "n/a"
                    ),
                    "reviewer_alias": "",
                    "reviewed_at": "",
                    "notes": "",
                },
                "truth_policy": TRUTH_POLICY,
                "historical_operator_replies_used_as_facts": False,
                "provenance": inputs["provenance"],
            }
        )
    return rows


def _validate_review_rows(
    rows: Sequence[dict[str, Any]], *, inputs: Mapping[str, Any]
) -> None:
    if len(rows) != EXPECTED_ESCALATIONS_TOTAL:
        raise ValueError("review must contain exactly 20 escalation rows")
    expected_by_id = {
        row["case_id"]: row for row in _build_review_rows(inputs)
    }
    expected_ids = set(expected_by_id)
    seen: set[str] = set()
    for row in rows:
        case_id = _nonempty_string(row.get("case_id"), "review case_id")
        if case_id in seen:
            raise ValueError(f"duplicate review case_id: {case_id!r}")
        seen.add(case_id)
        expected = expected_by_id.get(case_id)
        if expected is None:
            raise ValueError(f"review case is not a Phase 0 escalation: {case_id!r}")
        mechanical = {key: value for key, value in row.items() if key != "human_review"}
        expected_mechanical = {
            key: value for key, value in expected.items() if key != "human_review"
        }
        if mechanical != expected_mechanical:
            raise ValueError("review mechanical evidence differs from regenerated evidence")
        reason = _nonempty_string(row.get("escalation_reason"), "escalation_reason")
        reason_type = classify_escalation_reason(reason)

        human = row.get("human_review")
        if not isinstance(human, Mapping):
            raise ValueError("human_review must be an object")
        top1 = _enum(human, "top1_answerability", TOP1_ANSWERABILITY)
        frozen_seed = _enum(
            human, "frozen_seed_answerability", FROZEN_SEED_ANSWERABILITY
        )
        rejected = _enum(
            human,
            "rejected_candidate_correctness",
            REJECTED_CANDIDATE_CORRECTNESS,
        )
        if reason_type == "output":
            if row.get("rejected_candidate_available") is not False:
                raise ValueError("Phase 0 did not retain rejected generation drafts")
            if rejected != "unavailable":
                raise ValueError(
                    "generation-contract failures require unavailable correctness"
                )
        elif rejected != "n/a":
            raise ValueError("rejected candidate verdict is only for output failures")
        if reason_type != "threshold" and top1 != "n/a":
            raise ValueError("top1 answerability is only for threshold failures")
        if reason_type != "source_gap" and frozen_seed != "n/a":
            raise ValueError("frozen seed answerability is only for source gaps")
        if reason_type in {"threshold", "source_gap"}:
            _nonempty_string(human.get("reviewer_alias"), "reviewer_alias")
            _parse_reviewer_timestamp(human.get("reviewed_at"))
    if seen != expected_ids:
        raise ValueError("review rows do not match the 20 Phase 0 escalations")


def _review_chunk(
    chunk: Mapping[str, Any],
    *,
    seed_by_id: Mapping[str, Mapping[str, Any]],
    score_kind: str,
) -> dict[str, Any]:
    chunk_id = _nonempty_string(chunk.get("chunk_id"), "trace chunk_id")
    seed = seed_by_id.get(chunk_id)
    if seed is None:
        raise ValueError("trace chunk is outside the frozen published-Yonote universe")
    text = str(seed.get("text_clean") or seed.get("text_raw") or "")
    score_key = "reranker_score" if score_kind == "reranker" else "retrieval_score"
    score = chunk.get(score_key)
    if score is None and score_kind == "retrieval":
        score = chunk.get("score")
    return {
        "chunk_id": chunk_id,
        score_key: _optional_number(score),
        "status": seed.get("status"),
        "source_type": seed.get("source_type"),
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _generation_contract_reason(trace: Mapping[str, Any]) -> str | None:
    if str(trace.get("escalation_reason") or "") in OUTPUT_REASONS:
        return str(trace.get("escalation_reason"))
    generation_trace = trace.get("generation_trace")
    if isinstance(generation_trace, Mapping):
        reason = generation_trace.get("reason")
        if reason and reason != "passed":
            return str(reason)
    for event in reversed(_as_object_list(trace.get("trace_events"))):
        if event.get("node") != "generate_selection":
            continue
        metadata = event.get("metadata")
        if isinstance(metadata, Mapping):
            reason = metadata.get("reason")
            if reason and reason != "passed":
                return str(reason)
    return None


def _generation_retry_reasons(trace: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for retry in _as_object_list(trace.get("generate_retries")):
        reason = str(retry.get("reason") or "")
        if reason and reason != "passed" and reason not in reasons:
            reasons.append(reason)
    for event in _as_object_list(trace.get("trace_events")):
        metadata = event.get("metadata")
        if event.get("node") != "generate_selection" or not isinstance(metadata, Mapping):
            continue
        reason = str(metadata.get("reason") or "")
        if reason and reason != "passed" and reason not in reasons:
            reasons.append(reason)
    return reasons


def _generation_execution(trace: Mapping[str, Any]) -> str:
    model = str(trace.get("generator_model") or "unknown")
    generation_trace = _mapping_copy(trace.get("generation_trace"))
    retries = _as_object_list(trace.get("generate_retries"))
    reason = str(trace.get("escalation_reason") or "")
    if reason in OUTPUT_REASONS or reason == "insufficient_sources":
        return "attempted"
    node_sequence = [
        str(item["node"])
        for item in _as_object_list(trace.get("trace_timeline"))
        if isinstance(item.get("node"), str) and item["node"]
    ]
    if any(
        node.startswith("generate")
        or node == "guard"
        or node.startswith("verify")
        or node in {"respond", "response"}
        for node in node_sequence
    ):
        return "attempted"
    if model == "not_run":
        return "not_run"
    if model not in {"unknown", ""} or generation_trace or retries:
        return "attempted"
    if reason in {"low_confidence", "no_relevant_chunks", "operator_requested"}:
        return "not_run"
    if any(node in {"clarify", "escalate"} for node in node_sequence):
        return "not_run"
    return "missing_telemetry"


def _safe_query_analysis(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    trace_exporter._validate_query_analysis(value)
    return _mapping_copy(value)


def _mapping_copy(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _trace_chunk_ids(trace: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("retrieved_chunks", "reranker_scores", "reranked_chunks"):
        ids.update(
            str(item["chunk_id"])
            for item in _as_object_list(trace.get(key))
            if isinstance(item.get("chunk_id"), str) and item["chunk_id"]
        )
    generation = _mapping_copy(trace.get("generation_trace"))
    verifier = _mapping_copy(trace.get("verify_trace"))
    for value in (
        generation.get("selected_source_ids"),
        generation.get("cited_source_ids"),
        trace.get("selected_source_ids"),
        trace.get("cited_source_ids"),
        trace.get("verification_source_ids"),
        verifier.get("referenced_source_ids"),
    ):
        ids.update(_string_list(value))
    for key in ("retrieve_trace", "reranker_trace", "generation_trace", "verify_trace"):
        _collect_nested_chunk_ids(trace.get(key), ids)
    return ids


def _collect_nested_chunk_ids(value: Any, destination: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "chunk_id" and isinstance(child, str) and child:
                destination.add(child)
            elif (
                key.endswith("_chunk_ids") or key.endswith("_source_ids")
            ):
                destination.update(_string_list(child))
            _collect_nested_chunk_ids(child, destination)
    elif isinstance(value, list):
        for child in value:
            _collect_nested_chunk_ids(child, destination)


def _parse_reviewer_timestamp(value: Any) -> datetime:
    text = _nonempty_string(value, "reviewed_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reviewed_at must be an ISO-8601 timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError("reviewed_at must include a timezone offset")
    return parsed


def _decode_trace_export(
    raw: bytes,
) -> tuple[list[dict[str, Any]], Mapping[str, Any]]:
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("trace export must be UTF-8") from exc
    try:
        payload = json.loads(decoded, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise ValueError("trace export must be the bound JSON export") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("trace export must be the bound JSON export")
    if set(payload) != TRACE_EXPORT_FIELDS:
        raise ValueError("trace export fields differ from the exact exporter contract")
    if payload.get("schema_version") != TRACE_EXPORT_SCHEMA_VERSION:
        raise ValueError("trace export schema_version differs from the exporter contract")
    if payload.get("eval_run_id") != PHASE0_EVAL_RUN_ID:
        raise ValueError("trace export header eval_run_id differs from approved Phase 0")
    expected_window = {
        "started_at": PHASE0_RUN_STARTED_AT.isoformat().replace("+00:00", "Z"),
        "completed_at": PHASE0_RUN_COMPLETED_AT.isoformat().replace("+00:00", "Z"),
    }
    if payload.get("run_window") != expected_window:
        raise ValueError("trace export run window differs from approved Phase 0")
    if type(payload.get("cases_total")) is not int or payload["cases_total"] != 30:
        raise ValueError("trace export cases_total must equal 30")
    rows = payload.get("rows")
    bindings = payload.get("bindings")
    if not isinstance(rows, list) or not isinstance(bindings, Mapping):
        raise ValueError("trace export must contain rows and provenance bindings")
    if set(bindings) != TRACE_BINDING_FIELDS:
        raise ValueError("trace export bindings differ from the exact exporter contract")
    typed_rows = _objects(rows, label="trace export")
    for row in typed_rows:
        trace_exporter._validate_row(row, eval_run_id=PHASE0_EVAL_RUN_ID)
    return typed_rows, bindings


def _decode_jsonl_bytes(raw: bytes, *, label: str) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError(f"{label} must be UTF-8 JSONL") from exc
    return _decode_jsonl(text, label=label)


def _decode_jsonl(text: str, *, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, parse_constant=_reject_json_constant)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} line {line_number} is not valid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label} line {line_number} must be an object")
        rows.append(row)
    return rows


def _decode_json_array(raw: bytes, *, label: str) -> list[dict[str, Any]]:
    payload = _decode_json(raw, label=label)
    if not isinstance(payload, list):
        raise ValueError(f"{label} must be a JSON array")
    return _objects(payload, label=label)


def _decode_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    payload = _decode_json(raw, label=label)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _decode_json(raw: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _read_bounded_regular_bytes(
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> bytes:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"cannot read {label}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a regular, non-symlink file")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot safely open {label}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"{label} changed while being opened")
        if opened.st_size <= 0 or opened.st_size > max_bytes:
            raise ValueError(f"{label} has an invalid size")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError(f"{label} changed while being read")
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if not payload or len(payload) > max_bytes or len(payload) != opened.st_size:
        raise ValueError(f"{label} has an invalid or unstable size")
    return payload


def _objects(values: Iterable[Any], *, label: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError(f"{label} entries must be objects")
        result.append(value)
    return result


def _as_object_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _unique_by_string_key(
    rows: Sequence[dict[str, Any]], key: str, *, label: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = _nonempty_string(row.get(key), f"{label} {key}")
        if value in indexed:
            raise ValueError(f"duplicate {label} {key}: {value!r}")
        indexed[value] = row
    return indexed


def _trace_case_id(trace: Mapping[str, Any]) -> str:
    return _nonempty_string(trace.get("eval_case_id"), "trace eval_case_id")


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_number(value: Any) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("score must be numeric or null")
    return value


def _enum(row: Mapping[str, Any], key: str, allowed: frozenset[str]) -> str:
    value = row.get(key)
    if value not in allowed:
        values = ", ".join(sorted(allowed))
        raise ValueError(f"{key} must be one of: {values}")
    return str(value)


def _file_sha256(path: Path) -> str:
    payload = _read_bounded_regular_bytes(
        path,
        label=str(path),
        max_bytes=MAX_REVIEW_BYTES,
    )
    return hashlib.sha256(payload).hexdigest()


def _require_private_path(path: Path, *, label: str) -> None:
    resolved = path.resolve(strict=False)
    if resolved == PRIVATE_DATA_ROOT or not resolved.is_relative_to(PRIVATE_DATA_ROOT):
        raise ValueError(f"{label} must be under data/private")


def _stat_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _open_stable_parent_fd(
    canonical_parent: Path,
    expected_identity: os.stat_result,
) -> int | None:
    if os.name == "nt":
        return None
    if not _SUPPORTS_STABLE_PARENT_DIR_FD:
        raise OSError("secure output publication is unsupported on this platform")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_fd = os.open(canonical_parent, flags)
    if _stat_identity(os.fstat(parent_fd)) != _stat_identity(expected_identity):
        os.close(parent_fd)
        raise ValueError("output parent changed before atomic publish")
    return parent_fd


def _stable_entry_stat(
    *,
    parent_fd: int | None,
    canonical_path: Path,
    entry_name: str,
) -> os.stat_result:
    if parent_fd is None:
        return os.stat(canonical_path, follow_symlinks=False)
    return os.stat(entry_name, dir_fd=parent_fd, follow_symlinks=False)


def _stable_entry_unlink(
    *,
    parent_fd: int | None,
    canonical_path: Path,
    entry_name: str,
    missing_ok: bool,
) -> None:
    try:
        if parent_fd is None:
            canonical_path.unlink()
        else:
            os.unlink(entry_name, dir_fd=parent_fd)
    except FileNotFoundError:
        if not missing_ok:
            raise


def _unlink_output_if_same_file(
    *,
    parent_fd: int | None,
    canonical_target: Path,
    target_name: str,
    expected_identity: os.stat_result,
) -> None:
    try:
        target_identity = _stable_entry_stat(
            parent_fd=parent_fd,
            canonical_path=canonical_target,
            entry_name=target_name,
        )
    except FileNotFoundError:
        return
    if _stat_identity(target_identity) != _stat_identity(expected_identity):
        return
    _stable_entry_unlink(
        parent_fd=parent_fd,
        canonical_path=canonical_target,
        entry_name=target_name,
        missing_ok=True,
    )


def _write_jsonl_exclusive(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    encoded = b"".join(
        (
            json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
        for row in rows
    )
    _write_bytes_exclusive(path, encoded)


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    _write_bytes_exclusive(path, encoded)


def _write_bytes_exclusive(path: Path, encoded: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"output already exists: {path}")
    try:
        private_root = PRIVATE_DATA_ROOT.resolve(strict=True)
        canonical_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError("output parent cannot be safely resolved") from exc
    if (
        not private_root.is_dir()
        or not canonical_parent.is_relative_to(private_root)
    ):
        raise ValueError("output parent must remain under data/private")
    canonical_target = canonical_parent / path.name
    if canonical_target.exists() or canonical_target.is_symlink():
        raise FileExistsError(f"output already exists: {canonical_target}")
    parent_identity = os.stat(canonical_parent, follow_symlinks=False)
    parent_fd = _open_stable_parent_fd(canonical_parent, parent_identity)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=canonical_parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as destination:
            destination.write(encoded)
            destination.flush()
            os.fsync(destination.fileno())
        temporary_identity = os.fstat(descriptor)
        if parent_fd is not None:
            try:
                stable_temporary_identity = _stable_entry_stat(
                    parent_fd=parent_fd,
                    canonical_path=temporary,
                    entry_name=temporary.name,
                )
            except OSError as exc:
                raise ValueError("output parent changed before atomic publish") from exc
            if _stat_identity(stable_temporary_identity) != _stat_identity(
                temporary_identity
            ):
                raise ValueError("output parent changed before atomic publish")
        try:
            revalidated_parent = path.parent.resolve(strict=True)
            current_identity = os.stat(
                revalidated_parent,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError("output parent changed before atomic publish") from exc
        if (
            revalidated_parent != canonical_parent
            or (current_identity.st_dev, current_identity.st_ino)
            != (parent_identity.st_dev, parent_identity.st_ino)
        ):
            raise ValueError("output parent changed before atomic publish")
        if parent_fd is None:
            os.link(temporary, canonical_target)
        else:
            os.link(
                temporary.name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        try:
            final_parent = path.parent.resolve(strict=True)
            final_identity = os.stat(final_parent, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("output parent changed during atomic publish") from exc
        if (
            final_parent != canonical_parent
            or (final_identity.st_dev, final_identity.st_ino)
            != (parent_identity.st_dev, parent_identity.st_ino)
        ):
            raise ValueError("output parent changed during atomic publish")
    except BaseException:
        if descriptor >= 0:
            temporary_identity = os.fstat(descriptor)
            try:
                os.ftruncate(descriptor, 0)
                os.fsync(descriptor)
            finally:
                _unlink_output_if_same_file(
                    parent_fd=parent_fd,
                    canonical_target=canonical_target,
                    target_name=path.name,
                    expected_identity=temporary_identity,
                )
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            _stable_entry_unlink(
                parent_fd=parent_fd,
                canonical_path=temporary,
                entry_name=temporary.name,
                missing_ok=True,
            )
        if parent_fd is not None:
            os.close(parent_fd)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or summarize the private Phase 0 escalation review."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "summarize"):
        command = subparsers.add_parser(name)
        command.add_argument("--cases", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--traces", type=Path, required=True)
        command.add_argument("--kb-seed", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--review-output", type=Path, required=True)
        else:
            command.add_argument("--review", type=Path, required=True)
            command.add_argument("--result-output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    common = {
        "cases_path": args.cases,
        "manifest_path": args.manifest,
        "traces_path": args.traces,
        "kb_seed_path": args.kb_seed,
    }
    if args.command == "prepare":
        prepare_review(**common, review_output_path=args.review_output)
        output = args.review_output
        status = "PREPARED"
    else:
        result = summarize_review(
            **common,
            review_path=args.review,
            result_output_path=args.result_output,
        )
        output = args.result_output
        status = str(result["status"])
    # No queries, responses, review cells, or small-cell breakdowns on stdout.
    print(f"STATUS={status}")
    print(f"OUTPUT={output.resolve()}")
    print(f"SHA256={_file_sha256(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
