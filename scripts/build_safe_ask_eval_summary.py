from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SAFE_SCHEMA = "ask-eval-safe-diagnostics-v1"
BEHAVIOR_VALUES = frozenset({"answer", "clarify", "escalate", "scope_note"})
GENERATION_DIAGNOSTIC_REASONS = frozenset(
    {
        "empty_generated_response",
        "llm_generation_failed",
        "llm_response_contract_failed",
        "llm_response_profile_failed",
        "llm_response_too_long",
        "llm_source_citation_failed",
        "llm_source_coverage_failed",
        "llm_source_fact_binding_failed",
        "source_response_contract_failed",
    }
)

_REQUIRED_CASE_BOOL_FIELDS = (
    "passed",
    "http_success",
    "trace_found",
)
_OPTIONAL_CASE_BOOL_FIELDS = (
    "behavior_match",
    "routing_response_profile_match",
    "was_escalated",
    "escalation_match",
    "escalation_reason_match",
    "expected_chunk_hit",
    "expected_or_equivalent_chunk_hit",
    "expected_cited_chunk_hit",
    "expected_cited_or_equivalent_chunk_hit",
    "forbidden_response_profiles_absent",
    "cited_source_types_allowed",
    "answer_contains_match",
    "no_false_insufficient_source_response",
    "no_non_answer_response",
    "cache_hit",
)
_OUTPUT_CASE_BOOL_FIELDS = {
    "routing_response_profile_match": "routing_match",
    "forbidden_response_profiles_absent": "forbidden_profiles_absent",
    "no_false_insufficient_source_response": "no_false_insufficient",
    "no_non_answer_response": "no_non_answer",
}
_RATE_FIELDS = {
    "pass_rate": "passed",
    "http_success_rate": "http_success",
    "behavior_match_rate": "behavior_match",
    "routing_match_rate": "routing_match",
    "expected_chunk_hit_rate": "expected_chunk_hit",
    "expected_or_equivalent_chunk_hit_rate": "expected_or_equivalent_chunk_hit",
    "expected_cited_chunk_hit_rate": "expected_cited_chunk_hit",
    "expected_cited_or_equivalent_chunk_hit_rate": (
        "expected_cited_or_equivalent_chunk_hit"
    ),
    "escalation_reason_match_rate": "escalation_reason_match",
    "forbidden_profile_absence_rate": "forbidden_profiles_absent",
    "cited_source_type_policy_rate": "cited_source_types_allowed",
    "answer_contains_rate": "answer_contains_match",
    "trace_coverage_rate": "trace_found",
    "escalation_rate": "was_escalated",
    "cache_hit_rate": "cache_hit",
}
_CLASSIFICATION_BOOL_FIELDS = (
    "provisional",
    "calibration_only",
    "independent_evaluation",
    "previously_exposed",
    "product_verdict_eligible",
    "human_product_verdict",
)


def build_safe_ask_eval_summary(input_path: Path, output_path: Path) -> dict[str, Any]:
    """Build an allowlist-only diagnostic summary from a raw ask-eval report."""

    source = input_path.expanduser()
    destination = output_path.expanduser()
    _ensure_regular_input(source)
    _ensure_distinct_paths(source, destination)

    raw = _read_json_object(source)
    raw_results = raw.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("input report must contain a results array")

    cases = [
        _sanitize_case(item, case_no=index)
        for index, item in enumerate(raw_results, start=1)
    ]
    summary: dict[str, Any] = {
        "schema": SAFE_SCHEMA,
        **_sanitize_classification(raw.get("report_classification")),
        "cases_total": len(cases),
        "cases_passed": sum(case["passed"] is True for case in cases),
    }
    summary.update(
        {
            output_name: _bool_rate(cases, case_field)
            for output_name, case_field in _RATE_FIELDS.items()
        }
    )
    summary["behavior_confusion_matrix"] = _behavior_confusion_matrix(cases)
    summary["generate_retry_reason_counts"] = _reason_counts(
        reason
        for case in cases
        for reason in case["generate_retry_reasons"]
    )
    summary["generation_failure_reason_counts"] = _reason_counts(
        case["generation_failure_reason"]
        for case in cases
        if case["generation_failure_reason"] is not None
    )
    summary["cases"] = cases

    _write_json_atomically(source, destination, summary)
    return summary


def _sanitize_case(value: Any, *, case_no: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"result {case_no} must be a JSON object")

    sanitized: dict[str, Any] = {"case_no": case_no}
    for field in _REQUIRED_CASE_BOOL_FIELDS:
        sanitized[field] = _required_bool(value.get(field), field=field, case_no=case_no)
    sanitized["expected_behavior"] = _behavior_enum(
        value.get("expected_behavior"),
        field="expected_behavior",
        case_no=case_no,
        required=False,
    )
    sanitized["observed_behavior"] = _behavior_enum(
        value.get("observed_behavior"),
        field="observed_behavior",
        case_no=case_no,
        required=True,
    )
    sanitized["generate_retry_reasons"] = _generation_retry_reasons(
        value.get("generate_retry_reasons"),
        case_no=case_no,
    )
    sanitized["generation_failure_reason"] = _generation_failure_reason(
        value.get("escalation_reason"),
        case_no=case_no,
    )
    for field in _OPTIONAL_CASE_BOOL_FIELDS:
        output_field = _OUTPUT_CASE_BOOL_FIELDS.get(field, field)
        sanitized[output_field] = _optional_bool(
            value.get(field),
            field=field,
            case_no=case_no,
        )
    return sanitized


def _sanitize_classification(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "evaluation_classification": "unclassified",
            **{field: None for field in _CLASSIFICATION_BOOL_FIELDS},
        }
    if not isinstance(value, dict):
        raise ValueError("report_classification must be a JSON object")

    sanitized = {
        field: _optional_bool(value.get(field), field=field)
        for field in _CLASSIFICATION_BOOL_FIELDS
    }
    if sanitized["calibration_only"] is True:
        classification = "calibration_only"
    elif sanitized["provisional"] is True:
        classification = "provisional"
    else:
        classification = "diagnostic"
    return {"evaluation_classification": classification, **sanitized}


def _required_bool(value: Any, *, field: str, case_no: int | None = None) -> bool:
    if type(value) is not bool:
        raise ValueError(_field_error(field, case_no, "must be a boolean"))
    return value


def _optional_bool(
    value: Any,
    *,
    field: str,
    case_no: int | None = None,
) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise ValueError(_field_error(field, case_no, "must be a boolean or null"))
    return value


def _behavior_enum(
    value: Any,
    *,
    field: str,
    case_no: int,
    required: bool,
) -> str | None:
    if value is None and not required:
        return None
    if type(value) is not str or value not in BEHAVIOR_VALUES:
        allowed = ", ".join(sorted(BEHAVIOR_VALUES))
        raise ValueError(
            _field_error(field, case_no, f"must be one of: {allowed}")
        )
    return value


def _field_error(field: str, case_no: int | None, detail: str) -> str:
    prefix = f"result {case_no} field {field}" if case_no is not None else field
    return f"{prefix} {detail}"


def _generation_retry_reasons(value: Any, *, case_no: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(
            _field_error("generate_retry_reasons", case_no, "must be an array")
        )
    reasons: list[str] = []
    for reason in value:
        if type(reason) is not str or reason not in GENERATION_DIAGNOSTIC_REASONS:
            raise ValueError(
                _field_error(
                    "generate_retry_reasons",
                    case_no,
                    "contains an unknown diagnostic reason",
                )
            )
        reasons.append(reason)
    return reasons


def _generation_failure_reason(value: Any, *, case_no: int) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(
            _field_error("escalation_reason", case_no, "must be a string or null")
        )
    return value if value in GENERATION_DIAGNOSTIC_REASONS else None


def _bool_rate(cases: list[dict[str, Any]], field: str) -> float | None:
    scored = [case[field] for case in cases if case.get(field) is not None]
    if not scored:
        return None
    return sum(value is True for value in scored) / len(scored)


def _behavior_confusion_matrix(cases: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {}
    for case in cases:
        expected = case.get("expected_behavior")
        observed = case["observed_behavior"]
        if expected is None:
            continue
        row = matrix.setdefault(expected, {})
        row[observed] = row.get(observed, 0) + 1
    return {
        expected: dict(sorted(observed.items()))
        for expected, observed in sorted(matrix.items())
    }


def _reason_counts(reasons: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _ensure_regular_input(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("input must be a regular file, not a symlink")


def _ensure_distinct_paths(input_path: Path, output_path: Path) -> None:
    input_resolved = input_path.resolve(strict=True)
    output_resolved = output_path.resolve(strict=False)
    if os.path.normcase(str(input_resolved)) == os.path.normcase(str(output_resolved)):
        raise ValueError("input and output must be different files")
    if output_path.is_symlink():
        raise ValueError("output must not be a symlink")
    if output_path.exists():
        if not output_path.is_file():
            raise ValueError("output must be a regular file")
        if os.path.samefile(input_path, output_path):
            raise ValueError("input and output must be different files")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            value = json.load(
                source,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite_number,
            )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("input must be readable UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("input report must be a JSON object")
    return value


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON keys are not allowed")
        result[key] = value
    return result


def _reject_non_finite_number(value: str) -> Any:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _write_json_atomically(
    input_path: Path,
    output_path: Path,
    payload: dict[str, Any],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_distinct_paths(input_path, output_path)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        try:
            os.chmod(temporary_path, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                descriptor = -1
                destination.write(encoded)
                destination.flush()
                os.fsync(destination.fileno())
            _ensure_distinct_paths(input_path, output_path)
            os.replace(temporary_path, output_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create an allowlist-only safe summary of an ask-eval JSON report."
    )
    parser.add_argument("--input", required=True, type=Path, help="Raw ask-eval JSON report")
    parser.add_argument("--output", required=True, type=Path, help="Safe output JSON file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    build_safe_ask_eval_summary(args.input, args.output)
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(f"SAFE_REPORT={args.output.resolve()}")
    print(f"SHA256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
