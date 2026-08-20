from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import index_kb


def test_parse_args_defaults_to_runtime_kb_seed_path(monkeypatch) -> None:
    monkeypatch.setattr(
        index_kb,
        "get_settings",
        lambda: SimpleNamespace(
            kb_seed_path="/app/data/private/admin-kb/knowledge_base_seed.json",
            qdrant_knowledge_collection="knowledge_base",
        ),
    )
    monkeypatch.setattr(sys, "argv", ["index_kb.py", "--validate-only"])

    args = index_kb.parse_args()

    assert args.path == "/app/data/private/admin-kb/knowledge_base_seed.json"
    assert args.expected_seed_sha256 is None


def test_main_requires_expected_seed_sha256_before_indexing(monkeypatch) -> None:
    monkeypatch.setattr(
        index_kb,
        "parse_args",
        lambda: SimpleNamespace(
            validate_only=False,
            expected_seed_sha256=None,
        ),
    )

    with pytest.raises(ValueError, match="expected-seed-sha256"):
        index_kb.main()


@pytest.mark.asyncio
async def test_index_refuses_unreviewed_seed_before_qdrant_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    seed_path.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(
        index_kb,
        "AsyncQdrantClient",
        lambda **_kwargs: pytest.fail("Qdrant must not be opened on seed mismatch"),
    )

    with pytest.raises(index_kb.SeedRevisionMismatch, match="before_index"):
        await index_kb.index_kb(
            seed_path,
            "knowledge_base",
            expected_seed_sha256="0" * 64,
        )


@pytest.mark.asyncio
async def test_index_rechecks_exact_seed_bytes_before_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    original = json.dumps(
        [
            {
                "chunk_id": "archived-record",
                "text_clean": "Архивная запись для проверки revision binding.",
                "status": "archived",
                "category": "general",
                "source_type": "xlsx",
                "topic": "revision_binding",
            }
        ],
        ensure_ascii=False,
    ).encode()
    seed_path.write_bytes(original)

    class FakeQdrant:
        async def collection_exists(self, _collection: str) -> bool:
            seed_path.write_text("[]\n", encoding="utf-8")
            return True

        async def close(self) -> None:
            return None

    monkeypatch.setattr(index_kb, "AsyncQdrantClient", lambda **_kwargs: FakeQdrant())
    monkeypatch.setattr(
        index_kb,
        "get_settings",
        lambda: SimpleNamespace(qdrant_url="http://qdrant:6333", qdrant_api_key=""),
    )

    with pytest.raises(index_kb.SeedRevisionMismatch, match="before_completion"):
        await index_kb.index_kb(
            seed_path,
            "knowledge_base",
            expected_seed_sha256=sha256(original).hexdigest(),
        )
