from pathlib import Path


def test_plain_http_refuses_rag_and_webhook_requests() -> None:
    config = Path("nginx/default.conf").read_text(encoding="utf-8")

    ask_block = config.split("location = /ask", maxsplit=1)[1].split("}", maxsplit=1)[0]
    webhook_block = config.split("location ^~ /webhook/", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]

    assert "return 426;" in ask_block
    assert "return 426;" in webhook_block
    assert "app-ml:8000" not in ask_block
    assert "app-ml:8000" not in webhook_block


def test_https_rag_endpoints_and_readiness_use_prewarmed_ml_runtime() -> None:
    config = Path("nginx/admin-tls.conf").read_text(encoding="utf-8")
    https_server = config.split("server {\n    listen 443 ssl;", maxsplit=1)[1]

    for location in ("location = /ask", "location ^~ /webhook/", "location = /ready"):
        block = https_server.split(location, maxsplit=1)[1].split("}", maxsplit=1)[0]
        assert "app-ml:8000" in block


def test_only_health_uses_lightweight_app_and_root_fails_closed() -> None:
    config = Path("nginx/default.conf").read_text(encoding="utf-8")
    health_block = config.split("location = /health {", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]
    default_block = config.split("location / {", maxsplit=1)[1].split("}", maxsplit=1)[0]

    assert "app:8000" in health_block
    assert "return 404;" in default_block
    assert "proxy_pass" not in default_block


def test_nginx_does_not_publish_api_documentation_or_schema() -> None:
    default = Path("nginx/default.conf").read_text(encoding="utf-8")
    tls = Path("nginx/admin-tls.conf").read_text(encoding="utf-8")

    for config in (default, tls):
        for location in ("/docs", "/redoc", "/openapi.json"):
            assert f"location = {location} {{\n        return 404;" in config


def test_nginx_sets_admin_and_api_security_headers() -> None:
    config = Path("nginx/default.conf").read_text(encoding="utf-8")

    assert "server_tokens off;" in config
    assert 'add_header Content-Security-Policy "' in config
    assert "frame-ancestors 'none'" in config
    assert 'add_header X-Content-Type-Options "nosniff" always;' in config
    assert 'add_header X-Frame-Options "DENY" always;' in config
    assert 'add_header Referrer-Policy "no-referrer" always;' in config
    assert 'add_header Cache-Control "no-store" always;' in config
