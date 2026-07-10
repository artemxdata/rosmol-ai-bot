from pathlib import Path


def test_rag_endpoints_use_prewarmed_ml_runtime() -> None:
    config = Path("nginx/default.conf").read_text(encoding="utf-8")

    ask_block = config.split("location = /ask", maxsplit=1)[1].split("}", maxsplit=1)[0]
    webhook_block = config.split("location ^~ /webhook/", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]

    assert "app-ml:8000" in ask_block
    assert "app-ml:8000" in webhook_block


def test_default_location_keeps_lightweight_app() -> None:
    config = Path("nginx/default.conf").read_text(encoding="utf-8")
    default_block = config.split("location / {", maxsplit=1)[1].split("}", maxsplit=1)[0]

    assert "proxy_pass http://app:8000" in default_block
