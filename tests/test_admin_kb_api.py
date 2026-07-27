from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from scripts.sync_yonote_kb import (
    YonoteApiError,
    YonoteDataTooLarge,
    YonoteOperationTimeout,
)
from src.admin import ui
from src.admin.yonote_database import YonoteDatabaseExportTooLarge
from src.main import _admin_reindex_record
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
async def test_admin_reindex_invalidates_keyword_and_semantic_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidated_keyword_sources: list[str | None] = []
    invalidated_forums: list[str | None] = []

    async def fake_upsert(*_args, **_kwargs):
        return {"ok": True, "chunk_id": "travel"}

    class FakeRetriever:
        def invalidate_keyword_cache(self, source_type: str | None = None) -> None:
            invalidated_keyword_sources.append(source_type)

    class FakeCache:
        async def invalidate_forum(self, forum: str | None) -> None:
            invalidated_forums.append(forum)

    monkeypatch.setattr("src.main.kb_index.upsert_chunk", fake_upsert)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(qdrant_knowledge_collection="knowledge_base"),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                qdrant=object(),
                embedder=object(),
                retriever=FakeRetriever(),
                semantic_cache=FakeCache(),
            )
        )
    )

    result = await _admin_reindex_record(
        request,  # type: ignore[arg-type]
        {
            "chunk_id": "travel",
            "source_type": "xlsx",
            "forum_normalized": "Машук",
        },
    )

    assert invalidated_keyword_sources == ["xlsx"]
    assert invalidated_forums == ["Машук"]
    assert result["keyword_cache_invalidated_source"] == "xlsx"
    assert result["cache_invalidated_forum"] == "Машук"


@pytest.mark.asyncio
async def test_admin_reindex_invalidates_global_cache_for_unscoped_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidated_forums: list[str | None] = []

    async def fake_upsert(*_args, **_kwargs):
        return {"ok": True, "chunk_id": "general"}

    class FakeRetriever:
        def invalidate_keyword_cache(self, source_type: str | None = None) -> None:
            return None

    class FakeCache:
        async def invalidate_forum(self, forum: str | None) -> None:
            invalidated_forums.append(forum)

    monkeypatch.setattr("src.main.kb_index.upsert_chunk", fake_upsert)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(qdrant_knowledge_collection="knowledge_base"),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                qdrant=object(),
                embedder=object(),
                retriever=FakeRetriever(),
                semantic_cache=FakeCache(),
            )
        )
    )

    result = await _admin_reindex_record(
        request,  # type: ignore[arg-type]
        {"chunk_id": "general", "source_type": "yonote"},
    )

    assert invalidated_forums == [None]
    assert result["cache_invalidated_forum"] == "global"


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
    assert 'id="qualityDashboard"' in enabled.text
    assert 'id="yonoteButton"' in enabled.text
    assert 'id="yonoteDashboard"' in enabled.text
    assert 'id="yonoteDatabaseButton"' in enabled.text
    assert 'id="yonoteDatabaseDashboard"' in enabled.text
    assert "/admin/kb/ops-report?days=7" in enabled.text
    assert "/admin/kb/yonote/preview" in enabled.text
    assert "/admin/kb/yonote/apply" in enabled.text
    assert "/admin/kb/yonote/database-statistics" in enabled.text
    assert "/admin/kb/yonote/database-export" in enabled.text
    assert "Подсчёт БД Yonote" in enabled.text
    assert "Работа бота" in enabled.text
    assert "Проблемные темы" in enabled.text
    assert "ожидаемые эскалации" in enabled.text
    assert "проблемы качества" in enabled.text
    assert "Блоки quality gate" in enabled.text
    assert "pass rate" in enabled.text
    assert "Сохранение не подтверждено" in enabled.text
    assert "Текст сохранён, но RAG-индекс не обновлён" in enabled.text
    assert "Операция не завершилась за" in enabled.text
    assert "текстовых секций (оценка)" in enabled.text
    assert "может включать чувствительные данные" in enabled.text
    assert "не добавляй в Git" in enabled.text
    assert 'let activeWorkspace = "knowledge";' in enabled.text
    assert "lastYonoteDatabaseReport = data;" in enabled.text
    assert "Подсчёт Yonote ещё не выполнен" in enabled.text
    assert "const errorText = await response.text();" in enabled.text
    assert "const payload = JSON.parse(errorText);" in enabled.text
    assert "Чтение Yonote отключено или не настроено" in enabled.text
    assert "const adminReadOnly = false;" in enabled.text


@pytest.mark.asyncio
async def test_admin_kb_page_disables_mutation_ui_in_read_only_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            admin_auth_token="admin-secret",
            admin_read_only=True,
            yonote_sync_enabled=True,
            yonote_api_token="read-only-yonote-token",
            kb_seed_path=str(seed_path),
        ),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/admin/kb")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "const adminReadOnly = true;" in response.text
    assert "const yonoteSyncEnabled = true;" in response.text
    assert "read-only-yonote-token" not in response.text
    assert "Production работает только для просмотра" in response.text
    assert "adminReadOnly || isBusy" in response.text
    assert 'document.getElementById("textClean").readOnly = adminReadOnly;' in response.text
    assert 'document.getElementById("textClean").disabled = false;' in response.text
    assert 'document.getElementById("saveChunkButton").disabled = adminReadOnly;' in response.text
    assert 'document.getElementById("reindexButton").disabled = adminReadOnly;' in response.text
    assert "if (!requireWritableAdmin()) return;" in response.text
    assert "Применение изменений к базе бота отключено" in response.text


@pytest.mark.parametrize("admin_read_only", [False, True])
@pytest.mark.parametrize("yonote_sync_enabled", [False, True])
def test_admin_kb_html_renders_runtime_capabilities_without_placeholders(
    admin_read_only: bool,
    yonote_sync_enabled: bool,
) -> None:
    html = ui.render_admin_kb_html(
        admin_read_only=admin_read_only,
        yonote_sync_enabled=yonote_sync_enabled,
    )

    assert "__ADMIN_READ_ONLY__" not in html
    assert "__YONOTE_SYNC_ENABLED__" not in html
    assert f"const adminReadOnly = {str(admin_read_only).lower()};" in html
    assert f"const yonoteSyncEnabled = {str(yonote_sync_enabled).lower()};" in html


@pytest.mark.asyncio
async def test_read_only_admin_counts_and_exports_live_yonote_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    original_seed = seed_path.read_bytes()
    settings = SimpleNamespace(
        app_env="production",
        admin_auth_token="admin-secret",
        admin_read_only=True,
        yonote_sync_enabled=True,
        yonote_api_token="read-only-yonote-token",
        kb_seed_path=str(seed_path),
    )
    report = {
        "ok": True,
        "source": "yonote_live_api",
        "read_only": True,
        "documents_total": 42,
        "sections_total": 84,
        "characters_with_spaces": 12345,
    }
    calls: list[str] = []

    def fake_count(received_settings: object) -> dict[str, object]:
        assert received_settings is settings
        calls.append("count")
        return report

    def fake_export(received_settings: object) -> str:
        assert received_settings is settings
        calls.append("export")
        return "ВЫГРУЗКА БАЗЫ ЗНАНИЙ YONOTE\n\nБез секретов."

    monkeypatch.setattr("src.main.get_settings", lambda: settings)
    monkeypatch.setattr("src.main.count_yonote_database", fake_count)
    monkeypatch.setattr("src.main.export_yonote_database", fake_export)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        unauthorized = await client.post(
            "/admin/kb/yonote/database-statistics",
            json={},
        )
        counted = await client.post(
            "/admin/kb/yonote/database-statistics",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )
        exported = await client.post(
            "/admin/kb/yonote/database-export",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert unauthorized.status_code == 401
    assert counted.status_code == 200
    assert counted.json() == report
    assert exported.status_code == 200
    assert exported.text.startswith("ВЫГРУЗКА БАЗЫ ЗНАНИЙ YONOTE")
    assert exported.headers["content-type"].startswith("text/plain")
    assert exported.headers["cache-control"] == "no-store"
    assert exported.headers["pragma"] == "no-cache"
    assert exported.headers["x-content-type-options"] == "nosniff"
    assert exported.headers["content-disposition"].startswith(
        'attachment; filename="yonote-database-'
    )
    assert exported.headers["content-disposition"].endswith('.txt"')
    assert "read-only-yonote-token" not in exported.text
    assert calls == ["count", "export"]
    assert seed_path.read_bytes() == original_seed


@pytest.mark.asyncio
async def test_admin_yonote_database_sanitizes_provider_errors_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    original_seed = seed_path.read_bytes()
    secret_marker = "read-only-yonote-token"
    settings = SimpleNamespace(
        admin_auth_token="admin-secret",
        yonote_sync_enabled=True,
        yonote_api_token=secret_marker,
        kb_seed_path=str(seed_path),
    )
    results: list[Exception | dict[str, object]] = [
        YonoteApiError(f"provider echoed {secret_marker}"),
        YonoteOperationTimeout(f"deadline contained {secret_marker}"),
        YonoteDataTooLarge(f"oversized payload contained {secret_marker}"),
        {"ok": True, "documents_total": 1},
    ]

    def fake_count(_settings: object) -> dict[str, object]:
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("src.main.get_settings", lambda: settings)
    monkeypatch.setattr("src.main.count_yonote_database", fake_count)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        responses = [
            await client.post(
                "/admin/kb/yonote/database-statistics",
                json={},
                headers={"X-Admin-Token": "admin-secret"},
            )
            for _ in range(4)
        ]

    assert [response.status_code for response in responses] == [502, 504, 413, 200]
    assert secret_marker not in responses[0].text
    assert secret_marker not in responses[1].text
    assert secret_marker not in responses[2].text
    assert responses[3].json()["documents_total"] == 1
    assert seed_path.read_bytes() == original_seed


@pytest.mark.asyncio
async def test_admin_yonote_oversized_export_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    settings = SimpleNamespace(
        admin_auth_token="admin-secret",
        yonote_sync_enabled=True,
        yonote_api_token="read-only-yonote-token",
        kb_seed_path=str(seed_path),
    )

    def fail_export(_settings: object) -> str:
        raise YonoteDatabaseExportTooLarge(
            "provider echoed read-only-yonote-token"
        )

    monkeypatch.setattr("src.main.get_settings", lambda: settings)
    monkeypatch.setattr("src.main.export_yonote_database", fail_export)
    monkeypatch.setattr(
        "src.main.count_yonote_database",
        lambda _settings: {"ok": True, "documents_total": 1},
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        oversized = await client.post(
            "/admin/kb/yonote/database-export",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )
        counted = await client.post(
            "/admin/kb/yonote/database-statistics",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert oversized.status_code == 413
    assert "безопасный лимит размера" in oversized.json()["detail"]
    assert "read-only-yonote-token" not in oversized.text
    assert counted.status_code == 200


@pytest.mark.asyncio
async def test_admin_yonote_database_operations_share_one_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    started = threading.Event()
    release = threading.Event()
    export_calls = 0
    settings = SimpleNamespace(
        admin_auth_token="admin-secret",
        yonote_sync_enabled=True,
        yonote_api_token="read-only-yonote-token",
        kb_seed_path=str(seed_path),
    )

    def slow_count(_settings: object) -> dict[str, object]:
        started.set()
        assert release.wait(timeout=5)
        return {"ok": True, "documents_total": 1}

    def fake_export(_settings: object) -> str:
        nonlocal export_calls
        export_calls += 1
        return "ВЫГРУЗКА ДОСТУПНОГО СОДЕРЖИМОГО YONOTE"

    monkeypatch.setattr("src.main.get_settings", lambda: settings)
    monkeypatch.setattr("src.main.count_yonote_database", slow_count)
    monkeypatch.setattr("src.main.export_yonote_database", fake_export)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        count_task = asyncio.create_task(
            client.post(
                "/admin/kb/yonote/database-statistics",
                json={},
                headers={"X-Admin-Token": "admin-secret"},
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        blocked_export = await client.post(
            "/admin/kb/yonote/database-export",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )
        release.set()
        counted = await count_task
        successful_export = await client.post(
            "/admin/kb/yonote/database-export",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert blocked_export.status_code == 409
    assert counted.status_code == 200
    assert successful_export.status_code == 200
    assert export_calls == 1


@pytest.mark.asyncio
async def test_admin_read_only_mode_survives_login_reload_and_logout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            admin_auth_token="admin-secret",
            admin_read_only=True,
            yonote_sync_enabled=True,
            yonote_api_token="read-only-yonote-token",
            kb_seed_path=str(seed_path),
        ),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        login = await client.post("/admin/kb/login", json={"token": "admin-secret"})
        first_page = await client.get("/admin/kb")
        reloaded_page = await client.get("/admin/kb")
        chunk = await client.get("/admin/kb/chunks/travel")
        patch = await client.patch(
            "/admin/kb/chunks/travel",
            json={"text_clean": "updated"},
        )
        reindex = await client.post("/admin/kb/chunks/travel/reindex")
        apply = await client.post("/admin/kb/yonote/apply", json={})
        logout = await client.post("/admin/kb/logout", json={})
        after_logout = await client.get("/admin/kb/chunks")

    assert login.status_code == 200
    for page in (first_page, reloaded_page):
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "const adminReadOnly = true;" in page.text
    assert chunk.status_code == 200
    for response in (patch, reindex, apply):
        assert response.status_code == 403
        assert response.json()["detail"] == "Admin mutations are disabled in this runtime"
    assert logout.status_code == 200
    assert after_logout.status_code == 401


@pytest.mark.asyncio
async def test_admin_kb_api_previews_and_applies_yonote_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            admin_auth_token="admin-secret",
            yonote_sync_enabled=True,
            yonote_api_token="read-only-yonote-token",
            kb_seed_path=str(seed_path),
        ),
    )

    def fake_preview(path: Path, settings: object, *, limit_documents: int | None = None):
        assert path == seed_path
        assert settings.admin_auth_token == "admin-secret"
        assert limit_documents == 3
        return {
            "ok": True,
            "applied": False,
            "documents": 3,
            "fresh_yonote_records": 12,
            "added": 2,
            "changed": 1,
            "removed": 0,
        }

    def fake_apply(path: Path, _settings: object, *, limit_documents: int | None = None):
        assert path == seed_path
        assert limit_documents is None
        return {
            "ok": True,
            "applied": True,
            "index_required": True,
            "documents": 109,
            "fresh_yonote_records": 1412,
            "merged_records": 2162,
        }

    monkeypatch.setattr("src.main.preview_yonote_sync", fake_preview)
    monkeypatch.setattr("src.main.apply_yonote_sync", fake_apply)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.post("/admin/kb/yonote/preview", json={})
        preview = await client.post(
            "/admin/kb/yonote/preview",
            json={"limit_documents": 3},
            headers={"X-Admin-Token": "admin-secret"},
        )
        applied = await client.post(
            "/admin/kb/yonote/apply",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert unauthorized.status_code == 401
    assert preview.status_code == 200
    assert preview.json()["applied"] is False
    assert preview.json()["fresh_yonote_records"] == 12
    assert applied.status_code == 200
    assert applied.json()["applied"] is True
    assert applied.json()["index_required"] is True


@pytest.mark.asyncio
async def test_admin_kb_api_rejects_yonote_pull_when_runtime_capability_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            admin_auth_token="admin-secret",
            yonote_sync_enabled=False,
            yonote_api_token="",
            kb_seed_path=str(seed_path),
        ),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        page = await client.get("/admin/kb")
        preview = await client.post(
            "/admin/kb/yonote/preview",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )
        statistics = await client.post(
            "/admin/kb/yonote/database-statistics",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )
        exported = await client.post(
            "/admin/kb/yonote/database-export",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert page.status_code == 200
    assert "const yonoteSyncEnabled = false;" in page.text
    for response in (preview, statistics, exported):
        assert response.status_code == 503
        assert response.json()["detail"] == "Yonote sync is disabled"


@pytest.mark.asyncio
async def test_admin_kb_api_rejects_partial_yonote_preview_in_production(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    preview_called = False

    def fake_preview(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal preview_called
        preview_called = True
        return {}

    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            admin_auth_token="admin-secret",
            admin_read_only=True,
            yonote_sync_enabled=True,
            yonote_api_token="read-only-yonote-token",
            kb_seed_path=str(seed_path),
        ),
    )
    monkeypatch.setattr("src.main.preview_yonote_sync", fake_preview)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(
            "/admin/kb/yonote/preview",
            json={"limit_documents": 3},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "limit_documents is not allowed for production Yonote preview"
    )
    assert preview_called is False


@pytest.mark.asyncio
async def test_read_only_admin_yonote_preview_is_full_and_does_not_mutate_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    original_seed = seed_path.read_bytes()

    def fake_load(_settings: object, *, limit_documents: int | None):
        assert limit_documents is None
        return [object()], []

    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            admin_auth_token="admin-secret",
            admin_read_only=True,
            yonote_sync_enabled=True,
            yonote_api_token="read-only-yonote-token",
            kb_seed_path=str(seed_path),
        ),
    )
    monkeypatch.setattr(
        "src.admin.yonote_sync._load_fresh_yonote_records",
        fake_load,
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(
            "/admin/kb/yonote/preview",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert response.json()["index_required"] is False
    assert seed_path.read_bytes() == original_seed


@pytest.mark.asyncio
async def test_admin_yonote_preview_rejects_concurrent_pull(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    started = threading.Event()
    release = threading.Event()
    call_count = 0

    def slow_preview(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        started.set()
        assert release.wait(timeout=5)
        return {"ok": True, "applied": False, "index_required": False}

    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            admin_auth_token="admin-secret",
            admin_read_only=True,
            yonote_sync_enabled=True,
            yonote_api_token="read-only-yonote-token",
            kb_seed_path=str(seed_path),
        ),
    )
    monkeypatch.setattr("src.main.preview_yonote_sync", slow_preview)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        first_task = asyncio.create_task(
            client.post(
                "/admin/kb/yonote/preview",
                json={},
                headers={"X-Admin-Token": "admin-secret"},
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        second = await client.post(
            "/admin/kb/yonote/preview",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )
        release.set()
        first = await first_task

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "Yonote sync is already running"
    assert call_count == 1


@pytest.mark.asyncio
async def test_admin_kb_mutations_are_explicitly_forbidden_in_read_only_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            admin_auth_token="admin-secret",
            admin_read_only=True,
            yonote_sync_enabled=True,
            yonote_api_token="read-only-yonote-token",
            kb_seed_path=str(seed_path),
        ),
    )
    transport = httpx.ASGITransport(app=fastapi_app)
    headers = {"X-Admin-Token": "admin-secret"}

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        apply_response = await client.post(
            "/admin/kb/yonote/apply",
            json={},
            headers=headers,
        )
        patch_response = await client.patch(
            "/admin/kb/chunks/travel",
            json={"text_clean": "updated"},
            headers=headers,
        )
        reindex_response = await client.post(
            "/admin/kb/chunks/travel/reindex",
            headers=headers,
        )

    for response in (apply_response, patch_response, reindex_response):
        assert response.status_code == 403
        assert response.json()["detail"] == "Admin mutations are disabled in this runtime"


@pytest.mark.asyncio
async def test_production_admin_mutations_require_explicit_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    settings = SimpleNamespace(
        app_env="production",
        admin_auth_token="admin-secret",
        admin_read_only=False,
        admin_mutations_enabled=False,
        yonote_sync_enabled=False,
        kb_seed_path=str(seed_path),
    )
    monkeypatch.setattr("src.main.get_settings", lambda: settings)
    transport = httpx.ASGITransport(app=fastapi_app)
    headers = {"X-Admin-Token": "admin-secret"}

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        blocked = await client.patch(
            "/admin/kb/chunks/travel",
            json={"text_clean": "Не должно сохраниться", "reindex": False},
            headers=headers,
        )
        settings.admin_mutations_enabled = True
        allowed = await client.patch(
            "/admin/kb/chunks/travel",
            json={"text_clean": "Изменение тестовой базы", "reindex": False},
            headers=headers,
        )

    assert blocked.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["record"]["text_clean"] == "Изменение тестовой базы"


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
async def test_admin_kb_login_marks_session_cookie_secure_behind_https_proxy(
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
        login = await client.post(
            "/admin/kb/login",
            json={"token": "admin-secret"},
            headers={"X-Forwarded-Proto": "https"},
        )

    cookie = login.headers["set-cookie"]
    assert login.status_code == 200
    assert "rosmol_admin_session=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/admin/kb" in cookie


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
async def test_admin_kb_api_keeps_saved_text_when_reindex_after_update_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="admin-secret", kb_seed_path=str(seed_path)),
    )

    async def fake_reindex(_request, _record):
        raise HTTPException(status_code=503, detail="ML service is unavailable")

    monkeypatch.setattr("src.main._admin_reindex_record", fake_reindex)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/admin/kb/chunks/draft_docs",
            json={
                "status": "published",
                "text_clean": "Новый текст сохранён, но индекс временно недоступен.",
            },
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["record"]["text_clean"] == "Новый текст сохранён, но индекс временно недоступен."
    assert payload["record"]["status"] == "published"
    assert payload["reindex"] == {
        "ok": False,
        "chunk_id": "draft_docs",
        "status_code": 503,
        "error": "ML service is unavailable",
    }
    stored = json.loads(seed_path.read_text(encoding="utf-8"))
    assert stored[1]["text_clean"] == "Новый текст сохранён, но индекс временно недоступен."


@pytest.mark.asyncio
async def test_admin_kb_api_keeps_saved_text_when_reindex_after_update_crashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "kb.json"
    _write_seed(seed_path)
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(admin_auth_token="admin-secret", kb_seed_path=str(seed_path)),
    )

    async def fake_reindex(_request, _record):
        raise RuntimeError("qdrant timeout")

    monkeypatch.setattr("src.main._admin_reindex_record", fake_reindex)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/admin/kb/chunks/draft_docs",
            json={"text_clean": "Текст сохранён даже при сбое Qdrant."},
            headers={"X-Admin-Token": "admin-secret"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["record"]["text_clean"] == "Текст сохранён даже при сбое Qdrant."
    assert payload["reindex"] == {
        "ok": False,
        "chunk_id": "draft_docs",
        "status_code": 500,
        "error": "Ошибка обновления индекса: RuntimeError",
    }
    stored = json.loads(seed_path.read_text(encoding="utf-8"))
    assert stored[1]["text_clean"] == "Текст сохранён даже при сбое Qdrant."


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
