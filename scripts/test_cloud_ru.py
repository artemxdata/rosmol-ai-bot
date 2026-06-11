from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from time import perf_counter

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.llm.cascade import select_generator_model
from src.llm.client import CloudRuLLMClient
from src.models import Complexity


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Cloud.ru chat completions access.")
    parser.add_argument(
        "--complex",
        action="store_true",
        help="Check the configured complex/Max model instead of the simple model.",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    client = CloudRuLLMClient()
    failed = False
    complexity = Complexity.COMPLEX if args.complex else Complexity.SIMPLE
    model = select_generator_model(complexity)
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
                "complexity": complexity.value,
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
                "complexity": complexity.value,
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
