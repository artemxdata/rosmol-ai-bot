from __future__ import annotations

import base64
import hashlib
import io
import json
import shlex
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from scripts import export_phase0_trace_review as exporter


def _approved_inputs(private_root: Path, monkeypatch) -> tuple[Path, Path, list[str]]:
    artifact_dir = private_root / "eval" / "phase0-real-rag-7d244e4"
    artifact_dir.mkdir(parents=True)
    case_ids = [f"case-{index:02d}" for index in range(exporter.PHASE0_CASES_TOTAL)]
    cases = [
        {"id": case_id, "query": f"PRIVATE-QUERY-{index}"} for index, case_id in enumerate(case_ids)
    ]
    manifest = {"cases": [{"id": case_id} for case_id in case_ids]}
    cases_path = artifact_dir / "phase0-cases.json"
    manifest_path = artifact_dir / "phase0-manifest.json"
    cases_bytes = (json.dumps(cases, ensure_ascii=False) + "\n").encode()
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False) + "\n").encode()
    cases_path.write_bytes(cases_bytes)
    manifest_path.write_bytes(manifest_bytes)
    monkeypatch.setattr(exporter, "PRIVATE_DATA_ROOT", private_root.resolve())
    monkeypatch.setattr(exporter, "CASES_PATH", cases_path)
    monkeypatch.setattr(exporter, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        exporter,
        "PHASE0_CASES_SHA256",
        hashlib.sha256(cases_bytes).hexdigest(),
    )
    monkeypatch.setattr(
        exporter,
        "PHASE0_MANIFEST_SHA256",
        hashlib.sha256(manifest_bytes).hexdigest(),
    )
    return cases_path, manifest_path, case_ids


def _row(case_id: str, index: int) -> dict:
    timestamp = exporter.PHASE0_RUN_STARTED_AT + timedelta(seconds=index + 1)
    source_id = f"source-{index}"
    return {
        "schema_version": exporter.ROW_SCHEMA_VERSION,
        "eval_run_id": exporter.PHASE0_EVAL_RUN_ID,
        "eval_case_id": case_id,
        "request_id": str(UUID(int=index + 1)),
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "query_analysis": {
            "forum": "Машук",
            "forum_normalized": "машук",
            "category": "forums",
            "complexity": "simple",
            "needs_clarification": False,
            "should_escalate": False,
            "is_technical": False,
            "is_offtopic": False,
            "response_profile": "dates",
            "questions_count": 1,
            "topics_count": 1,
        },
        "retrieved_chunks": [{"chunk_id": source_id, "retrieval_score": 0.45}],
        "reranked_chunks": [
            {
                "chunk_id": source_id,
                "retrieval_score": 0.45,
                "reranker_score": 0.72,
            }
        ],
        "reranker_trace": {
            "max_confidence": 0.72,
            "confidence_source": "reranker",
            "reranker_invoked": True,
            "raw_reranker_scores": [0.72],
            "raw_reranker_scores_total": 1,
            "raw_reranker_scores_recorded": 1,
            "raw_reranker_scores_truncated_count": 0,
            "raw_reranker_max": 0.72,
            "score_origin": "reranker",
            "synthetic_score_applied": False,
            "synthetic_high_score_applied": False,
            "floor_applied": False,
            "confidence_components": {
                "raw_reranker_max": 0.72,
                "reranked_output_max": 0.72,
                "retrieval_exact_filter_floor": 0.0,
                "decision_confidence": 0.72,
            },
            "question_provenance": [
                {
                    "schema_version": exporter.PROVENANCE_SCHEMA_VERSION,
                    "question_id": "q1",
                    "input_chunk_ids": [source_id],
                    "input_chunk_ids_total": 1,
                    "input_chunk_ids_recorded": 1,
                    "input_chunk_ids_truncated_count": 0,
                    "output_chunks": [{"chunk_id": source_id, "score": 0.72}],
                    "dropped_chunk_ids": [],
                    "questions_total": 1,
                    "questions_recorded": 1,
                    "questions_truncated_count": 0,
                }
            ],
        },
        "retrieve_trace": {
            "retrieval_method": "hybrid",
            "metadata_lookup_attempted": False,
            "metadata_lookup_succeeded": False,
            "metadata_lookup_result_count": 0,
            "hybrid_candidates_present": True,
            "question_provenance": [
                {
                    "schema_version": exporter.PROVENANCE_SCHEMA_VERSION,
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
                                    "score": 0.45,
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
        },
        "generation_trace": {
            "schema_version": exporter.PROVENANCE_SCHEMA_VERSION,
            "mode": "source_chunk",
            "generator_path": "source_chunk",
            "source_chunk_applied": True,
            "selected_source_ids": [source_id],
            "selected_source_ids_total": 1,
            "selected_source_ids_recorded": 1,
            "selected_source_ids_truncated_count": 0,
            "cited_source_ids": [source_id],
            "cited_source_ids_total": 1,
            "cited_source_ids_recorded": 1,
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
            "contract_status": "passed",
            "reason": "passed",
        },
        "selected_source_ids": [source_id],
        "generate_retries": [],
        "verify_trace": {
            "schema_version": exporter.PROVENANCE_SCHEMA_VERSION,
            "decision": "pass",
            "reason": "passed",
            "referenced_source_ids": [source_id],
            "referenced_source_ids_total": 1,
            "referenced_source_ids_recorded": 1,
            "referenced_source_ids_truncated_count": 0,
            "reference_scope": "actual_response_explicit",
            "candidate_uncovered_question_ids": [],
            "verifier_triggered": False,
        },
        "verification_source_ids": [source_id],
        "trace_timeline": [
            {
                "node": "analyze",
                "latency_ms": 1,
                "error_present": False,
            }
        ],
        "max_reranker_score": 0.72,
        "cache_hit": False,
        "generator_model": "source_chunk",
        "cited_source_ids": [source_id],
        "verifier_triggered": False,
        "verifier_result": {
            "has_hallucination": False,
            "confidence": 1.0,
            "triggered_llm_judge": False,
        },
        "was_escalated": False,
        "escalation_reason": None,
        "ticket_outcome": "answered",
        "llm_usage": [
            {
                "model": "model-safe-name",
                "latency_ms": 100,
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "estimated_cost_rub": 0.01,
                "priced": True,
            }
        ],
        "llm_prompt_tokens": 10,
        "llm_completion_tokens": 5,
        "llm_total_tokens": 15,
        "llm_estimated_cost_rub": 0.01,
        "total_latency_ms": 100,
        "prompt_version": "v1",
        "error_present": False,
        "error_code": None,
    }


def _payload(case_ids: list[str]) -> bytes:
    return _rows_payload([_row(case_id, index) for index, case_id in enumerate(case_ids)])


def _rows_payload(rows: list[dict]) -> bytes:
    encoded_rows = [
        base64.b64encode(
            json.dumps(
                row,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        for row in rows
    ]
    return b"\n".join(encoded_rows) + b"\n"


def _install_fake_ssh(monkeypatch, payload: bytes) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr=b"")

    monkeypatch.setattr(exporter, "_run_bounded_ssh", fake_run)
    return calls


def test_export_writes_only_validated_safe_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_root = tmp_path / "data" / "private"
    _, _, case_ids = _approved_inputs(private_root, monkeypatch)
    calls = _install_fake_ssh(monkeypatch, _payload(list(reversed(case_ids))))
    output = private_root / "eval" / "phase0-review.json"

    result = exporter.export_phase0_trace_review(
        ssh_target="rosmol",
        eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
        output_path=output,
    )

    assert result["status"] == "OK"
    assert result["cases_total"] == 30
    report = json.loads(output.read_text(encoding="utf-8"))
    assert [row["eval_case_id"] for row in report["rows"]] == case_ids
    assert report["bindings"]["cases_file_sha256"] == exporter.PHASE0_CASES_SHA256
    assert all("text" not in chunk for row in report["rows"] for chunk in row["retrieved_chunks"])
    assert all(not (set(row) & exporter.FORBIDDEN_OUTPUT_FIELDS) for row in report["rows"])
    assert all("error" not in row for row in report["rows"])
    assert all("error" not in event for row in report["rows"] for event in row["trace_timeline"])
    assert len(calls) == 1
    args = calls[0]
    assert args[:6] == [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "--",
        "rosmol",
    ]
    remote = args[6]
    assert "docker --host unix:///var/run/docker.sock version" in remote
    assert "sudo --non-interactive docker --host unix:///var/run/docker.sock" in remote
    assert '"$@" exec -i rosmol-postgres psql' in remote
    assert f"exit {exporter.REMOTE_DOCKER_ACCESS_EXIT}" in remote
    assert f"exit {exporter.REMOTE_POSTGRES_CONTAINER_MISSING_EXIT}" in remote
    assert f"exit {exporter.REMOTE_POSTGRES_CONTAINER_NOT_RUNNING_EXIT}" in remote
    assert f"exit {exporter.REMOTE_POSTGRES_EXPORT_EXIT}" in remote
    assert "COPY" in remote
    assert "encode(convert_to(" in remote
    assert "'base64'" in remote
    assert "chr(10)" in remote
    assert "chr(13)" in remote
    assert "TO STDOUT WITH (FORMAT text)" in remote
    assert exporter.PHASE0_EVAL_RUN_ID in remote
    assert "BEGIN TRANSACTION READ ONLY" in remote
    assert "--no-password" in remote
    assert ".env" not in remote
    assert "postgresql://" not in remote
    assert "user_id_hash" not in remote
    assert "ticket_id_hash" not in remote
    assert "upstream_event_id" not in remote
    assert "response_text" not in remote
    assert "clarification_question" not in remote


def test_interactive_remote_command_uses_tty_sudo_without_persisting_credentials() -> None:
    command = shlex.split(exporter._remote_command(interactive_sudo=True))
    assert command[:2] == ["sh", "-c"]
    remote = command[2]

    assert "sudo -p '' docker --host unix:///var/run/docker.sock version" in remote
    assert "set -- sudo -p '' docker --host unix:///var/run/docker.sock" in remote
    assert "--non-interactive" not in remote
    assert "sudoers" not in remote
    assert "usermod" not in remote
    assert "extracted_params" not in remote
    assert "verifier_result->'details'" not in remote
    assert "left(rt.error" not in remote
    assert "left(event->>'error'" not in remote
    assert "'error'," not in remote
    assert "PRIVATE-QUERY" not in remote


@pytest.mark.parametrize(
    ("ssh_target", "eval_run_id"),
    [
        ("rosmol;whoami", exporter.PHASE0_EVAL_RUN_ID),
        ("-oProxyCommand=whoami", exporter.PHASE0_EVAL_RUN_ID),
        ("--", exporter.PHASE0_EVAL_RUN_ID),
        ("rosmol", "ask-eval-other"),
    ],
)
def test_export_rejects_unapproved_target_or_run_before_ssh(
    tmp_path: Path,
    monkeypatch,
    ssh_target: str,
    eval_run_id: str,
) -> None:
    private_root = tmp_path / "data" / "private"
    _approved_inputs(private_root, monkeypatch)
    calls = _install_fake_ssh(monkeypatch, b"should-not-run")

    with pytest.raises(ValueError):
        exporter.export_phase0_trace_review(
            ssh_target=ssh_target,
            eval_run_id=eval_run_id,
            output_path=private_root / "review.json",
        )

    assert calls == []


def test_export_rejects_output_outside_private_or_existing_before_ssh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_root = tmp_path / "data" / "private"
    _approved_inputs(private_root, monkeypatch)
    calls = _install_fake_ssh(monkeypatch, b"should-not-run")
    existing = private_root / "existing.json"
    existing.write_text("do-not-overwrite", encoding="utf-8")

    with pytest.raises(ValueError, match="data/private"):
        exporter.export_phase0_trace_review(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output_path=tmp_path / "outside.json",
        )
    with pytest.raises(FileExistsError):
        exporter.export_phase0_trace_review(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output_path=existing,
        )

    assert existing.read_text(encoding="utf-8") == "do-not-overwrite"
    assert calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_case",
        "duplicate_request",
        "wrong_membership",
        "outside_window",
        "cache_hit",
        "raw_chunk_text",
        "forbidden_field",
        "raw_analysis_text",
        "response_text",
        "verifier_details",
        "unexpected_llm_usage",
        "untyped_llm_usage",
        "retrieve_unexpected_nested_key",
        "retrieve_inconsistent_bounds",
        "rerank_unexpected_component",
        "generation_bad_enum",
        "verify_bad_enum",
        "retry_uncontrolled_reason",
        "source_ids_over_bound",
        "timeline_unknown_node",
    ],
)
def test_export_fails_closed_on_invalid_trace_rows(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
) -> None:
    private_root = tmp_path / "data" / "private"
    _, _, case_ids = _approved_inputs(private_root, monkeypatch)
    rows = [_row(case_id, index) for index, case_id in enumerate(case_ids)]
    if mutation == "duplicate_case":
        rows[-1]["eval_case_id"] = rows[0]["eval_case_id"]
    elif mutation == "duplicate_request":
        rows[-1]["request_id"] = rows[0]["request_id"]
    elif mutation == "wrong_membership":
        rows[-1]["eval_case_id"] = "unexpected-case"
    elif mutation == "outside_window":
        rows[0]["timestamp"] = datetime(2026, 8, 7, tzinfo=UTC).isoformat()
    elif mutation == "cache_hit":
        rows[0]["cache_hit"] = True
    elif mutation == "raw_chunk_text":
        rows[0]["retrieved_chunks"][0]["text"] = "RAW-CHUNK-CANARY"
    elif mutation == "forbidden_field":
        rows[0]["user_id_hash"] = "PRIVATE-IDENTIFIER"
    elif mutation == "raw_analysis_text":
        rows[0]["query_analysis"]["questions"] = [{"text": "PRIVATE-ANALYSIS-QUESTION-CANARY"}]
        rows[0]["query_analysis"]["extracted_params"] = {
            "private": "PRIVATE-EXTRACTED-PARAM-CANARY"
        }
    elif mutation == "response_text":
        rows[0]["response_text"] = "PRIVATE-FINAL-RESPONSE-CANARY"
    elif mutation == "verifier_details":
        rows[0]["verifier_result"]["details"] = "PRIVATE-VERIFIER-DETAILS-CANARY"
    elif mutation == "unexpected_llm_usage":
        rows[0]["llm_usage"][0]["provider_payload"] = "PRIVATE-USAGE-CANARY"
    elif mutation == "untyped_llm_usage":
        rows[0]["llm_usage"][0]["prompt_tokens"] = "10"
    elif mutation == "retrieve_unexpected_nested_key":
        rows[0]["retrieve_trace"]["question_provenance"][0]["attempts"][0]["query"] = (
            "PRIVATE-QUERY-CANARY"
        )
    elif mutation == "retrieve_inconsistent_bounds":
        rows[0]["retrieve_trace"]["question_provenance"][0]["attempts"][0]["candidates_total"] = 2
    elif mutation == "rerank_unexpected_component":
        rows[0]["reranker_trace"]["confidence_components"]["note"] = "PRIVATE-NOTE-CANARY"
    elif mutation == "generation_bad_enum":
        rows[0]["generation_trace"]["contract_status"] = "PRIVATE-STATUS"
    elif mutation == "verify_bad_enum":
        rows[0]["verify_trace"]["reference_scope"] = "PRIVATE-SCOPE"
    elif mutation == "retry_uncontrolled_reason":
        rows[0]["generate_retries"] = [{"latency_ms": 1, "reason": "PRIVATE QUERY", "chunks": 1}]
    elif mutation == "source_ids_over_bound":
        rows[0]["selected_source_ids"] = [
            f"source-overflow-{item}" for item in range(exporter.MAX_PROVENANCE_SOURCE_IDS + 1)
        ]
    elif mutation == "timeline_unknown_node":
        rows[0]["trace_timeline"][0]["node"] = "PRIVATE-NODE"
    payload = _rows_payload(rows)
    _install_fake_ssh(monkeypatch, payload)
    output = private_root / f"{mutation}.json"

    with pytest.raises(ValueError):
        exporter.export_phase0_trace_review(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output_path=output,
        )

    assert not output.exists()


def test_base64_transport_round_trips_copy_sensitive_characters() -> None:
    case_ids = [f"case-{index:02d}" for index in range(exporter.PHASE0_CASES_TOTAL)]
    rows = [_row(case_id, index) for index, case_id in enumerate(case_ids)]
    copy_sensitive = 'case "quoted" \\ path\nline\tcell\x01'
    rows[0]["eval_case_id"] = copy_sensitive
    case_ids[0] = copy_sensitive
    payload = _rows_payload(rows)

    encoded_lines = payload[:-1].split(b"\n")
    assert len(encoded_lines) == exporter.PHASE0_CASES_TOTAL
    assert all(b"\r" not in line for line in encoded_lines)
    assert all(base64.b64decode(line, validate=True) for line in encoded_lines)

    parsed = exporter._parse_and_validate_rows(
        payload,
        expected_case_ids=case_ids,
        eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
    )

    assert parsed[0]["eval_case_id"] == copy_sensitive


@pytest.mark.parametrize("framing", ["raw_json", "missing_final_lf", "wrapped", "blank"])
def test_base64_transport_rejects_non_copy_framing(framing: str) -> None:
    case_ids = [f"case-{index:02d}" for index in range(exporter.PHASE0_CASES_TOTAL)]
    rows = [_row(case_id, index) for index, case_id in enumerate(case_ids)]
    valid = _rows_payload(rows)
    if framing == "raw_json":
        payload = ("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8")
    elif framing == "missing_final_lf":
        payload = valid[:-1]
    elif framing == "wrapped":
        first, remainder = valid.split(b"\n", maxsplit=1)
        payload = first[:16] + b"\n" + first[16:] + b"\n" + remainder
    else:
        first, remainder = valid.split(b"\n", maxsplit=1)
        payload = first + b"\n\n" + remainder

    with pytest.raises(ValueError):
        exporter._parse_and_validate_rows(
            payload,
            expected_case_ids=case_ids,
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
        )


@pytest.mark.parametrize(
    "canary",
    [
        "token=PRIVATE-TOKEN-CANARY",
        "access_token=PRIVATE-ACCESS-CANARY",
        "cookie=PRIVATE-COOKIE-CANARY",
        "-----BEGIN PRIVATE KEY-----",
        "query=PRIVATE-USER-QUERY-CANARY",
    ],
)
@pytest.mark.parametrize(
    "location",
    [
        "request_raw",
        "request_code",
        "timeline_raw",
        "timeline_code",
        "generation_raw",
    ],
)
def test_export_rejects_raw_error_canaries(
    tmp_path: Path,
    monkeypatch,
    canary: str,
    location: str,
) -> None:
    private_root = tmp_path / "data" / "private"
    _, _, case_ids = _approved_inputs(private_root, monkeypatch)
    rows = [_row(case_id, index) for index, case_id in enumerate(case_ids)]
    if location == "request_raw":
        rows[0]["error"] = canary
    elif location == "request_code":
        rows[0]["error_present"] = True
        rows[0]["error_code"] = canary
    elif location == "timeline_raw":
        rows[0]["trace_timeline"][0]["error"] = canary
    elif location == "timeline_code":
        rows[0]["trace_timeline"][0]["error_present"] = True
        rows[0]["trace_timeline"][0]["error_code"] = canary
    else:
        rows[0]["generation_trace"]["error_message"] = canary
    _install_fake_ssh(monkeypatch, _rows_payload(rows))
    output = private_root / f"raw-error-{location}.json"

    with pytest.raises(ValueError):
        exporter.export_phase0_trace_review(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output_path=output,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    "field",
    ["Error", "ERROR-MESSAGE", "errorMessage", "Stack_Trace", "TraceBack"],
)
def test_forbidden_raw_error_keys_are_case_and_separator_normalized(field: str) -> None:
    with pytest.raises(ValueError, match="raw error telemetry"):
        exporter._reject_forbidden_nested_keys({"nested": {field: "PRIVATE-CANARY"}})


def test_validator_accepts_real_v2_skipped_retrieve_question_shape() -> None:
    trace = _row("case-00", 0)["retrieve_trace"]
    trace["question_provenance"] = [
        {
            "schema_version": exporter.PROVENANCE_SCHEMA_VERSION,
            "question_id": "q1",
            "attempts": [],
            "retrieved_chunk_ids": [],
            "skipped_reason": "unscoped_multi_topic_question",
            "questions_total": 1,
            "questions_recorded": 1,
            "questions_truncated_count": 0,
            "attributable_questions_total": 1,
        }
    ]

    exporter._validate_retrieve_trace(trace)


def test_export_rejects_parent_symlink_swap_after_ssh_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_root = tmp_path / "data" / "private"
    _, _, case_ids = _approved_inputs(private_root, monkeypatch)
    output_parent = private_root / "exports"
    output_parent.mkdir()
    original_parent = private_root / "exports-original"
    outside_parent = tmp_path / "outside"
    outside_parent.mkdir()
    output = output_parent / "review.json"
    payload = _payload(case_ids)

    def fake_run(args):
        output_parent.rename(original_parent)
        try:
            output_parent.symlink_to(outside_parent, target_is_directory=True)
        except OSError:
            original_parent.rename(output_parent)
            pytest.skip("directory symlinks are unavailable on this platform")
        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr=b"")

    monkeypatch.setattr(exporter, "_run_bounded_ssh", fake_run)

    with pytest.raises(ValueError, match="output parent changed"):
        exporter.export_phase0_trace_review(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output_path=output,
        )

    assert not (outside_parent / "review.json").exists()
    assert not (original_parent / "review.json").exists()


def test_writer_cleans_publication_when_parent_changes_inside_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_root = tmp_path / "data" / "private"
    _, _, case_ids = _approved_inputs(private_root, monkeypatch)
    output_parent = private_root / "exports"
    output_parent.mkdir()
    original_parent = private_root / "exports-original"
    output = output_parent / "review.json"
    _install_fake_ssh(monkeypatch, _payload(case_ids))
    real_link = exporter.os.link

    def swapping_link(source, destination):
        output_parent.rename(original_parent)
        output_parent.mkdir()
        return real_link(source, destination)

    monkeypatch.setattr(exporter.os, "link", swapping_link)

    with pytest.raises(ValueError, match="output parent changed"):
        exporter.export_phase0_trace_review(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output_path=output,
        )

    assert not output.exists()
    assert not (original_parent / "review.json").exists()
    assert list(private_root.glob(".phase0-trace-export.*.tmp")) == []


def test_export_enforces_max_bytes_and_sanitizes_ssh_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    private_root = tmp_path / "data" / "private"
    _approved_inputs(private_root, monkeypatch)
    output = private_root / "review.json"
    monkeypatch.setattr(exporter, "MAX_EXPORT_BYTES", 8)
    _install_fake_ssh(monkeypatch, b"PRIVATE-PAYLOAD")

    with pytest.raises(exporter.SafeExportFailure, match="remote_output_too_large"):
        exporter.export_phase0_trace_review(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output_path=output,
        )
    assert not output.exists()
    assert "PRIVATE-PAYLOAD" not in capsys.readouterr().out


@pytest.mark.parametrize("oversized_stream", ["stdout", "stderr"])
def test_bounded_ssh_kills_oversized_stream_without_emitting_payload(
    monkeypatch,
    capsys,
    oversized_stream: str,
) -> None:
    monkeypatch.setattr(exporter, "MAX_EXPORT_BYTES", 8)
    payload = b"PRIVATE-OVERSIZED-STREAM-CANARY"

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(payload if oversized_stream == "stdout" else b"")
            self.stderr = io.BytesIO(payload if oversized_stream == "stderr" else b"")
            self.killed = False

        def wait(self, timeout):
            assert timeout == exporter.SSH_TIMEOUT_SECONDS
            return 0

        def kill(self):
            self.killed = True

    process = FakeProcess()
    monkeypatch.setattr(exporter.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(exporter.SafeExportFailure, match="remote_output_too_large"):
        exporter._run_bounded_ssh(["ssh", "safe-target"])

    assert process.killed is True
    captured = capsys.readouterr()
    assert payload.decode() not in captured.out
    assert payload.decode() not in captured.err


def test_bounded_ssh_classifies_start_failure_without_exception_text(
    monkeypatch,
) -> None:
    def fail_start(*_args, **_kwargs):
        raise OSError("PRIVATE-START-FAILURE-CANARY")

    monkeypatch.setattr(exporter.subprocess, "Popen", fail_start)

    with pytest.raises(exporter.SafeExportFailure, match="ssh_start_failed"):
        exporter._run_bounded_ssh(["ssh", "safe-target"])


def test_bounded_ssh_classifies_timeout_and_kills_process(monkeypatch) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")
            self.wait_calls = 0
            self.killed = False

        def wait(self, timeout):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(["ssh"], timeout)
            return -9

        def kill(self):
            self.killed = True

    process = FakeProcess()
    monkeypatch.setattr(exporter.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(exporter.SafeExportFailure, match="ssh_timeout"):
        exporter._run_bounded_ssh(["ssh", "safe-target"])

    assert process.killed is True


def test_bounded_ssh_interactive_mode_inherits_owner_tty(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")

        def wait(self, timeout):
            return 0

        def kill(self):
            raise AssertionError("successful interactive SSH must not be killed")

    def fake_popen(args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(exporter.subprocess, "Popen", fake_popen)

    completed = exporter._run_bounded_ssh(
        ["ssh", "-tt", "rosmol"],
        interactive=True,
    )

    assert completed.returncode == 0
    assert captured["stdin"] is None
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE


def test_interactive_sudo_fails_closed_without_owner_tty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_root = tmp_path / "data" / "private"
    _approved_inputs(private_root, monkeypatch)
    output = private_root / "review.json"
    monkeypatch.setattr(exporter.sys, "stdin", io.StringIO())
    monkeypatch.setattr(
        exporter,
        "_run_bounded_ssh",
        lambda *_args, **_kwargs: pytest.fail("SSH must not start without an owner TTY"),
    )

    with pytest.raises(exporter.SafeExportFailure, match="interactive_tty_required"):
        exporter.export_phase0_trace_review(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output_path=output,
            interactive_sudo=True,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("timeout", "ssh_timeout"),
        ("start", "ssh_start_failed"),
        ("ssh_exit", "ssh_exit"),
        ("remote", "remote_failure"),
        ("docker", "docker_access_failed"),
        ("container_missing", "postgres_container_missing"),
        ("container_stopped", "postgres_container_not_running"),
        ("postgres_export", "postgres_export_failed"),
    ],
)
def test_main_reports_stable_payload_free_ssh_failure_codes(
    tmp_path: Path,
    monkeypatch,
    capsys,
    failure: str,
    expected_code: str,
) -> None:
    private_root = tmp_path / "data" / "private"
    _approved_inputs(private_root, monkeypatch)
    output = private_root / "review.json"
    private_canary = "PRIVATE-SSH-STDERR-CANARY"
    monkeypatch.setattr(
        exporter,
        "parse_args",
        lambda: argparse_namespace(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output=output,
        ),
    )

    def fake_run(args):
        if failure in {"timeout", "start"}:
            raise exporter.SafeExportFailure(expected_code)
        exit_codes = {
            "ssh_exit": 255,
            "remote": 23,
            "docker": exporter.REMOTE_DOCKER_ACCESS_EXIT,
            "container_missing": exporter.REMOTE_POSTGRES_CONTAINER_MISSING_EXIT,
            "container_stopped": exporter.REMOTE_POSTGRES_CONTAINER_NOT_RUNNING_EXIT,
            "postgres_export": exporter.REMOTE_POSTGRES_EXPORT_EXIT,
        }
        return subprocess.CompletedProcess(
            args,
            exit_codes[failure],
            stdout=b"",
            stderr=private_canary.encode(),
        )

    monkeypatch.setattr(exporter, "_run_bounded_ssh", fake_run)

    assert exporter.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"phase0_trace_export=FAIL reason={expected_code}\n"
    assert private_canary not in captured.err
    assert not output.exists()


@pytest.mark.parametrize("field", ["confidence", "cost"])
def test_main_rejects_huge_numeric_values_without_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
    field: str,
) -> None:
    private_root = tmp_path / "data" / "private"
    _, _, case_ids = _approved_inputs(private_root, monkeypatch)
    rows = [_row(case_id, index) for index, case_id in enumerate(case_ids)]
    huge = 10**400
    if field == "confidence":
        rows[0]["verifier_result"]["confidence"] = huge
    else:
        rows[0]["llm_usage"][0]["estimated_cost_rub"] = huge
    _install_fake_ssh(monkeypatch, _rows_payload(rows))
    output = private_root / f"huge-{field}.json"
    monkeypatch.setattr(
        exporter,
        "parse_args",
        lambda: argparse_namespace(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output=output,
        ),
    )

    assert exporter.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "phase0_trace_export=FAIL reason=validation_failed\n"
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_main_catches_writer_oserror_without_traceback_or_path_leak(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    private_root = tmp_path / "data" / "private"
    _, _, case_ids = _approved_inputs(private_root, monkeypatch)
    _install_fake_ssh(monkeypatch, _payload(case_ids))
    output = private_root / "review.json"
    private_canary = "PRIVATE-WRITER-PATH-CANARY"
    monkeypatch.setattr(
        exporter,
        "parse_args",
        lambda: argparse_namespace(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output=output,
        ),
    )

    def fail_write(*_args, **_kwargs):
        raise OSError(private_canary)

    monkeypatch.setattr(exporter, "_write_exclusive_atomic", fail_write)

    assert exporter.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "phase0_trace_export=FAIL reason=io_failure\n"
    assert private_canary not in captured.err
    assert not output.exists()


def test_main_stdout_contains_only_safe_status_path_and_hash(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    output = tmp_path / "data" / "private" / "review.json"
    expected = {
        "status": "OK",
        "path": str(output),
        "sha256": "a" * 64,
        "cases_total": 30,
    }
    monkeypatch.setattr(
        exporter,
        "parse_args",
        lambda: argparse_namespace(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output=output,
        ),
    )
    monkeypatch.setattr(exporter, "export_phase0_trace_review", lambda **_kwargs: expected)

    assert exporter.main() == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "phase0_trace_export=OK",
        f"path={output}",
        f"sha256={'a' * 64}",
    ]


def test_main_reports_stop_when_approved_trace_evidence_is_unavailable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    private_root = tmp_path / "data" / "private"
    _approved_inputs(private_root, monkeypatch)
    _install_fake_ssh(monkeypatch, b"")
    output = private_root / "phase0-review.json"
    monkeypatch.setattr(
        exporter,
        "parse_args",
        lambda: argparse_namespace(
            ssh_target="rosmol",
            eval_run_id=exporter.PHASE0_EVAL_RUN_ID,
            output=output,
        ),
    )

    assert exporter.main() == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == "phase0_trace_export=STOP reason=evidence_unavailable\n"
    assert not output.exists()


def argparse_namespace(**values):
    class Namespace:
        pass

    namespace = Namespace()
    for key, value in values.items():
        setattr(namespace, key, value)
    return namespace
