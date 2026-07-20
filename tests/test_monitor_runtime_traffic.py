from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_traffic_monitor_is_fail_closed() -> None:
    script = (ROOT / "scripts" / "monitor_runtime_traffic.sh").read_text(
        encoding="utf-8"
    )

    assert "set -Eeuo pipefail" in script
    assert "docker CLI is required" in script
    assert "no running rosmol containers" in script
    assert "ss -H -lntup || true" not in script
    assert "ss -H -ntp state established || true" not in script
    assert "print_container_stats || true" not in script
