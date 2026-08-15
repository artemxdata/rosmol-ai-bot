from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from eval import run_gold_ticket as runner
from eval.gold_ticket import seal_gold_ticket
from eval.run_gold_ticket import (
    build_replay_plan,
    load_gold_tickets,
    run_gold_ticket_eval,
    validate_knowledge_snapshot,
)


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _seed_payload() -> list[dict[str, object]]:
    return [
        {
            "id": "yonote-source-a",
            "source_type": "yonote",
            "published": True,
            "text": "Published source text",
        }
    ]


def _ticket_payload(*, seed_sha256: str, ticket_marker: str = "a") -> dict[str, object]:
    return {
        "schema_version": "gold-ticket.v1",
        "dataset_id": "blind50_product",
        "ticket_id_hash": ticket_marker * 24,
        "duplicate_component_id": ticket_marker * 12,
        "split": "holdout",
        "measurement_unit": "full_ticket",
        "source_binding": {
            "artifact_manifest_sha256": "1" * 64,
            "normalized_source_sha256": "2" * 64,
            "source_record_fingerprint": "3" * 64,
            "available_at": "2026-08-01T00:00:00Z",
            "source_channel": "hde",
        },
        "turns": [
            {
                "turn_id": "t001",
                "source_turn_index": 0,
                "role_candidate": "user",
                "reviewed_role": "user",
                "assistant_kind": None,
                "text_deidentified": "Расскажи о форуме",
                "include_in_replay": True,
                "privacy_verdict": "approved",
            },
            {
                "turn_id": "t002",
                "source_turn_index": 1,
                "role_candidate": "user",
                "reviewed_role": "user",
                "assistant_kind": None,
                "text_deidentified": "Когда он проходит?",
                "include_in_replay": True,
                "privacy_verdict": "approved",
            },
            {
                "turn_id": "t003",
                "source_turn_index": 2,
                "role_candidate": "user",
                "reviewed_role": "user",
                "assistant_kind": None,
                "text_deidentified": "Какие нужны документы?",
                "include_in_replay": True,
                "privacy_verdict": "approved",
            },
        ],
        "evaluation_steps": [
            _step(
                step_id="s001",
                user_turn_ids=["t001", "t002"],
                history_turn_ids=[],
                aspect="dates",
                claim_id="c001",
                chunk_id="yonote-date",
            ),
            _step(
                step_id="s002",
                user_turn_ids=["t003"],
                history_turn_ids=["t001", "t002"],
                aspect="documents",
                claim_id="c002",
                chunk_id="yonote-documents",
            ),
        ],
        "expected_ticket_outcome": "bot_resolved_multi_turn",
        "operator_evidence": {
            "available": False,
            "behavior_tags": [],
            "used_as_factual_truth": False,
        },
        "review_provenance": {
            "status": "human_reviewed",
            "primary_reviewer_id": "reviewer-a",
            "primary_reviewed_at": "2026-08-15T00:00:00Z",
            "secondary_reviewer_id": None,
            "secondary_reviewed_at": None,
            "disagreement": False,
            "adjudicator_id": None,
            "adjudicated_at": None,
        },
        "privacy_provenance": {
            "status": "approved",
            "scanner": "pii-masker-v1",
            "reviewer_id": "privacy-a",
            "reviewed_at": "2026-08-15T00:00:00Z",
            "raw_text_exported": False,
        },
        "knowledge_snapshot": {
            "canonical_seed_sha256": seed_sha256,
            "published_yonote_chunks": 1,
            "source_type": "yonote",
        },
    }


def _step(
    *,
    step_id: str,
    user_turn_ids: list[str],
    history_turn_ids: list[str],
    aspect: str,
    claim_id: str,
    chunk_id: str,
) -> dict[str, object]:
    return {
        "step_id": step_id,
        "user_turn_ids": user_turn_ids,
        "history_turn_ids": history_turn_ids,
        "expected_action": "answer",
        "expected_escalation_reason": None,
        "intents": [f"forums.{aspect}"],
        "entities": [],
        "requested_aspects": [aspect],
        "constraints": [],
        "answerability": "full",
        "missing_aspects": [],
        "qrels": [
            {
                "chunk_id": chunk_id,
                "grade": 3,
                "supports_claim_ids": [claim_id],
                "source_span": {
                    "start": 0,
                    "end": 10,
                    "sha256": "4" * 64,
                },
            }
        ],
        "expected_claims": [
            {
                "claim_id": claim_id,
                "aspect": aspect,
                "predicate": f"event_{aspect}",
                "value_normalized": f"approved {aspect}",
                "qualifiers": {},
                "polarity": "positive",
                "modality": "fact",
                "required": True,
                "critical": False,
            }
        ],
        "forbidden_profiles": [],
    }


def _write_fixture(
    tmp_path: Path,
    *,
    payload: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    seed = _seed_payload()
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    ticket_payload = payload or _ticket_payload(seed_sha256=_canonical_sha256(seed))
    sealed = seal_gold_ticket(ticket_payload).model_dump(mode="json")
    tickets_path = tmp_path / "tickets.jsonl"
    tickets_path.write_text(
        json.dumps(sealed, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return tickets_path, seed_path


def test_loader_requires_sealed_holdout_and_detects_tampering(tmp_path: Path) -> None:
    tickets_path, _seed_path = _write_fixture(tmp_path)
    loaded = load_gold_tickets(tickets_path)

    assert len(loaded) == 1
    payload = json.loads(tickets_path.read_text(encoding="utf-8"))
    payload["turns"][0]["text_deidentified"] = "mutated"
    tickets_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="sealed GoldTicket"):
        load_gold_tickets(tickets_path)


def test_replay_plan_preserves_order_and_uses_last_current_turn_as_anchor(
    tmp_path: Path,
) -> None:
    tickets_path, _seed_path = _write_fixture(tmp_path)
    ticket = load_gold_tickets(tickets_path)[0]

    plan = build_replay_plan(ticket)

    assert [call.source_turn_id for call in plan] == ["t001", "t002", "t003"]
    assert [call.step_id for call in plan] == [None, "s001", "s002"]
    assert [call.scored for call in plan] == [False, True, True]


def test_replay_plan_rejects_non_chronological_evaluation_steps(tmp_path: Path) -> None:
    seed = _seed_payload()
    payload = _ticket_payload(seed_sha256=_canonical_sha256(seed))
    steps = payload["evaluation_steps"]
    assert isinstance(steps, list)
    payload["evaluation_steps"] = [deepcopy(steps[1]), deepcopy(steps[0])]
    tickets_path, _seed_path = _write_fixture(tmp_path, payload=payload)
    ticket = load_gold_tickets(tickets_path)[0]

    with pytest.raises(ValueError, match="strictly chronological"):
        build_replay_plan(ticket)


def test_knowledge_snapshot_is_bound_to_canonical_seed(tmp_path: Path) -> None:
    tickets_path, seed_path = _write_fixture(tmp_path)
    tickets = load_gold_tickets(tickets_path)

    snapshot = validate_knowledge_snapshot(tickets, seed_path)

    assert snapshot["published_yonote_chunks"] == 1
    seed_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        validate_knowledge_snapshot(tickets, seed_path)


@pytest.mark.asyncio
async def test_runner_replays_one_session_and_separates_private_from_safe_output(
    tmp_path: Path,
) -> None:
    tickets_path, seed_path = _write_fixture(tmp_path)
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "request_id": f"00000000-0000-0000-0000-{len(requests):012d}",
                "response": "Подтверждённый ответ",
            },
        )

    private_output = tmp_path / "private.json"
    safe_output = tmp_path / "safe.json"
    safe = await run_gold_ticket_eval(
        tickets_path=tickets_path,
        private_output_path=private_output,
        safe_output_path=safe_output,
        kb_seed_path=seed_path,
        transport=httpx.MockTransport(handler),
        trace_lookup=False,
    )

    assert [request["text"] for request in requests] == [
        "Расскажи о форуме",
        "Когда он проходит?",
        "Какие нужны документы?",
    ]
    assert len({request["user_id"] for request in requests}) == 1
    private = json.loads(private_output.read_text(encoding="utf-8"))
    assert private["observations_total"] == 2
    assert private["context_calls_executed"] == 1
    assert [row["step_id"] for row in private["observations"]] == ["s001", "s002"]
    assert safe["status"] == "OK"
    safe_text = safe_output.read_text(encoding="utf-8")
    assert "Подтверждённый ответ" not in safe_text
    assert "Какие нужны документы" not in safe_text
    assert "observations" not in safe


@pytest.mark.asyncio
async def test_live_runner_requires_owner_approval_before_network(tmp_path: Path) -> None:
    tickets_path, seed_path = _write_fixture(tmp_path)

    with pytest.raises(ValueError, match="owner approval"):
        await run_gold_ticket_eval(
            tickets_path=tickets_path,
            private_output_path=tmp_path / "private.json",
            safe_output_path=tmp_path / "safe.json",
            kb_seed_path=seed_path,
            expected_runtime_git_sha="a" * 40,
            high_cost_approval_id=None,
        )


@pytest.mark.asyncio
async def test_runtime_identity_is_checked_before_cost_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tickets_path, seed_path = _write_fixture(tmp_path)
    calls: list[str] = []

    class FakePool:
        async def close(self) -> None:
            calls.append("trace_pool_closed")

    async def fake_open_trace_pool(_trace_dsn: str | None) -> FakePool:
        return FakePool()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "release_git_sha": "b" * 40,
                "eval_cache_bypass": {
                    "scheme": runner.eval_cache_bypass.SCHEME,
                    "authorized": True,
                },
            },
        )

    monkeypatch.setattr(runner, "_is_in_process_mock_transport", lambda _value: False)
    monkeypatch.setattr(runner, "_local_llm_pricing_preflight_failure", lambda: None)
    monkeypatch.setattr(runner, "_open_trace_pool", fake_open_trace_pool)
    monkeypatch.setattr(
        runner,
        "_auth_headers",
        lambda _env: {"Content-Type": "application/json", "X-API-Key": "test-key"},
    )
    monkeypatch.setattr(
        runner,
        "reserve_live_eval_cost",
        lambda **_kwargs: pytest.fail("reservation must follow runtime identity"),
    )

    with pytest.raises(ValueError, match="release_git_sha"):
        await run_gold_ticket_eval(
            tickets_path=tickets_path,
            private_output_path=tmp_path / "private.json",
            safe_output_path=tmp_path / "safe.json",
            kb_seed_path=seed_path,
            expected_runtime_git_sha="a" * 40,
            high_cost_approval_id="owner-test-blind50",
            transport=httpx.MockTransport(handler),
        )

    assert calls == ["/ready", "trace_pool_closed"]
