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
