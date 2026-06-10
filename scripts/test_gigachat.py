from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from time import perf_counter

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.llm.cascade import ANALYZER_MODEL, GENERATOR_MODEL_SIMPLE
from src.llm.client import GigaChatClient


async def main() -> None:
    client = GigaChatClient()
    failed = False
    for model in (ANALYZER_MODEL, GENERATOR_MODEL_SIMPLE):
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
                    "ok": False,
                    "latency_ms": latency_ms,
                    "error_type": type(exc).__name__,
                    "error": str(exc).splitlines()[0][:300],
                }
            )
            continue

        latency_ms = int((perf_counter() - started_at) * 1000)
        print(
            {
                "model": model,
                "ok": bool(answer),
                "latency_ms": latency_ms,
            }
        )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
