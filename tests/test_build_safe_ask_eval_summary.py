from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.build_safe_ask_eval_summary import (
    SAFE_SCHEMA,
    build_safe_ask_eval_summary,
    main,
)


def _raw_report(canary: str = "") -> dict[str, object]:
    return {
        "target": f"https://{canary}.invalid/ask",
        "cases_path": f"/private/{canary}/cases.json",
        "report_classification": {
            "calibration_only": True,
            "provisional": True,
            "independent_evaluation": False,
            "previously_exposed": True,
            "product_verdict_eligible": False,
            "human_product_verdict": False,
            "measurement_disclaimer": canary,
            "source_holdout_contract": {"secret": canary},
        },
        "results": [
            {
                "id": canary,
                "query": canary,
                "response": canary,
                "message_masked": canary,
                "request_id": canary,
                "cited_source_ids": [canary],
                "citations": [{"text": canary}],
                "trace_events": [{"metadata": {"secret": canary}}],
                "passed": False,
                "http_success": True,
                "trace_found": True,
                "expected_behavior": "answer",
                "observed_behavior": "escalate",
                "generate_retry_reasons": ["llm_source_fact_binding_failed"],
                "escalation_reason": "llm_response_profile_failed",
                "behavior_match": False,
                "routing_response_profile_match": True,
                "was_escalated": True,
                "escalation_match": False,
                "escalation_reason_match": None,
                "expected_chunk_hit": True,
                "expected_or_equivalent_chunk_hit": True,
                "expected_cited_chunk_hit": False,
                "expected_cited_or_equivalent_chunk_hit": False,
                "forbidden_response_profiles_absent": True,
                "cited_source_types_allowed": True,
                "answer_contains_match": None,
                "no_false_insufficient_source_response": True,
                "no_non_answer_response": True,
                "cache_hit": False,
            },
            {
                "id": f"second-{canary}",
                "query": canary,
                "response": canary,
                "message_masked": canary,
                "passed": True,
                "http_success": True,
                "trace_found": True,
                "expected_behavior": "clarify",
                "observed_behavior": "clarify",
                "generate_retry_reasons": [],
                "escalation_reason": "personal_status",
                "behavior_match": True,
                "routing_response_profile_match": None,
                "was_escalated": False,
                "escalation_match": True,
                "escalation_reason_match": None,
                "expected_chunk_hit": None,
                "expected_or_equivalent_chunk_hit": None,
                "expected_cited_chunk_hit": None,
                "expected_cited_or_equivalent_chunk_hit": None,
                "forbidden_response_profiles_absent": None,
                "cited_source_types_allowed": True,
                "answer_contains_match": None,
                "no_false_insufficient_source_response": None,
                "no_non_answer_response": None,
                "cache_hit": False,
            },
        ],
    }


def test_safe_summary_uses_allowlist_and_drops_canary_secrets(tmp_path: Path) -> None:
    canary = "CANARY-PRIVATE-QUERY-SECRET-9f91"
    raw_path = tmp_path / "raw.json"
    safe_path = tmp_path / "safe.json"
    raw_path.write_text(json.dumps(_raw_report(canary)), encoding="utf-8")

    summary = build_safe_ask_eval_summary(raw_path, safe_path)
    serialized = safe_path.read_text(encoding="utf-8")

    assert canary not in serialized
    assert summary["schema"] == SAFE_SCHEMA
    assert summary["evaluation_classification"] == "calibration_only"
    assert summary["cases_total"] == 2
    assert summary["cases_passed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["behavior_match_rate"] == 0.5
    assert summary["expected_chunk_hit_rate"] == 1.0
    assert summary["expected_cited_chunk_hit_rate"] == 0.0
    assert summary["escalation_rate"] == 0.5
    assert summary["behavior_confusion_matrix"] == {
        "answer": {"escalate": 1},
        "clarify": {"clarify": 1},
    }
    assert summary["generate_retry_reason_counts"] == {
        "llm_source_fact_binding_failed": 1
    }
    assert summary["generation_failure_reason_counts"] == {
        "llm_response_profile_failed": 1
    }
    assert summary["cases"][0] == {
        "case_no": 1,
        "passed": False,
        "http_success": True,
        "trace_found": True,
        "expected_behavior": "answer",
        "observed_behavior": "escalate",
        "generate_retry_reasons": ["llm_source_fact_binding_failed"],
        "generation_failure_reason": "llm_response_profile_failed",
        "behavior_match": False,
        "routing_match": True,
        "was_escalated": True,
        "escalation_match": False,
        "escalation_reason_match": None,
        "expected_chunk_hit": True,
        "expected_or_equivalent_chunk_hit": True,
        "expected_cited_chunk_hit": False,
        "expected_cited_or_equivalent_chunk_hit": False,
        "forbidden_profiles_absent": True,
        "cited_source_types_allowed": True,
        "answer_contains_match": None,
        "no_false_insufficient": True,
        "no_non_answer": True,
        "cache_hit": False,
    }
    assert not any(
        key in serialized
        for key in (
            '"query"',
            '"response"',
            '"message_masked"',
            '"id"',
            '"citations"',
            '"trace_events"',
            '"request_id"',
        )
    )


def test_safe_summary_rejects_direct_input_output_alias(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    original = json.dumps(_raw_report())
    report_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="different files"):
        build_safe_ask_eval_summary(report_path, report_path)

    assert report_path.read_text(encoding="utf-8") == original


def test_safe_summary_rejects_hardlink_alias(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw.json"
    alias_path = tmp_path / "alias.json"
    original = json.dumps(_raw_report())
    raw_path.write_text(original, encoding="utf-8")
    try:
        os.link(raw_path, alias_path)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="different files"):
        build_safe_ask_eval_summary(raw_path, alias_path)

    assert raw_path.read_text(encoding="utf-8") == original


def test_safe_summary_rejects_canary_in_allowlisted_enum(tmp_path: Path) -> None:
    canary = "CANARY-IN-ENUM"
    raw = _raw_report()
    results = raw["results"]
    assert isinstance(results, list)
    assert isinstance(results[0], dict)
    results[0]["observed_behavior"] = canary
    raw_path = tmp_path / "raw.json"
    safe_path = tmp_path / "safe.json"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="observed_behavior"):
        build_safe_ask_eval_summary(raw_path, safe_path)

    assert not safe_path.exists()


def test_safe_summary_rejects_unknown_generate_retry_reason(tmp_path: Path) -> None:
    raw = _raw_report()
    results = raw["results"]
    assert isinstance(results, list)
    assert isinstance(results[0], dict)
    results[0]["generate_retry_reasons"] = ["CANARY-PRIVATE-REASON"]
    raw_path = tmp_path / "raw.json"
    safe_path = tmp_path / "safe.json"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown diagnostic reason"):
        build_safe_ask_eval_summary(raw_path, safe_path)

    assert not safe_path.exists()


def test_safe_summary_cli_prints_only_output_path_and_digest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "CANARY-PRIVATE-CLI"
    raw_path = tmp_path / "raw.json"
    safe_path = tmp_path / "safe.json"
    raw_path.write_text(json.dumps(_raw_report(canary)), encoding="utf-8")

    assert main(["--input", str(raw_path), "--output", str(safe_path)]) == 0

    output = capsys.readouterr().out
    assert "SAFE_REPORT=" in output
    assert "SHA256=" in output
    assert canary not in output
