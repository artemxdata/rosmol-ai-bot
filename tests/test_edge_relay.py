from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_edge_relay_is_layer_four_only_and_keeps_tls_inside_nginx() -> None:
    config = (ROOT / "haproxy" / "edge-relay.cfg").read_text(encoding="utf-8")

    assert "mode tcp" in config
    assert "bind :8080" in config
    assert "bind :8443" in config
    assert "server nginx nginx:80" in config
    assert "server nginx nginx:443" in config
    assert "mode http" not in config
    assert "ssl crt" not in config
    assert "http-request" not in config
    assert "capture" not in config
    assert config.count("bind ") == 2
    assert config.count("server nginx ") == 2
