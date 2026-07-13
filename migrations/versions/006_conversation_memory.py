"""persist complete masked conversations and structured memory

Revision ID: 006_conversation_memory
Revises: 005_routing_hint_trace
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "006_conversation_memory"
down_revision = "005_routing_hint_trace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_memory",
        sa.Column(
            "structured_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("user_memory", "structured_context", server_default=None)

    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id_hash", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("user_text_masked", sa.Text(), nullable=False),
        sa.Column("bot_text", sa.Text(), nullable=False),
        sa.Column(
            "structured_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_conversation_turns_user_channel_created",
        "conversation_turns",
        ["user_id_hash", "channel", "created_at"],
    )
    op.create_index(
        "idx_conversation_turns_created_at",
        "conversation_turns",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_conversation_turns_created_at",
        table_name="conversation_turns",
    )
    op.drop_index(
        "idx_conversation_turns_user_channel_created",
        table_name="conversation_turns",
    )
    op.drop_table("conversation_turns")
    op.drop_column("user_memory", "structured_context")
