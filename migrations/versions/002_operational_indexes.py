"""operational indexes and updated_at trigger

Revision ID: 002_operational_indexes
Revises: 001_initial
Create Date: 2026-06-10
"""
from __future__ import annotations

from alembic import op

revision = "002_operational_indexes"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_traces_channel_timestamp",
        "request_traces",
        ["channel", "timestamp"],
    )
    op.create_index(
        "idx_traces_user_timestamp",
        "request_traces",
        ["user_id_hash", "timestamp"],
    )
    op.create_index(
        "idx_traces_escalated_timestamp",
        "request_traces",
        ["was_escalated", "timestamp"],
    )
    op.create_index("idx_traces_escalation_reason", "request_traces", ["escalation_reason"])
    op.create_index("idx_traces_max_reranker_score", "request_traces", ["max_reranker_score"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_chunk_versions_updated_at
        BEFORE UPDATE ON chunk_versions
        FOR EACH ROW
        EXECUTE FUNCTION set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_chunk_versions_updated_at ON chunk_versions")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at")
    op.drop_index("idx_traces_max_reranker_score", table_name="request_traces")
    op.drop_index("idx_traces_escalation_reason", table_name="request_traces")
    op.drop_index("idx_traces_escalated_timestamp", table_name="request_traces")
    op.drop_index("idx_traces_user_timestamp", table_name="request_traces")
    op.drop_index("idx_traces_channel_timestamp", table_name="request_traces")
