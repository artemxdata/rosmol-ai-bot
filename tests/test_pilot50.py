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
            "trace_total_latency_ms": 100 + index,
            "llm_estimated_cost_rub": 0.01,
            "query": f"{canary}-query-{index}",
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
        "llm_estimated_cost_rub": 0.5,
        "cases_file_sha256": cases_sha256,
        "cost_control": {
            "strict_live": True,
            "high_cost_approval_id": APPROVAL_ID,
            "pricing_complete": True,
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
            "required": False,
            "status": "observed_unbound",
            "expected_runtime_git_sha": None,
            "preflight_release_git_sha": RUNTIME_GIT_SHA,
            "postflight_release_git_sha": None,
            "verified_release_git_sha": RUNTIME_GIT_SHA,
            "matched_expected_runtime": None,
        },
        "results": results,
        "private_canary": canary,
    }
    return report, trace_rows


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
    assert safe["pricing"] == {"complete": True, "stopped": False}
    assert safe["latency_ms"] == {"p50": 124, "p95": 147}
    assert safe["llm_cost_rub"] == 0.5
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
