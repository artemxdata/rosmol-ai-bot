"""initial tables

Revision ID: 001_initial
Revises:
Create Date: 2026-06-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "request_traces",
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("channel", sa.String(length=20), nullable=True),
        sa.Column("user_id_hash", sa.String(length=64), nullable=True),
        sa.Column("message_masked", sa.Text(), nullable=True),
        sa.Column("query_analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("analyzer_model", sa.String(length=50), nullable=True),
        sa.Column("analyzer_latency_ms", sa.Integer(), nullable=True),
        sa.Column("metadata_filter", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("retrieved_chunks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("retrieval_latency_ms", sa.Integer(), nullable=True),
        sa.Column("reranker_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("max_reranker_score", sa.Float(), nullable=True),
        sa.Column("reranker_latency_ms", sa.Integer(), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("generator_model", sa.String(length=50), nullable=True),
        sa.Column("cited_sources", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("generation_latency_ms", sa.Integer(), nullable=True),
        sa.Column("verifier_triggered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("verifier_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("verification_latency_ms", sa.Integer(), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("was_escalated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("escalation_reason", sa.String(length=100), nullable=True),
        sa.Column("total_latency_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_version", sa.String(length=20), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("idx_traces_timestamp", "request_traces", ["timestamp"])
    op.create_index(
        "idx_traces_forum",
        "request_traces",
        [sa.text("(query_analysis->>'forum')")],
    )
    op.create_index("idx_traces_escalated", "request_traces", ["was_escalated"])
    op.create_index("idx_traces_model", "request_traces", ["generator_model"])
    op.create_index("idx_traces_cache", "request_traces", ["cache_hit"])

    op.create_table(
        "user_memory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id_hash", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("last_forum", sa.String(length=100), nullable=True),
        sa.Column("last_topics", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("turn_summary", sa.Text(), nullable=True),
        sa.Column("interaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "last_interaction",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id_hash", "channel", name="uq_user_memory_user_channel"),
    )
    op.create_index("idx_user_memory_last_interaction", "user_memory", ["last_interaction"])
    op.create_index("idx_user_memory_forum", "user_memory", ["last_forum"])

    op.create_table(
        "chunk_versions",
        sa.Column("chunk_id", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_by", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("chunk_id", "version", name="pk_chunk_versions"),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="ck_chunk_versions_status",
        ),
    )
    op.create_index("idx_chunk_versions_chunk_id", "chunk_versions", ["chunk_id"])
    op.create_index("idx_chunk_versions_status", "chunk_versions", ["status"])
    op.create_index(
        "idx_chunk_versions_forum",
        "chunk_versions",
        [sa.text("(metadata->>'forum_normalized')")],
    )
    op.create_index(
        "idx_chunk_versions_topic",
        "chunk_versions",
        [sa.text("(metadata->>'topic')")],
    )


def downgrade() -> None:
    op.drop_index("idx_chunk_versions_topic", table_name="chunk_versions")
    op.drop_index("idx_chunk_versions_forum", table_name="chunk_versions")
    op.drop_index("idx_chunk_versions_status", table_name="chunk_versions")
    op.drop_index("idx_chunk_versions_chunk_id", table_name="chunk_versions")
    op.drop_table("chunk_versions")

    op.drop_index("idx_user_memory_forum", table_name="user_memory")
    op.drop_index("idx_user_memory_last_interaction", table_name="user_memory")
    op.drop_table("user_memory")

    op.drop_index("idx_traces_cache", table_name="request_traces")
    op.drop_index("idx_traces_model", table_name="request_traces")
    op.drop_index("idx_traces_escalated", table_name="request_traces")
    op.drop_index("idx_traces_forum", table_name="request_traces")
    op.drop_index("idx_traces_timestamp", table_name="request_traces")
    op.drop_table("request_traces")
