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
    assert "Knowledge Base Admin" in enabled.text
    assert "/admin/kb/chunks" in enabled.text


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
            json={"status": "published", "text_clean": "Документы нужно взять по положению."},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "published"
    assert response.json()["text_clean"] == "Документы нужно взять по положению."
    stored = json.loads(seed_path.read_text(encoding="utf-8"))
    assert stored[1]["status"] == "published"
    assert stored[1]["text_clean"] == "Документы нужно взять по положению."


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
