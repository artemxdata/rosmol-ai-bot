from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP_AND_QDRANT_IDS = {
    "CVE-2026-8376",
    "CVE-2026-42496",
    "CVE-2026-13221",
}
PERL_PURL = (
    "pkg:deb/debian/perl-base@5.40.1-6?arch=amd64&distro=debian-13.6"
)
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
        assert date.today() <= expiry <= date.today() + timedelta(days=14)


def test_perl_vex_files_are_exact_scoped_and_short_lived() -> None:
    for filename in ("trivy-app-vex.yaml", "trivy-qdrant-vex.yaml"):
        entries = _load_entries(filename)

        assert {entry["id"] for entry in entries} == APP_AND_QDRANT_IDS
        assert len(entries) == len(APP_AND_QDRANT_IDS)
        assert all(entry["purls"] == [PERL_PURL] for entry in entries)
        _assert_short_lived(entries)


def test_postgres_vex_is_exact_scoped_and_short_lived() -> None:
    entries = _load_entries("trivy-postgres-vex.yaml")

    assert entries == [
        {
            "id": "CVE-2025-68121",
            "purls": [POSTGRES_PURL],
            "expired_at": date(2026, 7, 27),
            "statement": entries[0]["statement"],
        }
    ]
    _assert_short_lived(entries)
