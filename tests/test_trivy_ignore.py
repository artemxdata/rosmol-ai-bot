from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PERL_IDS = {
    "CVE-2026-8376",
    "CVE-2026-42496",
    "CVE-2026-13221",
    "CVE-2026-57433",
}
QDRANT_IDS = PERL_IDS | {"CVE-2026-59873"}
PERL_PURL = (
    "pkg:deb/debian/perl-base@5.40.1-6?arch=amd64&distro=debian-13.6"
)
QDRANT_WEB_UI_TAR_PURL = "pkg:npm/tar@7.5.16"
POSTGRES_PURL = "pkg:golang/stdlib@v1.24.6"


def _load_entries(filename: str) -> list[dict[str, object]]:
    payload = yaml.safe_load(
        (ROOT / "security" / filename).read_text(encoding="utf-8")
    )
    return payload["vulnerabilities"]


def _assert_short_lived(entries: list[dict[str, object]]) -> None:
    for entry in entries:
        assert str(entry["statement"]).strip()
        expiry = entry["expired_at"]
        assert isinstance(expiry, date)
        assert date.today() < expiry <= date.today() + timedelta(days=14)


def test_perl_ignore_policies_are_exact_scoped_and_short_lived() -> None:
    expected_ids = {
        "trivy-app-ignore.yaml": PERL_IDS,
        "trivy-qdrant-ignore.yaml": QDRANT_IDS,
    }
    for filename, expected in expected_ids.items():
        entries = _load_entries(filename)
        perl_entries = [entry for entry in entries if entry["id"] in PERL_IDS]

        assert {entry["id"] for entry in entries} == expected
        assert len(perl_entries) == len(PERL_IDS)
        assert all(entry["purls"] == [PERL_PURL] for entry in perl_entries)
        assert all(
            entry["expired_at"] == date(2026, 8, 24) for entry in entries
        )
        _assert_short_lived(entries)


def test_storable_exception_records_the_unavailable_runtime_module() -> None:
    for filename in ("trivy-app-ignore.yaml", "trivy-qdrant-ignore.yaml"):
        entries = _load_entries(filename)
        entry = next(
            item for item in entries if item["id"] == "CVE-2026-57433"
        )
        statement = str(entry["statement"])

        assert "Storable" in statement
        assert "no Perl" in statement or "does not ship" in statement


def test_qdrant_node_tar_exception_records_metadata_only_reachability() -> None:
    entries = _load_entries("trivy-qdrant-ignore.yaml")
    entry = next(item for item in entries if item["id"] == "CVE-2026-59873")

    assert entry["purls"] == [QDRANT_WEB_UI_TAR_PURL]
    assert entry["expired_at"] == date(2026, 8, 24)
    statement = str(entry["statement"])
    assert "qdrant-web-ui.spdx.json" in statement
    assert "zero node_modules/tar files" in statement
    assert "no node, npm, or npx executable" in statement
    assert "Rust Qdrant binary" in statement


def test_postgres_ignore_policy_is_exact_scoped_and_short_lived() -> None:
    entries = _load_entries("trivy-postgres-ignore.yaml")

    assert entries == [
        {
            "id": "CVE-2025-68121",
            "purls": [POSTGRES_PURL],
            "expired_at": date(2026, 8, 24),
            "statement": entries[0]["statement"],
        }
    ]
    _assert_short_lived(entries)
