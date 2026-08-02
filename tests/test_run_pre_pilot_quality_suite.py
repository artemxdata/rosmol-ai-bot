from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from eval import run_pre_pilot_quality_suite as suite


def _reviewed_ticket_followup() -> list[dict[str, object]]:
    return [
        {
            "schema_version": "2.0.0",
            "id": "ticket::reviewed",
            "label_status": "human_reviewed",
            "requires_human_review": False,
            "turns": [
                {
                    "id": "ticket::reviewed::t001",
                    "query": "Когда проходит форум?",
                    "expected_behavior": "answer",
                    "label_status": "human_reviewed",
                    "requires_human_review": False,
                }
            ],
        }
    ]


def _write_followup_payload(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_followup_loader_rejects_weak_role_reconstruction(
    tmp_path: Path,
) -> None:
    cases_path = tmp_path / "weak_followup.json"
    cases_path.write_text(
        json.dumps(
            [
                {
                    "id": "ticket::weak",
                    "label_status": "weak_unreviewed",
                    "requires_human_review": True,
                    "turns": [
                        {
                            "id": "ticket::weak::t001",
                            "query": "Когда проходит форум?",
                            "predicted_behavior": "answer",
                            "requires_human_review": True,
                        }
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requiring human review"):
        suite._load_followup_cases(cases_path)


def test_followup_loader_accepts_explicitly_reviewed_ticket_schema(
    tmp_path: Path,
) -> None:
    cases_path = tmp_path / "reviewed_followup.json"
    payload = _reviewed_ticket_followup()
    _write_followup_payload(cases_path, payload)

    assert suite._load_followup_cases(cases_path) == payload


@pytest.mark.parametrize(
    ("target", "field", "unsafe_value"),
    (
        ("conversation", "label_status", "Weak_Unreviewed "),
        ("conversation", "label_status", "HUMAN_REVIEWED"),
        ("conversation", "label_status", "human_reviewed "),
        ("conversation", "requires_human_review", "false"),
        ("conversation", "requires_human_review", 0),
        ("turn", "label_status", " weak_unreviewed"),
        ("turn", "label_status", "Human_Reviewed"),
        ("turn", "requires_human_review", "true"),
        ("turn", "requires_human_review", 0),
    ),
)
def test_followup_loader_rejects_review_state_type_case_and_space_bypasses(
    tmp_path: Path,
    target: str,
    field: str,
    unsafe_value: object,
) -> None:
    cases_path = tmp_path / "unsafe_followup.json"
    payload = _reviewed_ticket_followup()
    record = payload[0] if target == "conversation" else payload[0]["turns"][0]
    assert isinstance(record, dict)
    record[field] = unsafe_value
    _write_followup_payload(cases_path, payload)

    with pytest.raises(ValueError, match="requiring human review"):
        suite._load_followup_cases(cases_path)


@pytest.mark.parametrize(
    ("target", "field"),
    (
        ("conversation", "label_status"),
        ("conversation", "requires_human_review"),
        ("turn", "label_status"),
        ("turn", "requires_human_review"),
    ),
)
def test_followup_loader_requires_explicit_review_state_fields(
    tmp_path: Path,
    target: str,
    field: str,
) -> None:
    cases_path = tmp_path / "missing_review_state.json"
    payload = _reviewed_ticket_followup()
    record = payload[0] if target == "conversation" else payload[0]["turns"][0]
    assert isinstance(record, dict)
    record.pop(field)
    _write_followup_payload(cases_path, payload)

    with pytest.raises(ValueError, match="requiring human review"):
        suite._load_followup_cases(cases_path)


@pytest.mark.parametrize(
    "relative_path",
    (
        Path("eval/cases/pre_pilot_followup.json"),
        Path("eval/cases/dialog_memory_regression.json"),
    ),
)
def test_followup_loader_preserves_explicit_legacy_v1_allowlist(
    relative_path: Path,
) -> None:
    path = Path(__file__).resolve().parents[1] / relative_path

    loaded = suite._load_followup_cases(path)

    assert loaded
    assert {str(item["id"]) for item in loaded}.issubset(
        suite.LEGACY_FOLLOWUP_SCHEMAS_V1
    )


def test_followup_loader_rejects_unversioned_non_allowlisted_legacy_shape(
    tmp_path: Path,
) -> None:
    cases_path = tmp_path / "unknown_legacy.json"
    _write_followup_payload(
        cases_path,
        [
            {
                "id": "legacy_like_but_not_allowlisted",
                "turns": [
                    {
                        "id": "legacy_like_but_not_allowlisted_t1",
                        "query": "Когда проходит форум?",
                    }
                ],
            }
        ],
    )

    with pytest.raises(ValueError, match="requiring human review"):
        suite._load_followup_cases(cases_path)


def test_followup_loader_rejects_copied_legacy_ids_outside_allowlisted_file(
    tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[1] / "eval/cases/pre_pilot_followup.json"
    copied = tmp_path / "copied_legacy.json"
    copied.write_bytes(source.read_bytes())

    with pytest.raises(ValueError, match="requiring human review"):
        suite._load_followup_cases(copied)


def test_compact_stdout_summary_excludes_case_results() -> None:
    compact = suite._compact_stdout_summary(
        {
            "passed": True,
            "release_run_id": "quality-run",
            "expected_git_sha": "a" * 40,
            "completed_sections": ["forums"],
            "llm_estimated_cost_rub": 1.25,
            "sections": {
                "forums": {"trace_coverage_rate": 1.0, "results": ["private"]}
            },
        }
    )

    assert compact["trace_coverage"] == {"forums": 1.0}
    assert "sections" not in compact
    assert "results" not in json.dumps(compact)
    assert "private" not in json.dumps(compact)


@pytest.mark.parametrize(
    ("target", "expected"),
    (
        ("http://localhost:8001/ask", True),
        ("http://127.0.0.1:18001/ask", True),
        ("http://[::1]:8001/ask", True),
        ("http://app-ml:8000/ask", True),
        ("https://app-ml:8000/ask", False),
        ("http://app-ml:8001/ask", False),
        ("http://user:secret@localhost:8001/ask", False),
        ("http://public.example.test/ask", False),
        ("http://localhost:8001/ask?copy=1", False),
    ),
)
def test_quality_target_is_restricted_to_local_runtime(
    target: str,
    expected: bool,
) -> None:
    assert suite._valid_quality_target(target) is expected


@pytest.mark.asyncio
async def test_run_pre_pilot_quality_suite_writes_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    custom_followup_cases = tmp_path / "dialog_memory_regression.json"
    custom_followup_cases.write_text("[]", encoding="utf-8")
    captured_followup_path: Path | None = None

    async def fake_run_ask_eval(**kwargs):
        kwargs["output_path"].write_text("{}", encoding="utf-8")
        kwargs["markdown_path"].write_text("# ask\n", encoding="utf-8")
        return {
            "cases_total": 2,
            "pass_rate": 1.0,
            "http_success_rate": 1.0,
            "behavior_match_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "expected_or_equivalent_chunk_hit_rate": 1.0,
            "llm_estimated_cost_rub": 0.5,
            "generator_model_counts": {"source_chunk": 1, "GigaChat/GigaChat-2-Max": 1},
            "failure_reason_counts": {},
            "eval_run_id": "ask-eval-test",
            "results": [],
        }

    async def fake_followup_eval(**kwargs):
        nonlocal captured_followup_path
        captured_followup_path = kwargs["cases_path"]
        kwargs["output_path"].write_text("{}", encoding="utf-8")
        kwargs["markdown_path"].write_text("# followup\n", encoding="utf-8")
        return {
            "turns_total": 2,
            "conversations_total": 1,
            "conversations_executed": 1,
            "turn_pass_rate": 1.0,
            "conversation_pass_rate": 1.0,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "expected_or_equivalent_chunk_hit_rate": 1.0,
            "llm_estimated_cost_rub": 0.25,
            "generator_model_counts": {"source_chunk": 2},
            "failure_reason_counts": {},
            "eval_run_id": "followup-eval-test",
            "results": [],
        }

    def fake_release_provenance(**kwargs):
        return {
            "release_run_id": kwargs["release_run_id"],
            "target": kwargs["target"],
            "git_sha": "a" * 40,
            "expected_git_sha": kwargs.get("expected_git_sha"),
            "git_worktree_clean": True,
            "kb_seed": {"sha256": "b" * 64},
            "case_files": {
                name: {"sha256": "c" * 64} for name in kwargs["case_paths"]
            },
            "complete": True,
            "errors": [],
        }

    monkeypatch.setattr(suite, "run_ask_eval", fake_run_ask_eval)
    monkeypatch.setattr(suite, "run_followup_eval", fake_followup_eval)
    monkeypatch.setattr(suite, "build_release_provenance", fake_release_provenance)

    summary = await suite.run_pre_pilot_quality_suite(
        output_dir=tmp_path / "out",
        cases_dir=tmp_path / "cases",
        kb_seed_path=Path("data/knowledge_base_seed.json"),
        sections=("forums", "pii", "followup"),
        followup_cases_path=custom_followup_cases,
        max_llm_cost_rub=10.0,
        expected_git_sha="a" * 40,
    )

    assert summary["passed"] is True
    assert summary["completed_sections"] == ["forums", "pii", "followup"]
    assert summary["llm_estimated_cost_rub"] == 1.25
    assert summary["sections_complete"] is True
    assert summary["trace_required"] is True
    assert summary["release_run_id"].startswith("pre-pilot-")
    assert summary["expected_git_sha"] == "a" * 40
    assert summary["provenance"]["complete"] is True
    assert len(summary["provenance"]["git_sha"]) == 40
    assert set(summary["provenance"]["case_files"]) == {"forums", "pii", "followup"}
    assert captured_followup_path == custom_followup_cases
    assert (tmp_path / "out" / "summary.md").exists()
    stored = json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    assert stored["sections"]["followup"]["turn_pass_rate"] == 1.0


@pytest.mark.asyncio
async def test_run_pre_pilot_quality_suite_stops_on_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_run_ask_eval(**kwargs):
        return {
            "cases_total": 2,
            "pass_rate": 1.0,
            "http_success_rate": 1.0,
            "trace_coverage_rate": 1.0,
            "llm_estimated_cost_rub": 2.0,
            "results": [],
        }

    monkeypatch.setattr(suite, "run_ask_eval", fake_run_ask_eval)
    monkeypatch.setattr(
        suite,
        "build_release_provenance",
        lambda **kwargs: {
            "release_run_id": kwargs["release_run_id"],
            "target": kwargs["target"],
            "git_sha": "a" * 40,
            "expected_git_sha": kwargs.get("expected_git_sha"),
            "git_worktree_clean": True,
            "kb_seed": {"sha256": "b" * 64},
            "case_files": {},
            "complete": True,
            "errors": [],
        },
    )

    summary = await suite.run_pre_pilot_quality_suite(
        output_dir=tmp_path / "out",
        cases_dir=tmp_path / "cases",
        kb_seed_path=Path("data/knowledge_base_seed.json"),
        sections=("forums", "pii"),
        max_llm_cost_rub=1.0,
    )

    assert summary["passed"] is False
    assert summary["completed_sections"] == ["forums"]
    assert summary["llm_budget_stopped"] is True


@pytest.mark.asyncio
async def test_incomplete_provenance_stops_before_network_sections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def unexpected_eval(**kwargs):
        pytest.fail(f"network eval must not run: {kwargs}")

    monkeypatch.setattr(suite, "run_ask_eval", unexpected_eval)
    monkeypatch.setattr(suite, "run_followup_eval", unexpected_eval)
    monkeypatch.setattr(
        suite,
        "build_release_provenance",
        lambda **kwargs: {
            "release_run_id": kwargs["release_run_id"],
            "target": kwargs["target"],
            "git_sha": None,
            "expected_git_sha": kwargs.get("expected_git_sha"),
            "git_worktree_clean": None,
            "kb_seed": None,
            "case_files": {},
            "complete": False,
            "errors": ["git_sha_unavailable"],
        },
    )

    summary = await suite.run_pre_pilot_quality_suite(
        output_dir=tmp_path / "out",
        cases_dir=tmp_path / "cases",
        kb_seed_path=Path("data/knowledge_base_seed.json"),
        sections=("forums",),
        expected_git_sha="a" * 40,
    )

    assert summary["passed"] is False
    assert summary["completed_sections"] == []
    assert summary["provenance"]["errors"] == ["git_sha_unavailable"]
    assert (tmp_path / "out" / "summary.json").exists()


def test_followup_section_requires_conversation_pass_rate() -> None:
    assert suite._section_passed(
        "followup",
        {
            "turns_total": 2,
            "conversations_total": 1,
            "conversations_executed": 1,
            "turn_pass_rate": 0.9375,
            "conversation_pass_rate": 0.75,
            "trace_coverage_rate": 1.0,
        },
    ) is False
    assert suite._section_passed(
        "followup",
        {
            "turns_total": 2,
            "conversations_total": 1,
            "conversations_executed": 1,
            "turn_pass_rate": 0.9,
            "conversation_pass_rate": 0.9,
            "trace_coverage_rate": 1.0,
        },
    ) is True


def test_release_sections_fail_closed_for_empty_safety_and_missing_trace() -> None:
    with pytest.raises(ValueError, match="At least one"):
        suite._validate_sections(())

    assert suite._section_passed(
        "forums",
        {"cases_total": 0, "pass_rate": 1.0, "trace_coverage_rate": 1.0},
    ) is False
    assert suite._section_passed(
        "safety",
        {"cases_total": 16, "pass_rate": 0.99, "trace_coverage_rate": 1.0},
    ) is False
    assert suite._section_passed(
        "forums",
        {"cases_total": 16, "pass_rate": 1.0, "trace_coverage_rate": 0.99},
    ) is False


def test_release_summary_requires_every_requested_section() -> None:
    summary = suite._build_summary(
        output_dir=Path("reports/test"),
        cases_dir=Path("eval/cases"),
        target="http://localhost:8001/ask",
        sections=("forums", "safety"),
        section_reports={
            "forums": {
                "cases_total": 1,
                "pass_rate": 1.0,
                "trace_coverage_rate": 1.0,
            }
        },
        max_llm_cost_rub=1.0,
        stopped_by_budget=False,
        release_run_id="release-test",
        trace_required=True,
        provenance={"complete": True},
    )

    assert summary["passed"] is False
    assert summary["sections_complete"] is False
    assert summary["completed_sections"] == ["forums"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    (
        "http://app-ml:8000/ask",
        "http://rosmol-app-ml:8000/ask",
        "http://172.20.0.9:8000/ask",
    ),
)
async def test_non_loopback_followup_rejects_old_runtime_before_first_ask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "test-eval-secret")
    cases_path = tmp_path / "reviewed_followup.json"
    output_path = tmp_path / "followup_result.json"
    _write_followup_payload(cases_path, _reviewed_ticket_followup())
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
        await suite.run_followup_eval(
            cases_path=cases_path,
            output_path=output_path,
            target=target,
            trace_lookup=False,
            bypass_cache=True,
            transport=httpx.MockTransport(handler),
        )

    assert requests == [("GET", "/ready")]
    assert not output_path.exists()


@pytest.mark.asyncio
async def test_followup_turn_sends_eval_trace_headers() -> None:
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        return httpx.Response(
            200,
            json={"request_id": "request-1", "response": "Тестовый ответ"},
        )

    case = suite._normalize_case(
        {
            "id": "followup-turn-1",
            "query": "Когда начинается мероприятие?",
            "user_id": "eval-user",
            "channel": "api",
            "expected_behavior": "answer",
            "expected_escalated": False,
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await suite._run_followup_turn(
            client=client,
            target="http://test/ask",
            headers={"X-Eval-Run-Id": "followup-eval-test"},
            case=case,
            trace_pool=None,
            conversation_id="conversation-1",
        )

    assert captured_headers["x-eval-run-id"] == "followup-eval-test"
    assert captured_headers["x-eval-case-id"] == "followup-turn-1"
    assert result["conversation_id"] == "conversation-1"


@pytest.mark.asyncio
async def test_followup_turn_signs_server_local_cache_bypass_payload() -> None:
    captured_headers: dict[str, str] = {}
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        captured_payload.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"request_id": "request-1", "response": "Тестовый ответ"},
        )

    case = suite._normalize_case(
        {
            "id": "followup-turn-signed",
            "query": "Когда начинается мероприятие?",
            "user_id": "eval-user",
            "channel": "api",
            "expected_behavior": "answer",
            "expected_escalated": False,
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await suite._run_followup_turn(
            client=client,
            target="http://app-ml:8000/ask",
            headers={
                "X-Eval-Run-Id": "followup-eval-test",
                "X-Bypass-Cache": "1",
            },
            case=case,
            trace_pool=None,
            conversation_id="conversation-1",
            cache_bypass_secret="test-eval-secret",
        )

    timestamp = captured_headers["x-eval-cache-bypass-timestamp"]
    nonce = captured_headers["x-eval-cache-bypass-nonce"]
    expected_signature = suite.eval_cache_bypass.signature(
        "test-eval-secret",
        method="POST",
        path="/ask",
        eval_run_id="followup-eval-test",
        eval_case_id="followup-turn-signed",
        timestamp=timestamp,
        nonce=nonce,
        payload_sha256=suite.eval_cache_bypass.canonical_payload_sha256(
            suite.eval_cache_bypass.canonical_ask_payload(captured_payload)
        ),
    )
    assert (
        captured_headers["x-eval-cache-bypass-version"]
        == suite.eval_cache_bypass.SCHEME
    )
    assert (
        captured_headers["x-eval-cache-bypass-signature"]
        == expected_signature
    )
