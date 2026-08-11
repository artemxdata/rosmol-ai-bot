from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = ROOT / "reports" / "pilot50_balanced_v1_baseline_20260811.json"

EXPECTED_TOP_LEVEL_KEYS = {
    "schema_version",
    "classification",
    "dataset",
    "provenance",
    "run_window_utc",
    "mechanical_first_turn_closure",
    "failure_breakdown",
    "escalation_reasons",
    "escalation_buckets",
    "runtime_metrics",
    "interpretation",
    "evaluation_contract_audit",
    "next_candidate_plan",
}
EXPECTED_REASON_COUNTS = {
    "final_response_too_long": 1,
    "insufficient_sources": 3,
    "llm_response_contract_failed": 3,
    "llm_response_profile_failed": 1,
    "llm_response_too_long": 2,
    "llm_source_fact_binding_failed": 5,
    "low_confidence": 2,
    "no_relevant_chunks": 1,
    "source_response_contract_failed": 6,
}
EXPECTED_PROVENANCE = {
    "runtime_git_sha": "c38f0e055630fae2af50720fae81acee20ff4f6a",
    "cases_sha256": "65da11ebc790b37e0b8e5dff2601f6cc2cd3956d17652f7d74ab95eb1c21c6ed",
    "report_sha256": "b3f771036f34299f59bbe3f060b4fa93d7d3653f4a6a1cddc8f2c168216c74a4",
    "safe_result_sha256": "0950cc14c4e951857809592adf736f0f73b23af33a889ed1310d1bab536c093b",
    "rate_card_sha256": "3aebb12db82391bad23ec9256781e3439f2692ad63814070e4341bd28ea27bd6",
}
FORBIDDEN_KEYS = {
    "approval_id",
    "case_id",
    "case_ids",
    "eval_run_id",
    "query",
    "queries",
    "request_id",
    "request_ids",
    "response",
    "responses",
    "raw",
    "raw_report",
    "results_by_case",
    "trace_rows",
    "user",
    "user_id",
}
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")


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


def test_pilot50_baseline_artifact_schema_provenance_and_privacy() -> None:
    artifact = _load_artifact()

    assert set(artifact) == EXPECTED_TOP_LEVEL_KEYS
    assert artifact["schema_version"] == "pilot50-baseline-artifact-v1"
    assert artifact["classification"] == "calibration_only"
    assert set(artifact["dataset"]) == {"id", "composition"}
    assert artifact["dataset"]["id"] == "pilot50_balanced_v1"

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

    run_window = artifact["run_window_utc"]
    assert run_window == {
        "started_at": "2026-08-11T02:21:50.194758+00:00",
        "completed_at": "2026-08-11T02:24:43.589106+00:00",
    }
    started_at = datetime.fromisoformat(run_window["started_at"])
    completed_at = datetime.fromisoformat(run_window["completed_at"])
    assert started_at.utcoffset() is not None
    assert started_at.utcoffset().total_seconds() == 0
    assert completed_at.utcoffset() is not None
    assert completed_at.utcoffset().total_seconds() == 0
    assert started_at < completed_at

    assert not (_walk_keys(artifact) & FORBIDDEN_KEYS)
    serialized = ARTIFACT_PATH.read_text(encoding="utf-8").lower()
    assert "ask-eval-" not in serialized
    assert "owner-chat-" not in serialized


def test_pilot50_baseline_artifact_metric_sums_and_exact_values() -> None:
    artifact = _load_artifact()
    composition = artifact["dataset"]["composition"]
    assert composition == {"typical": 25, "atypical": 25, "total": 50}
    assert composition["typical"] + composition["atypical"] == composition["total"]

    closure = artifact["mechanical_first_turn_closure"]
    assert closure == {
        "typical": {"closed": 11, "total": 25, "rate": 0.44},
        "atypical": {"closed": 7, "total": 25, "rate": 0.28},
        "overall": {"closed": 18, "total": 50, "rate": 0.36},
    }
    for group in ("typical", "atypical", "overall"):
        assert math.isclose(
            closure[group]["rate"],
            closure[group]["closed"] / closure[group]["total"],
        )
    assert closure["overall"]["closed"] == (
        closure["typical"]["closed"] + closure["atypical"]["closed"]
    )

    failures = artifact["failure_breakdown"]
    assert failures == {
        "failed_total": 32,
        "escalated": 24,
        "failed_non_escalated": 8,
    }
    assert failures["failed_total"] == composition["total"] - closure["overall"]["closed"]
    assert failures["failed_total"] == failures["escalated"] + failures["failed_non_escalated"]

    reasons = artifact["escalation_reasons"]
    assert reasons == EXPECTED_REASON_COUNTS
    assert sum(reasons.values()) == failures["escalated"]

    assert set(artifact["escalation_buckets"]) == {
        "output_contract",
        "retrieval_or_source_confidence",
    }
    bucket_reasons: set[str] = set()
    for bucket in artifact["escalation_buckets"].values():
        assert set(bucket) == {"count", "reasons"}
        assert bucket["count"] == sum(reasons[reason] for reason in bucket["reasons"])
        assert not (bucket_reasons & set(bucket["reasons"]))
        bucket_reasons.update(bucket["reasons"])
    assert bucket_reasons == set(reasons)
    assert artifact["escalation_buckets"]["output_contract"]["count"] == 18
    assert artifact["escalation_buckets"]["retrieval_or_source_confidence"]["count"] == 6

    metrics = artifact["runtime_metrics"]
    assert set(metrics) == {
        "latency_ms",
        "llm_cost_rub",
        "trace_coverage",
        "cache_hits",
        "cache_hit_rate",
        "billing_status",
        "pricing",
    }
    assert metrics["latency_ms"] == {"p50": 1997, "p95": 14235}
    assert metrics["llm_cost_rub"] == 11.647398
    assert metrics["trace_coverage"] == {"found": 50, "total": 50, "rate": 1.0}
    assert metrics["cache_hits"] == 0
    assert metrics["cache_hit_rate"] == 0.0
    assert metrics["billing_status"] == "pending_provider_reconciliation"
    assert metrics["pricing"] == {
        "source": "eval_repriced",
        "complete": True,
        "target_telemetry_preserved": True,
        "target_telemetry_pricing_complete": False,
    }


def test_pilot50_baseline_artifact_next_candidate_acceptance_contract() -> None:
    artifact = _load_artifact()
    plan = artifact["next_candidate_plan"]
    assert set(plan) == {
        "hypothesis",
        "changes",
        "verification_sequence",
        "paid_calibration_gate",
        "acceptance_criteria",
        "constraints",
    }
    assert len(plan["changes"]) == 4
    assert len(plan["verification_sequence"]) == 4
    assert len(plan["constraints"]) == 6
    assert plan["paid_calibration_gate"] == {
        "required_dataset_id": "pilot50_balanced_v2",
        "published_yonote_qrels_required": True,
        "new_runtime_and_tooling_sha_required": True,
        "one_time_owner_approval_required": True,
        "runner_projected_stop_limit_rub_maximum": 30,
        "single_server_local_run": True,
    }
    assert plan["acceptance_criteria"] == {
        "mechanical_first_turn_closure": {
            "minimum_closed": 30,
            "denominator": 50,
        },
        "slice_floor": {
            "typical": {"minimum_closed": 11, "denominator": 25},
            "atypical": {"minimum_closed": 7, "denominator": 25},
        },
        "output_contract_escalations": {"maximum": 6},
        "source_binding_failures": {
            "maximum": 0,
            "applicable_qrel_cases": 38,
            "denominator": 50,
        },
        "critical_case_failures": {
            "maximum": 0,
            "applicable_critical_cases": 15,
            "denominator": 50,
            "definition_tags": ["adversarial", "off_aspect_guard"],
        },
    }
    assert artifact["interpretation"] == {
        "metric": "mechanical_first_turn_closure",
        "scope": "balanced_tracked_regression_calibration",
        "not_claimed": [
            "independent_holdout_quality",
            "human_product_verdict",
            "ticket_level_conversion",
            "production_traffic_conversion",
        ],
    }
    assert artifact["evaluation_contract_audit"] == {
        "factual_authority": "published_yonote_only",
        "mechanically_compatible_cases": 39,
        "legacy_qrel_incompatible_cases": 11,
        "affected_group": "atypical",
        "affected_source": "pre_pilot_forums",
        "implication": (
            "The observed v1 result remains historical calibration evidence, but v1 "
            "must not be reused as the candidate acceptance set because 11 answer-labelled "
            "cases cannot satisfy the published-Yonote scorer contract."
        ),
    }
