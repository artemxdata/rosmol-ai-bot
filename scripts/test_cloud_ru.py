from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from time import perf_counter

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import get_settings
from src.llm.cascade import DEFAULT_CLOUD_RU_MODEL
from src.llm.client import CloudRuLLMClient


async def main() -> None:
    settings = get_settings()
    client = CloudRuLLMClient()
    failed = False
    model = settings.cloud_ru_model or DEFAULT_CLOUD_RU_MODEL
    started_at = perf_counter()
    try:
        answer = await client.generate(
            model=model,
            system="Ответь коротко.",
            user="Проверка доступа. Напиши: OK.",
            max_tokens=50,
        )
    except Exception as exc:
        failed = True
        latency_ms = int((perf_counter() - started_at) * 1000)
        print(
            {
                "model": model,
                "provider": "cloud.ru",
                "auth": "bearer_api_key",
                "ok": False,
                "latency_ms": latency_ms,
                "error_type": type(exc).__name__,
                "error": str(exc).splitlines()[0][:300],
            }
        )
    else:
        latency_ms = int((perf_counter() - started_at) * 1000)
        print(
            {
                "model": model,
                "provider": "cloud.ru",
                "auth": "bearer_api_key",
                "ok": bool(answer),
                "latency_ms": latency_ms,
            }
        )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
