from __future__ import annotations

import hashlib

import asyncpg

from src.models import Channel, MemoryRecord


def hash_user_id(channel: str, user_id: str) -> str:
    return hashlib.sha256(f"{channel}:{user_id}".encode()).hexdigest()


class UserMemory:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool

    async def get(self, user_id_hash: str, channel: str) -> MemoryRecord | None:
        row = await self.pg_pool.fetchrow(
            """
            SELECT user_id_hash, channel, last_forum, last_topics, turn_summary,
                   interaction_count, last_interaction
            FROM user_memory
            WHERE user_id_hash = $1 AND channel = $2
            """,
            user_id_hash,
            channel,
        )
        if row is None:
            return None
        return MemoryRecord(
            user_id_hash=row["user_id_hash"],
            channel=Channel(row["channel"]),
            last_forum=row["last_forum"],
            last_topics=list(row["last_topics"] or []),
            turn_summary=row["turn_summary"],
            interaction_count=row["interaction_count"],
            last_interaction=row["last_interaction"],
        )

    async def upsert(
        self,
        user_id_hash: str,
        channel: str,
        forum: str | None,
        topics: list[str],
        summary: str | None,
    ) -> None:
        await self.pg_pool.execute(
            """
            INSERT INTO user_memory (
                user_id_hash, channel, last_forum, last_topics, turn_summary,
                interaction_count, last_interaction
            )
            VALUES ($1, $2, $3, $4, $5, 1, NOW())
            ON CONFLICT (user_id_hash, channel)
            DO UPDATE SET
                last_forum = COALESCE(EXCLUDED.last_forum, user_memory.last_forum),
                last_topics = EXCLUDED.last_topics,
                turn_summary = EXCLUDED.turn_summary,
                interaction_count = user_memory.interaction_count + 1,
                last_interaction = NOW()
            """,
            user_id_hash,
            channel,
            forum,
            topics,
            summary,
        )
