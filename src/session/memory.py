from __future__ import annotations

import hashlib
import json
from typing import Any

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
                   structured_context, interaction_count, last_interaction
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
            structured_context=_decode_json_object(row["structured_context"]),
            interaction_count=row["interaction_count"],
            last_interaction=row["last_interaction"],
        )

    async def upsert(
        self,
        user_id_hash: str,
        channel: str,
        forum: str | None,
        topics: list[str],
        structured_context: dict[str, Any],
    ) -> None:
        await self.pg_pool.execute(
            """
            INSERT INTO user_memory (
                user_id_hash, channel, last_forum, last_topics, turn_summary,
                structured_context, interaction_count, last_interaction
            )
            VALUES ($1, $2, $3, $4, NULL, $5::jsonb, 0, NOW())
            ON CONFLICT (user_id_hash, channel)
            DO UPDATE SET
                last_forum = EXCLUDED.last_forum,
                last_topics = EXCLUDED.last_topics,
                structured_context = user_memory.structured_context
                    || EXCLUDED.structured_context,
                last_interaction = NOW()
            """,
            user_id_hash,
            channel,
            forum,
            topics,
            json.dumps(structured_context, ensure_ascii=False),
        )

    async def get_recent_turns(
        self,
        user_id_hash: str,
        channel: str,
        *,
        limit: int,
    ) -> list[dict[str, str]]:
        rows = await self.pg_pool.fetch(
            """
            SELECT user_text_masked, bot_text
            FROM conversation_turns
            WHERE user_id_hash = $1 AND channel = $2
            ORDER BY turn_index DESC, id DESC
            LIMIT $3
            """,
            user_id_hash,
            channel,
            limit,
        )
        return [
            {
                "user": str(row["user_text_masked"] or ""),
                "bot": str(row["bot_text"] or ""),
            }
            for row in reversed(rows)
        ]

    async def append_turn(
        self,
        *,
        user_id_hash: str,
        channel: str,
        turn_index: int,
        user_text_masked: str,
        bot_text: str,
        summary: str | None,
        structured_context: dict[str, Any],
    ) -> None:
        context_json = json.dumps(structured_context, ensure_ascii=False)
        async with self.pg_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO conversation_turns (
                        user_id_hash, channel, turn_index, user_text_masked,
                        bot_text, structured_context
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                    """,
                    user_id_hash,
                    channel,
                    turn_index,
                    user_text_masked,
                    bot_text,
                    context_json,
                )
                await connection.execute(
                    """
                    INSERT INTO user_memory (
                        user_id_hash, channel, last_topics, turn_summary,
                        structured_context, interaction_count, last_interaction
                    )
                    VALUES (
                        $1, $2, ARRAY[]::text[], $3, $4::jsonb, $5, NOW()
                    )
                    ON CONFLICT (user_id_hash, channel)
                    DO UPDATE SET
                        turn_summary = EXCLUDED.turn_summary,
                        structured_context = user_memory.structured_context
                            || EXCLUDED.structured_context,
                        interaction_count = GREATEST(
                            user_memory.interaction_count,
                            EXCLUDED.interaction_count
                        ),
                        last_interaction = NOW()
                    """,
                    user_id_hash,
                    channel,
                    summary,
                    context_json,
                    turn_index,
                )


def _decode_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    return {}
