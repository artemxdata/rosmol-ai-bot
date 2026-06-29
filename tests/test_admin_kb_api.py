from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.main import app as fastapi_app


def _write_seed(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "travel",
                    "text_clean": "Проезд оплачивает направляющая сторона.",
                    "status": "published",
                    "category": "форумы",
                    "forum_normalized": "Машук",
                    "source_type": "xlsx",
                    "intent_name": "Оплата проезда",
                },
                {
                    "chunk_id": "draft_docs",
                    "text_clean": "Черновик про документы.",
                    "status": "draft",
                    "category": "форумы",
                    "forum_normalized": "Машук",
                    "source_type": "ticket_answer_bank",
                    "intent_name": "Документы",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_eval_cases(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pre_pilot_forums.json").write_text(
        json.dumps(
            [
                {
                    "id": "case_travel_direct",
                    "query": "Who pays travel?",
                    "expected_behavior": "answer",
                    "expected_chunk_ids": ["travel"],
                    "expected_cited_chunk_ids": ["travel"],
                    "tags": ["pre_pilot", "forums"],
                },
                {
                    "id": "case_travel_equivalent",
                    "query": "Equivalent travel wording",
                    "expected_behavior": "answer",
                    "expected_chunk_ids": ["legacy_travel"],
                    "expected_cited_chunk_ids": ["legacy_travel"],
                    "equivalent_chunk_ids": {"legacy_travel": ["travel"]},
                    "tags": ["pre_pilot", "forums"],
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (path / "pre_pilot_followup.json").write_text(
        json.dumps(
            [
                {
                    "id": "conversation_docs",
                    "turns": [
                        {
                            "id": "case_docs_turn",
                            "query": "Which documents?",
                            "expected_behavior": "answer",
                            "expected_chunk_ids": ["draft_docs"],
                            "expected_cited_chunk_ids": ["draft_docs"],
                            "tags": ["pre_pilot", "followup"],
                        }
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_quality_report(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "passed": True,
                "llm_estimated_cost_rub": 12.34,
                "sections": {"forums": {"pass_rate": 1.0}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_admin_kb_api_is_disabled_without_admin_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="", kb_seed_path=str(seed_path)),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/kb/chunks")

    assert response.status_code == 503
    assert response.json()["detail"] == "Admin API is disabled"


@pytest.mark.asyncio
async def test_admin_kb_api_lists_and_reads_chunks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="admin-secret", kb_seed_path=str(seed_path)),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/admin/kb/chunks")
        listed = await client.get(
            "/admin/kb/chunks",
            params={"status": "published", "q": "проезд"},
            headers={"X-Admin-Token": "admin-secret"},
        )
        fetched = await client.get(
            "/admin/kb/chunks/travel",
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert missing.status_code == 401
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["chunk_id"] == "travel"
    assert fetched.status_code == 200
    assert fetched.json()["text_clean"] == "Проезд оплачивает направляющая сторона."


@pytest.mark.asyncio
async def test_admin_kb_page_requires_enabled_admin_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    transport = httpx.ASGITransport(app=fastapi_app)

    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="", kb_seed_path=str(seed_path)),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        disabled = await client.get("/admin/kb")

    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="admin-secret", kb_seed_path=str(seed_path)),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        enabled = await client.get("/admin/kb")

    assert disabled.status_code == 503
    assert enabled.status_code == 200
    assert "Админка знаний" in enabled.text
    assert "/admin/kb/chunks" in enabled.text
    assert 'id="opsButton"' in enabled.text
    assert 'id="opsDashboard"' in enabled.text
    assert "/admin/kb/ops-report?days=7" in enabled.text
    assert "Работа бота" in enabled.text
    assert "Проблемные темы" in enabled.text
    assert "ожидаемые эскалации" in enabled.text
    assert "проблемы качества" in enabled.text


@pytest.mark.asyncio
async def test_admin_kb_login_sets_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="admin-secret", kb_seed_path=str(seed_path)),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.get("/admin/kb/chunks")
        login = await client.post("/admin/kb/login", json={"token": "admin-secret"})
        authorized = await client.get("/admin/kb/chunks")

    assert unauthorized.status_code == 401
    assert login.status_code == 200
    assert login.json() == {"ok": True}
    assert "rosmol_admin_session" in client.cookies
    assert authorized.status_code == 200
    assert authorized.json()["total"] == 2


@pytest.mark.asyncio
async def test_admin_kb_login_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="admin-secret", kb_seed_path=str(seed_path)),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/admin/kb/login", json={"token": "wrong-secret"})
        authorized = await client.get("/admin/kb/chunks")

    assert login.status_code == 401
    assert "rosmol_admin_session" not in client.cookies
    assert authorized.status_code == 401


@pytest.mark.asyncio
async def test_admin_kb_api_updates_chunk_status_and_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="admin-secret", kb_seed_path=str(seed_path)),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/admin/kb/chunks/draft_docs",
            json={
                "status": "published",
                "text_clean": "Документы нужно взять по положению.",
                "reindex": False,
            },
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert response.status_code == 200
    assert response.json()["record"]["status"] == "published"
    assert response.json()["record"]["text_clean"] == "Документы нужно взять по положению."
    assert response.json()["reindex"] is None
    stored = json.loads(seed_path.read_text(encoding="utf-8"))
    assert stored[1]["status"] == "published"
    assert stored[1]["text_clean"] == "Документы нужно взять по положению."


@pytest.mark.asyncio
async def test_admin_kb_api_reindexes_chunk_after_update_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="admin-secret", kb_seed_path=str(seed_path)),
    )
    captured: dict[str, str] = {}

    async def fake_reindex(_request, record):
        captured["chunk_id"] = record["chunk_id"]
        return {"ok": True, "chunk_id": record["chunk_id"], "collection": "knowledge_base"}

    monkeypatch.setattr("src.main._admin_reindex_record", fake_reindex)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/admin/kb/chunks/draft_docs",
            json={"status": "published"},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert response.status_code == 200
    assert captured == {"chunk_id": "draft_docs"}
    assert response.json()["record"]["status"] == "published"
    assert response.json()["reindex"] == {
        "ok": True,
        "chunk_id": "draft_docs",
        "collection": "knowledge_base",
    }


@pytest.mark.asyncio
async def test_admin_kb_api_reindexes_chunk_on_demand(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="admin-secret", kb_seed_path=str(seed_path)),
    )
    captured: dict[str, str] = {}

    async def fake_reindex(_request, record):
        captured["chunk_id"] = record["chunk_id"]
        return {"ok": True, "chunk_id": record["chunk_id"], "collection": "knowledge_base"}

    monkeypatch.setattr("src.main._admin_reindex_record", fake_reindex)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/kb/chunks/travel/reindex",
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert response.status_code == 200
    assert captured == {"chunk_id": "travel"}
    assert response.json() == {
        "ok": True,
        "chunk_id": "travel",
        "collection": "knowledge_base",
    }


@pytest.mark.asyncio
async def test_admin_kb_api_rejects_invalid_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="admin-secret", kb_seed_path=str(seed_path)),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/admin/kb/chunks/travel",
            json={"status": "deleted"},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert response.status_code == 422
    assert "status must be draft" in response.json()["detail"]


@pytest.mark.asyncio
async def test_admin_kb_api_validates_seed_and_runs_quality_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    report_path = tmp_path / "reports" / "summary.json"
    _write_seed(seed_path)
    _write_quality_report(report_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            admin_auth_token="admin-secret",
            kb_seed_path=str(seed_path),
            admin_quality_report_path=str(report_path),
        ),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        validation = await client.post(
            "/admin/kb/validate",
            headers={"X-Admin-Token": "admin-secret"},
        )
        quality_check = await client.post(
            "/admin/kb/quality-check",
            json={"include_latest_eval_report": True},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert validation.status_code == 200
    assert validation.json()["valid_records"] == 2
    assert validation.json()["status_counts"] == {"draft": 1, "published": 1}
    assert quality_check.status_code == 200
    assert quality_check.json()["validation"]["ok"] is True
    assert quality_check.json()["latest_eval_report_exists"] is True
    assert quality_check.json()["latest_eval_report"]["passed"] is True


@pytest.mark.asyncio
async def test_admin_kb_api_returns_latest_eval_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    report_path = tmp_path / "reports" / "summary.json"
    _write_seed(seed_path)
    _write_quality_report(report_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            admin_auth_token="admin-secret",
            kb_seed_path=str(seed_path),
            admin_quality_report_path=str(report_path),
        ),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/admin/kb/eval-report",
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert response.status_code == 200
    assert response.json()["llm_estimated_cost_rub"] == 12.34


@pytest.mark.asyncio
async def test_admin_kb_api_returns_ops_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="admin-secret", kb_seed_path=str(seed_path)),
    )

    class FakeConn:
        async def fetchrow(self, _query: str, days: int) -> dict[str, object]:
            assert days == 7
            return {
                "request_count": 10,
                "escalated_count": 2,
                "cache_hit_count": 3,
                "avg_latency_ms": 1200,
                "p95_latency_ms": 3400.0,
                "llm_prompt_tokens": 100,
                "llm_completion_tokens": 50,
                "llm_total_tokens": 150,
                "llm_estimated_cost_rub": 1.25,
            }

        async def fetch(self, query: str, days: int, *_args: object) -> list[dict[str, object]]:
            assert days == 7
            if "jsonb_array_elements(llm_usage)" in query:
                return [
                    {
                        "model": "GigaChat/GigaChat-2-Max",
                        "calls": 2,
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150,
                        "estimated_cost_rub": 1.25,
                    }
                ]
            if "routing_hint" in query:
                return [{"complexity": "complex", "reason": "multi_aspect", "requests": 4}]
            if "ANY($2::text[])" in query:
                if "NOT (" in query:
                    return [{"reason": "partial_source_coverage", "requests": 1}]
                return [{"reason": "operator_requested", "requests": 1}]
            if "question->>'topic'" in query:
                return [
                    {
                        "topic": "oplata_proezda",
                        "forum": "Амур",
                        "reason": "partial_source_coverage",
                        "requests": 2,
                    }
                ]
            if "message_preview" in query:
                return [
                    {
                        "timestamp": "2026-06-29 12:00:00+00",
                        "channel": "api",
                        "forum": "Амур",
                        "reason": "partial_source_coverage",
                        "message_preview": "Сложный вопрос",
                        "response_preview": "Передаю специалисту",
                        "total_latency_ms": 1200,
                    }
                ]
            if "query_analysis->>'forum_normalized'" in query:
                return [{"forum": "Амур", "reason": "partial_source_coverage", "requests": 2}]
            return [{"reason": "operator_requested", "requests": 2}]

    class FakeAcquire:
        async def __aenter__(self) -> FakeConn:
            return FakeConn()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakePool:
        def acquire(self) -> FakeAcquire:
            return FakeAcquire()

    monkeypatch.setattr(fastapi_app.state, "pg_pool", FakePool(), raising=False)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.get("/admin/kb/ops-report")
        response = await client.get(
            "/admin/kb/ops-report",
            params={"days": 7},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    data = response.json()
    assert data["days"] == 7
    assert data["summary"]["request_count"] == 10
    assert data["summary"]["escalation_rate"] == 0.2
    assert data["summary"]["expected_escalation_rate"] == 0.1
    assert data["summary"]["quality_issue_rate"] == 0.1
    assert data["summary"]["cache_hit_rate"] == 0.3
    assert data["model_usage"][0]["model"] == "GigaChat/GigaChat-2-Max"
    assert data["routing"][0]["reason"] == "multi_aspect"
    assert data["escalations"][0]["reason"] == "operator_requested"
    assert data["expected_escalations"][0]["reason"] == "operator_requested"
    assert data["quality_issue_escalations"][0]["reason"] == "partial_source_coverage"
    assert data["failed_topics"][0]["topic"] == "oplata_proezda"
    assert data["failed_forums"][0]["forum"] == "Амур"
    assert data["recent_escalations"][0]["message_preview"] == "Сложный вопрос"


@pytest.mark.asyncio
async def test_admin_kb_api_returns_related_eval_cases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    cases_dir = tmp_path / "cases"
    _write_seed(seed_path)
    _write_eval_cases(cases_dir)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            admin_auth_token="admin-secret",
            kb_seed_path=str(seed_path),
            admin_eval_cases_dir=str(cases_dir),
        ),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        travel = await client.get(
            "/admin/kb/chunks/travel/eval-cases",
            headers={"X-Admin-Token": "admin-secret"},
        )
        docs = await client.get(
            "/admin/kb/chunks/draft_docs/eval-cases",
            headers={"X-Admin-Token": "admin-secret"},
        )
        missing = await client.get(
            "/admin/kb/chunks/missing/eval-cases",
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert travel.status_code == 200
    assert travel.json()["total"] == 2
    assert {item["id"] for item in travel.json()["items"]} == {
        "case_travel_direct",
        "case_travel_equivalent",
    }
    assert docs.status_code == 200
    assert docs.json()["total"] == 1
    assert docs.json()["items"][0]["conversation_id"] == "conversation_docs"
    assert missing.status_code == 404
