from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = ROOT / "reports" / "pilot50_balanced_v2_candidate_20260811.json"

EXPECTED_TOP_LEVEL_KEYS = {
    "schema_version",
    "classification",
    "execution_status",
    "quality_status",
    "dataset",
    "provenance",
    "mechanical_first_turn_closure",
    "policy_pass",
    "runtime_metrics",
    "cost_guard",
    "pricing",
    "quality_gate",
    "interpretation",
    "contextual_baseline_comparison",
}
EXPECTED_PROVENANCE = {
    "runtime_git_sha": "64cc182d37a3c060439ed7a55f5cc875a27d786d",
    "cases_sha256": "b027e469e062682b6dc341b2dd4c87440edffb1955c2111f38e6c44a92a3a14d",
    "report_sha256": "07fdfebf505e3df9b2461386e37f89a836dd80f3a5c445ec93bfca765e47add9",
    "safe_result_sha256": "4e5b0ebb4e04b9d449e7ed54db9a1167c19cce02ef27839073fba280e435b61d",
    "rate_card_sha256": "3aebb12db82391bad23ec9256781e3439f2692ad63814070e4341bd28ea27bd6",
}
FORBIDDEN_KEYS = {
    "approval_id",
    "case_id",
    "case_ids",
    "completed_at",
    "eval_run_id",
    "query",
    "queries",
    "raw",
    "raw_report",
    "request_id",
    "request_ids",
    "response",
    "responses",
    "results_by_case",
    "run_window_utc",
    "run_id",
    "started_at",
    "timestamp",
    "ticket_id",
    "trace_id",
    "trace_rows",
    "user",
    "user_id",
}
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}")
UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)


def _load_artifact() -> dict[str, Any]:
    return json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))


def _walk_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested_key
            for nested_value in value.values()
            for nested_key in _walk_keys(nested_value)
        }
    if isinstance(value, list):
        return {
            nested_key
            for nested_value in value
            for nested_key in _walk_keys(nested_value)
        }
    return set()


def test_v2_result_artifact_schema_provenance_and_privacy() -> None:
    artifact = _load_artifact()

    assert set(artifact) == EXPECTED_TOP_LEVEL_KEYS
    assert artifact["schema_version"] == "pilot50-v2-candidate-result-artifact-v1"
    assert artifact["classification"] == "calibration_only"
    assert artifact["execution_status"] == "OK"
    assert artifact["quality_status"] == "STOP"
    assert artifact["dataset"] == {
        "name": "pilot50_balanced_v2",
        "composition": {"typical": 25, "atypical": 25, "total": 50},
    }

    provenance = artifact["provenance"]
    assert provenance == EXPECTED_PROVENANCE
    assert HEX_40.fullmatch(provenance["runtime_git_sha"])
    for name in (
        "cases_sha256",
        "report_sha256",
        "safe_result_sha256",
        "rate_card_sha256",
    ):
        assert HEX_64.fullmatch(provenance[name]), name

    artifact_keys = _walk_keys(artifact)
    assert not (artifact_keys & FORBIDDEN_KEYS)
    assert not {
        key
        for key in artifact_keys
        if key == "id" or key.endswith(("_id", "_ids"))
    }
    serialized = ARTIFACT_PATH.read_text(encoding="utf-8").lower()
    assert "ask-eval-" not in serialized
    assert "owner-chat-" not in serialized
    assert "approval_id" not in serialized
    assert "eval_run_id" not in serialized
    assert not ISO_TIMESTAMP.search(serialized)
    assert not UUID.search(serialized)


def test_v2_result_artifact_metrics_are_exact_and_consistent() -> None:
    artifact = _load_artifact()
    composition = artifact["dataset"]["composition"]
    assert composition["typical"] + composition["atypical"] == composition["total"]

    closure = artifact["mechanical_first_turn_closure"]
    assert closure == {
        "typical": {"closed": 17, "total": 25, "rate": 0.68},
        "atypical": {"closed": 8, "total": 25, "rate": 0.32},
        "overall": {"closed": 25, "total": 50, "rate": 0.5},
    }
    for group in ("typical", "atypical", "overall"):
        assert math.isclose(
            closure[group]["rate"],
            closure[group]["closed"] / closure[group]["total"],
        )
    assert closure["overall"]["closed"] == (
        closure["typical"]["closed"] + closure["atypical"]["closed"]
    )

    policy_pass = artifact["policy_pass"]
    assert policy_pass == {
        "typical": {"passed": 17, "total": 25, "rate": 0.68},
        "atypical": {"passed": 8, "total": 25, "rate": 0.32},
        "overall": {"passed": 25, "total": 50, "rate": 0.5},
    }
    for group in ("typical", "atypical", "overall"):
        assert policy_pass[group]["passed"] == closure[group]["closed"]
        assert math.isclose(policy_pass[group]["rate"], closure[group]["rate"])

    assert artifact["runtime_metrics"] == {
        "latency_ms": {"p50": 2988, "p95": 40015},
        "llm_cost_rub": 13.375452,
        "trace_coverage": {"found": 50, "total": 50, "rate": 1.0},
        "cache_hits": 0,
        "billing_status": "pending_provider_reconciliation",
    }
    assert artifact["cost_guard"] == {
        "maximum_rub": 30,
        "exceeded": False,
        "stopped": False,
    }
    assert artifact["pricing"] == {
        "source": "target_reported",
        "complete": True,
        "stopped": False,
        "target_telemetry_preserved": True,
        "target_telemetry_pricing_complete": True,
    }


def test_v2_result_artifact_quality_gate_is_exact_and_self_consistent() -> None:
    artifact = _load_artifact()
    quality_gate = artifact["quality_gate"]

    assert quality_gate["schema_version"] == "pilot50-v2-quality-gate-v1"
    assert quality_gate["status"] == artifact["quality_status"] == "STOP"
    criteria = quality_gate["criteria"]
    assert criteria == {
        "overall_closed": {"actual": 25, "minimum": 30, "passed": False},
        "typical_closed": {"actual": 17, "minimum": 11, "passed": True},
        "atypical_closed": {"actual": 8, "minimum": 7, "passed": True},
        "output_contract_escalations": {
            "actual": 8,
            "maximum": 6,
            "passed": False,
        },
        "source_binding_failures": {
            "actual": 5,
            "maximum": 0,
            "applicable_cases": 38,
            "total_cases": 50,
            "passed": False,
        },
        "critical_case_failures": {
            "actual": 7,
            "maximum": 0,
            "applicable_cases": 15,
            "total_cases": 50,
            "passed": False,
        },
    }
    failed_criteria = {
        name for name, result in criteria.items() if result["passed"] is False
    }
    assert quality_gate["failed_criteria"] == [
        "overall_closed",
        "output_contract_escalations",
        "source_binding_failures",
        "critical_case_failures",
    ]
    assert set(quality_gate["failed_criteria"]) == failed_criteria
    assert criteria["overall_closed"]["actual"] == (
        artifact["mechanical_first_turn_closure"]["overall"]["closed"]
    )
    assert criteria["typical_closed"]["actual"] == (
        artifact["mechanical_first_turn_closure"]["typical"]["closed"]
    )
    assert criteria["atypical_closed"]["actual"] == (
        artifact["mechanical_first_turn_closure"]["atypical"]["closed"]
    )
    assert quality_gate["output_contract_reasons"] == [
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
    ]


def test_v2_result_artifact_baseline_comparison_is_context_only() -> None:
    artifact = _load_artifact()
    comparison = artifact["contextual_baseline_comparison"]
    baseline_closure = comparison["baseline"]["mechanical_first_turn_closure"]
    candidate_closure = artifact["mechanical_first_turn_closure"]

    assert comparison["baseline_artifact"] == (
        "reports/pilot50_balanced_v1_baseline_20260811.json"
    )
    assert baseline_closure == {
        "typical": {"closed": 11, "total": 25, "rate": 0.44},
        "atypical": {"closed": 7, "total": 25, "rate": 0.28},
        "overall": {"closed": 18, "total": 50, "rate": 0.36},
    }
    assert comparison["observed_delta"] == {
        "typical_closed": 6,
        "typical_rate": 0.24,
        "atypical_closed": 1,
        "atypical_rate": 0.04,
        "overall_closed": 7,
        "overall_rate": 0.14,
    }
    for group in ("typical", "atypical", "overall"):
        assert math.isclose(
            comparison["observed_delta"][f"{group}_rate"],
            candidate_closure[group]["rate"] - baseline_closure[group]["rate"],
        )
        assert comparison["observed_delta"][f"{group}_closed"] == (
            candidate_closure[group]["closed"] - baseline_closure[group]["closed"]
        )
    assert "same 25 tracked typical cases" in comparison["comparability"]["typical"]
    assert "not an apples-to-apples comparison" in comparison["comparability"]["atypical"].lower()
    assert "not an independent quality" in comparison["comparability"]["overall"]
    assert artifact["interpretation"] == {
        "disclaimer": (
            "Tracked regression calibration only. This is a mechanical first-turn closure "
            "result for the balanced Pilot50 set, not an independent holdout, a human "
            "product verdict, ticket-level conversion, or production traffic conversion."
        ),
        "human_product_verdict": False,
        "not_claimed": [
            "independent_holdout_quality",
            "human_product_verdict",
            "ticket_level_conversion",
            "production_traffic_conversion",
        ],
    }
