from __future__ import annotations

import hashlib
from pathlib import Path

from eval import release_provenance


def test_release_provenance_hashes_clean_inputs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    kb_seed = tmp_path / "seed.json"
    cases = tmp_path / "cases.json"
    kb_seed.write_bytes(b"seed")
    cases.write_bytes(b"cases")
    monkeypatch.setattr(release_provenance, "_git_state", lambda: ("a" * 40, True))

    result = release_provenance.build_release_provenance(
        release_run_id="release-test",
        target="http://localhost:8001/ask",
        kb_seed_path=kb_seed,
        case_paths={"safety": cases},
        expected_git_sha="a" * 40,
    )

    assert result["complete"] is True
    assert result["git_worktree_clean"] is True
    assert result["expected_git_sha"] == "a" * 40
    assert result["kb_seed"]["sha256"] == hashlib.sha256(b"seed").hexdigest()
    assert result["case_files"]["safety"]["sha256"] == hashlib.sha256(b"cases").hexdigest()


def test_dirty_worktree_makes_release_provenance_incomplete(
    monkeypatch,
    tmp_path: Path,
) -> None:
    kb_seed = tmp_path / "seed.json"
    kb_seed.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(release_provenance, "_git_state", lambda: ("a" * 40, False))

    result = release_provenance.build_release_provenance(
        release_run_id="release-test",
        target="http://localhost:8001/ask",
        kb_seed_path=kb_seed,
    )

    assert result["complete"] is False
    assert result["git_worktree_clean"] is False
    assert result["errors"] == ["git_worktree_dirty"]


def test_git_sha_requires_40_lowercase_hex_characters() -> None:
    assert release_provenance.valid_git_sha("a" * 40) is True
    assert release_provenance.valid_git_sha("z" * 40) is False
    assert release_provenance.valid_git_sha("A" * 40) is False
    assert release_provenance.valid_git_sha("a" * 39) is False


def test_expected_git_sha_must_match_clean_head(
    monkeypatch,
    tmp_path: Path,
) -> None:
    kb_seed = tmp_path / "seed.json"
    kb_seed.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(release_provenance, "_git_state", lambda: ("b" * 40, True))

    mismatch = release_provenance.build_release_provenance(
        release_run_id="release-test",
        target="http://localhost:8001/ask",
        kb_seed_path=kb_seed,
        expected_git_sha="a" * 40,
    )
    invalid = release_provenance.build_release_provenance(
        release_run_id="release-test",
        target="http://localhost:8001/ask",
        kb_seed_path=kb_seed,
        expected_git_sha="A" * 40,
    )

    assert mismatch["complete"] is False
    assert mismatch["errors"] == ["expected_git_sha_mismatch"]
    assert invalid["complete"] is False
    assert invalid["errors"] == ["expected_git_sha_invalid"]
