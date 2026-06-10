from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import get_settings


async def main() -> None:
    settings = get_settings()
    pool = await asyncpg.create_pool(settings.postgres_dsn)
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM user_memory
            WHERE last_interaction < NOW() - ($1::int * INTERVAL '1 day')
            """,
            settings.memory_ttl_days,
        )
    await pool.close()
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
