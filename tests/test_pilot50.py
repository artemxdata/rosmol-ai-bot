from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from argparse import Namespace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from scripts import pilot50

MANIFEST_PATH = pilot50.PROJECT_ROOT / "eval" / "cases" / "pilot50_balanced_v1.json"
V2_MANIFEST_PATH = (
    pilot50.PROJECT_ROOT / "eval" / "cases" / "pilot50_balanced_v2.json"
)
V3_MANIFEST_PATH = (
    pilot50.PROJECT_ROOT / "eval" / "cases" / "pilot50_balanced_v3.json"
)
EVAL_RUN_ID = "ask-eval-11111111-1111-1111-1111-111111111111"
RUNTIME_GIT_SHA = "a" * 40
APPROVAL_ID = "PILOT50-OWNER-20260810"
RUN_STARTED_AT = "2026-08-10T12:00:00+00:00"
RUN_COMPLETED_AT = "2026-08-10T12:10:00+00:00"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _materialized_workspace(tmp_path: Path) -> tuple[list[dict[str, Any]], Path, str]:
    cases, receipt = pilot50.build_materialized_cases(MANIFEST_PATH)
    cases_path = tmp_path / "pilot50-cases.json"
    cases_path.write_bytes(pilot50._canonical_json_bytes(cases))
    return cases, cases_path, str(receipt["cases_sha256"])


def _raw_report(
    cases: list[dict[str, Any]],
    *,
    cases_sha256: str,
    canary: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    trace_rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        request_id = str(UUID(int=index + 1))
        passed = index not in {0, 25}
        observed_behavior = "escalate" if index == 25 else "answer"
        was_escalated = index == 25
        result = {
            "id": case["id"],
            "tags": case["tags"],
            "request_id": request_id,
            "http_status": 200,
            "http_success": True,
            "trace_found": True,
            "cache_hit": False,
            "error": None,
            "trace_error": None,
            "trace_eval_run_id": EVAL_RUN_ID,
            "trace_eval_case_id": case["id"],
            "trace_binding_match": True,
            "passed": passed,
            "observed_behavior": observed_behavior,
            "was_escalated": was_escalated,
            "escalation_reason": "low_confidence" if was_escalated else None,
            "trace_total_latency_ms": 100 + index,
            "generate_retry_reasons": [],
            "llm_accounting_present": True,
            "llm_prompt_tokens": 1,
            "llm_completion_tokens": 1,
            "llm_total_tokens": 2,
            "llm_estimated_cost_rub": 0.000024,
            "llm_usage": [
                {
                    "model": "ai-sage/GigaChat3-10B-A1.8B",
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "estimated_cost_rub": 0.000024,
                    "priced": True,
                    "pricing_source": pilot50.PRICING_SOURCE,
                    "pricing_contract_id": pilot50.PRICING_CONTRACT_ID,
                    "pricing_rate_card_sha256": pilot50.PRICING_RATE_CARD_SHA256,
                }
            ],
            "target_reported_llm_usage": [
                {
                    "model": "ai-sage/GigaChat3-10B-A1.8B",
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "estimated_cost_rub": 0.0,
                    "priced": False,
                }
            ],
            "target_reported_llm_prompt_tokens": 1,
            "target_reported_llm_completion_tokens": 1,
            "target_reported_llm_total_tokens": 2,
            "target_reported_llm_estimated_cost_rub": 0.0,
            "llm_cost_pricing_provenance": {
                **pilot50.PRICING_PROVENANCE_BASE,
                "status": "repriced",
            },
            "query": case["query"],
            "response": f"{canary}-response-{index}",
        }
        results.append(result)
        trace_rows.append(
            {
                "eval_run_id": EVAL_RUN_ID,
                "request_id": request_id,
                "eval_case_id": case["id"],
                "cache_hit": False,
                "error_present": False,
            }
        )
    report = {
        "run_started_at": RUN_STARTED_AT,
        "generated_at": RUN_COMPLETED_AT,
        "run_completed_at": RUN_COMPLETED_AT,
        "target": pilot50.PILOT50_TARGET,
        "cases_total": 50,
        "eval_run_id": EVAL_RUN_ID,
        "trace_coverage_rate": 1.0,
        "cache_hit_rate": 0.0,
        "llm_budget_rub": 20.0,
        "llm_budget_exceeded": False,
        "llm_budget_stopped": False,
        "llm_pricing_stopped": False,
        "llm_estimated_cost_rub": 0.0012,
        "cases_file_sha256": cases_sha256,
        "cost_control": {
            "strict_live": True,
            "high_cost_approval_id": APPROVAL_ID,
            "pricing_complete": True,
            "pricing_projection": pilot50.PRICING_PROJECTION,
            "reservation": {
                "valid": True,
                "run_id": EVAL_RUN_ID,
                "scope": "ask-eval",
                "runtime_git_sha": RUNTIME_GIT_SHA,
                "manifest_sha256": cases_sha256,
                "case_count": 50,
                "approved_cap_rub": 20.0,
                "approval_required": True,
                "high_cost_approval_id": APPROVAL_ID,
                "cases_file_sha256": cases_sha256,
                "manifest_matches_cases_file": True,
            },
        },
        "runtime_identity": {
            "required": True,
            "status": "verified",
            "expected_runtime_git_sha": RUNTIME_GIT_SHA,
            "preflight_release_git_sha": RUNTIME_GIT_SHA,
            "postflight_release_git_sha": RUNTIME_GIT_SHA,
            "verified_release_git_sha": RUNTIME_GIT_SHA,
            "matched_expected_runtime": True,
        },
        "results": results,
        "private_canary": canary,
    }
    return report, trace_rows


def _candidate_raw_report(
    cases: list[dict[str, Any]],
    *,
    cases_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report, trace_rows = _raw_report(cases, cases_sha256=cases_sha256)
    report["target"] = pilot50.PILOT50_CANDIDATE_TARGET
    report["llm_budget_rub"] = pilot50.CANDIDATE_MAX_LLM_COST_RUB
    cost_control = report["cost_control"]
    cost_control.pop("pricing_projection")
    cost_control["candidate_contract"] = {
        "schema_version": "pilot50-candidate-eval-v1",
        "contract_id": pilot50.CANDIDATE_CONTRACT_ID,
        "runtime_git_sha": RUNTIME_GIT_SHA,
        "cases_file_sha256": cases_sha256,
        "cases_total": 50,
        "target": pilot50.PILOT50_CANDIDATE_TARGET,
        "concurrency": 1,
        "cache_bypass": "signed_pre_and_per_request",
        "runtime_ready_checks": "signed_pre_and_post",
        "complete_traces_required": True,
        "max_llm_cost_rub": pilot50.CANDIDATE_MAX_LLM_COST_RUB,
        "cost_scope": pilot50.CANDIDATE_COST_SCOPE,
        "reservation_private_full": True,
        "pricing_source": pilot50.CANDIDATE_PRICING_SOURCE,
        "pricing_rate_card_sha256": pilot50.PRICING_RATE_CARD_SHA256,
        "target_telemetry_pricing_complete": True,
        "repricing_applied": False,
    }
    reservation = cost_control["reservation"]
    reservation.update(
        {
            "scope": pilot50.CANDIDATE_COST_SCOPE,
            "approved_cap_rub": pilot50.CANDIDATE_MAX_LLM_COST_RUB,
            "private_full": True,
            "reservation_class": "private_full",
        }
    )
    cases_by_id = {str(case["id"]): case for case in cases}
    for result in report["results"]:
        case = cases_by_id[str(result["id"])]
        if (
            set(case.get("tags") or []) & pilot50.CANDIDATE_CRITICAL_CASE_TAGS
            and result["passed"] is False
        ):
            result.update(
                {
                    "passed": True,
                    "observed_behavior": "answer",
                    "was_escalated": False,
                    "escalation_reason": None,
                }
            )
        if case.get("expected_chunk_ids"):
            result["expected_chunk_hit"] = True
            result["expected_or_equivalent_chunk_hit"] = True
        if case.get("expected_cited_chunk_ids"):
            result["expected_cited_chunk_hit"] = True
            result["expected_cited_or_equivalent_chunk_hit"] = True
        usage = []
        for event in result["llm_usage"]:
            usage.append(
                {
                    key: value
                    for key, value in event.items()
                    if key
                    not in {
                        "pricing_source",
                        "pricing_contract_id",
                        "pricing_rate_card_sha256",
                    }
                }
            )
        result["llm_usage"] = usage
        for field in list(result):
            if field.startswith("target_reported_llm_"):
                result.pop(field)
        result.pop("llm_cost_pricing_provenance")
    report["pilot50_candidate"] = {
        "status": "completed",
        "completed": True,
        "contract_id": pilot50.CANDIDATE_CONTRACT_ID,
        "expected_cases_total": 50,
        "executed_cases_total": 50,
        "cases_file_sha256": cases_sha256,
        "runtime_git_sha": RUNTIME_GIT_SHA,
        "integrity_failures": [],
        "selective_reruns_forbidden": True,
    }
    return report, trace_rows


def _v3_candidate_raw_report(
    cases: list[dict[str, Any]],
    *,
    cases_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report, trace_rows = _candidate_raw_report(
        cases,
        cases_sha256=cases_sha256,
    )
    approval_id = pilot50._v3_expected_approval_id(RUNTIME_GIT_SHA)
    waiver_id = pilot50._v3_expected_waiver_id(RUNTIME_GIT_SHA)
    cost_control = report["cost_control"]
    cost_control["high_cost_approval_id"] = approval_id
    contract = cost_control["candidate_contract"]
    contract.update(
        {
            "contract_id": pilot50.V3_CANDIDATE_CONTRACT_ID,
            "cost_scope": pilot50.V3_CANDIDATE_COST_SCOPE,
            "rolling_24h_comparison_waiver_id": waiver_id,
            "rolling_24h_comparison_waiver_decision_id": "D-041",
            "provider_residual_risk_ceiling_rub": 500.0,
        }
    )
    reservation = cost_control["reservation"]
    reservation.update(
        {
            "schema_version": "1.1.0",
            "scope": pilot50.V3_CANDIDATE_COST_SCOPE,
            "high_cost_approval_id": approval_id,
            "rolling_24h_waiver_id": waiver_id,
            "rolling_24h_waiver_decision_id": "D-041",
            "waived_reservation_sha256": "e" * 64,
            "provider_risk_ceiling_rub": 500.0,
        }
    )
    report["pilot50_candidate"]["contract_id"] = (
        pilot50.V3_CANDIDATE_CONTRACT_ID
    )
    return report, trace_rows


def _v3_candidate_materialized_workspace(
    tmp_path: Path,
) -> tuple[list[dict[str, Any]], Path, str]:
    cases, receipt = pilot50.build_materialized_cases(V3_MANIFEST_PATH)
    cases_path = tmp_path / "pilot50-v3-cases.json"
    cases_path.write_bytes(pilot50._canonical_json_bytes(cases))
    return cases, cases_path, str(receipt["cases_sha256"])


def _write_v3_rejected_diagnostics_fixture(
    tmp_path: Path,
    *,
    canary: str = "private-timeout-canary",
) -> tuple[Path, Path, dict[str, str], dict[str, Any]]:
    cases, cases_path, cases_sha = _v3_candidate_materialized_workspace(tmp_path)
    report, _trace_rows = _v3_candidate_raw_report(cases, cases_sha256=cases_sha)
    _add_diagnostic_checks(cases, report)
    for case, result in zip(cases, report["results"], strict=True):
        result["response"] = " ".join(case["expected_answer_contains"])
        result["observed_chunk_ids"] = list(case["expected_chunk_ids"])
        result["retrieved_chunk_ids"] = list(case["expected_chunk_ids"])
        result["reranked_chunk_ids"] = list(case["expected_chunk_ids"])
        result["selected_source_ids"] = list(case["expected_chunk_ids"])
        result["cited_source_ids"] = list(case["expected_cited_chunk_ids"])
        result["ordered_cited_source_ids"] = list(
            case["expected_cited_chunk_ids"]
        )
        result["lineage_stage_available"] = {
            "retrieve": True,
            "rerank": True,
            "source_selection": True,
            "citation": True,
            "verify": True,
        }
    report["results"][0]["observed_chunk_ids"].append(f"{canary}-source")
    timeout = report["results"][19]
    timeout.update(
        {
            "passed": False,
            "observed_behavior": "escalate",
            "was_escalated": True,
            "escalation_reason": "request_timeout",
            "error": "request_timeout",
            "trace_error": "request_timeout",
            "trace_total_latency_ms": 45_012,
            "behavior_match": False,
            "escalation_match": False,
            "failure_reasons": [canary],
        }
    )
    report["pilot50_candidate"].update(
        {
            "status": "integrity_rejected",
            "completed": False,
            "integrity_failures": ["trace_error_present"],
            "rejection_evidence": "private-rejected-report-name",
        }
    )
    report["trace_cardinality"] = {
        "eval_run_id": EVAL_RUN_ID,
        "expected_cases_total": 50,
        "traces_total": 50,
        "case_counts": {str(case["id"]): 1 for case in cases},
        "missing_case_ids": [],
        "duplicate_case_ids": [],
        "unknown_case_ids": [],
    }
    report_path = tmp_path / "pilot50-v3-rejected.json"
    _write_json(report_path, report)
    hashes = {
        "manifest": hashlib.sha256(V3_MANIFEST_PATH.read_bytes()).hexdigest(),
        "cases": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "report": hashlib.sha256(report_path.read_bytes()).hexdigest(),
    }
    return cases_path, report_path, hashes, report


def _diagnose_rejected_v3_argv(
    *,
    cases_path: Path,
    report_path: Path,
    hashes: dict[str, str],
    expected_runtime_git_sha: str = RUNTIME_GIT_SHA,
) -> list[str]:
    return [
        "diagnose-rejected-v3",
        "--manifest",
        str(V3_MANIFEST_PATH),
        "--cases",
        str(cases_path),
        "--report",
        str(report_path),
        "--expected-manifest-sha256",
        hashes["manifest"],
        "--expected-cases-sha256",
        hashes["cases"],
        "--expected-report-sha256",
        hashes["report"],
        "--expected-runtime-git-sha",
        expected_runtime_git_sha,
    ]


def _candidate_quality_gate(
    *,
    typical_closed: int = 15,
    atypical_closed: int = 15,
    output_contract_escalations: int = 0,
    source_binding_failures: int = 0,
    critical_case_failures: int = 0,
) -> dict[str, Any]:
    return pilot50._build_candidate_quality_gate(
        typical_closed=typical_closed,
        atypical_closed=atypical_closed,
        output_contract_escalations=output_contract_escalations,
        source_binding_failures=source_binding_failures,
        applicable_qrel_cases=38,
        critical_case_failures=critical_case_failures,
        applicable_critical_cases=15,
    )


def _write_report(
    tmp_path: Path,
    cases: list[dict[str, Any]],
    cases_sha256: str,
    *,
    canary: str = "",
) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    report, rows = _raw_report(cases, cases_sha256=cases_sha256, canary=canary)
    report_path = tmp_path / "pilot50-raw.json"
    _write_json(report_path, report)
    return report_path, report, rows


def _write_validated_safe_result(
    tmp_path: Path,
    *,
    cases_path: Path,
    report_path: Path,
    trace_rows: list[dict[str, Any]],
) -> tuple[Path, dict[str, Any]]:
    safe = pilot50.build_safe_result(
        manifest_path=MANIFEST_PATH,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
        expected_runtime_git_sha=RUNTIME_GIT_SHA,
        expected_approval_id=APPROVAL_ID,
    )
    safe_path = tmp_path / "pilot50-safe.json"
    _write_json(safe_path, safe)
    return safe_path, safe


def _show_review_argv(
    *,
    cases_path: Path,
    report_path: Path,
    safe_path: Path,
    expected_runtime_git_sha: str = RUNTIME_GIT_SHA,
) -> list[str]:
    return [
        "show-review",
        "--manifest",
        str(MANIFEST_PATH),
        "--cases",
        str(cases_path),
        "--report",
        str(report_path),
        "--safe-result",
        str(safe_path),
        "--expected-runtime-git-sha",
        expected_runtime_git_sha,
    ]


def _candidate_materialized_workspace(
    tmp_path: Path,
) -> tuple[list[dict[str, Any]], Path, str]:
    cases, receipt = pilot50.build_materialized_cases(V2_MANIFEST_PATH)
    cases_path = tmp_path / "pilot50-v2-cases.json"
    cases_path.write_bytes(pilot50._canonical_json_bytes(cases))
    return cases, cases_path, str(receipt["cases_sha256"])


def _add_diagnostic_checks(
    cases: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    assert len(cases) == 50
    for result in report["results"]:
        result.update(
            {
                "passed": True,
                "observed_behavior": "answer",
                "was_escalated": False,
                "escalation_reason": None,
                "private_boolean_canary": False,
            }
        )
        for field in (
            "expected_chunk_hit",
            "expected_or_equivalent_chunk_hit",
            "expected_cited_chunk_hit",
            "expected_cited_or_equivalent_chunk_hit",
            "cited_source_types_allowed",
            "answer_contains_match",
            "message_masked_contains_match",
            "message_masked_forbidden_absent_match",
            "behavior_match",
            "routing_response_profile_match",
            "forbidden_response_profiles_absent",
            "escalation_match",
            "escalation_reason_match",
            "generator_model_match",
            "no_false_insufficient_source_response",
            "no_non_answer_response",
        ):
            result[field] = True


def _write_candidate_diagnostics_fixture(
    tmp_path: Path,
    *,
    failed_rows: int = 7,
    canary: str = "",
) -> tuple[Path, Path, Path, dict[str, str], dict[str, Any], dict[str, Any]]:
    cases, cases_path, cases_sha = _candidate_materialized_workspace(tmp_path)
    report, trace_rows = _candidate_raw_report(cases, cases_sha256=cases_sha)
    _add_diagnostic_checks(cases, report)
    generator_examples = [
        ("source_chunk", 4_999),
        ("source_only", 5_000),
        (pilot50.PRICING_PROJECTION["simple_model"], 14_999),
        (pilot50.PRICING_PROJECTION["complex_model"], 15_000),
        (f"{canary}-private-model" if canary else "unrecognized-model", 29_999),
        (None, 30_000),
        ("not_run", 7_000),
        ("unknown", 8_000),
    ]
    for result, (generator_model, latency_ms) in zip(
        report["results"],
        generator_examples,
        strict=False,
    ):
        result["generator_model"] = generator_model
        result["trace_total_latency_ms"] = latency_ms
    report["results"][0]["generate_retry_reasons"] = [
        "llm_response_contract_failed"
    ]
    for result in report["results"][:failed_rows]:
        result.update(
            {
                "passed": False,
                "observed_behavior": "escalate",
                "was_escalated": True,
                "escalation_reason": "llm_response_contract_failed",
                "behavior_match": False,
                "escalation_match": False,
            }
        )
    if canary:
        report["private_diagnostic_canary"] = canary
        report["results"][0]["response"] = f"{canary}-response"
        report["results"][0]["failure_reasons"] = [f"{canary}-failure"]
        report["results"][0]["private_free_text"] = f"{canary}-private"
        report["results"][0]["escalation_reason"] = (
            "private_escalation_reason_canary"
        )
    report_path = tmp_path / "pilot50-v2-raw.json"
    _write_json(report_path, report)
    safe = pilot50.build_safe_result(
        manifest_path=V2_MANIFEST_PATH,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
        expected_runtime_git_sha=RUNTIME_GIT_SHA,
        expected_approval_id=APPROVAL_ID,
        candidate_contract=pilot50.CANDIDATE_CONTRACT_ID,
    )
    safe_path = tmp_path / "pilot50-v2-safe.json"
    _write_json(safe_path, safe)
    hashes = {
        "manifest": hashlib.sha256(V2_MANIFEST_PATH.read_bytes()).hexdigest(),
        "cases": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "report": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "safe": hashlib.sha256(safe_path.read_bytes()).hexdigest(),
    }
    return cases_path, report_path, safe_path, hashes, report, safe


def _diagnose_argv(
    *,
    cases_path: Path,
    report_path: Path,
    safe_path: Path,
    hashes: dict[str, str],
    expected_runtime_git_sha: str = RUNTIME_GIT_SHA,
) -> list[str]:
    return [
        "diagnose",
        "--manifest",
        str(V2_MANIFEST_PATH),
        "--cases",
        str(cases_path),
        "--report",
        str(report_path),
        "--safe-result",
        str(safe_path),
        "--expected-manifest-sha256",
        hashes["manifest"],
        "--expected-cases-sha256",
        hashes["cases"],
        "--expected-report-sha256",
        hashes["report"],
        "--expected-safe-result-sha256",
        hashes["safe"],
        "--expected-runtime-git-sha",
        expected_runtime_git_sha,
    ]


def _rewrite_report_and_rebind_safe_result(
    report_path: Path,
    report: dict[str, Any],
    safe_path: Path,
    safe: dict[str, Any],
) -> None:
    _write_json(report_path, report)
    safe["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    _write_json(safe_path, safe)


def _clone_bound_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, Any]]:
    original_root = pilot50.PROJECT_ROOT
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for source in manifest["sources"]:
        relative = Path(source["path"])
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((original_root / relative).read_bytes())
    manifest_path = tmp_path / "pilot50-manifest.json"
    _write_json(manifest_path, manifest)
    monkeypatch.setattr(pilot50, "PROJECT_ROOT", tmp_path)
    return manifest_path, manifest


def _rewrite_source_and_hash(
    root: Path,
    manifest: dict[str, Any],
    *,
    source_path: str,
    rows: list[dict[str, Any]],
) -> None:
    payload = (
        json.dumps(rows, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    (root / source_path).write_bytes(payload)
    source = next(item for item in manifest["sources"] if item["path"] == source_path)
    source["sha256"] = hashlib.sha256(
        pilot50._canonical_json_bytes(rows)
    ).hexdigest()


def test_prepare_materializes_exact_balanced_answer_only_set(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "pilot50-cases.json"

    assert (
        pilot50.main(
            [
                "prepare",
                "--manifest",
                str(MANIFEST_PATH),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    receipt = json.loads(capsys.readouterr().out)
    cases = json.loads(output.read_text(encoding="utf-8"))
    assert receipt == {
        "status": "OK",
        "operation": "prepare",
        "dataset_id": "pilot50_balanced_v1",
        "cases_total": 50,
        "type_counts": {"typical": 25, "atypical": 25},
        "expected_behavior": "answer",
        "expected_escalated": False,
        "manifest_sha256": hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    assert len(cases) == 50
    assert len({case["id"] for case in cases}) == 50
    assert len({" ".join(case["query"].casefold().split()) for case in cases}) == 50
    assert {case["expected_behavior"] for case in cases} == {"answer"}
    assert {case["expected_escalated"] for case in cases} == {False}
    assert {case["privacy_class"] for case in cases} == {"standard"}
    assert len({case["user_id"] for case in cases}) == 50
    assert sum(case["pilot50_group"] == "typical" for case in cases) == 25
    assert sum(case["pilot50_group"] == "atypical" for case in cases) == 25
    for case in cases:
        group = case["pilot50_group"]
        assert "pilot50:v1" in case["tags"]
        assert f"type:{group}" in case["tags"]
        assert not any("holdout" in tag.casefold() for tag in case["tags"])
        assert not (pilot50.FORBIDDEN_CASE_FIELDS & set(case))


def test_safe_result_preserves_v2_dataset_identity(
    tmp_path: Path,
) -> None:
    cases, receipt = pilot50.build_materialized_cases(V2_MANIFEST_PATH)
    cases_path = tmp_path / "pilot50-v2-cases.json"
    cases_path.write_bytes(pilot50._canonical_json_bytes(cases))
    cases_sha = str(receipt["cases_sha256"])
    assert cases_sha == pilot50.CANDIDATE_CASES_SHA256
    report, trace_rows = _candidate_raw_report(
        cases,
        cases_sha256=cases_sha,
    )
    report_path = tmp_path / "pilot50-v2-report.json"
    _write_json(report_path, report)

    safe = pilot50.build_safe_result(
        manifest_path=V2_MANIFEST_PATH,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
        expected_runtime_git_sha=RUNTIME_GIT_SHA,
        expected_approval_id=APPROVAL_ID,
        candidate_contract=pilot50.CANDIDATE_CONTRACT_ID,
    )

    assert safe["dataset_id"] == "pilot50_balanced_v2"
    assert safe["status"] == "OK"
    assert safe["quality_gate"]["status"] == "GO"
    assert safe["quality_gate"]["criteria"]["source_binding_failures"] == {
        "actual": 0,
        "maximum": 0,
        "passed": True,
        "applicable_qrel_cases": 38,
        "total_cases": 50,
    }
    assert safe["quality_gate"]["criteria"]["critical_case_failures"] == {
        "actual": 0,
        "maximum": 0,
        "passed": True,
        "applicable_critical_cases": 15,
        "total_cases": 50,
    }
    assert pilot50.validate_safe_result(safe)["dataset_id"] == "pilot50_balanced_v2"
    safe_path = tmp_path / "pilot50-v2-safe.json"
    _write_json(safe_path, safe)
    review = pilot50.build_review_rows(
        manifest_path=V2_MANIFEST_PATH,
        cases_path=cases_path,
        report_path=report_path,
        safe_result_path=safe_path,
        expected_runtime_git_sha=RUNTIME_GIT_SHA,
    )
    assert len(review) == 50


def test_v3_safe_result_binds_exact_comparison_waiver_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    cases, receipt = pilot50.build_materialized_cases(V3_MANIFEST_PATH)
    cases_path = tmp_path / "pilot50-v3-cases.json"
    cases_path.write_bytes(pilot50._canonical_json_bytes(cases))
    cases_sha = str(receipt["cases_sha256"])
    report, trace_rows = _v3_candidate_raw_report(
        cases,
        cases_sha256=cases_sha,
    )
    report_path = tmp_path / "pilot50-v3-report.json"
    _write_json(report_path, report)
    approval_id = pilot50._v3_expected_approval_id(RUNTIME_GIT_SHA)
    waiver_id = pilot50._v3_expected_waiver_id(RUNTIME_GIT_SHA)

    safe = pilot50.build_safe_result(
        manifest_path=V3_MANIFEST_PATH,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
        expected_runtime_git_sha=RUNTIME_GIT_SHA,
        expected_approval_id=approval_id,
        expected_rolling_24h_comparison_waiver_id=waiver_id,
        candidate_contract=pilot50.V3_CANDIDATE_CONTRACT_ID,
    )

    assert safe["rolling_24h_waiver"] == {
        "waiver_id": waiver_id,
        "decision_id": "D-041",
        "waived_reservation_sha256": "e" * 64,
        "provider_residual_risk_ceiling_rub": 500,
        "runner_projected_stop_limit_rub": 30,
    }
    assert safe["budget"]["max_rub"] == 30
    assert pilot50.validate_safe_result(safe) == safe

    for field, value in (
        ("waiver_id", "owner-wrong-waiver"),
        ("decision_id", "D-999"),
        ("waived_reservation_sha256", "0" * 63),
        ("provider_residual_risk_ceiling_rub", 501),
        ("runner_projected_stop_limit_rub", 500),
    ):
        tampered_safe = copy.deepcopy(safe)
        tampered_safe["rolling_24h_waiver"][field] = value
        with pytest.raises(pilot50.Pilot50Error, match="comparison waiver"):
            pilot50.validate_safe_result(tampered_safe)

    tampered_report = copy.deepcopy(report)
    tampered_report["cost_control"]["reservation"][
        "rolling_24h_waiver_decision_id"
    ] = "D-999"
    _write_json(report_path, tampered_report)
    with pytest.raises(pilot50.Pilot50Error, match="comparison waiver"):
        pilot50.build_safe_result(
            manifest_path=V3_MANIFEST_PATH,
            cases_path=cases_path,
            report_path=report_path,
            trace_rows=trace_rows,
            expected_runtime_git_sha=RUNTIME_GIT_SHA,
            expected_approval_id=approval_id,
            expected_rolling_24h_comparison_waiver_id=waiver_id,
            candidate_contract=pilot50.V3_CANDIDATE_CONTRACT_ID,
        )


@pytest.mark.parametrize(
    ("typical_closed", "atypical_closed", "expected_status", "failed_criteria"),
    [
        (11, 18, "STOP", ["overall_closed"]),
        (12, 18, "GO", []),
    ],
)
def test_candidate_quality_gate_overall_boundary(
    typical_closed: int,
    atypical_closed: int,
    expected_status: str,
    failed_criteria: list[str],
) -> None:
    gate = _candidate_quality_gate(
        typical_closed=typical_closed,
        atypical_closed=atypical_closed,
    )

    assert gate["status"] == expected_status
    assert gate["failed_criteria"] == failed_criteria


@pytest.mark.parametrize(
    ("typical_closed", "atypical_closed", "failed_criterion"),
    [
        (10, 20, "typical_closed"),
        (24, 6, "atypical_closed"),
    ],
)
def test_candidate_quality_gate_slice_floor_boundary(
    typical_closed: int,
    atypical_closed: int,
    failed_criterion: str,
) -> None:
    failing = _candidate_quality_gate(
        typical_closed=typical_closed,
        atypical_closed=atypical_closed,
    )
    passing = _candidate_quality_gate(
        typical_closed=(11 if failed_criterion == "typical_closed" else 23),
        atypical_closed=(19 if failed_criterion == "typical_closed" else 7),
    )

    assert failing["status"] == "STOP"
    assert failing["failed_criteria"] == [failed_criterion]
    assert passing["status"] == "GO"


@pytest.mark.parametrize(
    ("output_contract_escalations", "expected_status"),
    [(6, "GO"), (7, "STOP")],
)
def test_candidate_quality_gate_output_contract_boundary(
    output_contract_escalations: int,
    expected_status: str,
) -> None:
    gate = _candidate_quality_gate(
        output_contract_escalations=output_contract_escalations,
    )

    assert gate["status"] == expected_status


@pytest.mark.parametrize(
    ("source_binding_failures", "expected_status"),
    [(0, "GO"), (1, "STOP")],
)
def test_candidate_quality_gate_source_binding_boundary(
    source_binding_failures: int,
    expected_status: str,
) -> None:
    gate = _candidate_quality_gate(
        source_binding_failures=source_binding_failures,
    )

    assert gate["status"] == expected_status


@pytest.mark.parametrize(
    ("critical_case_failures", "expected_status"),
    [(0, "GO"), (1, "STOP")],
)
def test_candidate_quality_gate_critical_case_boundary(
    critical_case_failures: int,
    expected_status: str,
) -> None:
    gate = _candidate_quality_gate(critical_case_failures=critical_case_failures)

    assert gate["status"] == expected_status


def test_candidate_output_contract_escalation_bucket_is_fixed() -> None:
    assert pilot50.CANDIDATE_OUTPUT_CONTRACT_ESCALATION_REASONS == frozenset(
        {
            "empty_generated_response",
            "final_response_empty",
            "final_response_too_long",
            "final_response_too_many_links",
            "final_response_unapproved_emoji",
            "llm_response_contract_failed",
            "llm_response_profile_failed",
            "llm_response_too_long",
            "llm_source_citation_failed",
            "llm_source_coverage_failed",
            "llm_source_fact_binding_failed",
            "source_response_contract_failed",
        }
    )
    assert {
        "generation_failed",
        "llm_generation_failed",
    }.isdisjoint(pilot50.CANDIDATE_OUTPUT_CONTRACT_ESCALATION_REASONS)


def test_candidate_source_binding_uses_equivalent_source_checks() -> None:
    expected = {
        "expected_chunk_ids": ["expected"],
        "expected_cited_chunk_ids": ["expected"],
        "equivalent_chunk_ids": {"expected": ["equivalent"]},
    }
    result = {
        "was_escalated": False,
        "expected_chunk_hit": False,
        "expected_or_equivalent_chunk_hit": True,
        "expected_cited_chunk_hit": False,
        "expected_cited_or_equivalent_chunk_hit": True,
    }

    assert pilot50._candidate_source_binding_failed(expected, result) is False
    result["expected_cited_or_equivalent_chunk_hit"] = None
    assert pilot50._candidate_source_binding_failed(expected, result) is True
    result["was_escalated"] = True
    assert pilot50._candidate_source_binding_failed(expected, result) is False


@pytest.mark.parametrize(
    ("output_contract_escalations", "expected_status"),
    [(6, "GO"), (7, "STOP")],
)
def test_candidate_safe_result_derives_output_contract_gate(
    tmp_path: Path,
    output_contract_escalations: int,
    expected_status: str,
) -> None:
    cases, receipt = pilot50.build_materialized_cases(V2_MANIFEST_PATH)
    cases_path = tmp_path / "pilot50-v2-cases.json"
    cases_path.write_bytes(pilot50._canonical_json_bytes(cases))
    cases_sha = str(receipt["cases_sha256"])
    report, trace_rows = _candidate_raw_report(cases, cases_sha256=cases_sha)
    noncritical_results = [
        result
        for result in report["results"]
        if not (
            set(result.get("tags") or [])
            & pilot50.CANDIDATE_CRITICAL_CASE_TAGS
        )
    ]
    for result in noncritical_results[:output_contract_escalations]:
        result["passed"] = False
        result["observed_behavior"] = "escalate"
        result["was_escalated"] = True
        result["escalation_reason"] = "llm_source_coverage_failed"
    report_path = tmp_path / "pilot50-v2-report.json"
    _write_json(report_path, report)

    safe = pilot50.build_safe_result(
        manifest_path=V2_MANIFEST_PATH,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
        expected_runtime_git_sha=RUNTIME_GIT_SHA,
        expected_approval_id=APPROVAL_ID,
        candidate_contract=pilot50.CANDIDATE_CONTRACT_ID,
    )

    assert safe["status"] == "OK"
    assert safe["quality_gate"]["status"] == expected_status
    assert (
        safe["quality_gate"]["criteria"]["output_contract_escalations"]["actual"]
        == output_contract_escalations
    )


@pytest.mark.parametrize(
    ("source_check", "expected_failures", "expected_status"),
    [(True, 0, "GO"), (False, 1, "STOP")],
)
def test_candidate_safe_result_derives_source_binding_gate(
    tmp_path: Path,
    source_check: bool,
    expected_failures: int,
    expected_status: str,
) -> None:
    cases, receipt = pilot50.build_materialized_cases(V2_MANIFEST_PATH)
    cases_path = tmp_path / "pilot50-v2-cases.json"
    cases_path.write_bytes(pilot50._canonical_json_bytes(cases))
    cases_sha = str(receipt["cases_sha256"])
    report, trace_rows = _candidate_raw_report(cases, cases_sha256=cases_sha)
    report["results"][0]["expected_chunk_hit"] = source_check
    report_path = tmp_path / "pilot50-v2-report.json"
    _write_json(report_path, report)

    safe = pilot50.build_safe_result(
        manifest_path=V2_MANIFEST_PATH,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
        expected_runtime_git_sha=RUNTIME_GIT_SHA,
        expected_approval_id=APPROVAL_ID,
        candidate_contract=pilot50.CANDIDATE_CONTRACT_ID,
    )

    assert safe["status"] == "OK"
    assert safe["quality_gate"]["status"] == expected_status
    assert (
        safe["quality_gate"]["criteria"]["source_binding_failures"]["actual"]
        == expected_failures
    )


@pytest.mark.parametrize(
    ("critical_case_passed", "expected_failures", "expected_status"),
    [(True, 0, "GO"), (False, 1, "STOP")],
)
def test_candidate_safe_result_derives_critical_case_gate(
    tmp_path: Path,
    critical_case_passed: bool,
    expected_failures: int,
    expected_status: str,
) -> None:
    cases, receipt = pilot50.build_materialized_cases(V2_MANIFEST_PATH)
    cases_path = tmp_path / "pilot50-v2-cases.json"
    cases_path.write_bytes(pilot50._canonical_json_bytes(cases))
    cases_sha = str(receipt["cases_sha256"])
    report, trace_rows = _candidate_raw_report(cases, cases_sha256=cases_sha)
    critical_result = next(
        result
        for result in report["results"]
        if set(result.get("tags") or [])
        & pilot50.CANDIDATE_CRITICAL_CASE_TAGS
    )
    critical_result["passed"] = critical_case_passed
    report_path = tmp_path / "pilot50-v2-report.json"
    _write_json(report_path, report)

    safe = pilot50.build_safe_result(
        manifest_path=V2_MANIFEST_PATH,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
        expected_runtime_git_sha=RUNTIME_GIT_SHA,
        expected_approval_id=APPROVAL_ID,
        candidate_contract=pilot50.CANDIDATE_CONTRACT_ID,
    )

    assert safe["status"] == "OK"
    assert safe["quality_gate"]["status"] == expected_status
    assert (
        safe["quality_gate"]["criteria"]["critical_case_failures"]["actual"]
        == expected_failures
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda gate: gate["criteria"]["source_binding_failures"].__setitem__(
            "applicable_qrel_cases", 37
        ),
        lambda gate: gate["criteria"]["source_binding_failures"].__setitem__(
            "total_cases", 49
        ),
        lambda gate: gate["criteria"]["critical_case_failures"].__setitem__(
            "applicable_critical_cases", 14
        ),
        lambda gate: gate["criteria"]["critical_case_failures"].__setitem__(
            "total_cases", 49
        ),
        lambda gate: gate.__setitem__(
            "source_binding_definition", "semantic_wrong_entity"
        ),
        lambda gate: gate.__setitem__(
            "critical_case_definition", "mutable_critical_cases"
        ),
    ],
)
def test_candidate_quality_gate_rejects_coverage_or_definition_tampering(
    mutation: Any,
) -> None:
    gate = _candidate_quality_gate()
    mutation(gate)

    with pytest.raises(pilot50.Pilot50Error, match="candidate quality gate"):
        pilot50._validate_candidate_quality_gate(
            gate,
            typical_closed=15,
            atypical_closed=15,
        )


def test_v2_safe_result_requires_exact_candidate_contract(tmp_path: Path) -> None:
    cases, receipt = pilot50.build_materialized_cases(V2_MANIFEST_PATH)
    cases_path = tmp_path / "pilot50-v2-cases.json"
    cases_path.write_bytes(pilot50._canonical_json_bytes(cases))
    report, trace_rows = _candidate_raw_report(
        cases,
        cases_sha256=str(receipt["cases_sha256"]),
    )
    report_path = tmp_path / "pilot50-v2-report.json"
    _write_json(report_path, report)

    with pytest.raises(pilot50.Pilot50Error, match="exact candidate contract"):
        pilot50.build_safe_result(
            manifest_path=V2_MANIFEST_PATH,
            cases_path=cases_path,
            report_path=report_path,
            trace_rows=trace_rows,
            expected_runtime_git_sha=RUNTIME_GIT_SHA,
            expected_approval_id=APPROVAL_ID,
        )


@pytest.mark.parametrize(
    ("model", "expected_cost"),
    [
        ("ai-sage/GigaChat3-10B-A1.8B", 0.000024),
        ("GigaChat/GigaChat-2-Max", 0.001139),
    ],
)
def test_candidate_target_reported_cost_accepts_both_fixed_models(
    model: str,
    expected_cost: float,
) -> None:
    result = {
        "http_status": 200,
        "http_success": True,
        "error": None,
        "trace_error": None,
        "generate_retry_reasons": [],
        "generator_model": model,
        "llm_accounting_present": True,
        "llm_prompt_tokens": 1,
        "llm_completion_tokens": 1,
        "llm_total_tokens": 2,
        "llm_estimated_cost_rub": expected_cost,
        "llm_usage": [
            {
                "model": model,
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "estimated_cost_rub": expected_cost,
                "priced": True,
            }
        ],
    }

    assert pilot50._validated_target_reported_case_cost(result) == expected_cost


def test_candidate_target_reported_cost_accepts_only_deterministic_not_run() -> None:
    result = {
        "http_status": 200,
        "http_success": True,
        "error": None,
        "trace_error": None,
        "generate_retry_reasons": [],
        "generator_model": "not_run",
        "analyzer_execution_mode": "deterministic",
        "llm_accounting_present": True,
        "llm_prompt_tokens": 0,
        "llm_completion_tokens": 0,
        "llm_total_tokens": 0,
        "llm_estimated_cost_rub": 0.0,
        "llm_usage": [],
    }

    assert pilot50._validated_target_reported_case_cost(result) == 0.0
    result["analyzer_execution_mode"] = "fallback"
    with pytest.raises(pilot50.Pilot50Error, match="not-run"):
        pilot50._validated_target_reported_case_cost(result)


def test_candidate_target_reported_cost_rejects_repricing_metadata() -> None:
    result = {
        "llm_accounting_present": True,
        "llm_prompt_tokens": 1,
        "llm_completion_tokens": 1,
        "llm_total_tokens": 2,
        "llm_estimated_cost_rub": 0.000024,
        "llm_usage": [
            {
                "model": "ai-sage/GigaChat3-10B-A1.8B",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "estimated_cost_rub": 0.000024,
                "priced": True,
                "pricing_source": "eval_repriced",
            }
        ],
    }

    with pytest.raises(pilot50.Pilot50Error, match="repricing metadata"):
        pilot50._validated_target_reported_case_cost(result)


def test_summarize_candidate_contract_cli_is_bounded(tmp_path: Path) -> None:
    base = [
        "summarize",
        "--cases",
        str(tmp_path / "cases.json"),
        "--report",
        str(tmp_path / "report.json"),
        "--output",
        str(tmp_path / "safe.json"),
        "--expected-runtime-git-sha",
        RUNTIME_GIT_SHA,
        "--expected-approval-id",
        APPROVAL_ID,
        "--candidate-contract",
    ]
    args = pilot50._parse_args([*base, pilot50.CANDIDATE_CONTRACT_ID])
    assert args.candidate_contract == pilot50.CANDIDATE_CONTRACT_ID
    waiver_id = pilot50._v3_expected_waiver_id(RUNTIME_GIT_SHA)
    v3_args = pilot50._parse_args(
        [
            *base,
            pilot50.V3_CANDIDATE_CONTRACT_ID,
            "--rolling-24h-comparison-waiver-id",
            waiver_id,
        ]
    )
    assert v3_args.rolling_24h_comparison_waiver_id == waiver_id
    with pytest.raises(SystemExit):
        pilot50._parse_args([*base, "mutable-candidate-contract"])


def test_prepare_refuses_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "pilot50-cases.json"
    args = Namespace(manifest=MANIFEST_PATH, output=output)

    pilot50._prepare(args)
    original = output.read_bytes()
    with pytest.raises(pilot50.Pilot50Error, match="already exists"):
        pilot50._prepare(args)

    assert output.read_bytes() == original


def test_prepare_rejects_a_quota_preserving_membership_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest = _clone_bound_sources(tmp_path, monkeypatch)
    source = next(
        item
        for item in manifest["sources"]
        if item["path"] == "eval/cases/product_calibration_synthetic_pilot_20.json"
    )
    source["case_ids"][0] = "synthetic_capabilities_scope"
    _write_json(manifest_path, manifest)

    with pytest.raises(
        pilot50.Pilot50Error,
        match="frozen Pilot50 v1 selection",
    ):
        pilot50.build_materialized_cases(manifest_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda manifest: manifest.pop("classification"), "manifest fields"),
        (
            lambda manifest: manifest.__setitem__("human_product_verdict", True),
            "human product verdict",
        ),
        (
            lambda manifest: manifest["expected_contract"].__setitem__(
                "cases_total", 49
            ),
            "expected contract",
        ),
        (
            lambda manifest: manifest["sources"][1].__setitem__(
                "case_ids",
                [
                    manifest["sources"][0]["case_ids"][0],
                    *manifest["sources"][1]["case_ids"][1:],
                ],
            ),
            "selected case membership",
        ),
    ],
)
def test_manifest_contract_mutations_fail_closed(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    mutation(manifest)
    path = tmp_path / "manifest.json"
    _write_json(path, manifest)

    with pytest.raises(pilot50.Pilot50Error, match=message):
        pilot50.build_materialized_cases(path)


def test_source_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["sources"][0]["sha256"] = "0" * 64
    path = tmp_path / "manifest.json"
    _write_json(path, manifest)

    with pytest.raises(pilot50.Pilot50Error, match="source canonical hash mismatch"):
        pilot50.build_materialized_cases(path)


def test_source_hashes_are_canonical_and_eol_independent() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    for source in manifest["sources"]:
        path = pilot50.PROJECT_ROOT / source["path"]
        rows = json.loads(path.read_text(encoding="utf-8-sig"))
        canonical = pilot50._canonical_json_bytes(rows)
        crlf = canonical.replace(b"\n", b"\r\n")
        reparsed = pilot50._load_json_bytes(crlf, label="CRLF source fixture")

        assert hashlib.sha256(canonical).hexdigest() == source["sha256"]
        assert pilot50._canonical_json_bytes(reparsed) == canonical


def test_selected_case_missing_from_bound_source_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest = _clone_bound_sources(tmp_path, monkeypatch)
    source_path = "eval/cases/pre_pilot_adversarial.json"
    source_rows = json.loads((tmp_path / source_path).read_text(encoding="utf-8"))
    selected_id = manifest["sources"][3]["case_ids"][0]
    source_rows = [row for row in source_rows if row["id"] != selected_id]
    _rewrite_source_and_hash(
        tmp_path,
        manifest,
        source_path=source_path,
        rows=source_rows,
    )
    _write_json(manifest_path, manifest)

    with pytest.raises(pilot50.Pilot50Error, match="missing from its source"):
        pilot50.build_materialized_cases(manifest_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("privacy_class", "private_ticket_derived", "standard synthetic regression"),
        ("ticket_id_hash", "private-canary", "forbidden identity fields"),
    ],
)
def test_private_or_identity_bound_source_case_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
) -> None:
    manifest_path, manifest = _clone_bound_sources(tmp_path, monkeypatch)
    source_path = "eval/cases/pre_pilot_adversarial.json"
    source_rows = json.loads((tmp_path / source_path).read_text(encoding="utf-8"))
    selected_id = manifest["sources"][3]["case_ids"][0]
    next(row for row in source_rows if row["id"] == selected_id)[field] = value
    _rewrite_source_and_hash(
        tmp_path,
        manifest,
        source_path=source_path,
        rows=source_rows,
    )
    _write_json(manifest_path, manifest)

    with pytest.raises(pilot50.Pilot50Error, match=message):
        pilot50.build_materialized_cases(manifest_path)


def test_holdout_tag_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest = _clone_bound_sources(tmp_path, monkeypatch)
    source_path = "eval/cases/pre_pilot_adversarial.json"
    source_rows = json.loads((tmp_path / source_path).read_text(encoding="utf-8"))
    selected_id = manifest["sources"][3]["case_ids"][0]
    selected = next(row for row in source_rows if row["id"] == selected_id)
    selected["tags"] = [*selected.get("tags", []), "split:holdout"]
    _rewrite_source_and_hash(
        tmp_path,
        manifest,
        source_path=source_path,
        rows=source_rows,
    )
    _write_json(manifest_path, manifest)

    with pytest.raises(pilot50.Pilot50Error, match="holdout-marked"):
        pilot50.build_materialized_cases(manifest_path)


def test_selected_query_with_pii_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest = _clone_bound_sources(tmp_path, monkeypatch)
    source_path = "eval/cases/pre_pilot_adversarial.json"
    source_rows = json.loads((tmp_path / source_path).read_text(encoding="utf-8"))
    selected_id = manifest["sources"][3]["case_ids"][0]
    selected = next(row for row in source_rows if row["id"] == selected_id)
    selected["query"] = "Напиши мне на private.person@example.org"
    _rewrite_source_and_hash(
        tmp_path,
        manifest,
        source_path=source_path,
        rows=source_rows,
    )
    _write_json(manifest_path, manifest)

    with pytest.raises(pilot50.Pilot50Error, match="failed the PII scan"):
        pilot50.build_materialized_cases(manifest_path)


def test_summarize_builds_only_safe_balanced_aggregates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases, cases_path, cases_sha = _materialized_workspace(tmp_path)
    canary = "CANARY-PRIVATE-PILOT50-RAW"
    report_path, _report, trace_rows = _write_report(
        tmp_path,
        cases,
        cases_sha,
        canary=canary,
    )
    safe_path = tmp_path / "pilot50-safe.json"

    async def fake_fetch(eval_run_id: str) -> list[dict[str, Any]]:
        assert eval_run_id == EVAL_RUN_ID
        return trace_rows

    monkeypatch.setattr(pilot50, "_fetch_trace_rows", fake_fetch)

    assert (
        pilot50.main(
            [
                "summarize",
                "--manifest",
                str(MANIFEST_PATH),
                "--cases",
                str(cases_path),
                "--report",
                str(report_path),
                "--output",
                str(safe_path),
                "--expected-runtime-git-sha",
                RUNTIME_GIT_SHA,
                "--expected-approval-id",
                APPROVAL_ID,
            ]
        )
        == 0
    )

    stdout = capsys.readouterr().out
    safe = json.loads(stdout)
    assert safe == json.loads(safe_path.read_text(encoding="utf-8"))
    assert set(safe) == pilot50.SAFE_FIELDS
    assert "quality_gate" not in safe
    assert safe["classification"] == "calibration_only"
    assert safe["human_product_verdict"] is False
    assert safe["eval_run_id"] == EVAL_RUN_ID
    assert safe["runtime_git_sha"] == RUNTIME_GIT_SHA
    assert safe["approval_id"] == APPROVAL_ID
    assert safe["run_window_utc"] == {
        "started_at": RUN_STARTED_AT,
        "completed_at": RUN_COMPLETED_AT,
    }
    assert safe["billing_status"] == "pending_provider_reconciliation"
    assert safe["denominator"] == 50
    assert safe["counts"] == {"typical": 25, "atypical": 25}
    assert safe["mechanical_first_turn_closure"] == {
        "typical": {"closed": 24, "total": 25, "rate": 0.96},
        "atypical": {"closed": 24, "total": 25, "rate": 0.96},
        "overall": {"closed": 48, "total": 50, "rate": 0.96},
    }
    assert safe["policy_pass"] == {
        "typical": {"passed": 24, "total": 25, "rate": 0.96},
        "atypical": {"passed": 24, "total": 25, "rate": 0.96},
        "overall": {"passed": 48, "total": 50, "rate": 0.96},
    }
    assert safe["trace_coverage"] == {"found": 50, "total": 50, "rate": 1.0}
    assert safe["cache_hits"] == 0
    assert safe["budget"] == {"max_rub": 20, "exceeded": False, "stopped": False}
    assert safe["pricing"] == {
        "complete": True,
        "stopped": False,
        "source": pilot50.PRICING_SOURCE,
        "contract_id": pilot50.PRICING_CONTRACT_ID,
        "rate_card_sha256": pilot50.PRICING_RATE_CARD_SHA256,
        "target_telemetry_preserved": True,
        "target_telemetry_pricing_complete": False,
    }
    assert safe["latency_ms"] == {"p50": 124, "p95": 147}
    assert safe["llm_cost_rub"] == 0.0012
    assert canary not in stdout
    serialized = safe_path.read_text(encoding="utf-8")
    assert canary not in serialized
    assert not any(
        forbidden in serialized
        for forbidden in ('"id"', '"query"', '"response"', '"request_id"')
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda report: report["results"].__setitem__(
                1, copy.deepcopy(report["results"][0])
            ),
            "membership",
        ),
        (
            lambda report: report["results"][0].__setitem__("trace_found", False),
            "trace_found invariant",
        ),
        (
            lambda report: report["results"][0].__setitem__("cache_hit", True),
            "cache_hit invariant",
        ),
        (
            lambda report: report.__setitem__("trace_coverage_rate", 0.98),
            "trace coverage",
        ),
        (
            lambda report: report.__setitem__("cache_hit_rate", 0.02),
            "cache hit rate",
        ),
        (
            lambda report: report.__setitem__("llm_budget_stopped", True),
            "stopped on budget",
        ),
        (
            lambda report: report.__setitem__("llm_budget_exceeded", True),
            "stopped on budget",
        ),
        (
            lambda report: report.__setitem__("llm_pricing_stopped", True),
            "stopped on pricing",
        ),
        (
            lambda report: report.__setitem__("llm_budget_rub", 19.0),
            "budget differs",
        ),
        (
            lambda report: report.__setitem__("llm_estimated_cost_rub", 0.75),
            "cost accounting",
        ),
        (
            lambda report: report["results"][0]["llm_usage"][0].__setitem__(
                "estimated_cost_rub", 0.0
            ),
            "projected event differs",
        ),
        (
            lambda report: report["results"][0]["target_reported_llm_usage"][
                0
            ].__setitem__("model", "unknown-model"),
            "model is not approved",
        ),
        (
            lambda report: report["results"][0].__setitem__(
                "target_reported_llm_total_tokens", 3
            ),
            "cost projection is inconsistent",
        ),
        (
            lambda report: report["cost_control"].__setitem__(
                "pricing_complete", False
            ),
            "cost-control evidence",
        ),
        (
            lambda report: report["cost_control"]["reservation"].__setitem__(
                "valid", False
            ),
            "reservation does not bind",
        ),
        (
            lambda report: report.__setitem__("target", "http://example.invalid/ask"),
            "target is invalid",
        ),
        (
            lambda report: report.__setitem__(
                "run_completed_at", "2026-08-10T11:59:59+00:00"
            ),
            "run window",
        ),
        (
            lambda report: report["runtime_identity"].__setitem__(
                "preflight_release_git_sha", "b" * 40
            ),
            "runtime identity",
        ),
        (
            lambda report: report["cost_control"].__setitem__(
                "high_cost_approval_id", "OTHER-APPROVAL"
            ),
            "cost-control evidence",
        ),
        (
            lambda report: report["cost_control"]["reservation"].__setitem__(
                "runtime_git_sha", "b" * 40
            ),
            "reservation does not bind",
        ),
        (
            lambda report: report["cost_control"]["reservation"].__setitem__(
                "run_id", "ask-eval-22222222-2222-2222-2222-222222222222"
            ),
            "reservation does not bind",
        ),
    ],
)
def test_report_integrity_failures_are_rejected(
    tmp_path: Path,
    mutate: Any,
    message: str,
) -> None:
    cases, cases_path, cases_sha = _materialized_workspace(tmp_path)
    report, trace_rows = _raw_report(cases, cases_sha256=cases_sha)
    mutate(report)
    report_path = tmp_path / "report.json"
    _write_json(report_path, report)

    with pytest.raises(pilot50.Pilot50Error, match=message):
        pilot50.build_safe_result(
            manifest_path=MANIFEST_PATH,
            cases_path=cases_path,
            report_path=report_path,
            trace_rows=trace_rows,
            expected_runtime_git_sha=RUNTIME_GIT_SHA,
            expected_approval_id=APPROVAL_ID,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rows: rows.pop(), "cardinality"),
        (
            lambda rows: rows[1].__setitem__("request_id", rows[0]["request_id"]),
            "request IDs are not unique",
        ),
        (
            lambda rows: rows[0].__setitem__("eval_case_id", "unknown-case"),
            "case membership",
        ),
        (
            lambda rows: rows[0].__setitem__("cache_hit", True),
            "cache invariant",
        ),
        (
            lambda rows: rows[0].__setitem__("error_present", True),
            "execution error",
        ),
        (
            lambda rows: rows[0].__setitem__("eval_run_id", "ask-eval-wrong"),
            "run ID mismatch",
        ),
    ],
)
def test_database_trace_cardinality_and_binding_fail_closed(
    tmp_path: Path,
    mutate: Any,
    message: str,
) -> None:
    cases, _cases_path, cases_sha = _materialized_workspace(tmp_path)
    report, trace_rows = _raw_report(cases, cases_sha256=cases_sha)
    mutate(trace_rows)

    with pytest.raises(pilot50.Pilot50Error, match=message):
        pilot50.validate_trace_rows(
            trace_rows,
            eval_run_id=EVAL_RUN_ID,
            expected_results=report["results"],
        )


def test_trace_fetch_is_bounded_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []

    class Transaction:
        async def __aenter__(self) -> None:
            calls.append(("transaction_enter", None))

        async def __aexit__(self, *_args: object) -> None:
            calls.append(("transaction_exit", None))

    class Connection:
        def transaction(self, *, readonly: bool) -> Transaction:
            calls.append(("readonly", readonly))
            return Transaction()

        async def fetch(
            self,
            query: str,
            eval_run_id: str,
            **kwargs: Any,
        ) -> list[dict[str, Any]]:
            calls.append(("query", " ".join(query.split())))
            calls.append(("run_id", eval_run_id))
            calls.append(("query_timeout", kwargs.get("timeout")))
            return []

        async def close(self) -> None:
            calls.append(("close", None))

    async def fake_connect(
        dsn: str,
        **kwargs: Any,
    ) -> Connection:
        calls.append(("dsn_present", bool(dsn)))
        calls.append(("connect_timeout", kwargs.get("timeout")))
        calls.append(("command_timeout", kwargs.get("command_timeout")))
        return Connection()

    monkeypatch.setenv("ASK_EVAL_POSTGRES_DSN", "postgresql://private-placeholder")
    monkeypatch.setattr(pilot50.asyncpg, "connect", fake_connect)

    assert asyncio.run(pilot50._fetch_trace_rows(EVAL_RUN_ID)) == []
    assert ("readonly", True) in calls
    assert ("connect_timeout", 15) in calls
    assert ("command_timeout", 15) in calls
    assert ("query_timeout", 15) in calls
    assert ("run_id", EVAL_RUN_ID) in calls
    assert calls[-1] == ("close", None)


def test_summarize_cli_fails_closed_when_trace_fetch_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases, cases_path, cases_sha = _materialized_workspace(tmp_path)
    report_path, _report, _trace_rows = _write_report(tmp_path, cases, cases_sha)
    output = tmp_path / "safe.json"

    async def unavailable(_eval_run_id: str) -> list[dict[str, Any]]:
        raise pilot50.Pilot50Error("CANARY-PRIVATE-DSN-FAILURE")

    monkeypatch.setattr(pilot50, "_fetch_trace_rows", unavailable)

    assert (
        pilot50.main(
            [
                "summarize",
                "--manifest",
                str(MANIFEST_PATH),
                "--cases",
                str(cases_path),
                "--report",
                str(report_path),
                "--output",
                str(output),
                "--expected-runtime-git-sha",
                RUNTIME_GIT_SHA,
                "--expected-approval-id",
                APPROVAL_ID,
            ]
        )
        == 2
    )
    stdout = capsys.readouterr().out
    assert stdout == "pilot50=SUMMARIZE reason=validation_failed\n"
    assert "CANARY" not in stdout
    assert not output.exists()


def test_show_review_prints_only_the_exact_owner_review_jsonl_projection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases, cases_path, cases_sha = _materialized_workspace(tmp_path)
    report_path, report, trace_rows = _write_report(tmp_path, cases, cases_sha)
    report["private_canary"] = "PRIVATE-REPORT-FIELD-CANARY"
    report["results"][0]["tags"] = [
        *report["results"][0]["tags"],
        "PRIVATE-TAG-CANARY",
    ]
    report["results"][0]["runtime_private_field"] = "PRIVATE-RUNTIME-CANARY"
    _write_json(report_path, report)
    safe_path, _safe = _write_validated_safe_result(
        tmp_path,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
    )

    assert (
        pilot50.main(
            _show_review_argv(
                cases_path=cases_path,
                report_path=report_path,
                safe_path=safe_path,
            )
        )
        == 0
    )

    stdout = capsys.readouterr().out
    lines = stdout.splitlines()
    review_rows = [json.loads(line) for line in lines]
    expected_fields = {
        "ordinal",
        "group",
        "query",
        "response",
        "was_escalated",
        "escalation_reason",
        "passed",
        "observed_behavior",
    }
    assert len(lines) == 50
    assert len(review_rows) == 50
    assert [row["ordinal"] for row in review_rows] == list(range(1, 51))
    assert [row["group"] for row in review_rows] == [
        case["pilot50_group"] for case in cases
    ]
    assert sum(row["group"] == "typical" for row in review_rows) == 25
    assert sum(row["group"] == "atypical" for row in review_rows) == 25
    for ordinal, (row, case, result) in enumerate(
        zip(review_rows, cases, report["results"], strict=True),
        start=1,
    ):
        assert set(row) == expected_fields
        assert row == {
            "ordinal": ordinal,
            "group": case["pilot50_group"],
            "query": case["query"],
            "response": result["response"],
            "was_escalated": result["was_escalated"],
            "escalation_reason": result["escalation_reason"],
            "passed": result["passed"],
            "observed_behavior": result["observed_behavior"],
        }
    for forbidden in (
        '"id"',
        '"request_id"',
        '"tags"',
        '"runtime_git_sha"',
        '"private_canary"',
        "PRIVATE-REPORT-FIELD-CANARY",
        "PRIVATE-TAG-CANARY",
        "PRIVATE-RUNTIME-CANARY",
        RUNTIME_GIT_SHA,
    ):
        assert forbidden not in stdout


def test_show_review_jsonl_escapes_terminal_control_characters(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases, cases_path, cases_sha = _materialized_workspace(tmp_path)
    report_path, report, trace_rows = _write_report(tmp_path, cases, cases_sha)
    terminal_payload = "line-one\nline-two\r\t\x1b[31mred\x00done"
    report["results"][0]["response"] = terminal_payload
    _write_json(report_path, report)
    safe_path, _safe = _write_validated_safe_result(
        tmp_path,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
    )

    assert (
        pilot50.main(
            _show_review_argv(
                cases_path=cases_path,
                report_path=report_path,
                safe_path=safe_path,
            )
        )
        == 0
    )

    stdout = capsys.readouterr().out
    lines = stdout.splitlines()
    assert len(lines) == 50
    assert stdout.count("\n") == 50
    assert json.loads(lines[0])["response"] == terminal_payload
    assert "\r" not in stdout
    assert "\t" not in stdout
    assert "\x1b" not in stdout
    assert "\x00" not in stdout
    assert "\\n" in lines[0]
    assert "\\r" in lines[0]
    assert "\\t" in lines[0]
    assert "\\u001b" in lines[0]
    assert "\\u0000" in lines[0]


@pytest.mark.parametrize(
    "mutation",
    ["materialized_query", "report_membership", "report_query"],
)
def test_show_review_rejects_tampered_case_and_report_bindings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    cases, cases_path, cases_sha = _materialized_workspace(tmp_path)
    report_path, report, trace_rows = _write_report(tmp_path, cases, cases_sha)
    safe_path, safe = _write_validated_safe_result(
        tmp_path,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
    )
    if mutation == "materialized_query":
        tampered_cases = copy.deepcopy(cases)
        tampered_cases[0]["query"] = "PRIVATE-TAMPERED-CASE-QUERY"
        cases_path.write_bytes(pilot50._canonical_json_bytes(tampered_cases))
        safe["cases_sha256"] = hashlib.sha256(cases_path.read_bytes()).hexdigest()
        _write_json(safe_path, safe)
    elif mutation == "report_membership":
        report["results"][0]["id"] = "PRIVATE-TAMPERED-CASE-ID"
        _rewrite_report_and_rebind_safe_result(report_path, report, safe_path, safe)
    else:
        report["results"][0]["query"] = "PRIVATE-TAMPERED-REPORT-QUERY"
        _rewrite_report_and_rebind_safe_result(report_path, report, safe_path, safe)

    assert (
        pilot50.main(
            _show_review_argv(
                cases_path=cases_path,
                report_path=report_path,
                safe_path=safe_path,
            )
        )
        == 2
    )
    stdout = capsys.readouterr().out
    assert stdout == "pilot50=SHOW-REVIEW reason=validation_failed\n"
    assert "PRIVATE-TAMPERED" not in stdout


@pytest.mark.parametrize(
    "response",
    [None, 123, "x" * (pilot50.MAX_REVIEW_TEXT_LENGTH + 1)],
    ids=["null", "integer", "oversized"],
)
def test_show_review_rejects_untyped_or_oversized_responses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    response: Any,
) -> None:
    cases, cases_path, cases_sha = _materialized_workspace(tmp_path)
    report_path, report, trace_rows = _write_report(tmp_path, cases, cases_sha)
    safe_path, safe = _write_validated_safe_result(
        tmp_path,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
    )
    report["results"][0]["response"] = response
    _rewrite_report_and_rebind_safe_result(report_path, report, safe_path, safe)

    assert (
        pilot50.main(
            _show_review_argv(
                cases_path=cases_path,
                report_path=report_path,
                safe_path=safe_path,
            )
        )
        == 2
    )
    assert capsys.readouterr().out == "pilot50=SHOW-REVIEW reason=validation_failed\n"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_result",
        "trace_incomplete",
        "cache_hit",
        "untyped_verdict",
        "missing_response",
    ],
)
def test_show_review_rejects_report_that_was_not_fully_validated(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    cases, cases_path, cases_sha = _materialized_workspace(tmp_path)
    report_path, report, trace_rows = _write_report(tmp_path, cases, cases_sha)
    safe_path, safe = _write_validated_safe_result(
        tmp_path,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
    )
    if mutation == "missing_result":
        report["results"].pop()
    elif mutation == "trace_incomplete":
        report["results"][0]["trace_found"] = False
    elif mutation == "cache_hit":
        report["results"][0]["cache_hit"] = True
    elif mutation == "untyped_verdict":
        report["results"][0]["passed"] = 1
    else:
        report["results"][0].pop("response")
    _rewrite_report_and_rebind_safe_result(report_path, report, safe_path, safe)

    assert (
        pilot50.main(
            _show_review_argv(
                cases_path=cases_path,
                report_path=report_path,
                safe_path=safe_path,
            )
        )
        == 2
    )
    assert capsys.readouterr().out == "pilot50=SHOW-REVIEW reason=validation_failed\n"


@pytest.mark.parametrize(
    "mutation",
    ["cases_hash", "report_hash", "runtime_sha", "safe_schema", "expected_runtime"],
)
def test_show_review_rejects_unbound_or_tampered_safe_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    cases, cases_path, cases_sha = _materialized_workspace(tmp_path)
    report_path, _report, trace_rows = _write_report(tmp_path, cases, cases_sha)
    safe_path, safe = _write_validated_safe_result(
        tmp_path,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=trace_rows,
    )
    expected_runtime = RUNTIME_GIT_SHA
    if mutation == "cases_hash":
        safe["cases_sha256"] = "b" * 64
    elif mutation == "report_hash":
        safe["report_sha256"] = "c" * 64
    elif mutation == "runtime_sha":
        safe["runtime_git_sha"] = "b" * 40
    elif mutation == "safe_schema":
        safe["human_product_verdict"] = True
    else:
        expected_runtime = "b" * 40
    _write_json(safe_path, safe)

    assert (
        pilot50.main(
            _show_review_argv(
                cases_path=cases_path,
                report_path=report_path,
                safe_path=safe_path,
                expected_runtime_git_sha=expected_runtime,
            )
        )
        == 2
    )
    assert capsys.readouterr().out == "pilot50=SHOW-REVIEW reason=validation_failed\n"


def test_show_safe_validates_and_prints_only_the_safe_schema(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases, cases_path, cases_sha = _materialized_workspace(tmp_path)
    report_path, _report, rows = _write_report(tmp_path, cases, cases_sha)
    safe = pilot50.build_safe_result(
        manifest_path=MANIFEST_PATH,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=rows,
        expected_runtime_git_sha=RUNTIME_GIT_SHA,
        expected_approval_id=APPROVAL_ID,
    )
    safe_path = tmp_path / "safe.json"
    _write_json(safe_path, safe)

    assert pilot50.main(["show-safe", "--input", str(safe_path)]) == 0
    assert json.loads(capsys.readouterr().out) == safe


@pytest.mark.parametrize(
    "mutation",
    [
        lambda safe: safe.__setitem__("query", "CANARY-PRIVATE-QUERY"),
        lambda safe: safe["mechanical_first_turn_closure"]["overall"].__setitem__(
            "rate", 1.0
        ),
        lambda safe: safe.__setitem__("human_product_verdict", True),
        lambda safe: safe.__setitem__("cases_sha256", "not-a-hash"),
        lambda safe: safe["mechanical_first_turn_closure"]["overall"].update(
            {"closed": 47, "rate": 0.94}
        ),
        lambda safe: safe["policy_pass"]["typical"].update(
            {"passed": 23, "rate": 0.92}
        ),
        lambda safe: safe["latency_ms"].update({"p50": 1_000_000}),
        lambda safe: safe.__setitem__("llm_cost_rub", 20.01),
        lambda safe: safe.__setitem__("billing_status", "reconciled_without_owner"),
        lambda safe: safe["run_window_utc"].update(
            {"completed_at": "2026-08-10T11:59:59+00:00"}
        ),
        lambda safe: safe.__setitem__("approval_id", "contains a space"),
    ],
)
def test_show_safe_rejects_tampered_or_expanded_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: Any,
) -> None:
    cases, cases_path, cases_sha = _materialized_workspace(tmp_path)
    report_path, _report, rows = _write_report(tmp_path, cases, cases_sha)
    safe = pilot50.build_safe_result(
        manifest_path=MANIFEST_PATH,
        cases_path=cases_path,
        report_path=report_path,
        trace_rows=rows,
        expected_runtime_git_sha=RUNTIME_GIT_SHA,
        expected_approval_id=APPROVAL_ID,
    )
    mutation(safe)
    safe_path = tmp_path / "safe.json"
    _write_json(safe_path, safe)

    assert pilot50.main(["show-safe", "--input", str(safe_path)]) == 2
    stdout = capsys.readouterr().out
    assert stdout == "pilot50=SHOW-SAFE reason=validation_failed\n"
    assert "CANARY" not in stdout


def test_diagnose_prints_one_bounded_payload_free_failure_matrix(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "PRIVATE-DIAGNOSTIC-CANARY"
    cases_path, report_path, safe_path, hashes, report, safe = (
        _write_candidate_diagnostics_fixture(tmp_path, failed_rows=8, canary=canary)
    )
    assert safe["quality_gate"]["status"] == "STOP"

    assert (
        pilot50.main(
            _diagnose_argv(
                cases_path=cases_path,
                report_path=report_path,
                safe_path=safe_path,
                hashes=hashes,
            )
        )
        == 0
    )

    stdout = capsys.readouterr().out
    assert stdout.count("\n") == 1
    assert len(stdout.encode("utf-8")) <= pilot50.MAX_DIAGNOSTICS_BYTES
    diagnostics = json.loads(stdout)
    assert stdout == pilot50._compact_canonical_json(diagnostics) + "\n"
    assert set(diagnostics) == {"schema_version", "bindings", "failure_matrix"}
    assert diagnostics["schema_version"] == pilot50.DIAGNOSTIC_SCHEMA_VERSION
    assert diagnostics["bindings"] == {
        "manifest_sha256": hashes["manifest"],
        "cases_sha256": hashes["cases"],
        "report_sha256": hashes["report"],
        "safe_result_sha256": hashes["safe"],
        "quality_status": "STOP",
    }
    matrix = diagnostics["failure_matrix"]
    assert len(matrix) == 50
    assert [row["ordinal"] for row in matrix] == list(range(1, 51))
    assert [row["group"] for row in matrix] == ["typical"] * 25 + [
        "atypical"
    ] * 25
    assert all(set(row) == pilot50.DIAGNOSTIC_ROW_FIELDS for row in matrix)
    assert all(
        row["failed_boolean_checks"] == ["behavior_match", "escalation_match"]
        for row in matrix[:8]
    )
    assert all(row["passed"] is False for row in matrix[:8])
    assert all(row["failed_boolean_checks"] == [] for row in matrix[8:])
    assert all(row["passed"] is True for row in matrix[8:])
    assert matrix[0]["escalation_reason"] == "other"
    assert all(
        row["escalation_reason"] == "llm_response_contract_failed"
        for row in matrix[1:8]
    )
    assert [row["generator_path"] for row in matrix[:8]] == [
        "source_chunk",
        "source_chunk",
        "simple",
        "complex",
        "unknown",
        "not_run",
        "not_run",
        "unknown",
    ]
    assert [row["latency_bucket"] for row in matrix[:8]] == [
        "<5s",
        "5-15s",
        "5-15s",
        "15-30s",
        "15-30s",
        ">=30s",
        "5-15s",
        "5-15s",
    ]
    assert matrix[0]["generate_retry_reasons"] == [
        "llm_response_contract_failed"
    ]
    assert all(row["generate_retry_reasons"] == [] for row in matrix[1:])

    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    forbidden_values = {
        canary,
        f"{canary}-response",
        f"{canary}-failure",
        "private_escalation_reason_canary",
        str(cases[0]["id"]),
        str(cases[0]["query"]),
        str(report["results"][0]["request_id"]),
        EVAL_RUN_ID,
        RUNTIME_GIT_SHA,
        APPROVAL_ID,
    }
    assert all(value not in stdout for value in forbidden_values)
    for forbidden_key in (
        '"query"',
        '"response"',
        '"id"',
        '"request_id"',
        '"eval_run_id"',
        '"trace"',
        '"chunk"',
        '"timestamp"',
        '"cost"',
        '"failure_reasons"',
        '"private_boolean_canary"',
    ):
        assert forbidden_key not in stdout


def test_diagnose_rejected_v3_prints_only_directional_payload_free_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "PRIVATE-REJECTED-V3-CANARY"
    cases_path, report_path, hashes, report = (
        _write_v3_rejected_diagnostics_fixture(tmp_path, canary=canary)
    )

    assert (
        pilot50.main(
            _diagnose_rejected_v3_argv(
                cases_path=cases_path,
                report_path=report_path,
                hashes=hashes,
            )
        )
        == 0
    )
    stdout = capsys.readouterr().out
    assert stdout.count("\n") == 1
    assert len(stdout.encode("utf-8")) <= pilot50.MAX_DIAGNOSTICS_BYTES
    diagnostics = json.loads(stdout)
    assert stdout == pilot50._compact_canonical_json(diagnostics) + "\n"
    assert set(diagnostics) == pilot50.REJECTED_V3_DIAGNOSTIC_FIELDS
    assert (
        diagnostics["schema_version"]
        == pilot50.REJECTED_V3_DIAGNOSTIC_SCHEMA_VERSION
    )
    assert diagnostics["bindings"] == {
        "manifest_sha256": hashes["manifest"],
        "cases_sha256": hashes["cases"],
        "report_sha256": hashes["report"],
        "runtime_git_sha": RUNTIME_GIT_SHA,
    }
    assert diagnostics["integrity"] == {
        "status": "integrity_rejected",
        "failures": ["trace_error_present"],
        "executed_cases_total": 50,
        "canonical_quality_gate_eligible": False,
        "selective_reruns_forbidden": True,
    }
    directional = diagnostics["directional_quality"]
    assert directional["classification"] == (
        "directional_calibration_only_integrity_rejected"
    )
    assert directional["human_product_verdict"] is False
    assert directional["mechanical_first_turn_closure"]["overall"] == {
        "closed": 49,
        "total": 50,
        "rate": 0.98,
    }
    assert directional["trace_coverage"] == {"found": 50, "total": 50, "rate": 1.0}
    matrix = diagnostics["failure_matrix"]
    assert len(matrix) == 50
    assert [row["ordinal"] for row in matrix] == list(range(1, 51))
    assert all(
        set(row) == pilot50.REJECTED_V3_DIAGNOSTIC_ROW_FIELDS for row in matrix
    )
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    for index, (case, row) in enumerate(zip(cases, matrix, strict=True)):
        assert row["answer_anchor_matches"] == [
            True for _ in case["expected_answer_contains"]
        ]
        assert row["pipeline_qrel_matches"] == [
            True for _ in case["expected_chunk_ids"]
        ]
        assert row["retrieved_qrel_matches"] == [
            True for _ in case["expected_chunk_ids"]
        ]
        assert row["reranked_qrel_matches"] == [
            True for _ in case["expected_chunk_ids"]
        ]
        assert row["selected_qrel_matches"] == [
            True for _ in case["expected_chunk_ids"]
        ]
        assert row["citation_qrel_matches"] == [
            True for _ in case["expected_cited_chunk_ids"]
        ]
        assert row["pipeline_observed_source_count"] == len(
            case["expected_chunk_ids"]
        ) + (1 if index == 0 else 0)
        assert row["retrieved_source_count"] == len(case["expected_chunk_ids"])
        assert row["reranked_source_count"] == len(case["expected_chunk_ids"])
        assert row["selected_source_count"] == len(case["expected_chunk_ids"])
        assert row["cited_source_count"] == len(case["expected_cited_chunk_ids"])
        assert row["lineage_stage_available"] == {
            "citation": True,
            "rerank": True,
            "retrieve": True,
            "source_selection": True,
            "verify": True,
        }
    timeout = matrix[19]
    assert timeout["failure_stage"] == "execution"
    assert timeout["execution_issue"] == "request_timeout"
    assert timeout["latency_bucket"] == ">=30s"
    assert timeout["escalation_reason"] == "request_timeout"
    assert all(
        row["execution_issue"] == "none"
        for index, row in enumerate(matrix)
        if index != 19
    )

    forbidden_values = {
        canary,
        str(cases[19]["id"]),
        str(cases[19]["query"]),
        str(report["results"][19]["request_id"]),
        EVAL_RUN_ID,
        "private-rejected-report-name",
    }
    assert all(value not in stdout for value in forbidden_values)
    for forbidden_key in (
        '"query"',
        '"response"',
        '"id"',
        '"request_id"',
        '"eval_run_id"',
        '"trace_metadata"',
        '"failure_reasons"',
        '"error"',
    ):
        assert forbidden_key not in stdout


def test_diagnose_rejected_v3_emits_ordered_boolean_fact_and_qrel_slots(
    tmp_path: Path,
) -> None:
    cases_path, report_path, hashes, report = (
        _write_v3_rejected_diagnostics_fixture(tmp_path)
    )
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    case = cases[25]
    result = report["results"][25]
    result.update(
        {
            "response": str(case["expected_answer_contains"][0]),
            "observed_chunk_ids": [case["expected_chunk_ids"][0]],
            "retrieved_chunk_ids": [case["expected_chunk_ids"][0]],
            "reranked_chunk_ids": [],
            "selected_source_ids": [],
            "cited_source_ids": [case["expected_cited_chunk_ids"][0]],
            "ordered_cited_source_ids": [case["expected_cited_chunk_ids"][0]],
            "lineage_stage_available": {
                "retrieve": True,
                "rerank": True,
                "source_selection": True,
                "citation": True,
                "verify": True,
            },
            "expected_chunk_hit": False,
            "expected_or_equivalent_chunk_hit": False,
            "expected_cited_chunk_hit": False,
            "expected_cited_or_equivalent_chunk_hit": False,
            "answer_contains_match": False,
            "passed": False,
        }
    )
    _write_json(report_path, report)
    hashes["report"] = hashlib.sha256(report_path.read_bytes()).hexdigest()

    diagnostics = pilot50.build_rejected_v3_diagnostics(
        manifest_path=V3_MANIFEST_PATH,
        cases_path=cases_path,
        report_path=report_path,
        expected_manifest_sha256=hashes["manifest"],
        expected_cases_sha256=hashes["cases"],
        expected_report_sha256=hashes["report"],
        expected_runtime_git_sha=RUNTIME_GIT_SHA,
    )

    row = diagnostics["failure_matrix"][25]
    assert row["answer_anchor_matches"] == [True, False, False]
    assert row["pipeline_qrel_matches"] == [True, False]
    assert row["retrieved_qrel_matches"] == [True, False]
    assert row["reranked_qrel_matches"] == [False, False]
    assert row["selected_qrel_matches"] == [False, False]
    assert row["citation_qrel_matches"] == [True, False]
    assert row["pipeline_observed_source_count"] == 1
    assert row["retrieved_source_count"] == 1
    assert row["reranked_source_count"] == 0
    assert row["selected_source_count"] == 0
    assert row["cited_source_count"] == 1
    assert row["failure_stage"] == "retrieval"


def test_diagnose_rejected_v3_rejects_wrong_report_binding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases_path, report_path, hashes, _report = (
        _write_v3_rejected_diagnostics_fixture(tmp_path)
    )
    argv = _diagnose_rejected_v3_argv(
        cases_path=cases_path,
        report_path=report_path,
        hashes=hashes,
    )
    argv[argv.index("--expected-report-sha256") + 1] = "b" * 64

    assert pilot50.main(argv) == 2
    assert capsys.readouterr().out == (
        "pilot50=DIAGNOSE-REJECTED-V3 reason=validation_failed\n"
    )


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--expected-manifest-sha256", "a" * 64),
        ("--expected-cases-sha256", "c" * 64),
        ("--expected-runtime-git-sha", "d" * 40),
    ],
)
def test_diagnose_rejected_v3_rejects_wrong_manifest_cases_or_runtime_binding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
) -> None:
    cases_path, report_path, hashes, _report = (
        _write_v3_rejected_diagnostics_fixture(tmp_path)
    )
    argv = _diagnose_rejected_v3_argv(
        cases_path=cases_path,
        report_path=report_path,
        hashes=hashes,
    )
    argv[argv.index(flag) + 1] = value

    assert pilot50.main(argv) == 2
    assert capsys.readouterr().out == (
        "pilot50=DIAGNOSE-REJECTED-V3 reason=validation_failed\n"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "error_mismatch",
        "unknown_error",
        "second_error",
        "timeout_row_moved",
        "row_order_mismatch",
    ],
)
def test_diagnose_rejected_v3_rejects_non_exact_timeout_error_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    cases_path, report_path, hashes, report = (
        _write_v3_rejected_diagnostics_fixture(tmp_path)
    )
    if mutation == "error_mismatch":
        report["results"][19]["trace_error"] = "different_error"
    elif mutation == "unknown_error":
        report["results"][19]["error"] = "unknown_execution_error"
        report["results"][19]["trace_error"] = "unknown_execution_error"
    elif mutation == "second_error":
        report["results"][20]["error"] = "request_timeout"
        report["results"][20]["trace_error"] = "request_timeout"
    elif mutation == "timeout_row_moved":
        report["results"][19].update(
            {
                "passed": True,
                "observed_behavior": "answer",
                "was_escalated": False,
                "escalation_reason": None,
                "error": None,
                "trace_error": None,
                "behavior_match": True,
                "escalation_match": True,
            }
        )
        report["results"][20].update(
            {
                "passed": False,
                "observed_behavior": "escalate",
                "was_escalated": True,
                "escalation_reason": "request_timeout",
                "error": "request_timeout",
                "trace_error": "request_timeout",
                "trace_total_latency_ms": 45_012,
                "behavior_match": False,
                "escalation_match": False,
            }
        )
    else:
        report["results"][0], report["results"][1] = (
            report["results"][1],
            report["results"][0],
        )
    _write_json(report_path, report)
    hashes["report"] = hashlib.sha256(report_path.read_bytes()).hexdigest()

    assert (
        pilot50.main(
            _diagnose_rejected_v3_argv(
                cases_path=cases_path,
                report_path=report_path,
                hashes=hashes,
            )
        )
        == 2
    )
    assert capsys.readouterr().out == (
        "pilot50=DIAGNOSE-REJECTED-V3 reason=validation_failed\n"
    )


def test_diagnose_rejected_v3_rejects_qrel_slot_aggregate_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases_path, report_path, hashes, report = (
        _write_v3_rejected_diagnostics_fixture(tmp_path)
    )
    result = report["results"][25]
    result["observed_chunk_ids"] = []
    result["cited_source_ids"] = []
    assert result["expected_chunk_hit"] is True
    assert result["expected_cited_chunk_hit"] is True
    _write_json(report_path, report)
    hashes["report"] = hashlib.sha256(report_path.read_bytes()).hexdigest()

    assert (
        pilot50.main(
            _diagnose_rejected_v3_argv(
                cases_path=cases_path,
                report_path=report_path,
                hashes=hashes,
            )
        )
        == 2
    )
    assert capsys.readouterr().out == (
        "pilot50=DIAGNOSE-REJECTED-V3 reason=validation_failed\n"
    )


@pytest.mark.parametrize(
    ("binding", "flag"),
    [
        ("manifest", "--expected-manifest-sha256"),
        ("cases", "--expected-cases-sha256"),
        ("report", "--expected-report-sha256"),
        ("safe", "--expected-safe-result-sha256"),
    ],
)
def test_diagnose_rejects_each_incorrect_exact_artifact_binding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    binding: str,
    flag: str,
) -> None:
    cases_path, report_path, safe_path, hashes, _report, _safe = (
        _write_candidate_diagnostics_fixture(tmp_path)
    )
    argv = _diagnose_argv(
        cases_path=cases_path,
        report_path=report_path,
        safe_path=safe_path,
        hashes=hashes,
    )
    assert argv[argv.index(flag) + 1] == hashes[binding]
    argv[argv.index(flag) + 1] = "b" * 64

    assert pilot50.main(argv) == 2
    assert capsys.readouterr().out == "pilot50=DIAGNOSE reason=validation_failed\n"


def test_diagnose_rejects_incorrect_exact_runtime_binding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases_path, report_path, safe_path, hashes, _report, _safe = (
        _write_candidate_diagnostics_fixture(tmp_path)
    )

    assert (
        pilot50.main(
            _diagnose_argv(
                cases_path=cases_path,
                report_path=report_path,
                safe_path=safe_path,
                hashes=hashes,
                expected_runtime_git_sha="b" * 40,
            )
        )
        == 2
    )
    assert capsys.readouterr().out == "pilot50=DIAGNOSE reason=validation_failed\n"


def test_diagnose_requires_quality_stop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases_path, report_path, safe_path, hashes, _report, safe = (
        _write_candidate_diagnostics_fixture(tmp_path, failed_rows=0)
    )
    assert safe["quality_gate"]["status"] == "GO"

    assert (
        pilot50.main(
            _diagnose_argv(
                cases_path=cases_path,
                report_path=report_path,
                safe_path=safe_path,
                hashes=hashes,
            )
        )
        == 2
    )
    assert capsys.readouterr().out == "pilot50=DIAGNOSE reason=validation_failed\n"


@pytest.mark.parametrize(
    "mutation",
    [
        "untyped_check",
        "verdict_mismatch",
        "unknown_retry_reason",
        "duplicate_retry_reason",
        "untyped_latency",
        "untyped_generator",
    ],
)
def test_diagnose_rejects_tampered_boolean_failure_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    cases_path, report_path, safe_path, hashes, report, safe = (
        _write_candidate_diagnostics_fixture(tmp_path)
    )
    if mutation == "untyped_check":
        report["results"][0]["behavior_match"] = "false"
    elif mutation == "verdict_mismatch":
        report["results"][0]["behavior_match"] = True
        report["results"][0]["escalation_match"] = True
    elif mutation == "unknown_retry_reason":
        report["results"][0]["generate_retry_reasons"] = ["PRIVATE-RETRY-CANARY"]
    elif mutation == "duplicate_retry_reason":
        report["results"][0]["generate_retry_reasons"] = [
            "llm_response_contract_failed",
            "llm_response_contract_failed",
        ]
    elif mutation == "untyped_latency":
        report["results"][0]["trace_total_latency_ms"] = "40000"
    else:
        report["results"][0]["generator_model"] = {"private": "model"}
    _rewrite_report_and_rebind_safe_result(report_path, report, safe_path, safe)
    hashes["report"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    hashes["safe"] = hashlib.sha256(safe_path.read_bytes()).hexdigest()

    assert (
        pilot50.main(
            _diagnose_argv(
                cases_path=cases_path,
                report_path=report_path,
                safe_path=safe_path,
                hashes=hashes,
            )
        )
        == 2
    )
    stdout = capsys.readouterr().out
    assert stdout == "pilot50=DIAGNOSE reason=validation_failed\n"
    assert "PRIVATE" not in stdout


def test_diagnose_rejects_duplicate_json_keys_without_echoing_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "PRIVATE-DUPLICATE-CANARY"
    cases_path, report_path, safe_path, hashes, _report, _safe = (
        _write_candidate_diagnostics_fixture(tmp_path)
    )
    safe_text = safe_path.read_text(encoding="utf-8")
    safe_path.write_text(
        safe_text.replace("{", f'{{"schema_version":"{canary}",', 1),
        encoding="utf-8",
    )
    hashes["safe"] = hashlib.sha256(safe_path.read_bytes()).hexdigest()

    assert (
        pilot50.main(
            _diagnose_argv(
                cases_path=cases_path,
                report_path=report_path,
                safe_path=safe_path,
                hashes=hashes,
            )
        )
        == 2
    )
    stdout = capsys.readouterr().out
    assert stdout == "pilot50=DIAGNOSE reason=validation_failed\n"
    assert canary not in stdout


def test_diagnose_enforces_input_and_output_size_limits(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases_path, report_path, safe_path, hashes, _report, _safe = (
        _write_candidate_diagnostics_fixture(tmp_path)
    )
    monkeypatch.setattr(pilot50, "MAX_DIAGNOSTICS_BYTES", 1)
    argv = _diagnose_argv(
        cases_path=cases_path,
        report_path=report_path,
        safe_path=safe_path,
        hashes=hashes,
    )
    assert pilot50.main(argv) == 2
    assert capsys.readouterr().out == "pilot50=DIAGNOSE reason=validation_failed\n"

    monkeypatch.setattr(pilot50, "MAX_DIAGNOSTICS_BYTES", 64 * 1024)
    oversized_canary = "PRIVATE-OVERSIZED-CANARY"
    safe_path.write_bytes(
        (oversized_canary + "x" * pilot50.MAX_SAFE_BYTES).encode("utf-8")
    )
    hashes["safe"] = hashlib.sha256(safe_path.read_bytes()).hexdigest()
    assert pilot50.main(argv) == 2
    stdout = capsys.readouterr().out
    assert stdout == "pilot50=DIAGNOSE reason=validation_failed\n"
    assert oversized_canary not in stdout
