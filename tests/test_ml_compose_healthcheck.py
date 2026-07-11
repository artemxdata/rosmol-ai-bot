from __future__ import annotations

from pathlib import Path


def test_ml_healthcheck_allows_model_prewarm_to_finish() -> None:
    config = Path("docker-compose.ml.yml").read_text(encoding="utf-8")
    app_ml_block = config.split("  ml-check:", maxsplit=1)[0]

    assert "ML_PREWARM_TIMEOUT_SECONDS: ${ML_PREWARM_TIMEOUT_SECONDS:-180}" in app_ml_block
    assert "start_period: 240s" in app_ml_block
    assert "interval: 10s" in app_ml_block
    assert "retries: 6" in app_ml_block
