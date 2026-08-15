from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from eval.cost_governance import inspect_routine_cost_capacity

DATASET_ID = "semantic_recovery10_v1"
SCHEMA_VERSION = "semantic-recovery10-v1"
SAFE_RESULT_SCHEMA_VERSION = "semantic-recovery10-safe-result-v1"
PRIOR_RUNTIME_GIT_SHA = "d5cf413492a079c396c56017f51acaa3ebbacb3c"
PRIOR_CASES_SHA256 = "c88a52225f6eec3b21a5837a94f12670f5a8ff1006818f559cb81e438d52fab8"
PRIOR_REPORT_SHA256 = "2defcace63de2a2184b162fcae5fa8f4d50ed8317042ae64aabbb49181076a8d"
CASES_TOTAL = 10
CASES_PER_GROUP = 5
COST_CAP_RUB = 200.0
TARGET = "http://pilot50-candidate-ml:8000/ask"

RECOVERABLE_ESCALATION_REASONS = frozenset(
    {
        "insufficient_sources",
        "low_confidence",
        "no_relevant_chunks",
        "no_sources_for_generation",
    }
)
SOURCE_FAILURE_REASONS = frozenset(
    {
        "expected_chunk_not_cited",
        "expected_chunk_not_observed",
        "expected_chunk_not_retrieved",
        "expected_or_equivalent_chunk_not_cited",
        "expected_or_equivalent_chunk_not_observed",
        "expected_or_equivalent_chunk_not_retrieved",
        "false_insufficient_source_response",
        "non_answer_response",
        "unexpected_escalation",
    }
)
SAFE_FAILURE_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,95}")
FULL_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_exclusive(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical_json_bytes(value)
    with path.open("xb") as handle:
        handle.write(data)


def _validated_cost_cap(value: float) -> float:
    cost_cap = float(value)
    if not math.isfinite(cost_cap) or cost_cap <= 0 or cost_cap > COST_CAP_RUB:
        raise ValueError("Recovery10 cost cap must be within (0, 200] RUB")
    return cost_cap


def _group(case: dict[str, Any]) -> str:
    group = str(case.get("pilot50_group") or "").strip().casefold()
    if group in {"typical", "atypical"}:
        return group
    tags = {str(tag).strip().casefold() for tag in case.get("tags") or []}
    for candidate in ("typical", "atypical"):
        if f"type:{candidate}" in tags:
            return candidate
    raise ValueError("prior case does not have an exact Pilot50 group")


def _safe_failure_reasons(result: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for raw in result.get("failure_reasons") or []:
        reason = str(raw).strip().casefold()
        if SAFE_FAILURE_RE.fullmatch(reason) and reason not in reasons:
            reasons.append(reason)
    return reasons


def _selection_priority(result: dict[str, Any]) -> tuple[int, int]:
    escalation_reason = str(result.get("escalation_reason") or "").casefold()
    failures = set(_safe_failure_reasons(result))
    if escalation_reason in RECOVERABLE_ESCALATION_REASONS:
        return (0, 0)
    if failures & SOURCE_FAILURE_REASONS:
        return (1, -len(failures & SOURCE_FAILURE_REASONS))
    return (2, -len(failures))


def prepare(
    *,
    prior_cases_path: Path,
    prior_report_path: Path,
    output_cases_path: Path,
    output_manifest_path: Path,
    cost_cap_rub: float = COST_CAP_RUB,
) -> dict[str, Any]:
    cost_cap_rub = _validated_cost_cap(cost_cap_rub)
    if _file_sha256(prior_cases_path) != PRIOR_CASES_SHA256:
        raise ValueError("prior cases SHA-256 mismatch")
    if _file_sha256(prior_report_path) != PRIOR_REPORT_SHA256:
        raise ValueError("prior report SHA-256 mismatch")

    prior_cases = _read_json(prior_cases_path)
    prior_report = _read_json(prior_report_path)
    if not isinstance(prior_cases, list) or len(prior_cases) != 50:
        raise ValueError("prior cases must contain exactly 50 rows")
    if not isinstance(prior_report, dict):
        raise ValueError("prior report must be a JSON object")
    prior_results = prior_report.get("results")
    if not isinstance(prior_results, list) or len(prior_results) != 50:
        raise ValueError("prior report must contain exactly 50 results")
    if prior_report.get("cases_file_sha256") != PRIOR_CASES_SHA256:
        raise ValueError("prior report is not bound to the expected cases")

    cases_by_id = {str(case.get("id") or ""): case for case in prior_cases}
    results_by_id = {str(row.get("id") or ""): row for row in prior_results}
    if (
        len(cases_by_id) != 50
        or len(results_by_id) != 50
        or set(cases_by_id) != set(results_by_id)
        or "" in cases_by_id
    ):
        raise ValueError("prior case/result identity mismatch")

    selected_cases: list[dict[str, Any]] = []
    selected_baseline: list[dict[str, Any]] = []
    for group in ("typical", "atypical"):
        candidates: list[tuple[tuple[int, int], int, dict[str, Any], dict[str, Any]]] = []
        for ordinal, case in enumerate(prior_cases, start=1):
            if _group(case) != group:
                continue
            result = results_by_id[str(case["id"])]
            if result.get("passed") is not False:
                continue
            candidates.append((_selection_priority(result), ordinal, case, result))
        candidates.sort(key=lambda item: (item[0], item[1]))
        if len(candidates) < CASES_PER_GROUP:
            raise ValueError(f"not enough failed {group} cases for Recovery10")
        for _priority, ordinal, case, result in candidates[:CASES_PER_GROUP]:
            materialized = dict(case)
            tags = [str(tag) for tag in materialized.get("tags") or []]
            diagnostic_tag = f"diagnostic:{DATASET_ID}"
            if diagnostic_tag not in tags:
                tags.append(diagnostic_tag)
            materialized["tags"] = tags
            materialized["user_id"] = f"semantic-recovery10-{len(selected_cases) + 1:02d}"
            selected_cases.append(materialized)
            selected_baseline.append(
                {
                    "id": str(case["id"]),
                    "ordinal": ordinal,
                    "group": group,
                    "passed": False,
                    "was_escalated": result.get("was_escalated") is True,
                    "escalation_reason": (
                        str(result.get("escalation_reason") or "").casefold() or None
                    ),
                    "failure_reasons": _safe_failure_reasons(result),
                }
            )

    if len(selected_cases) != CASES_TOTAL:
        raise AssertionError("Recovery10 selection cardinality changed")
    cases_bytes = _canonical_json_bytes(selected_cases)
    cases_sha256 = _sha256_bytes(cases_bytes)
    selected_ids_sha256 = _sha256_bytes(
        "\n".join(str(case["id"]) for case in selected_cases).encode("utf-8")
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "classification": "exposed_targeted_regression_diagnostic",
        "human_product_verdict": False,
        "disclaimer": (
            "Ten deterministic failures selected from the exposed Pilot50 v4 run. "
            "This tests a bounded semantic-recovery hypothesis and is not an "
            "independent holdout or production ticket conversion estimate."
        ),
        "cases_total": CASES_TOTAL,
        "group_counts": {"typical": CASES_PER_GROUP, "atypical": CASES_PER_GROUP},
        "selection_rule": (
            "Within each Pilot50 group select failed rows by recoverable escalation, "
            "then source/answer failure, then stable original ordinal."
        ),
        "prior_runtime_git_sha": PRIOR_RUNTIME_GIT_SHA,
        "prior_cases_sha256": PRIOR_CASES_SHA256,
        "prior_report_sha256": PRIOR_REPORT_SHA256,
        "selected_case_ids_sha256": selected_ids_sha256,
        "cases_sha256": cases_sha256,
        "baseline": selected_baseline,
        "targeted_gate": {
            "minimum_passed": 5,
            "minimum_no_operator": 5,
            "required_trace_coverage": 1.0,
            "maximum_cache_hits": 0,
            "cost_cap_rub": cost_cap_rub,
        },
    }
    manifest_bytes = _canonical_json_bytes(manifest)
    with output_cases_path.open("xb") as handle:
        handle.write(cases_bytes)
    with output_manifest_path.open("xb") as handle:
        handle.write(manifest_bytes)
    return {
        "dataset_id": DATASET_ID,
        "cases_total": CASES_TOTAL,
        "cases_sha256": cases_sha256,
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "selected_case_ids_sha256": selected_ids_sha256,
    }


def _semantic_status(row: dict[str, Any]) -> tuple[bool, bool]:
    attempted = row.get("semantic_recovery_attempted") is True
    succeeded = attempted and row.get("semantic_recovery_status") == "ok"
    return attempted, succeeded


def summarize(
    *,
    manifest_path: Path,
    cases_path: Path,
    report_path: Path,
    output_path: Path,
    expected_runtime_git_sha: str,
    expected_approval_id: str,
    expected_cost_cap_rub: float = COST_CAP_RUB,
) -> dict[str, Any]:
    expected_cost_cap_rub = _validated_cost_cap(expected_cost_cap_rub)
    if FULL_GIT_SHA_RE.fullmatch(expected_runtime_git_sha) is None:
        raise ValueError("expected runtime Git SHA must be a full lowercase SHA")
    manifest = _read_json(manifest_path)
    cases = _read_json(cases_path)
    report = _read_json(report_path)
    if not isinstance(manifest, dict) or manifest.get("dataset_id") != DATASET_ID:
        raise ValueError("Recovery10 manifest mismatch")
    targeted_gate = manifest.get("targeted_gate")
    raw_manifest_cost_cap = (
        targeted_gate.get("cost_cap_rub")
        if isinstance(targeted_gate, dict)
        else None
    )
    if (
        isinstance(raw_manifest_cost_cap, bool)
        or not isinstance(raw_manifest_cost_cap, (int, float))
        or float(raw_manifest_cost_cap) != expected_cost_cap_rub
    ):
        raise ValueError("Recovery10 manifest cost cap mismatch")
    if not isinstance(cases, list) or len(cases) != CASES_TOTAL:
        raise ValueError("Recovery10 cases mismatch")
    cases_sha256 = _file_sha256(cases_path)
    if manifest.get("cases_sha256") != cases_sha256:
        raise ValueError("Recovery10 cases are not bound to the manifest")
    if not isinstance(report, dict) or report.get("cases_total") != CASES_TOTAL:
        raise ValueError("Recovery10 report cardinality mismatch")
    if report.get("cases_file_sha256") != cases_sha256:
        raise ValueError("Recovery10 report cases binding mismatch")
    if report.get("target") != TARGET:
        raise ValueError("Recovery10 report target mismatch")
    results = report.get("results")
    if not isinstance(results, list) or len(results) != CASES_TOTAL:
        raise ValueError("Recovery10 report results mismatch")
    case_ids = [str(case.get("id") or "") for case in cases]
    result_ids = [str(row.get("id") or "") for row in results]
    if case_ids != result_ids or len(set(case_ids)) != CASES_TOTAL:
        raise ValueError("Recovery10 result ordering/identity mismatch")

    runtime_identity = report.get("runtime_identity") or {}
    if (
        runtime_identity.get("expected_runtime_git_sha") != expected_runtime_git_sha
        or runtime_identity.get("verified_release_git_sha") != expected_runtime_git_sha
        or runtime_identity.get("matched_expected_runtime") is not True
    ):
        raise ValueError("Recovery10 runtime identity mismatch")
    cost_control = report.get("cost_control") or {}
    reservation = cost_control.get("reservation") or {}
    if (
        cost_control.get("pricing_complete") is not True
        or reservation.get("valid") is not True
        or reservation.get("runtime_git_sha") != expected_runtime_git_sha
        or reservation.get("manifest_sha256") != cases_sha256
        or reservation.get("case_count") != CASES_TOTAL
        or float(reservation.get("approved_cap_rub", -1)) != expected_cost_cap_rub
        or reservation.get("approval_required")
        is not (expected_cost_cap_rub > 100.0)
        or reservation.get("high_cost_approval_id") != expected_approval_id
    ):
        raise ValueError("Recovery10 cost reservation mismatch")
    cost = float(report.get("llm_estimated_cost_rub", math.nan))
    if (
        not math.isfinite(cost)
        or cost < 0
        or cost > expected_cost_cap_rub
        or report.get("llm_budget_exceeded") is True
        or report.get("llm_budget_stopped") is True
        or report.get("llm_pricing_stopped") is True
    ):
        raise ValueError("Recovery10 cost accounting mismatch")

    passed = sum(row.get("passed") is True for row in results)
    no_operator = sum(
        row.get("was_escalated") is False and row.get("observed_behavior") == "answer"
        for row in results
    )
    trace_found = sum(row.get("trace_found") is True for row in results)
    cache_hits = sum(row.get("cache_hit") is True for row in results)
    recovery = [_semantic_status(row) for row in results]
    recovery_attempted = sum(item[0] for item in recovery)
    recovery_succeeded = sum(item[1] for item in recovery)
    failure_counts: Counter[str] = Counter()
    for row in results:
        if row.get("passed") is True:
            continue
        failure_counts.update(_safe_failure_reasons(row))

    criteria = {
        "passed": {"actual": passed, "minimum": 5, "passed": passed >= 5},
        "no_operator": {
            "actual": no_operator,
            "minimum": 5,
            "passed": no_operator >= 5,
        },
        "trace_coverage": {
            "actual": trace_found,
            "required": CASES_TOTAL,
            "passed": trace_found == CASES_TOTAL,
        },
        "cache_hits": {
            "actual": cache_hits,
            "maximum": 0,
            "passed": cache_hits == 0,
        },
        "pricing": {"passed": True},
        "budget": {
            "actual_rub": cost,
            "maximum_rub": expected_cost_cap_rub,
            "passed": True,
        },
    }
    status = "GO" if all(item["passed"] for item in criteria.values()) else "STOP"
    safe = {
        "schema_version": SAFE_RESULT_SCHEMA_VERSION,
        "status": "OK",
        "diagnostic_gate": {"status": status, "criteria": criteria},
        "classification": manifest["classification"],
        "human_product_verdict": False,
        "disclaimer": manifest["disclaimer"],
        "dataset_id": DATASET_ID,
        "candidate_sha": expected_runtime_git_sha,
        "approval_id": expected_approval_id,
        "cases_sha256": cases_sha256,
        "manifest_sha256": _file_sha256(manifest_path),
        "report_sha256": _file_sha256(report_path),
        "counts": {
            "total": CASES_TOTAL,
            "passed": passed,
            "no_operator": no_operator,
            "trace_found": trace_found,
            "cache_hits": cache_hits,
            "semantic_recovery_attempted": recovery_attempted,
            "semantic_recovery_succeeded": recovery_succeeded,
        },
        "historical_baseline": {
            "runtime_git_sha": PRIOR_RUNTIME_GIT_SHA,
            "passed": 0,
            "total": CASES_TOTAL,
        },
        "failure_reason_counts": dict(sorted(failure_counts.items())),
        "latency_ms": {
            "p50": report.get("latency_ms", {}).get("p50"),
            "p95": report.get("latency_ms", {}).get("p95"),
        },
        "llm_cost_rub": cost,
        "budget": {
            "max_rub": expected_cost_cap_rub,
            "exceeded": False,
            "stopped": False,
        },
        "eval_run_id": report.get("eval_run_id"),
        "run_window_utc": {
            "started_at": report.get("run_started_at"),
            "completed_at": report.get("run_completed_at"),
        },
        "billing_status": "pending_provider_reconciliation",
    }
    _write_exclusive(output_path, safe)
    return safe


def show_safe(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != SAFE_RESULT_SCHEMA_VERSION:
        raise ValueError("invalid Recovery10 safe result")
    forbidden = ("query", "response", "message", "chunk_text", "request_id")
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    lowered = encoded.casefold()
    if any(f'"{field}"' in lowered for field in forbidden):
        raise ValueError("unsafe Recovery10 result field")
    return payload


def cost_preflight(
    ledger_dir: Path,
    *,
    requested_cap_rub: float = COST_CAP_RUB,
) -> dict[str, Any]:
    requested_cap_rub = _validated_cost_cap(requested_cap_rub)
    payload = inspect_routine_cost_capacity(
        requested_cap_rub=requested_cap_rub,
        ledger_dir=ledger_dir,
    )
    if set(payload) != {
        "status",
        "requested_cap_rub",
        "rolling_24h_cap_rub",
        "rolling_24h_routine_reserved_rub",
        "rolling_24h_routine_available_rub",
        "ledger_fingerprint_sha256",
    }:
        raise ValueError("unexpected cost capacity schema")
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded semantic-recovery diagnostic")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--prior-cases", type=Path, required=True)
    prepare_parser.add_argument("--prior-report", type=Path, required=True)
    prepare_parser.add_argument("--output-cases", type=Path, required=True)
    prepare_parser.add_argument("--output-manifest", type=Path, required=True)
    prepare_parser.add_argument("--cost-cap-rub", type=float, default=COST_CAP_RUB)
    summarize_parser = subparsers.add_parser("summarize")
    summarize_parser.add_argument("--manifest", type=Path, required=True)
    summarize_parser.add_argument("--cases", type=Path, required=True)
    summarize_parser.add_argument("--report", type=Path, required=True)
    summarize_parser.add_argument("--output", type=Path, required=True)
    summarize_parser.add_argument("--expected-runtime-git-sha", required=True)
    summarize_parser.add_argument("--expected-approval-id", required=True)
    summarize_parser.add_argument(
        "--expected-cost-cap-rub",
        type=float,
        default=COST_CAP_RUB,
    )
    show_parser = subparsers.add_parser("show-safe")
    show_parser.add_argument("--input", type=Path, required=True)
    cost_parser = subparsers.add_parser("cost-preflight")
    cost_parser.add_argument("--ledger-dir", type=Path, required=True)
    cost_parser.add_argument(
        "--requested-cap-rub",
        type=float,
        default=COST_CAP_RUB,
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    if args.command == "prepare":
        payload = prepare(
            prior_cases_path=args.prior_cases,
            prior_report_path=args.prior_report,
            output_cases_path=args.output_cases,
            output_manifest_path=args.output_manifest,
            cost_cap_rub=args.cost_cap_rub,
        )
    elif args.command == "summarize":
        payload = summarize(
            manifest_path=args.manifest,
            cases_path=args.cases,
            report_path=args.report,
            output_path=args.output,
            expected_runtime_git_sha=args.expected_runtime_git_sha,
            expected_approval_id=args.expected_approval_id,
            expected_cost_cap_rub=args.expected_cost_cap_rub,
        )
    elif args.command == "show-safe":
        payload = show_safe(args.input)
    else:
        payload = cost_preflight(
            args.ledger_dir,
            requested_cap_rub=args.requested_cap_rub,
        )
        print(f"cost_capacity_status={payload['status']}")
        print(f"requested_cap_rub={payload['requested_cap_rub']:g}")
        print(f"rolling_24h_cap_rub={payload['rolling_24h_cap_rub']:g}")
        print(
            "rolling_24h_routine_reserved_rub="
            f"{payload['rolling_24h_routine_reserved_rub']:g}"
        )
        print(
            "rolling_24h_routine_available_rub="
            f"{payload['rolling_24h_routine_available_rub']:g}"
        )
        print(
            "cost_ledger_fingerprint_sha256="
            f"{payload['ledger_fingerprint_sha256']}"
        )
        if payload["status"] != "GO":
            raise SystemExit(1)
        return
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
