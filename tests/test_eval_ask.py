from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest

import eval.run_ask as run_ask_module
from eval.run_ask import (
    _guard_eval_artifact_aliases,
    _guard_eval_privacy,
    _json_safe,
    _llm_cost_rub_total,
    _normalize_case,
    _quality_gate_failures,
    _trace_dsn_candidates,
    build_seed_ask_cases,
    holdout_cases_payload_sha256,
    run_eval,
    score_case,
    summarize_results,
)

HOLDOUT_CASE_IDS = [f"case-{index:03d}" for index in range(1, 81)]
CALIBRATION_REPLAY_RUNTIME_SHA = "9" * 40


def _holdout_contract(
    case_ids: list[str],
    **overrides: object,
) -> dict[str, object]:
    selected_case_ids_sha256 = hashlib.sha256(
        ("\n".join(sorted(case_ids)) + "\n").encode("utf-8")
    ).hexdigest()
    contract: dict[str, object] = {
        "schema_version": "1.1.0",
        "baseline_id": "independent_holdout_80_v1",
        "runtime_git_sha": "1" * 40,
        "review_mode": "human_reviewed",
        "product_verdict_eligible": True,
        "freeze_contract_sha256": "2" * 64,
        "review_manifest_sha256": "3" * 64,
        "selected_case_ids_sha256": selected_case_ids_sha256,
        "cases_payload_sha256": "4" * 64,
        "knowledge_base_seed_sha256": "5" * 64,
        "review_workbook_sha256": "6" * 64,
        "source_cases_sha256": "7" * 64,
        "selection_manifest_sha256": "8" * 64,
        "cases_total": 80,
        "execution_allowed": True,
    }
    contract.update(overrides)
    return contract


def _private_holdout_cases(
    case_ids: list[str],
    *,
    contract: dict[str, object] | None = None,
    review_mode: str | None = None,
) -> list[dict[str, object]]:
    if review_mode is not None:
        effective_review_mode = review_mode
    elif contract is not None:
        effective_review_mode = str(contract["review_mode"])
    else:
        effective_review_mode = run_ask_module.HUMAN_REVIEW_MODE
    shared_contract = dict(
        contract
        or _holdout_contract(
            case_ids,
            review_mode=effective_review_mode,
            product_verdict_eligible=(
                effective_review_mode == run_ask_module.HUMAN_REVIEW_MODE
            ),
        )
    )
    requires_human_review = (
        effective_review_mode != run_ask_module.HUMAN_REVIEW_MODE
    )
    cases = [
        {
            "id": case_id,
            "query": f"deidentified query {index}",
            "user_id": f"reviewed-holdout-{case_id}",
            "privacy_class": "private_ticket_derived",
            "split": "holdout",
            "label_status": effective_review_mode,
            "requires_human_review": requires_human_review,
            "tags": [
                "label_verdict:approved",
                "split:holdout",
                f"review_mode:{effective_review_mode}",
            ],
            "source_provenance": {
                "case_fingerprint": f"fingerprint-{index}",
            },
        }
        for index, case_id in enumerate(case_ids, start=1)
    ]
    if contract is None:
        shared_contract["cases_payload_sha256"] = holdout_cases_payload_sha256(
            cases
        )
    for case in cases:
        case["holdout_contract"] = dict(shared_contract)
    return cases


class _FakeTracePool:
    async def close(self) -> None:
        return None


def _install_holdout_trace_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cache_hit: object = False,
    trace_error: Exception | None = None,
    trace_record_error: object = None,
    binding_mismatch: bool = False,
    cardinality_case_counts: dict[str, int] | None = None,
    cardinality_error: Exception | None = None,
) -> None:
    async def fake_create_pool(*args: object, **kwargs: object) -> _FakeTracePool:
        return _FakeTracePool()

    async def fake_fetch_trace(
        pool: object,
        request_id: str,
        *,
        expected_eval_run_id: str = "",
        expected_eval_case_id: str = "",
    ) -> dict[str, object]:
        if trace_error is not None:
            raise trace_error
        resolved_cache_hit = (
            cache_hit.get(expected_eval_case_id, False)
            if isinstance(cache_hit, dict)
            else cache_hit
        )
        return {
            "cache_hit": resolved_cache_hit,
            "eval_run_id": (
                "wrong-eval-run" if binding_mismatch else expected_eval_run_id
            ),
            "eval_case_id": expected_eval_case_id,
            "error": trace_record_error,
        }

    async def fake_fetch_eval_trace_cardinality(
        pool: object,
        *,
        eval_run_id: str,
        expected_case_ids: list[str],
    ) -> dict[str, object]:
        if cardinality_error is not None:
            raise cardinality_error
        case_counts = (
            dict(cardinality_case_counts)
            if cardinality_case_counts is not None
            else {case_id: 1 for case_id in expected_case_ids}
        )
        expected = set(expected_case_ids)
        observed = set(case_counts)
        return {
            "eval_run_id": eval_run_id,
            "expected_cases_total": len(expected_case_ids),
            "traces_total": sum(case_counts.values()),
            "case_counts": dict(sorted(case_counts.items())),
            "missing_case_ids": sorted(expected - observed),
            "duplicate_case_ids": sorted(
                case_id
                for case_id, trace_count in case_counts.items()
                if trace_count != 1
            ),
            "unknown_case_ids": sorted(observed - expected),
        }

    monkeypatch.setattr(run_ask_module.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(run_ask_module, "_fetch_trace", fake_fetch_trace)
    monkeypatch.setattr(
        run_ask_module,
        "_fetch_eval_trace_cardinality",
        fake_fetch_eval_trace_cardinality,
    )


def _holdout_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path]:
    monkeypatch.setenv("API_AUTH_TOKEN", "test-eval-secret")
    private_root = tmp_path / "private"
    cases_dir = private_root / "cases" / "sealed"
    cases_dir.mkdir(parents=True)
    ledger_dir = (
        private_root / run_ask_module.CANONICAL_HOLDOUT_LEDGER_DIRNAME
    )
    monkeypatch.setattr(run_ask_module, "PRIVATE_DATA_ROOT", private_root)
    return private_root, cases_dir, ledger_dir


def _sealed_holdout_kwargs(
    ledger_dir: Path,
    cases: list[dict[str, object]],
    cases_path: Path,
) -> dict[str, object]:
    contract = cases[0]["holdout_contract"]
    return {
        "sealed_holdout": True,
        "expected_holdout_freeze_sha256": contract[
            "freeze_contract_sha256"
        ],
        "expected_cases_payload_sha256": contract["cases_payload_sha256"],
        "expected_cases_file_sha256": run_ask_module._file_sha256(cases_path),
        "holdout_ledger_dir": ledger_dir,
    }


def _calibration_replay_kwargs(
    ledger_dir: Path,
    cases_path: Path,
    *,
    runtime_git_sha: str | None = CALIBRATION_REPLAY_RUNTIME_SHA,
) -> dict[str, object]:
    raw_cases = json.loads(cases_path.read_text(encoding="utf-8"))
    contract = raw_cases[0]["holdout_contract"]
    return {
        "calibration_replay": True,
        "expected_runtime_git_sha": runtime_git_sha,
        "expected_holdout_freeze_sha256": contract[
            "freeze_contract_sha256"
        ],
        "expected_cases_payload_sha256": contract[
            "cases_payload_sha256"
        ],
        "expected_cases_file_sha256": run_ask_module._file_sha256(cases_path),
        "calibration_replay_ledger_dir": ledger_dir,
    }


def _seed_sealed_exposure_receipt(
    sealed_ledger_dir: Path,
    cases_path: Path,
    source_contract: dict[str, object],
) -> dict[str, bytes]:
    sealed_ledger_dir.mkdir(parents=True, exist_ok=True)
    receipt_key = run_ask_module._derive_holdout_receipt_key(
        str(source_contract["selected_case_ids_sha256"])
    )
    receipt_path = sealed_ledger_dir / f"{receipt_key}.started.json"
    run_ask_module._create_holdout_started_receipt(
        receipt_path,
        contract=source_contract,
        expected_cases_file_sha256=run_ask_module._file_sha256(cases_path),
        eval_run_id="ask-eval-original-exposure",
        cases_path=cases_path,
        output_path=cases_path.with_name("original-holdout-result.json"),
        receipt_key=receipt_key,
    )
    return _ledger_file_bytes(sealed_ledger_dir)


def _ledger_file_bytes(ledger_dir: Path) -> dict[str, bytes]:
    if not ledger_dir.exists():
        return {}
    return {
        path.name: path.read_bytes()
        for path in sorted(ledger_dir.iterdir())
        if path.is_file()
    }


def _request_id_for_case(request: httpx.Request) -> str:
    case_id = request.headers["X-Eval-Case-Id"]
    index = int(case_id.rsplit("-", maxsplit=1)[-1])
    return f"00000000-0000-0000-0000-{index:012d}"


def _ready_payload(
    *,
    release_git_sha: str = "1" * 40,
    authorized: bool = True,
) -> dict[str, object]:
    return {
        "status": "ready",
        "release_git_sha": release_git_sha,
        "eval_cache_bypass": {
            "scheme": run_ask_module.eval_cache_bypass.SCHEME,
            "authorized": authorized,
        },
    }


def test_calibration_replay_receipt_key_binds_selection_file_and_runtime() -> None:
    inputs = {
        "selected_case_ids_sha256": "1" * 64,
        "cases_file_sha256": "2" * 64,
        "runtime_git_sha": "3" * 40,
    }
    baseline = run_ask_module._derive_calibration_replay_receipt_key(
        **inputs
    )

    assert baseline == run_ask_module._derive_calibration_replay_receipt_key(
        **inputs
    )
    for field, replacement in (
        ("selected_case_ids_sha256", "4" * 64),
        ("cases_file_sha256", "5" * 64),
        ("runtime_git_sha", "6" * 40),
    ):
        changed = {**inputs, field: replacement}
        assert (
            run_ask_module._derive_calibration_replay_receipt_key(**changed)
            != baseline
        )


def test_sealed_started_receipt_keeps_v1_schema_without_replay_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _private_root, cases_dir, sealed_ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    _seed_sealed_exposure_receipt(
        sealed_ledger_dir,
        cases_path,
        raw_cases[0]["holdout_contract"],
    )
    receipt_path = next(sealed_ledger_dir.glob("*.started.json"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert receipt["schema_version"] == "1.0.0"
    assert receipt["status"] == "started"
    assert receipt["runtime_git_sha"] == "1" * 40
    assert receipt["product_verdict_eligible"] is True
    assert {
        "receipt_type",
        "run_mode",
        "source_runtime_git_sha",
        "evaluation_runtime_git_sha",
        "source_holdout_contract",
        "prior_sealed_exposure_receipts",
        "calibration_only",
        "independent_evaluation",
        "previously_exposed",
    }.isdisjoint(receipt)


@pytest.mark.asyncio
async def test_fetch_eval_trace_cardinality_groups_run_cases() -> None:
    class CardinalityPool:
        async def fetch(self, query: str, eval_run_id: str) -> list[dict[str, object]]:
            assert "WHERE eval_run_id = $1" in query
            assert "GROUP BY eval_case_id" in query
            assert eval_run_id == "ask-eval-cardinality"
            return [
                {"eval_case_id": "case-001", "trace_count": 2},
                {"eval_case_id": None, "trace_count": 1},
            ]

    summary = await run_ask_module._fetch_eval_trace_cardinality(
        CardinalityPool(),  # type: ignore[arg-type]
        eval_run_id="ask-eval-cardinality",
        expected_case_ids=["case-001", "case-002"],
    )

    assert summary == {
        "eval_run_id": "ask-eval-cardinality",
        "expected_cases_total": 2,
        "traces_total": 3,
        "case_counts": {"<null>": 1, "case-001": 2},
        "missing_case_ids": ["case-002"],
        "duplicate_case_ids": ["case-001"],
        "unknown_case_ids": ["<null>"],
    }


def test_normalize_case_accepts_common_fields() -> None:
    case = _normalize_case(
        {
            "case_id": "mashuk-travel",
            "question": "Кто оплачивает проезд на Машук?",
            "expected_chunks": "chunk_1",
            "answer_contains": "оплачивает самостоятельно",
            "expected_escalated": False,
            "expected_generator_model": "source_chunk",
            "tags": "travel",
        }
    )

    assert case == {
        "id": "mashuk-travel",
        "query": "Кто оплачивает проезд на Машук?",
        "privacy_class": "standard",
        "user_id": "ask-eval",
        "channel": "api",
        "forum_context": None,
        "expected_chunk_ids": ["chunk_1"],
        "expected_cited_chunk_ids": [],
        "allowed_cited_source_types": [],
        "equivalent_chunk_ids": {},
        "expected_answer_contains": ["оплачивает самостоятельно"],
        "expected_message_masked_contains": [],
        "forbidden_message_masked_contains": [],
        "expected_behavior": None,
        "expected_response_profile": None,
        "forbidden_response_profiles": [],
        "expected_escalated": False,
        "expected_escalation_reason": None,
        "expected_generator_model": "source_chunk",
        "tags": ["travel"],
    }


def test_normalize_case_keeps_explicit_forum_context() -> None:
    case = _normalize_case(
        {
            "id": "ticket-context",
            "query": "Где мой билет?",
            "forum_context": "День молодёжи",
        }
    )

    assert case["forum_context"] == "День молодёжи"


def test_normalize_case_keeps_private_ticket_privacy_class() -> None:
    case = _normalize_case(
        {
            "id": "private-ticket",
            "query": "private masked ticket query",
            "privacy_class": "private_ticket_derived",
            "split": "validation",
            "label_status": "human_reviewed",
            "requires_human_review": False,
        }
    )

    assert case["privacy_class"] == "private_ticket_derived"


def test_normalize_case_preserves_valid_private_holdout_contract() -> None:
    raw = _private_holdout_cases(["case-1"])[0]

    case = _normalize_case(raw)

    assert case["split"] == "holdout"
    assert case["holdout_contract"] == raw["holdout_contract"]


def test_normalize_case_accepts_model_assisted_prerun_only_with_opt_in() -> None:
    contract = _holdout_contract(
        ["case-1"],
        review_mode=run_ask_module.MODEL_ASSISTED_PRERUN_MODE,
        product_verdict_eligible=False,
    )
    raw = _private_holdout_cases(
        ["case-1"],
        contract=contract,
        review_mode=run_ask_module.MODEL_ASSISTED_PRERUN_MODE,
    )[0]

    with pytest.raises(ValueError, match="explicit runner opt-in"):
        _normalize_case(raw)

    case = _normalize_case(raw, allow_model_assisted_prerun=True)

    assert case["split"] == "holdout"
    assert case["holdout_contract"]["review_mode"] == (
        run_ask_module.MODEL_ASSISTED_PRERUN_MODE
    )


def test_normalize_case_rejects_model_assisted_contract_label_mismatch() -> None:
    contract = _holdout_contract(
        ["case-1"],
        review_mode=run_ask_module.MODEL_ASSISTED_PRERUN_MODE,
        product_verdict_eligible=False,
    )
    raw = _private_holdout_cases(
        ["case-1"],
        contract=contract,
        review_mode=run_ask_module.HUMAN_REVIEW_MODE,
    )[0]

    with pytest.raises(ValueError, match="review_mode must match"):
        _normalize_case(raw, allow_model_assisted_prerun=True)


def test_holdout_cases_payload_hash_is_order_independent() -> None:
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    original_digest = holdout_cases_payload_sha256(raw_cases)
    mutated = json.loads(json.dumps(raw_cases))
    mutated[0]["source_provenance"]["case_fingerprint"] = "mutated"

    assert original_digest == holdout_cases_payload_sha256(
        list(reversed(raw_cases))
    )
    assert original_digest != holdout_cases_payload_sha256(mutated)


@pytest.mark.parametrize(
    ("contract", "error"),
    [
        (None, "require holdout_contract"),
        (
            _holdout_contract(["case-1"], schema_version="2.0.0"),
            "schema_version",
        ),
        (
            _holdout_contract(["case-1"], baseline_id="../holdout"),
            "baseline_id",
        ),
        (
            _holdout_contract(["case-1"], baseline_id="holdout\n"),
            "baseline_id",
        ),
        (
            _holdout_contract(["case-1"], runtime_git_sha="abc123"),
            "full lowercase 40-hex",
        ),
        (
            _holdout_contract(["case-1"], review_mode="unknown"),
            "review_mode",
        ),
        (
            _holdout_contract(
                ["case-1"],
                review_mode=run_ask_module.MODEL_ASSISTED_PRERUN_MODE,
                product_verdict_eligible=True,
            ),
            "product_verdict_eligible conflicts",
        ),
        (
            _holdout_contract(["case-1"], freeze_contract_sha256="bad"),
            "SHA-256",
        ),
        (
            {
                key: value
                for key, value in _holdout_contract(["case-1"]).items()
                if key != "review_workbook_sha256"
            },
            "schema exactly",
        ),
        (
            {**_holdout_contract(["case-1"]), "unexpected": "value"},
            "schema exactly",
        ),
        (
            _holdout_contract(["case-1"], cases_total=79),
            "exactly 80",
        ),
        (
            _holdout_contract(["case-1"], execution_allowed=False),
            "execution_allowed",
        ),
    ],
)
def test_normalize_case_rejects_invalid_private_holdout_contract(
    contract: dict[str, object] | None,
    error: str,
) -> None:
    raw = _private_holdout_cases(["case-1"])[0]
    raw["holdout_contract"] = contract

    with pytest.raises(ValueError, match=error):
        _normalize_case(raw)


@pytest.mark.parametrize(
    ("requires_human_review", "label_status"),
    [
        (" true ", "human_reviewed"),
        (False, " Weak_Unreviewed "),
    ],
)
def test_normalize_case_rejects_weak_review_flags(
    requires_human_review: object,
    label_status: str,
) -> None:
    with pytest.raises(ValueError, match="requiring human review"):
        _normalize_case(
            {
                "query": "private masked ticket query",
                "requires_human_review": requires_human_review,
                "label_status": label_status,
            }
        )


def test_normalize_case_rejects_unreviewed_private_ticket() -> None:
    with pytest.raises(ValueError, match="human-reviewed"):
        _normalize_case(
            {
                "query": "private masked ticket query",
                "privacy_class": "private_ticket_derived",
            }
        )


def test_normalize_case_requires_explicit_split_for_private_ticket() -> None:
    with pytest.raises(ValueError, match="explicit split"):
        _normalize_case(
            {
                "query": "private masked ticket query",
                "privacy_class": "private_ticket_derived",
                "label_status": "human_reviewed",
                "requires_human_review": False,
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("split", "validation"),
        ("tags", ["split:validation"]),
        ("user_id", "reviewed-validation-case-001"),
        ("holdout_contract", None),
        ("privacy_class", "standard"),
    ],
)
def test_normalize_case_rejects_inconsistent_holdout_markers(
    field: str,
    value: object,
) -> None:
    raw = _private_holdout_cases(HOLDOUT_CASE_IDS)[0]
    if field == "holdout_contract" and value is None:
        raw.pop(field)
    else:
        raw[field] = value

    with pytest.raises(ValueError, match="holdout markers"):
        _normalize_case(raw)


def test_private_ticket_eval_allows_only_local_private_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    monkeypatch.setattr(run_ask_module, "PRIVATE_DATA_ROOT", private_root)
    private_case = [{"privacy_class": "private_ticket_derived"}]
    cases_path = private_root / "cases.json"
    output_path = private_root / "result.json"
    markdown_path = private_root / "result.md"

    _guard_eval_privacy(
        cases=private_case,
        cases_path=cases_path,
        output_path=output_path,
        markdown_path=markdown_path,
        target="http://127.0.0.1:8001/ask",
    )
    _guard_eval_privacy(
        cases=private_case,
        cases_path=cases_path,
        output_path=output_path,
        markdown_path=None,
        target="http://app-ml:8000/ask",
    )

    with pytest.raises(ValueError, match="loopback"):
        _guard_eval_privacy(
            cases=private_case,
            cases_path=cases_path,
            output_path=output_path,
            markdown_path=None,
            target="https://example.test/ask",
        )
    with pytest.raises(ValueError, match="must stay under"):
        _guard_eval_privacy(
            cases=private_case,
            cases_path=cases_path,
            output_path=tmp_path / "public-result.json",
            markdown_path=None,
            target="http://localhost:8001/ask",
        )


def test_private_directory_rejects_standard_privacy_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    monkeypatch.setattr(run_ask_module, "PRIVATE_DATA_ROOT", private_root)

    with pytest.raises(ValueError, match="cannot use privacy_class=standard"):
        _guard_eval_privacy(
            cases=[{"privacy_class": "standard"}],
            cases_path=private_root / "cases.json",
            output_path=private_root / "result.json",
            markdown_path=None,
            target="http://app-ml:8000/ask",
        )


def test_eval_artifact_paths_cannot_alias(
    tmp_path: Path,
) -> None:
    cases_path = tmp_path / "cases.json"
    cases_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="must not alias"):
        _guard_eval_artifact_aliases(
            cases_path=cases_path,
            output_path=tmp_path / "." / "cases.json",
            markdown_path=None,
        )
    with pytest.raises(ValueError, match="must not alias"):
        _guard_eval_artifact_aliases(
            cases_path=cases_path,
            output_path=tmp_path / "output.json",
            markdown_path=tmp_path / "output.json",
        )


def test_build_seed_ask_cases_uses_intent_examples() -> None:
    cases = build_seed_ask_cases(
        [
            {
                "chunk_id": "travel",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Машук",
                "intent_examples": ["кто платит за дорогу"],
            },
            {
                "chunk_id": "old",
                "status": "archived",
                "intent_examples": ["старый вопрос"],
            },
        ],
        user_prefix="local",
    )

    assert cases == [
        {
            "id": "seed_balanced::travel",
            "query": "Машук кто платит за дорогу",
            "user_id": "local-1",
            "channel": "api",
            "expected_chunk_ids": ["travel"],
            "expected_answer_contains": [],
            "expected_behavior": "answer",
            "expected_escalated": None,
            "expected_escalation_reason": None,
            "expected_generator_model": None,
            "tags": ["seed_balanced", "category:форумы", "forum:Машук"],
        }
    ]


def test_trace_dsn_candidates_add_localhost_fallback() -> None:
    candidates = _trace_dsn_candidates(
        "postgresql://rosmol:rosmol@postgres:5432/rosmol_ai_bot"
    )

    assert candidates == [
        "postgresql://rosmol:rosmol@postgres:5432/rosmol_ai_bot",
        "postgresql://rosmol:rosmol@localhost:5432/rosmol_ai_bot",
    ]


def test_json_safe_decodes_asyncpg_json_strings() -> None:
    value = _json_safe('[{"chunk_id": "travel", "score": 0.9}]')

    assert value == [{"chunk_id": "travel", "score": 0.9}]


def test_llm_cost_rub_total_ignores_missing_and_invalid_values() -> None:
    assert _llm_cost_rub_total(
        [
            {"llm_estimated_cost_rub": "1.25"},
            {"llm_estimated_cost_rub": 2},
            {"llm_estimated_cost_rub": None},
            {"llm_estimated_cost_rub": "not-a-number"},
            {},
        ]
    ) == 3.25


def test_score_case_uses_trace_for_chunk_model_and_escalation_checks() -> None:
    case = _normalize_case(
        {
            "id": "travel",
            "query": "Кто платит за дорогу?",
            "expected_chunk_ids": ["travel"],
            "expected_escalated": False,
            "expected_generator_model": "source_chunk",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Проезд оплачивается самостоятельно.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["travel"],
        "retrieved_chunks": [],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
        "cache_hit": False,
        "total_latency_ms": 110,
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is True
    assert result["escalation_match"] is True
    assert result["generator_model_match"] is True
    assert result["passed"] is True


def test_score_case_normalizes_spacing_inside_numeric_dates() -> None:
    case = _normalize_case(
        {
            "id": "date-spacing",
            "query": "До какого числа можно подать заявку?",
            "expected_answer_contains": ["12.09.2026"],
        }
    )

    result = score_case(
        case,
        {
            "http_status": 200,
            "request_id": "11111111-1111-1111-1111-111111111111",
            "response": "Подать заявку можно до 12. 09. 2026 включительно.",
            "latency_ms": 20,
            "error": None,
        },
        None,
    )

    assert result["answer_contains_match"] is True
    assert result["passed"] is True


def test_normalize_case_accepts_expected_behavior_aliases() -> None:
    case = _normalize_case(
        {
            "id": "scope",
            "query": "Какая погода завтра?",
            "expected_response_type": "offtopic",
        }
    )

    assert case["expected_behavior"] == "scope_note"


def test_normalize_case_validates_expected_response_profile() -> None:
    case = _normalize_case(
        {
            "id": "dates",
            "query": "Когда проходит форум Машук?",
            "expected_response_profile": "dates",
        }
    )

    assert case["expected_response_profile"] == "dates"
    assert case["forbidden_response_profiles"] == [
        "application",
        "selection_status",
        "travel",
    ]

    with pytest.raises(ValueError, match="expected_response_profile"):
        _normalize_case(
            {
                "id": "invalid-profile",
                "query": "Когда проходит форум?",
                "expected_response_profile": "travel_and_dates",
            }
        )


def test_normalize_case_unions_explicit_and_default_forbidden_profiles() -> None:
    case = _normalize_case(
        {
            "id": "dates-with-explicit-forbidden",
            "query": "Когда проходит форум Машук?",
            "expected_response_profile": "dates",
            "forbidden_response_profiles": ["food", "travel"],
        }
    )

    assert case["forbidden_response_profiles"] == [
        "application",
        "food",
        "selection_status",
        "travel",
    ]


def test_score_case_checks_routing_profile_from_trace() -> None:
    case = _normalize_case(
        {
            "id": "dates",
            "query": "Когда проходит форум Машук?",
            "expected_behavior": "answer",
            "expected_response_profile": "dates",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Форум проходит с 10 по 14 августа.",
        "latency_ms": 120,
        "error": None,
    }

    matched = score_case(
        case,
        http_result,
        {
            "was_escalated": False,
            "query_analysis": {"response_profile": "dates"},
        },
    )
    mismatched = score_case(
        case,
        http_result,
        {
            "was_escalated": False,
            "query_analysis": '{"response_profile": "travel"}',
        },
    )

    assert matched["routing_response_profile_match"] is True
    assert matched["passed"] is True
    assert mismatched["routing_response_profile_match"] is False
    assert mismatched["passed"] is False
    assert (
        "routing_response_profile_mismatch:dates!=travel"
        in mismatched["failure_reasons"]
    )


def test_score_case_fails_profile_check_without_trace() -> None:
    case = _normalize_case(
        {
            "id": "dates-no-trace",
            "query": "Когда проходит форум Машук?",
            "expected_response_profile": "dates",
        }
    )

    result = score_case(
        case,
        {
            "http_status": 200,
            "request_id": "11111111-1111-1111-1111-111111111111",
            "response": "Форум проходит в августе.",
            "latency_ms": 120,
            "error": None,
        },
        None,
    )

    assert result["routing_response_profile_match"] is None
    assert result["passed"] is False
    assert "trace_missing" in result["failure_reasons"]


def test_score_case_rejects_forbidden_answer_profile_drift() -> None:
    case = _normalize_case(
        {
            "id": "dates-vs-travel",
            "query": "Когда проходит форум Машук?",
            "expected_behavior": "answer",
            "expected_response_profile": "dates",
            "forbidden_response_profiles": [
                "travel",
                "application",
            ],
        }
    )
    trace = {
        "was_escalated": False,
        "query_analysis": {"response_profile": "dates"},
    }

    result = score_case(
        case,
        {
            "http_status": 200,
            "request_id": "11111111-1111-1111-1111-111111111111",
            "response": "Трансфер от вокзала до площадки будет бесплатным.",
            "latency_ms": 120,
            "error": None,
        },
        trace,
    )

    assert result["routing_response_profile_match"] is True
    assert result["detected_response_profiles"] == ["travel"]
    assert result["forbidden_response_profile_hits"] == ["travel"]
    assert result["forbidden_response_profiles_absent"] is False
    assert result["passed"] is False
    assert (
        "forbidden_response_profile_detected:travel"
        in result["failure_reasons"]
    )


def test_score_case_rejects_eval_only_travel_paraphrase_for_dates() -> None:
    case = _normalize_case(
        {
            "id": "dates-vs-travel-paraphrase",
            "query": "Когда проходит форум Машук?",
            "expected_behavior": "answer",
            "expected_response_profile": "dates",
        }
    )

    result = score_case(
        case,
        {
            "http_status": 200,
            "request_id": "11111111-1111-1111-1111-111111111111",
            "response": "Организаторы довезут участников от точки сбора.",
            "latency_ms": 120,
            "error": None,
        },
        {
            "was_escalated": False,
            "query_analysis": {"response_profile": "dates"},
        },
    )

    assert result["detected_response_profiles"] == ["travel"]
    assert result["forbidden_response_profile_hits"] == ["travel"]
    assert result["forbidden_response_profiles_absent"] is False
    assert result["passed"] is False


def test_normalize_case_infers_scope_note_from_seed_topic() -> None:
    case = _normalize_case(
        {
            "id": "seed_balanced::xlsx_fallback_r0022_offtop_ne_po_rosmolodezhi",
            "query": "как починить телефон",
            "expected_chunk_ids": ["xlsx_fallback_r0022_offtop_ne_po_rosmolodezhi"],
            "expected_cited_chunk_ids": ["xlsx_fallback_r0022_offtop_ne_po_rosmolodezhi"],
            "tags": ["seed_balanced", "topic:offtop_ne_po_rosmolodezhi"],
        }
    )

    assert case["expected_behavior"] == "scope_note"
    assert case["expected_chunk_ids"] == []
    assert case["expected_cited_chunk_ids"] == []


def test_normalize_case_infers_escalation_from_seed_topic() -> None:
    case = _normalize_case(
        {
            "id": "seed_balanced::xlsx_fallback_r0017_pereklyuchit_na_operatora",
            "query": "Жду ответ оператора",
            "expected_chunk_ids": ["xlsx_fallback_r0017_pereklyuchit_na_operatora"],
            "expected_cited_chunk_ids": ["xlsx_fallback_r0017_pereklyuchit_na_operatora"],
            "tags": ["seed_balanced", "topic:pereklyuchit_na_operatora"],
        }
    )

    assert case["expected_behavior"] == "escalate"
    assert case["expected_chunk_ids"] == []
    assert case["expected_cited_chunk_ids"] == []


def test_normalize_case_infers_clarify_for_generic_application_query() -> None:
    case = _normalize_case(
        {
            "id": "seed_balanced::xlsx_category_r0007_podat_zayavku_na_uchastie",
            "query": "Подать заявку на участие",
            "expected_chunk_ids": ["xlsx_category_r0007_podat_zayavku_na_uchastie"],
            "expected_cited_chunk_ids": ["xlsx_category_r0007_podat_zayavku_na_uchastie"],
        }
    )

    assert case["expected_behavior"] == "clarify"
    assert case["expected_chunk_ids"] == []
    assert case["expected_cited_chunk_ids"] == []


def test_score_case_accepts_scope_note_behavior() -> None:
    case = _normalize_case(
        {
            "id": "weather",
            "query": "Какая погода завтра?",
            "expected_behavior": "scope_note",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": (
            "Я отвечаю на вопросы по мероприятиям, форумам, ФГАИС «Молодёжь России» "
            "и грантам Росмолодёжи. Задай, пожалуйста, вопрос по этим темам."
        ),
        "latency_ms": 120,
        "error": None,
    }

    result = score_case(case, http_result, {"was_escalated": False})

    assert result["observed_behavior"] == "scope_note"
    assert result["behavior_match"] is True
    assert result["passed"] is True


def test_score_case_accepts_clarify_behavior() -> None:
    case = _normalize_case(
        {
            "id": "generic-application",
            "query": "Подать заявку на участие",
            "expected_behavior": "clarify",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Уточни, пожалуйста, речь о форуме, мероприятии или грантовом конкурсе?",
        "latency_ms": 120,
        "error": None,
    }

    result = score_case(case, http_result, {"was_escalated": False})

    assert result["observed_behavior"] == "clarify"


def test_score_case_treats_final_clarification_as_clarify_with_stale_citations() -> None:
    case = {
        "id": "clarify_after_verify",
        "query": "а это не форум, а программа",
        "expected_behavior": "clarify",
        "expected_escalated": False,
    }
    http_result = {
        "http_status": 200,
        "request_id": "request-clarify-after-verify",
        "response": "Уточни, пожалуйста, точное название программы.",
        "latency_ms": 10,
        "error": None,
    }
    trace = {
        "was_escalated": False,
        "cited_sources": ["stale_retrieval_source"],
    }

    result = score_case(case, http_result, trace)

    assert result["observed_behavior"] == "clarify"
    assert result["passed"] is True
    assert result["passed"] is True


def test_score_case_does_not_treat_supported_answer_wording_as_clarify() -> None:
    case = _normalize_case(
        {
            "id": "sourced-answer-with-clarify-word",
            "query": "Можно ли с ОВЗ?",
            "expected_behavior": "answer",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": (
            "Участники с ОВЗ могут участвовать. Детали можно уточнить через "
            "Службу Заботы организаторов."
        ),
        "latency_ms": 120,
        "error": None,
    }

    result = score_case(
        case,
        http_result,
        {"was_escalated": False, "cited_sources": ["ovz"]},
    )

    assert result["observed_behavior"] == "answer"
    assert result["passed"] is True


def test_score_case_accepts_escalate_behavior() -> None:
    case = _normalize_case(
        {
            "id": "operator",
            "query": "Позови оператора",
            "expected_behavior": "escalate",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Передаю обращение специалисту.",
        "latency_ms": 120,
        "error": None,
    }

    result = score_case(case, http_result, {"was_escalated": True})

    assert result["observed_behavior"] == "escalate"
    assert result["passed"] is True


def test_score_case_can_require_masked_trace_text() -> None:
    case = _normalize_case(
        {
            "id": "pii-phone",
            "query": "Мой телефон +7 999 123-45-67, как зарегистрироваться?",
            "expected_behavior": "answer",
            "expected_message_masked_contains": ["[ТЕЛЕФОН]"],
            "forbidden_message_masked_contains": ["+7 999 123-45-67"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Ответ по регистрации.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {"message_masked": "Мой телефон [ТЕЛЕФОН], как зарегистрироваться?"}

    result = score_case(case, http_result, trace)

    assert result["message_masked_contains_match"] is True
    assert result["message_masked_forbidden_absent_match"] is True
    assert result["passed"] is True


def test_score_case_fails_when_masked_trace_contains_raw_pii() -> None:
    case = _normalize_case(
        {
            "id": "pii-phone",
            "query": "Мой телефон +7 999 123-45-67, как зарегистрироваться?",
            "expected_behavior": "answer",
            "forbidden_message_masked_contains": ["+7 999 123-45-67"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Ответ по регистрации.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {"message_masked": "Мой телефон +7 999 123-45-67, как зарегистрироваться?"}

    result = score_case(case, http_result, trace)

    assert result["message_masked_forbidden_absent_match"] is False
    assert result["passed"] is False
    assert "message_masked_forbidden_contains_raw_pii" in result["failure_reasons"]


def test_score_case_rejects_behavior_mismatch() -> None:
    case = _normalize_case(
        {
            "id": "generic-application",
            "query": "Подать заявку на участие",
            "expected_behavior": "clarify",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Подать заявку можно на странице мероприятия.",
        "latency_ms": 120,
        "error": None,
    }

    result = score_case(case, http_result, {"was_escalated": False})

    assert result["observed_behavior"] == "answer"
    assert result["passed"] is False
    assert result["failure_reasons"] == ["behavior_mismatch:clarify!=answer"]


def test_score_case_rejects_false_insufficient_source_answer() -> None:
    case = _normalize_case(
        {
            "id": "travel",
            "query": "Кто платит за дорогу?",
            "expected_chunk_ids": ["travel"],
            "expected_escalated": False,
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Ответа на вопрос о проезде в источниках нет.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["travel"],
        "retrieved_chunks": [],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "ai-sage/GigaChat3-10B-A1.8B",
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is True
    assert result["no_false_insufficient_source_response"] is False
    assert result["passed"] is False


def test_score_case_rejects_non_answer_source_reference() -> None:
    case = _normalize_case(
        {
            "id": "travel",
            "query": "Кто платит за дорогу?",
            "expected_chunk_ids": ["travel"],
            "expected_escalated": False,
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Информация о проезде уже была предоставлена в источнике.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["travel"],
        "retrieved_chunks": [],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "ai-sage/GigaChat3-10B-A1.8B",
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is True
    assert result["no_non_answer_response"] is False
    assert result["passed"] is False


def test_score_case_requires_all_expected_chunks_when_multiple_are_declared() -> None:
    case = _normalize_case(
        {
            "id": "multi",
            "query": "Проезд и проживание?",
            "expected_chunk_ids": ["travel", "housing"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Проезд оплачивает направляющая сторона.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["travel"],
        "retrieved_chunks": [],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is False
    assert result["missing_expected_chunk_ids"] == ["housing"]
    assert result["passed"] is False


def test_score_case_requires_expected_cited_chunks() -> None:
    case = _normalize_case(
        {
            "id": "multi",
            "query": "Проезд и проживание?",
            "expected_chunk_ids": ["travel", "housing"],
            "expected_cited_chunk_ids": ["travel", "housing"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Проезд оплачивает направляющая сторона.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["travel"],
        "retrieved_chunks": [{"chunk_id": "housing"}],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is True
    assert result["expected_cited_chunk_hit"] is False
    assert result["missing_expected_cited_chunk_ids"] == ["housing"]
    assert result["cited_source_ids"] == ["travel"]
    assert result["cited_source_types"] == ["unknown"]
    assert result["passed"] is False
    assert result["failure_reasons"] == ["expected_chunk_not_cited"]


def test_score_case_accepts_equivalent_cited_chunk() -> None:
    case = _normalize_case(
        {
            "id": "equivalent",
            "query": "Where is the status?",
            "expected_chunk_ids": ["expected_status"],
            "expected_cited_chunk_ids": ["expected_status"],
            "equivalent_chunk_ids": {"expected_status": ["neighbor_status"]},
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Status answer.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["neighbor_status"],
        "retrieved_chunks": [{"chunk_id": "expected_status"}],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is True
    assert result["expected_cited_chunk_hit"] is False
    assert result["expected_cited_or_equivalent_chunk_hit"] is True
    assert result["passed"] is True
    assert result["failure_reasons"] == []


def test_score_case_reports_cited_source_types() -> None:
    case = _normalize_case(
        {
            "id": "source-type",
            "query": "Почему отклонили заявку?",
            "expected_chunk_ids": ["ticket_answer_bank_006"],
            "expected_cited_chunk_ids": ["ticket_answer_bank_006"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Причина отклонения доступна в личном кабинете.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["xlsx_category_r0004_otkazali_v_zayavke"],
        "retrieved_chunks": [
            {
                "chunk_id": "xlsx_category_r0004_otkazali_v_zayavke",
                "metadata": {"source_type": "xlsx"},
            }
        ],
        "reranker_scores": [
            {
                "chunk_id": "ticket_answer_bank_006",
                "metadata": {"source_type": "ticket_answer_bank"},
            }
        ],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["cited_source_ids"] == ["xlsx_category_r0004_otkazali_v_zayavke"]
    assert result["cited_source_types"] == ["xlsx"]
    assert result["failure_reasons"] == ["expected_chunk_not_cited"]


def test_normalize_case_accepts_allowed_cited_source_types() -> None:
    case = _normalize_case(
        {
            "id": "yonote-policy",
            "query": "Что такое Росмолодёжь?",
            "allowed_cited_source_types": [" Yonote ", "yonote"],
        }
    )

    assert case["allowed_cited_source_types"] == ["yonote"]


def test_score_case_rejects_any_citation_outside_allowed_source_types() -> None:
    expected_chunk = "yonote_api_source_s0001_fact"
    case = _normalize_case(
        {
            "id": "yonote-only-policy",
            "query": "Что подтверждает источник?",
            "expected_chunk_ids": [expected_chunk],
            "expected_cited_chunk_ids": [expected_chunk],
            "allowed_cited_source_types": ["yonote"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Подтверждённый ответ.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": [
            expected_chunk,
            "xlsx_category_r0001_legacy",
        ],
        "retrieved_chunks": [
            {
                "chunk_id": expected_chunk,
                "metadata": {"source_type": "yonote"},
            },
            {
                "chunk_id": "xlsx_category_r0001_legacy",
                "metadata": {"source_type": "xlsx"},
            },
        ],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["cited_source_types"] == ["xlsx", "yonote"]
    assert result["unexpected_cited_source_types"] == ["xlsx"]
    assert result["cited_source_types_allowed"] is False
    assert result["passed"] is False
    assert result["failure_reasons"] == ["forbidden_cited_source_type"]


def test_score_case_infers_yonote_source_type_from_chunk_id() -> None:
    expected_chunk = "yonote_api_source_s0001_fact"
    case = _normalize_case(
        {
            "id": "yonote-prefix",
            "query": "Что подтверждает источник?",
            "expected_chunk_ids": [expected_chunk],
            "expected_cited_chunk_ids": [expected_chunk],
            "allowed_cited_source_types": ["yonote"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Подтверждённый ответ.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": [expected_chunk],
        "retrieved_chunks": [],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["cited_source_types"] == ["yonote"]
    assert result["unexpected_cited_source_types"] == []
    assert result["cited_source_types_allowed"] is True
    assert result["passed"] is True


def test_clarification_rejects_forbidden_residual_citation_source() -> None:
    case = _normalize_case(
        {
            "id": "clarify-source-policy",
            "query": "Когда проходит мероприятие?",
            "expected_behavior": "clarify",
            "allowed_cited_source_types": ["yonote"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Уточни, пожалуйста, название мероприятия.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["xlsx_category_r0001_legacy"],
        "retrieved_chunks": [
            {
                "chunk_id": "xlsx_category_r0001_legacy",
                "metadata": {"source_type": "xlsx"},
            }
        ],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": None,
    }

    result = score_case(case, http_result, trace)

    assert case["expected_chunk_ids"] == []
    assert case["expected_cited_chunk_ids"] == []
    assert case["allowed_cited_source_types"] == ["yonote"]
    assert result["observed_behavior"] == "clarify"
    assert result["cited_source_types_allowed"] is False
    assert result["passed"] is False
    assert result["failure_reasons"] == ["forbidden_cited_source_type"]


def test_cited_source_type_metadata_is_case_insensitive() -> None:
    expected_chunk = "custom_yonote_chunk"
    case = _normalize_case(
        {
            "id": "yonote-metadata-case",
            "query": "Что подтверждает источник?",
            "expected_chunk_ids": [expected_chunk],
            "expected_cited_chunk_ids": [expected_chunk],
            "allowed_cited_source_types": ["yonote"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Подтверждённый ответ.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": [expected_chunk],
        "retrieved_chunks": [
            {
                "chunk_id": expected_chunk,
                "metadata": {"source_type": " Yonote "},
            }
        ],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["cited_source_types"] == ["yonote"]
    assert result["cited_source_types_allowed"] is True
    assert result["passed"] is True


def test_quality_gate_failures_are_opt_in_and_fail_closed() -> None:
    metrics = {
        "pass_rate": 0.95,
        "trace_coverage_rate": None,
    }

    assert _quality_gate_failures(
        metrics,
        fail_on_any_case=False,
        require_complete_traces=False,
    ) == []
    assert _quality_gate_failures(
        metrics,
        fail_on_any_case=True,
        require_complete_traces=True,
    ) == [
        "case_pass_rate_below_100_percent",
        "trace_coverage_below_100_percent",
    ]


def test_quality_gate_accepts_only_exact_complete_rates() -> None:
    assert _quality_gate_failures(
        {
            "pass_rate": 1.0,
            "trace_coverage_rate": 1.0,
        },
        fail_on_any_case=True,
        require_complete_traces=True,
    ) == []


def test_score_case_classifies_infrastructure_http_error() -> None:
    case = _normalize_case(
        {
            "id": "infra",
            "query": "Привет",
            "expected_chunk_ids": ["hello"],
        }
    )
    http_result = {
        "http_status": None,
        "request_id": None,
        "response": "",
        "latency_ms": 120,
        "error": "ConnectError: All connection attempts failed",
    }

    result = score_case(case, http_result, None)

    assert result["passed"] is False
    assert result["failure_reasons"] == [
        "http_error",
        "expected_chunk_not_observed",
    ]


def test_summarize_results_counts_core_metrics() -> None:
    metrics = summarize_results(
        [
            {
                "passed": True,
                "http_success": True,
                "expected_chunk_ids": ["a"],
                "expected_chunk_hit": True,
                "expected_answer_contains": [],
                "trace_found": True,
                "was_escalated": False,
                "cache_hit": False,
                "generator_model": "source_chunk",
                "max_reranker_score": 0.9,
                "latency_ms": 100,
                "trace_total_latency_ms": 90,
                "llm_prompt_tokens": 0,
                "llm_completion_tokens": 0,
                "llm_total_tokens": 0,
                "llm_estimated_cost_rub": 0.0,
                "llm_usage": [],
            },
            {
                "passed": False,
                "http_success": True,
                "failure_reasons": ["unexpected_escalation"],
                "expected_chunk_ids": ["b"],
                "expected_chunk_hit": True,
                "expected_answer_contains": [],
                "trace_found": True,
                "was_escalated": True,
                "escalation_reason": "low_confidence",
                "cache_hit": False,
                "generator_model": None,
                "max_reranker_score": 0.05,
                "latency_ms": 300,
                "trace_total_latency_ms": 250,
                "llm_prompt_tokens": 10,
                "llm_completion_tokens": 5,
                "llm_total_tokens": 15,
                "llm_estimated_cost_rub": 0.01,
                "llm_usage": [{"model": "m", "total_tokens": 15}],
            },
        ],
        target="http://127.0.0.1:8001/ask",
        cases_path=Path("cases.json"),
    )

    assert metrics["cases_total"] == 2
    assert metrics["pass_rate"] == 0.5
    assert metrics["expected_chunk_hit_rate"] == 1.0
    assert metrics["escalation_rate"] == 0.5
    assert metrics["source_chunk_rate"] == 0.5
    assert metrics["low_confidence_expected_chunk_hits"] == 1
    assert metrics["low_confidence_expected_chunk_hit_rate"] == 0.5
    assert metrics["reranker_score"] == {"avg": 0.475, "p50": 0.05, "p95": 0.9, "max": 0.9}
    assert metrics["latency_ms"]["p95"] == 300
    assert metrics["llm_total_tokens"] == 15
    assert metrics["llm_estimated_cost_rub"] == 0.01
    assert metrics["failure_reason_counts"] == {"unexpected_escalation": 1}
    assert metrics["likely_infrastructure_failure"] is False


def test_summarize_results_marks_likely_infrastructure_failure() -> None:
    metrics = summarize_results(
        [
            {
                "passed": False,
                "http_success": False,
                "failure_reasons": ["http_error"],
                "expected_chunk_ids": ["a"],
                "expected_answer_contains": [],
                "trace_found": False,
                "latency_ms": 100,
                "llm_prompt_tokens": 0,
                "llm_completion_tokens": 0,
                "llm_total_tokens": 0,
                "llm_estimated_cost_rub": 0.0,
                "llm_usage": [],
            }
        ],
        target="http://127.0.0.1:8001/ask",
        cases_path=Path("cases.json"),
    )

    assert metrics["failure_reason_counts"] == {"http_error": 1}
    assert metrics["likely_infrastructure_failure"] is True


@pytest.mark.asyncio
async def test_run_eval_writes_json_and_markdown_without_db(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    markdown = tmp_path / "ask_metrics.md"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "hello",
                    "query": "Привет",
                    "expected_answer_contains": ["Здравствуйте"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["text"] == "Привет"
        assert payload["user_id"].startswith("ask-eval-")
        assert not payload["user_id"].startswith("runner-holdout-")
        assert request.headers["X-Eval-Run-Id"].startswith("ask-eval-")
        assert request.headers["X-Eval-Case-Id"] == "hello"
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Здравствуйте!",
            },
        )

    metrics = await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://127.0.0.1:8001/ask",
        trace_lookup=False,
        api_key_env=None,
        markdown_path=markdown,
        transport=httpx.MockTransport(handler),
    )

    assert metrics["cases_total"] == 1
    assert metrics["pass_rate"] == 1.0
    assert metrics["eval_run_id"].startswith("ask-eval-")
    assert json.loads(output.read_text(encoding="utf-8"))["results"][0]["passed"] is True
    assert "Ask Eval Report" in markdown.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_eval_can_send_bypass_cache_header(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps([{"id": "hello", "query": "Привет"}], ensure_ascii=False),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Bypass-Cache"] == "1"
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Здравствуйте!",
            },
        )

    metrics = await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://127.0.0.1:8001/ask",
        trace_lookup=False,
        api_key_env=None,
        transport=httpx.MockTransport(handler),
        bypass_cache=True,
    )

    assert metrics["cases_total"] == 1
    assert metrics["http_success_rate"] == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "http://app-ml:8000/ask",
        "http://172.20.0.9:8000/ask",
        "http://rosmol-app-ml:8000/ask",
    ],
)
async def test_server_local_eval_rejects_old_runtime_before_first_ask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "test-eval-secret")
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps([{"id": "hello", "query": "Привет"}], ensure_ascii=False),
        encoding="utf-8",
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "release_git_sha": "old-runtime",
                "checks": {},
            },
        )

    with pytest.raises(ValueError, match="did not authorize signed cache bypass"):
        await run_eval(
            cases_path=cases,
            output_path=output,
            target=target,
            trace_lookup=False,
            transport=httpx.MockTransport(handler),
            bypass_cache=True,
        )

    assert requests == [("GET", "/ready")]
    assert not output.exists()


@pytest.mark.asyncio
async def test_server_local_eval_runs_after_authorized_capability_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "test-eval-secret")
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps([{"id": "hello", "query": "Привет"}], ensure_ascii=False),
        encoding="utf-8",
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=_ready_payload())
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Здравствуйте!",
            },
        )

    metrics = await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://app-ml:8000/ask",
        trace_lookup=False,
        transport=httpx.MockTransport(handler),
        bypass_cache=True,
    )

    assert requests == [("GET", "/ready"), ("POST", "/ask")]
    assert metrics["http_success_rate"] == 1.0


@pytest.mark.asyncio
async def test_run_eval_blocks_large_live_run_without_budget(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps(
            [{"id": f"case-{idx}", "query": f"Вопрос {idx}"} for idx in range(21)],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="explicit LLM budget"):
        await run_eval(
            cases_path=cases,
            output_path=output,
            target="http://localhost:8001/ask",
            trace_lookup=False,
            api_key_env=None,
        )

    assert not output.exists()


@pytest.mark.asyncio
async def test_run_eval_allows_large_mock_run_without_budget(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps(
            [{"id": f"case-{idx}", "query": f"Вопрос {idx}"} for idx in range(21)],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "OK",
            },
        )

    metrics = await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://test/ask",
        trace_lookup=False,
        api_key_env=None,
        transport=httpx.MockTransport(handler),
    )

    assert metrics["cases_total"] == 21
    assert metrics["llm_budget_rub"] is None
    assert metrics["llm_budget_exceeded"] is None


@pytest.mark.asyncio
async def test_run_eval_marks_budget_status_and_case_limit(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps(
            [{"id": f"case-{idx}", "query": f"Вопрос {idx}"} for idx in range(5)],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "OK",
            },
        )

    metrics = await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://test/ask",
        trace_lookup=False,
        api_key_env=None,
        transport=httpx.MockTransport(handler),
        max_cases=2,
        max_llm_cost_rub=0.0,
    )

    assert metrics["cases_total"] == 2
    assert metrics["cases_original_total"] == 5
    assert metrics["cases_limit"] == 2
    assert metrics["cases_limited"] is True
    assert metrics["llm_budget_rub"] == 0.0
    assert metrics["llm_budget_exceeded"] is False


@pytest.mark.asyncio
async def test_run_eval_user_prefix_isolates_loaded_case_users(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps(
            [
                {"id": "one", "query": "Первый", "user_id": "same"},
                {"id": "two", "query": "Второй", "user_id": "same"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    seen_user_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        seen_user_ids.append(payload["user_id"])
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "OK",
            },
        )

    await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://test/ask",
        trace_lookup=False,
        api_key_env=None,
        transport=httpx.MockTransport(handler),
        generated_user_prefix="isolated",
    )

    assert seen_user_ids == ["isolated-1", "isolated-2"]


@pytest.mark.asyncio
async def test_run_eval_executes_sealed_private_holdout_after_matching_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    _install_holdout_trace_stubs(monkeypatch)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    contract = raw_cases[0]["holdout_contract"]
    receipt_key = run_ask_module._derive_holdout_receipt_key(
        contract["selected_case_ids_sha256"]
    )
    receipt_path = ledger_dir / f"{receipt_key}.started.json"
    completed_path = ledger_dir / f"{receipt_key}.completed.json"
    cases_path.write_text(
        json.dumps(raw_cases, ensure_ascii=False),
        encoding="utf-8",
    )
    requests: list[tuple[str, str]] = []
    sealed_user_ids: list[str] = []
    exported_user_ids = {str(case["user_id"]) for case in raw_cases}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            assert request.headers["X-Bypass-Cache"] == "1"
            assert request.headers["X-Eval-Cache-Bypass-Probe"] == "1"
            assert (
                request.headers["X-Eval-Cache-Bypass-Version"]
                == run_ask_module.eval_cache_bypass.SCHEME
            )
            return httpx.Response(
                200,
                json=_ready_payload(),
            )
        assert receipt_path.is_file()
        assert request.headers["X-Bypass-Cache"] == "1"
        timestamp = request.headers["X-Eval-Cache-Bypass-Timestamp"]
        nonce = request.headers["X-Eval-Cache-Bypass-Nonce"]
        payload = json.loads(request.content.decode("utf-8"))
        expected_signature = run_ask_module.eval_cache_bypass.signature(
            "test-eval-secret",
            method="POST",
            path="/ask",
            eval_run_id=request.headers["X-Eval-Run-Id"],
            eval_case_id=request.headers["X-Eval-Case-Id"],
            timestamp=timestamp,
            nonce=nonce,
            payload_sha256=(
                run_ask_module.eval_cache_bypass.canonical_payload_sha256(
                    run_ask_module.eval_cache_bypass.canonical_ask_payload(
                        payload
                    )
                )
            ),
        )
        assert (
            request.headers["X-Eval-Cache-Bypass-Signature"]
            == expected_signature
        )
        request_user_id = str(json.loads(request.content)["user_id"])
        sealed_user_ids.append(request_user_id)
        assert request_user_id.startswith("runner-holdout-")
        assert len(request_user_id) <= 200
        assert request_user_id not in exported_user_ids
        return httpx.Response(
            200,
            json={
                "request_id": _request_id_for_case(request),
                "response": "OK",
            },
        )

    metrics = await run_eval(
        cases_path=cases_path,
        output_path=output_path,
        target="http://app-ml:8000/ask",
        trace_lookup=True,
        trace_dsn="postgresql://trace.test/db",
        api_key_env=None,
        transport=httpx.MockTransport(handler),
        bypass_cache=True,
        **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
    )

    assert requests[0] == ("GET", "/ready")
    assert requests[-1] == ("GET", "/ready")
    assert requests.count(("GET", "/ready")) == 2
    assert requests.count(("POST", "/ask")) == 80
    assert len(sealed_user_ids) == 80
    assert len(set(sealed_user_ids)) == 80
    assert metrics["cases_total"] == 80
    assert metrics["http_success_rate"] == 1.0
    assert metrics["trace_coverage_rate"] == 1.0
    assert metrics["cache_hit_rate"] == 0.0
    assert metrics["holdout_contract"] == raw_cases[0]["holdout_contract"]
    assert metrics["holdout_run"]["completed"] is True
    assert metrics["holdout_run"]["status"] == "completed"
    assert metrics["holdout_run"]["integrity_failures"] == []
    assert metrics["trace_cardinality"]["traces_total"] == 80
    assert metrics["trace_cardinality"]["missing_case_ids"] == []
    assert metrics["trace_cardinality"]["duplicate_case_ids"] == []
    assert metrics["trace_cardinality"]["unknown_case_ids"] == []
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected_cases_file_sha256 = run_ask_module._file_sha256(cases_path)
    assert receipt["status"] == "started"
    assert receipt["baseline_id"] == "independent_holdout_80_v1"
    assert receipt["cases_payload_sha256"] == raw_cases[0]["holdout_contract"][
        "cases_payload_sha256"
    ]
    assert (
        receipt["expected_cases_file_sha256"]
        == expected_cases_file_sha256
    )
    assert receipt["output_path"] == str(output_path.resolve())
    completed = json.loads(completed_path.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"
    assert (
        completed["expected_cases_file_sha256"]
        == expected_cases_file_sha256
    )
    assert completed["output_sha256"] == run_ask_module._file_sha256(output_path)

    copied_cases = json.loads(json.dumps(raw_cases))
    for case in copied_cases:
        case["holdout_contract"]["baseline_id"] = "renamed_baseline"
        case["holdout_contract"]["freeze_contract_sha256"] = "9" * 64
    copied_dir = private_root / "copied_cases"
    copied_dir.mkdir()
    copied_cases_path = copied_dir / "renamed.json"
    copied_output_path = copied_dir / "result.json"
    copied_cases_path.write_text(json.dumps(copied_cases), encoding="utf-8")
    copied_receipt_key = run_ask_module._derive_holdout_receipt_key(
        copied_cases[0]["holdout_contract"]["selected_case_ids_sha256"]
    )
    assert copied_receipt_key == receipt_key
    requests_before_rerun = list(requests)

    with pytest.raises(ValueError, match="rerun is forbidden"):
        await run_eval(
            cases_path=copied_cases_path,
            output_path=copied_output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            transport=httpx.MockTransport(handler),
            bypass_cache=True,
            **_sealed_holdout_kwargs(
                ledger_dir,
                copied_cases,
                copied_cases_path,
            ),
        )

    assert requests == requests_before_rerun
    assert not copied_output_path.exists()
    assert list(ledger_dir.glob("*.rejected.json"))


@pytest.mark.asyncio
async def test_run_eval_marks_model_assisted_prerun_as_provisional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    _install_holdout_trace_stubs(monkeypatch)
    raw_cases = _private_holdout_cases(
        HOLDOUT_CASE_IDS,
        review_mode=run_ask_module.MODEL_ASSISTED_PRERUN_MODE,
    )
    cases_path = cases_dir / "model-assisted-holdout.json"
    output_path = cases_dir / "model-assisted-result.json"
    markdown_path = cases_dir / "model-assisted-result.md"
    cases_path.write_text(
        json.dumps(raw_cases, ensure_ascii=False),
        encoding="utf-8",
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json=_ready_payload(),
            )
        return httpx.Response(
            200,
            json={
                "request_id": _request_id_for_case(request),
                "response": "OK",
            },
        )

    with pytest.raises(ValueError, match="explicit runner opt-in"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            transport=httpx.MockTransport(handler),
            bypass_cache=True,
            **_sealed_holdout_kwargs(
                ledger_dir,
                raw_cases,
                cases_path,
            ),
        )

    assert requests == []
    assert not output_path.exists()
    assert not ledger_dir.exists()

    metrics = await run_eval(
        cases_path=cases_path,
        output_path=output_path,
        markdown_path=markdown_path,
        target="http://app-ml:8000/ask",
        trace_lookup=True,
        trace_dsn="postgresql://trace.test/db",
        api_key_env=None,
        transport=httpx.MockTransport(handler),
        bypass_cache=True,
        allow_model_assisted_prerun=True,
        **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
    )

    classification = metrics["report_classification"]
    assert classification["review_mode"] == (
        run_ask_module.MODEL_ASSISTED_PRERUN_MODE
    )
    assert classification["report_status"] == (
        run_ask_module.MODEL_ASSISTED_REPORT_STATUS
    )
    assert classification["provisional"] is True
    assert classification["product_verdict_eligible"] is False
    assert classification["human_product_verdict"] is False
    assert metrics["holdout_run"]["provisional"] is True
    assert metrics["holdout_run"]["human_product_verdict"] is False
    assert output_path.is_file()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "PROVISIONAL MODEL-ASSISTED PRE-RUN DIAGNOSTIC" in markdown
    assert "must not be reported as product conversion" in markdown

    contract = raw_cases[0]["holdout_contract"]
    receipt_key = run_ask_module._derive_holdout_receipt_key(
        contract["selected_case_ids_sha256"]
    )
    started = json.loads(
        (ledger_dir / f"{receipt_key}.started.json").read_text(
            encoding="utf-8"
        )
    )
    completed = json.loads(
        (ledger_dir / f"{receipt_key}.completed.json").read_text(
            encoding="utf-8"
        )
    )
    for receipt in (started, completed):
        assert receipt["review_mode"] == (
            run_ask_module.MODEL_ASSISTED_PRERUN_MODE
        )
        assert receipt["provisional"] is True
        assert receipt["product_verdict_eligible"] is False
        assert receipt["human_product_verdict"] is False


@pytest.mark.asyncio
async def test_run_eval_executes_exposed_holdout_as_calibration_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root, cases_dir, sealed_ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    calibration_ledger_dir = (
        private_root
        / run_ask_module.CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME
    )
    _install_holdout_trace_stubs(monkeypatch)
    raw_cases = _private_holdout_cases(
        HOLDOUT_CASE_IDS,
        review_mode=run_ask_module.MODEL_ASSISTED_PRERUN_MODE,
    )
    source_contract = json.loads(
        json.dumps(raw_cases[0]["holdout_contract"])
    )
    cases_path = cases_dir / "exposed-holdout-calibration.json"
    output_path = cases_dir / "calibration-result.json"
    markdown_path = cases_dir / "calibration-result.md"
    cases_bytes = json.dumps(raw_cases, ensure_ascii=False)
    cases_path.write_text(cases_bytes, encoding="utf-8")
    sealed_ledger_before = _seed_sealed_exposure_receipt(
        sealed_ledger_dir,
        cases_path,
        source_contract,
    )
    requests: list[tuple[str, str]] = []
    ready_nonces: list[str] = []
    all_nonces: list[str] = []
    calibration_user_ids: list[str] = []
    exported_user_ids = {str(case["user_id"]) for case in raw_cases}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        assert request.headers["X-Bypass-Cache"] == "1"
        timestamp = request.headers["X-Eval-Cache-Bypass-Timestamp"]
        nonce = request.headers["X-Eval-Cache-Bypass-Nonce"]
        all_nonces.append(nonce)
        if request.method == "GET":
            ready_nonces.append(nonce)
            assert request.headers["X-Eval-Cache-Bypass-Probe"] == "1"
            expected_signature = run_ask_module.eval_cache_bypass.signature(
                "test-eval-secret",
                method="GET",
                path="/ready",
                eval_run_id=request.headers["X-Eval-Run-Id"],
                eval_case_id=(
                    run_ask_module.eval_cache_bypass.CAPABILITY_PROBE_CASE_ID
                ),
                timestamp=timestamp,
                nonce=nonce,
                payload_sha256=(
                    run_ask_module.eval_cache_bypass.EMPTY_PAYLOAD_SHA256
                ),
            )
            assert (
                request.headers["X-Eval-Cache-Bypass-Signature"]
                == expected_signature
            )
            return httpx.Response(
                200,
                json=_ready_payload(
                    release_git_sha=CALIBRATION_REPLAY_RUNTIME_SHA
                ),
            )

        assert list(calibration_ledger_dir.glob("*.started.json"))
        payload = json.loads(request.content.decode("utf-8"))
        expected_signature = run_ask_module.eval_cache_bypass.signature(
            "test-eval-secret",
            method="POST",
            path="/ask",
            eval_run_id=request.headers["X-Eval-Run-Id"],
            eval_case_id=request.headers["X-Eval-Case-Id"],
            timestamp=timestamp,
            nonce=nonce,
            payload_sha256=(
                run_ask_module.eval_cache_bypass.canonical_payload_sha256(
                    run_ask_module.eval_cache_bypass.canonical_ask_payload(
                        payload
                    )
                )
            ),
        )
        assert (
            request.headers["X-Eval-Cache-Bypass-Signature"]
            == expected_signature
        )
        request_user_id = str(payload["user_id"])
        calibration_user_ids.append(request_user_id)
        assert request_user_id not in exported_user_ids
        return httpx.Response(
            200,
            json={
                "request_id": _request_id_for_case(request),
                "response": "OK",
            },
        )

    metrics = await run_eval(
        cases_path=cases_path,
        output_path=output_path,
        markdown_path=markdown_path,
        target="http://app-ml:8000/ask",
        trace_lookup=True,
        trace_dsn="postgresql://trace.test/db",
        api_key_env=None,
        transport=httpx.MockTransport(handler),
        bypass_cache=True,
        allow_model_assisted_prerun=True,
        **_calibration_replay_kwargs(
            calibration_ledger_dir,
            cases_path,
        ),
    )

    assert requests[0] == ("GET", "/ready")
    assert requests[-1] == ("GET", "/ready")
    assert requests.count(("GET", "/ready")) == 2
    assert requests.count(("POST", "/ask")) == 80
    assert len(ready_nonces) == 2
    assert len(set(ready_nonces)) == 2
    assert len(all_nonces) == 82
    assert len(set(all_nonces)) == 82
    assert len(calibration_user_ids) == 80
    assert len(set(calibration_user_ids)) == 80
    assert metrics["cases_total"] == 80
    assert metrics["http_success_rate"] == 1.0
    assert metrics["trace_coverage_rate"] == 1.0
    assert metrics["cache_hit_rate"] == 0.0
    assert metrics["source_holdout_contract"] == source_contract
    assert metrics["source_holdout_contract"]["runtime_git_sha"] == "1" * 40
    replay = metrics["calibration_replay"]
    assert replay["completed"] is True
    assert replay["status"] == "completed"
    assert replay["integrity_failures"] == []
    assert replay["report_status"] == (
        run_ask_module.CALIBRATION_REPLAY_REPORT_STATUS
    )
    assert replay["calibration_only"] is True
    assert replay["independent_evaluation"] is False
    assert replay["previously_exposed"] is True
    assert replay["product_verdict_eligible"] is False
    assert replay["source_runtime_git_sha"] == "1" * 40
    assert (
        replay["evaluation_runtime_git_sha"]
        == CALIBRATION_REPLAY_RUNTIME_SHA
    )
    assert replay["prior_sealed_exposure_receipts"] == sorted(
        sealed_ledger_before
    )
    assert metrics["trace_cardinality"]["traces_total"] == 80
    assert metrics["trace_cardinality"]["missing_case_ids"] == []
    assert metrics["trace_cardinality"]["duplicate_case_ids"] == []
    assert metrics["trace_cardinality"]["unknown_case_ids"] == []
    assert output_path.is_file()
    markdown = markdown_path.read_text(encoding="utf-8")
    normalized_markdown = " ".join(markdown.split())
    assert "CALIBRATION REPLAY OF A PREVIOUSLY EXPOSED HOLDOUT" in markdown
    assert "not an independent evaluation" in markdown
    assert "must not be reported as product conversion" in normalized_markdown
    assert _ledger_file_bytes(sealed_ledger_dir) == sealed_ledger_before

    started_paths = list(calibration_ledger_dir.glob("*.started.json"))
    completed_paths = list(calibration_ledger_dir.glob("*.completed.json"))
    assert len(started_paths) == 1
    assert len(completed_paths) == 1
    expected_cases_file_sha256 = run_ask_module._file_sha256(cases_path)
    started = json.loads(started_paths[0].read_text(encoding="utf-8"))
    completed = json.loads(completed_paths[0].read_text(encoding="utf-8"))
    for receipt in (started, completed):
        assert receipt["schema_version"] == "1.0.0"
        assert receipt["receipt_type"] == (
            "exposed_holdout_calibration_replay"
        )
        assert receipt["run_mode"] == "calibration_replay"
        assert "runtime_git_sha" not in receipt
        assert receipt["report_status"] == (
            run_ask_module.CALIBRATION_REPLAY_REPORT_STATUS
        )
        assert receipt["calibration_only"] is True
        assert receipt["independent_evaluation"] is False
        assert receipt["previously_exposed"] is True
        assert receipt["product_verdict_eligible"] is False
        assert receipt["source_runtime_git_sha"] == "1" * 40
        assert (
            receipt["evaluation_runtime_git_sha"]
            == CALIBRATION_REPLAY_RUNTIME_SHA
        )
        assert (
            receipt["expected_cases_file_sha256"]
            == expected_cases_file_sha256
        )
    assert started["status"] == "started"
    assert completed["status"] == "completed"
    assert completed["output_sha256"] == run_ask_module._file_sha256(
        output_path
    )

    copied_dir = private_root / "copied-calibration-cases"
    copied_dir.mkdir()
    copied_cases_path = copied_dir / "same-exposed-holdout.json"
    copied_output_path = copied_dir / "calibration-result.json"
    copied_cases_path.write_text(cases_bytes, encoding="utf-8")
    requests_before_replay = list(requests)

    with pytest.raises(ValueError, match="rerun is forbidden"):
        await run_eval(
            cases_path=copied_cases_path,
            output_path=copied_output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            transport=httpx.MockTransport(handler),
            bypass_cache=True,
            allow_model_assisted_prerun=True,
            **_calibration_replay_kwargs(
                calibration_ledger_dir,
                copied_cases_path,
            ),
        )

    assert requests == requests_before_replay
    assert not copied_output_path.exists()
    assert len(list(calibration_ledger_dir.glob("*.rejected.json"))) == 1
    assert _ledger_file_bytes(sealed_ledger_dir) == sealed_ledger_before


@pytest.mark.asyncio
async def test_run_eval_rejects_sealed_and_calibration_modes_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root, cases_dir, sealed_ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    calibration_ledger_dir = (
        private_root
        / run_ask_module.CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    requests: list[httpx.Request] = []

    with pytest.raises(ValueError, match="mutually exclusive"):
        await run_eval(
            cases_path=cases_path,
            output_path=cases_dir / "result.json",
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(500)
            ),
            calibration_replay=True,
            expected_runtime_git_sha=CALIBRATION_REPLAY_RUNTIME_SHA,
            calibration_replay_ledger_dir=calibration_ledger_dir,
            **_sealed_holdout_kwargs(
                sealed_ledger_dir,
                raw_cases,
                cases_path,
            ),
        )

    assert requests == []
    assert not sealed_ledger_dir.exists()
    assert not calibration_ledger_dir.exists()


@pytest.mark.asyncio
async def test_calibration_replay_rejects_unexposed_holdout_before_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root, cases_dir, sealed_ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    calibration_ledger_dir = (
        private_root
        / run_ask_module.CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME
    )
    raw_cases = _private_holdout_cases(
        HOLDOUT_CASE_IDS,
        review_mode=run_ask_module.MODEL_ASSISTED_PRERUN_MODE,
    )
    cases_path = cases_dir / "fresh-unexposed-holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    requests: list[httpx.Request] = []

    with pytest.raises(ValueError, match="sealed.*receipt"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            allow_model_assisted_prerun=True,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(500)
            ),
            **_calibration_replay_kwargs(
                calibration_ledger_dir,
                cases_path,
            ),
        )

    assert requests == []
    assert not output_path.exists()
    assert not sealed_ledger_dir.exists()
    assert not calibration_ledger_dir.exists()


@pytest.mark.parametrize("receipt_status", ["started", "completed"])
@pytest.mark.asyncio
async def test_calibration_receipt_blocks_later_sealed_holdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_status: str,
) -> None:
    private_root, cases_dir, sealed_ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    calibration_ledger_dir = (
        private_root
        / run_ask_module.CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME
    )
    calibration_ledger_dir.mkdir(parents=True)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "sealed-result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    source_contract = dict(raw_cases[0]["holdout_contract"])
    cases_file_sha256 = run_ask_module._file_sha256(cases_path)
    receipt_key = run_ask_module._derive_calibration_replay_receipt_key(
        selected_case_ids_sha256=str(
            source_contract["selected_case_ids_sha256"]
        ),
        cases_file_sha256=cases_file_sha256,
        runtime_git_sha=CALIBRATION_REPLAY_RUNTIME_SHA,
    )
    sealed_receipt_key = run_ask_module._derive_holdout_receipt_key(
        str(source_contract["selected_case_ids_sha256"])
    )
    receipt_path = (
        calibration_ledger_dir / f"{receipt_key}.{receipt_status}.json"
    )
    receipt_kwargs = {
        "contract": source_contract,
        "expected_cases_file_sha256": cases_file_sha256,
        "eval_run_id": "ask-eval-exposed-calibration",
        "receipt_key": receipt_key,
        "output_path": cases_dir / "calibration-result.json",
        "calibration_replay": True,
        "evaluation_runtime_git_sha": CALIBRATION_REPLAY_RUNTIME_SHA,
        "prior_sealed_exposure_receipts": [
            f"{sealed_receipt_key}.started.json"
        ],
    }
    if receipt_status == "started":
        run_ask_module._create_holdout_started_receipt(
            receipt_path,
            cases_path=cases_path,
            **receipt_kwargs,
        )
    else:
        run_ask_module._create_holdout_completed_receipt(
            receipt_path,
            output_sha256="a" * 64,
            **receipt_kwargs,
        )
    calibration_ledger_before = _ledger_file_bytes(
        calibration_ledger_dir
    )
    requests: list[httpx.Request] = []

    with pytest.raises(ValueError, match="calibration.*(?:receipt|exposure)"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(500)
            ),
            **_sealed_holdout_kwargs(
                sealed_ledger_dir,
                raw_cases,
                cases_path,
            ),
        )

    assert requests == []
    assert not output_path.exists()
    assert not sealed_ledger_dir.exists()
    assert (
        _ledger_file_bytes(calibration_ledger_dir)
        == calibration_ledger_before
    )


@pytest.mark.parametrize("runtime_git_sha", [None, "not-a-full-git-sha"])
@pytest.mark.asyncio
async def test_calibration_replay_rejects_missing_or_invalid_runtime_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_git_sha: str | None,
) -> None:
    private_root, cases_dir, sealed_ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    calibration_ledger_dir = (
        private_root
        / run_ask_module.CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    requests: list[httpx.Request] = []

    with pytest.raises(ValueError, match="expected_runtime_git_sha"):
        await run_eval(
            cases_path=cases_path,
            output_path=cases_dir / "result.json",
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(500)
            ),
            **_calibration_replay_kwargs(
                calibration_ledger_dir,
                cases_path,
                runtime_git_sha=runtime_git_sha,
            ),
        )

    assert requests == []
    assert not sealed_ledger_dir.exists()
    assert not calibration_ledger_dir.exists()


@pytest.mark.asyncio
async def test_calibration_replay_rejects_runtime_sha_mismatch_before_ask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root, cases_dir, sealed_ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    calibration_ledger_dir = (
        private_root
        / run_ask_module.CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME
    )
    _install_holdout_trace_stubs(monkeypatch)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    sealed_ledger_before = _seed_sealed_exposure_receipt(
        sealed_ledger_dir,
        cases_path,
        raw_cases[0]["holdout_contract"],
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json=_ready_payload(release_git_sha="8" * 40),
        )

    with pytest.raises(ValueError, match="release_git_sha"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(handler),
            **_calibration_replay_kwargs(
                calibration_ledger_dir,
                cases_path,
            ),
        )

    assert requests == [("GET", "/ready")]
    assert not output_path.exists()
    assert _ledger_file_bytes(sealed_ledger_dir) == sealed_ledger_before
    assert not list(calibration_ledger_dir.glob("*.started.json"))
    assert not list(calibration_ledger_dir.glob("*.completed.json"))
    rejection_paths = list(calibration_ledger_dir.glob("*.rejected.json"))
    assert len(rejection_paths) == 1
    report = json.loads(rejection_paths[0].read_text(encoding="utf-8"))
    replay = report["calibration_replay"]
    assert replay["status"] == "runtime_rejected"
    assert replay["source_runtime_git_sha"] == "1" * 40
    assert (
        replay["evaluation_runtime_git_sha"]
        == CALIBRATION_REPLAY_RUNTIME_SHA
    )
    assert report["source_holdout_contract"] == raw_cases[0][
        "holdout_contract"
    ]


@pytest.mark.asyncio
async def test_calibration_replay_rejects_post_run_readiness_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root, cases_dir, sealed_ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    calibration_ledger_dir = (
        private_root
        / run_ask_module.CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME
    )
    _install_holdout_trace_stubs(monkeypatch)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    markdown_path = cases_dir / "result.md"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    sealed_ledger_before = _seed_sealed_exposure_receipt(
        sealed_ledger_dir,
        cases_path,
        raw_cases[0]["holdout_contract"],
    )
    get_count = 0
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count, post_count
        if request.method == "GET":
            get_count += 1
            return httpx.Response(
                200,
                json=_ready_payload(
                    release_git_sha=(
                        CALIBRATION_REPLAY_RUNTIME_SHA
                        if get_count == 1
                        else "8" * 40
                    )
                ),
            )
        post_count += 1
        return httpx.Response(
            200,
            json={
                "request_id": _request_id_for_case(request),
                "response": "OK",
            },
        )

    with pytest.raises(ValueError, match="release_git_sha"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            markdown_path=markdown_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(handler),
            **_calibration_replay_kwargs(
                calibration_ledger_dir,
                cases_path,
            ),
        )

    assert get_count == 2
    assert post_count == 80
    assert not output_path.exists()
    assert not markdown_path.exists()
    assert _ledger_file_bytes(sealed_ledger_dir) == sealed_ledger_before
    assert len(list(calibration_ledger_dir.glob("*.started.json"))) == 1
    assert not list(calibration_ledger_dir.glob("*.completed.json"))
    rejection_paths = list(calibration_ledger_dir.glob("*.rejected.json"))
    assert len(rejection_paths) == 1
    report = json.loads(rejection_paths[0].read_text(encoding="utf-8"))
    replay = report["calibration_replay"]
    assert replay["status"] == "post_runtime_rejected"
    assert replay["completed"] is False
    assert replay["integrity_failures"] == [
        "post_runtime_ready_check_failed"
    ]


@pytest.mark.parametrize(
    ("failure_mode", "expected_failures"),
    [
        ("cache", {"cache_hit_not_exactly_false"}),
        (
            "duplicate_trace",
            {
                "trace_cardinality_total_mismatch",
                "trace_cardinality_duplicate_case_ids",
            },
        ),
        (
            "missing_unknown_trace",
            {
                "trace_cardinality_missing_case_ids",
                "trace_cardinality_unknown_case_ids",
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_calibration_replay_integrity_failure_writes_only_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expected_failures: set[str],
) -> None:
    private_root, cases_dir, sealed_ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    calibration_ledger_dir = (
        private_root
        / run_ask_module.CANONICAL_CALIBRATION_REPLAY_LEDGER_DIRNAME
    )
    cardinality = {case_id: 1 for case_id in HOLDOUT_CASE_IDS}
    if failure_mode == "duplicate_trace":
        cardinality[HOLDOUT_CASE_IDS[0]] = 2
    elif failure_mode == "missing_unknown_trace":
        cardinality.pop(HOLDOUT_CASE_IDS[-1])
        cardinality["case-unknown"] = 1
    _install_holdout_trace_stubs(
        monkeypatch,
        cache_hit=(
            {HOLDOUT_CASE_IDS[0]: True}
            if failure_mode == "cache"
            else False
        ),
        cardinality_case_counts=cardinality,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    markdown_path = cases_dir / "result.md"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    sealed_ledger_before = _seed_sealed_exposure_receipt(
        sealed_ledger_dir,
        cases_path,
        raw_cases[0]["holdout_contract"],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=_ready_payload(
                    release_git_sha=CALIBRATION_REPLAY_RUNTIME_SHA
                ),
            )
        return httpx.Response(
            200,
            json={
                "request_id": _request_id_for_case(request),
                "response": "OK",
            },
        )

    with pytest.raises(RuntimeError, match="run-integrity"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            markdown_path=markdown_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(handler),
            **_calibration_replay_kwargs(
                calibration_ledger_dir,
                cases_path,
            ),
        )

    assert not output_path.exists()
    assert not markdown_path.exists()
    assert _ledger_file_bytes(sealed_ledger_dir) == sealed_ledger_before
    assert len(list(calibration_ledger_dir.glob("*.started.json"))) == 1
    assert not list(calibration_ledger_dir.glob("*.completed.json"))
    rejection_paths = list(calibration_ledger_dir.glob("*.rejected.json"))
    assert len(rejection_paths) == 1
    report = json.loads(rejection_paths[0].read_text(encoding="utf-8"))
    replay = report["calibration_replay"]
    assert replay["completed"] is False
    assert replay["calibration_only"] is True
    assert replay["independent_evaluation"] is False
    assert replay["product_verdict_eligible"] is False
    assert expected_failures <= set(replay["integrity_failures"])
    assert report["source_holdout_contract"] == raw_cases[0][
        "holdout_contract"
    ]
    if failure_mode == "cache":
        assert report["cache_hit_rate"] == pytest.approx(1 / 80)
        assert sum(
            item["cache_hit"] is True for item in report["results"]
        ) == 1


def test_cli_forwards_calibration_replay_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases_path = tmp_path / "cases.json"
    output_path = tmp_path / "result.json"
    ledger_dir = tmp_path / "calibration-replay-ledger-v1"
    captured: dict[str, object] = {}

    async def fake_run_eval(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "results": [],
            "pass_rate": 1.0,
            "trace_coverage_rate": 1.0,
        }

    monkeypatch.setattr(run_ask_module, "run_eval", fake_run_eval)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval.run_ask",
            "--cases",
            str(cases_path),
            "--output",
            str(output_path),
            "--calibration-replay",
            "--expected-runtime-git-sha",
            CALIBRATION_REPLAY_RUNTIME_SHA,
            "--calibration-replay-ledger-dir",
            str(ledger_dir),
        ],
    )

    run_ask_module.main()

    assert captured["calibration_replay"] is True
    assert (
        captured["expected_runtime_git_sha"]
        == CALIBRATION_REPLAY_RUNTIME_SHA
    )
    assert captured["calibration_replay_ledger_dir"] == ledger_dir


@pytest.mark.asyncio
async def test_run_eval_rejects_inconsistent_private_holdout_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    raw_cases[1]["holdout_contract"] = _holdout_contract(
        HOLDOUT_CASE_IDS,
        freeze_contract_sha256="9" * 64,
    )
    cases_path = cases_dir / "holdout.json"
    cases_path.write_text(
        json.dumps(raw_cases, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="identical holdout_contract"):
        await run_eval(
            cases_path=cases_path,
            output_path=cases_dir / "result.json",
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500)
            ),
        )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"selected_case_ids_sha256": "9" * 64}, "selected_case_ids_sha256"),
        ({"cases_payload_sha256": "9" * 64}, "cases_payload_sha256"),
    ],
)
@pytest.mark.asyncio
async def test_run_eval_rejects_private_holdout_selection_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    error: str,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    case_ids = HOLDOUT_CASE_IDS
    raw_cases = _private_holdout_cases(
        case_ids,
        contract=_holdout_contract(case_ids, **overrides),
    )
    cases_path = cases_dir / "holdout.json"
    cases_path.write_text(
        json.dumps(raw_cases, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=error):
        await run_eval(
            cases_path=cases_path,
            output_path=cases_dir / "result.json",
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500)
            ),
        )


@pytest.mark.asyncio
async def test_run_eval_rejects_mutated_private_holdout_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    monkeypatch.setattr(run_ask_module, "PRIVATE_DATA_ROOT", private_root)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    raw_cases[0]["query"] = "mutated after sealing"
    cases_path = private_root / "holdout.json"
    cases_path.write_text(
        json.dumps(raw_cases, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cases_payload_sha256"):
        await run_eval(
            cases_path=cases_path,
            output_path=private_root / "result.json",
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500)
            ),
        )


@pytest.mark.asyncio
async def test_run_eval_requires_exactly_80_loaded_holdout_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    monkeypatch.setattr(run_ask_module, "PRIVATE_DATA_ROOT", private_root)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS[:-1])
    cases_path = private_root / "holdout.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")

    with pytest.raises(ValueError, match="cases_total"):
        await run_eval(
            cases_path=cases_path,
            output_path=private_root / "result.json",
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500)
            ),
        )


@pytest.mark.asyncio
async def test_run_eval_requires_explicit_sealed_mode_in_both_directions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    holdout_path = cases_dir / "holdout.json"
    holdout_path.write_text(json.dumps(raw_cases), encoding="utf-8")

    with pytest.raises(ValueError, match="required together"):
        await run_eval(
            cases_path=holdout_path,
            output_path=cases_dir / "result.json",
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500)
            ),
        )

    public_cases = tmp_path / "standard.json"
    public_cases.write_text(
        json.dumps([{"id": "standard", "query": "test"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="required together"):
        await run_eval(
            cases_path=public_cases,
            output_path=tmp_path / "standard-result.json",
            sealed_holdout=True,
            expected_holdout_freeze_sha256="2" * 64,
            expected_cases_payload_sha256="3" * 64,
            holdout_ledger_dir=ledger_dir,
            trace_lookup=False,
            api_key_env=None,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500)
            ),
        )


@pytest.mark.parametrize(
    ("override", "value", "error"),
    [
        ("expected_holdout_freeze_sha256", None, "requires expected_holdout"),
        ("expected_holdout_freeze_sha256", "9" * 64, "does not match"),
        ("expected_cases_payload_sha256", None, "requires expected_cases"),
        ("expected_cases_payload_sha256", "9" * 64, "does not match"),
        ("expected_cases_file_sha256", None, "requires expected_cases_file"),
        ("expected_cases_file_sha256", "9" * 64, "does not match"),
        ("holdout_ledger_dir", None, "requires holdout_ledger_dir"),
    ],
)
@pytest.mark.asyncio
async def test_run_eval_rejects_missing_or_mismatched_external_holdout_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str,
    value: object,
    error: str,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    options = _sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path)
    options[override] = value
    requests: list[httpx.Request] = []

    with pytest.raises(ValueError, match=error):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(500)
            ),
            **options,
        )

    assert requests == []
    assert not output_path.exists()
    assert not list(ledger_dir.glob("*.started.json"))


@pytest.mark.asyncio
async def test_external_payload_digest_blocks_self_declared_mutated_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    old_external_digest = raw_cases[0]["holdout_contract"][
        "cases_payload_sha256"
    ]
    raw_cases[0]["query"] = "mutated query"
    raw_cases[0]["expected_chunk_ids"] = ["mutated_chunk"]
    new_self_declared_digest = holdout_cases_payload_sha256(raw_cases)
    for case in raw_cases:
        case["holdout_contract"]["cases_payload_sha256"] = (
            new_self_declared_digest
        )
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    requests: list[httpx.Request] = []

    with pytest.raises(ValueError, match="expected_cases_payload_sha256"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            sealed_holdout=True,
            expected_holdout_freeze_sha256="2" * 64,
            expected_cases_payload_sha256=old_external_digest,
            holdout_ledger_dir=ledger_dir,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(500)
            ),
        )

    assert requests == []
    assert not output_path.exists()
    assert not list(ledger_dir.glob("*.started.json"))


@pytest.mark.parametrize(
    ("contract_field", "mutated_value"),
    [
        ("runtime_git_sha", "9" * 40),
        ("review_manifest_sha256", "9" * 64),
    ],
)
@pytest.mark.asyncio
async def test_external_file_digest_blocks_mutated_holdout_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract_field: str,
    mutated_value: str,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    sealed_options = _sealed_holdout_kwargs(
        ledger_dir,
        raw_cases,
        cases_path,
    )
    for case in raw_cases:
        case["holdout_contract"][contract_field] = mutated_value
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    requests: list[httpx.Request] = []

    with pytest.raises(ValueError, match="expected_cases_file_sha256"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(500)
            ),
            **sealed_options,
        )

    assert requests == []
    assert not output_path.exists()
    assert not list(ledger_dir.glob("*.started.json"))


@pytest.mark.parametrize("existing_artifact", ["output", "markdown"])
@pytest.mark.asyncio
async def test_sealed_holdout_never_overwrites_canonical_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_artifact: str,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    markdown_path = cases_dir / "result.md"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    existing_path = (
        output_path if existing_artifact == "output" else markdown_path
    )
    existing_path.write_text("sentinel", encoding="utf-8")
    requests: list[httpx.Request] = []

    with pytest.raises(FileExistsError, match="must be absent"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            markdown_path=markdown_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(500)
            ),
            **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
        )

    assert existing_path.read_text(encoding="utf-8") == "sentinel"
    assert requests == []
    assert len(list(ledger_dir.glob("*.rejected.json"))) == 1
    assert not list(ledger_dir.glob("*.started.json"))


@pytest.mark.parametrize(
    ("failure_mode", "error"),
    [
        ("sha_mismatch", "release_git_sha"),
        ("unavailable", "runtime /ready check failed"),
    ],
)
@pytest.mark.asyncio
async def test_post_run_ready_failure_rejects_before_canonical_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    error: str,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    _install_holdout_trace_stubs(monkeypatch)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    get_count = 0
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count, post_count
        if request.method == "GET":
            get_count += 1
            if get_count == 2 and failure_mode == "unavailable":
                raise httpx.ConnectError("runtime unavailable", request=request)
            return httpx.Response(
                200,
                json=_ready_payload(
                    release_git_sha=(
                        "1" * 40 if get_count == 1 else "9" * 40
                    )
                ),
            )
        post_count += 1
        return httpx.Response(
            200,
            json={
                "request_id": _request_id_for_case(request),
                "response": "OK",
            },
        )

    with pytest.raises(ValueError, match=error):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(handler),
            **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
        )

    assert get_count == 2
    assert post_count == 80
    assert not output_path.exists()
    report = json.loads(
        next(ledger_dir.glob("*.rejected.json")).read_text(encoding="utf-8")
    )
    assert report["holdout_run"]["status"] == "post_runtime_rejected"
    assert list(ledger_dir.glob("*.started.json"))
    assert not list(ledger_dir.glob("*.completed.json"))


@pytest.mark.parametrize(
    ("trace_lookup", "bypass_cache", "max_cases", "failure"),
    [
        (False, True, None, "trace_lookup_required"),
        (True, False, None, "bypass_cache_required"),
        (True, True, 80, "max_cases_forbidden"),
    ],
)
@pytest.mark.asyncio
async def test_run_eval_writes_preflight_rejection_for_invalid_holdout_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trace_lookup: bool,
    bypass_cache: bool,
    max_cases: int | None,
    failure: str,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    requests: list[httpx.Request] = []

    with pytest.raises(ValueError, match="preflight failed"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=trace_lookup,
            api_key_env=None,
            bypass_cache=bypass_cache,
            max_cases=max_cases,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(500)
            ),
            **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
        )

    rejection_paths = list(ledger_dir.glob("*.rejected.json"))
    assert len(rejection_paths) == 1
    report = json.loads(rejection_paths[0].read_text(encoding="utf-8"))
    assert report["holdout_run"]["completed"] is False
    assert report["holdout_run"]["status"] == "preflight_rejected"
    assert report["holdout_run"]["expected_cases_file_sha256"] == (
        run_ask_module._file_sha256(cases_path)
    )
    assert failure in report["holdout_run"]["integrity_failures"]
    assert requests == []
    assert not output_path.exists()
    assert not list(ledger_dir.glob("*.started.json"))


@pytest.mark.asyncio
async def test_sealed_holdout_rejects_missing_cache_bypass_capability_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    _install_holdout_trace_stubs(monkeypatch)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={"status": "ready", "release_git_sha": "1" * 40},
        )

    with pytest.raises(ValueError, match="cache bypass capability"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(handler),
            **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
        )

    assert requests == [("GET", "/ready")]
    assert not list(ledger_dir.glob("*.started.json"))
    assert not list(ledger_dir.glob("*.completed.json"))
    assert not output_path.exists()
    report = json.loads(
        next(ledger_dir.glob("*.rejected.json")).read_text(encoding="utf-8")
    )
    assert report["holdout_run"]["status"] == "runtime_rejected"
    assert report["holdout_run"]["executed_cases_total"] == 0


@pytest.mark.asyncio
async def test_run_eval_writes_but_rejects_partial_budget_stopped_holdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    monkeypatch.setattr(
        run_ask_module,
        "_llm_cost_rub_total",
        lambda results: 1.0,
    )
    _install_holdout_trace_stubs(monkeypatch)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(
        json.dumps(raw_cases, ensure_ascii=False),
        encoding="utf-8",
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json=_ready_payload(),
            )
        return httpx.Response(
            200,
            json={
                "request_id": _request_id_for_case(request),
                "response": "OK",
            },
        )

    with pytest.raises(RuntimeError, match="run-integrity"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            transport=httpx.MockTransport(handler),
            bypass_cache=True,
            max_llm_cost_rub=0.0,
            **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
        )

    rejection_path = next(ledger_dir.glob("*.rejected.json"))
    report = json.loads(rejection_path.read_text(encoding="utf-8"))
    assert requests == [
        ("GET", "/ready"),
        ("POST", "/ask"),
        ("GET", "/ready"),
    ]
    assert report["holdout_run"]["status"] == "incomplete_budget_stop"
    assert report["holdout_run"]["completed"] is False
    assert report["holdout_run"]["expected_cases_total"] == 80
    assert report["holdout_run"]["executed_cases_total"] == 1
    assert "case_count_incomplete" in report["holdout_run"]["integrity_failures"]
    assert report["llm_budget_stopped"] is True
    assert not output_path.exists()


@pytest.mark.asyncio
async def test_run_eval_rejects_private_holdout_runtime_sha_mismatch_before_ask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    _install_holdout_trace_stubs(monkeypatch)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(
        json.dumps(raw_cases, ensure_ascii=False),
        encoding="utf-8",
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json=_ready_payload(release_git_sha="9" * 40),
        )

    with pytest.raises(ValueError, match="release_git_sha"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(handler),
            **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
        )

    assert requests == [("GET", "/ready")]
    report = json.loads(
        next(ledger_dir.glob("*.rejected.json")).read_text(encoding="utf-8")
    )
    assert report["holdout_run"]["status"] == "runtime_rejected"
    assert report["holdout_run"]["completed"] is False
    assert not output_path.exists()
    assert not list(ledger_dir.glob("*.started.json"))


@pytest.mark.parametrize("receipt_suffix", ["started", "completed"])
@pytest.mark.asyncio
async def test_run_eval_existing_receipt_rejects_holdout_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_suffix: str,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    _install_holdout_trace_stubs(monkeypatch)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    receipt_key = run_ask_module._derive_holdout_receipt_key(
        raw_cases[0]["holdout_contract"]["selected_case_ids_sha256"]
    )
    ledger_dir.mkdir()
    receipt_path = ledger_dir / f"{receipt_key}.{receipt_suffix}.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    receipt_path.write_text('{"status":"started"}', encoding="utf-8")
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json=_ready_payload(),
        )

    with pytest.raises(ValueError, match="rerun is forbidden"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(handler),
            **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
        )

    assert requests == []
    report = json.loads(
        next(ledger_dir.glob("*.rejected.json")).read_text(encoding="utf-8")
    )
    assert report["holdout_run"]["status"] == "rerun_rejected"
    assert report["holdout_run"]["completed"] is False
    assert receipt_path.read_text(encoding="utf-8") == '{"status":"started"}'
    assert not output_path.exists()


@pytest.mark.parametrize(
    ("failure_mode", "expected_status", "expected_failure"),
    [
        ("http", "http_failed", "http_success_below_100_percent"),
        ("trace_missing", "trace_failed", "trace_coverage_below_100_percent"),
        ("trace_error", "trace_failed", "trace_lookup_error"),
        ("trace_record_error", "trace_failed", "trace_error_present"),
        ("binding", "trace_failed", "trace_binding_mismatch"),
        ("cache", "cache_contaminated", "cache_hit_not_exactly_false"),
        ("cache_missing", "cache_contaminated", "cache_hit_not_exactly_false"),
        ("duplicate_request_id", "integrity_failed", "request_ids_not_unique"),
        ("missing_request_id", "trace_failed", "request_ids_missing"),
    ],
)
@pytest.mark.asyncio
async def test_run_eval_writes_and_rejects_holdout_integrity_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expected_status: str,
    expected_failure: str,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    if failure_mode == "trace_missing":
        _install_holdout_trace_stubs(monkeypatch)

        async def missing_trace(
            pool: object,
            request_id: str,
            **kwargs: object,
        ) -> None:
            return None

        monkeypatch.setattr(run_ask_module, "_fetch_trace", missing_trace)
    else:
        _install_holdout_trace_stubs(
            monkeypatch,
            cache_hit=(
                True
                if failure_mode == "cache"
                else None
                if failure_mode == "cache_missing"
                else False
            ),
            trace_error=(
                RuntimeError("trace read failed")
                if failure_mode == "trace_error"
                else None
            ),
            trace_record_error=(
                "trace failed"
                if failure_mode == "trace_record_error"
                else None
            ),
            binding_mismatch=failure_mode == "binding",
        )

    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "GET":
            return httpx.Response(
                200,
                json=_ready_payload(),
            )
        post_count += 1
        return httpx.Response(
            503 if failure_mode == "http" and post_count == 1 else 200,
            json={
                "request_id": (
                    ""
                    if failure_mode == "missing_request_id"
                    else
                    "11111111-1111-1111-1111-111111111111"
                    if failure_mode == "duplicate_request_id"
                    else _request_id_for_case(request)
                ),
                "response": "OK",
            },
        )

    with pytest.raises(RuntimeError, match="run-integrity"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(handler),
            **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
        )

    report = json.loads(
        next(ledger_dir.glob("*.rejected.json")).read_text(encoding="utf-8")
    )
    assert post_count == 80
    assert report["holdout_run"]["completed"] is False
    assert report["holdout_run"]["status"] == expected_status
    assert expected_failure in report["holdout_run"]["integrity_failures"]
    assert not output_path.exists()


@pytest.mark.parametrize(
    ("cardinality_mode", "expected_failures"),
    [
        (
            "missing",
            {
                "trace_cardinality_total_mismatch",
                "trace_cardinality_missing_case_ids",
            },
        ),
        (
            "duplicate",
            {
                "trace_cardinality_total_mismatch",
                "trace_cardinality_duplicate_case_ids",
            },
        ),
        (
            "unknown",
            {
                "trace_cardinality_missing_case_ids",
                "trace_cardinality_unknown_case_ids",
            },
        ),
        ("lookup_error", {"trace_cardinality_lookup_error"}),
    ],
)
@pytest.mark.asyncio
async def test_sealed_holdout_rejects_non_exact_trace_cardinality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cardinality_mode: str,
    expected_failures: set[str],
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    case_counts = {case_id: 1 for case_id in HOLDOUT_CASE_IDS}
    cardinality_error: Exception | None = None
    if cardinality_mode == "missing":
        case_counts.pop(HOLDOUT_CASE_IDS[-1])
    elif cardinality_mode == "duplicate":
        case_counts[HOLDOUT_CASE_IDS[0]] = 2
    elif cardinality_mode == "unknown":
        case_counts.pop(HOLDOUT_CASE_IDS[-1])
        case_counts["case-unknown"] = 1
    else:
        cardinality_error = RuntimeError("cardinality query failed")

    _install_holdout_trace_stubs(
        monkeypatch,
        cardinality_case_counts=case_counts,
        cardinality_error=cardinality_error,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_ready_payload())
        return httpx.Response(
            200,
            json={
                "request_id": _request_id_for_case(request),
                "response": "OK",
            },
        )

    with pytest.raises(RuntimeError, match="run-integrity"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(handler),
            **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
        )

    report = json.loads(
        next(ledger_dir.glob("*.rejected.json")).read_text(encoding="utf-8")
    )
    assert report["holdout_run"]["status"] == "trace_failed"
    assert expected_failures <= set(
        report["holdout_run"]["integrity_failures"]
    )
    assert report["holdout_run"]["completed"] is False
    assert not output_path.exists()
    assert not list(ledger_dir.glob("*.completed.json"))
    if cardinality_error is None:
        assert report["trace_cardinality"]["case_counts"] == dict(
            sorted(case_counts.items())
        )
    else:
        assert report["trace_cardinality"] is None
        assert "RuntimeError" in report["trace_cardinality_error"]


@pytest.mark.asyncio
async def test_run_eval_trace_connection_failure_writes_report_before_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _private_root, cases_dir, ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )

    async def fail_create_pool(*args: object, **kwargs: object) -> None:
        raise OSError("database unavailable")

    monkeypatch.setattr(run_ask_module.asyncpg, "create_pool", fail_create_pool)
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    output_path = cases_dir / "result.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    requests: list[httpx.Request] = []

    with pytest.raises(RuntimeError, match="trace lookup"):
        await run_eval(
            cases_path=cases_path,
            output_path=output_path,
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            trace_dsn="postgresql://trace.test/db",
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(500)
            ),
            **_sealed_holdout_kwargs(ledger_dir, raw_cases, cases_path),
        )

    report = json.loads(
        next(ledger_dir.glob("*.rejected.json")).read_text(encoding="utf-8")
    )
    assert requests == []
    assert report["holdout_run"]["status"] == "trace_unavailable"
    assert report["holdout_run"]["completed"] is False
    assert "trace_lookup_error" in report
    assert not output_path.exists()


@pytest.mark.asyncio
async def test_holdout_ledger_must_be_separate_from_cases_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _private_root, cases_dir, _ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")

    with pytest.raises(ValueError, match="separate private directory tree"):
        await run_eval(
            cases_path=cases_path,
            output_path=cases_dir / "result.json",
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500)
            ),
            **_sealed_holdout_kwargs(cases_dir, raw_cases, cases_path),
        )


@pytest.mark.asyncio
async def test_holdout_ledger_must_use_canonical_persistent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root, cases_dir, _ledger_dir = _holdout_workspace(
        tmp_path,
        monkeypatch,
    )
    raw_cases = _private_holdout_cases(HOLDOUT_CASE_IDS)
    cases_path = cases_dir / "holdout.json"
    cases_path.write_text(json.dumps(raw_cases), encoding="utf-8")
    alternate_ledger = private_root / "alternate-ledger"
    requests: list[httpx.Request] = []

    with pytest.raises(ValueError, match="canonical persistent private"):
        await run_eval(
            cases_path=cases_path,
            output_path=cases_dir / "result.json",
            target="http://app-ml:8000/ask",
            trace_lookup=True,
            api_key_env=None,
            bypass_cache=True,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(500)
            ),
            **_sealed_holdout_kwargs(
                alternate_ledger,
                raw_cases,
                cases_path,
            ),
        )

    assert requests == []
    assert not alternate_ledger.exists()
