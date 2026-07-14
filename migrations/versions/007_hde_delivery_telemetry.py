"""add HDE delivery, ticket outcome and eval trace identifiers

Revision ID: 007_hde_delivery_telemetry
Revises: 006_conversation_memory
Create Date: 2026-07-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "007_hde_delivery_telemetry"
down_revision = "006_conversation_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("request_traces", sa.Column("upstream_event_id", sa.String(255)))
    op.add_column("request_traces", sa.Column("upstream_event_id_source", sa.String(100)))
    op.add_column("request_traces", sa.Column("ticket_id_hash", sa.String(64)))
    op.add_column("request_traces", sa.Column("eval_run_id", sa.String(200)))
    op.add_column("request_traces", sa.Column("eval_case_id", sa.String(200)))
    op.add_column("request_traces", sa.Column("ticket_outcome", sa.String(32)))
    op.add_column("request_traces", sa.Column("delivery_status", sa.String(32)))
    op.add_column(
        "request_traces",
        sa.Column("delivery_attempted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("request_traces", sa.Column("delivery_http_status", sa.Integer()))
    op.add_column("request_traces", sa.Column("delivery_retry_after_seconds", sa.Float()))
    op.add_column("request_traces", sa.Column("delivery_error_code", sa.String(100)))
    op.add_column(
        "request_traces",
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        "UPDATE request_traces SET ticket_id_hash = user_id_hash "
        "WHERE ticket_id_hash IS NULL"
    )
    op.execute(
        """
        UPDATE request_traces
        SET ticket_outcome = CASE
            WHEN error IS NOT NULL THEN 'error'
            WHEN was_escalated THEN 'escalated'
            WHEN query_analysis->>'needs_clarification' = 'true' THEN 'clarification'
            WHEN response_text IS NULL OR response_text = '' THEN 'no_response'
            ELSE 'answered'
        END
        WHERE ticket_outcome IS NULL
        """
    )
    op.create_index(
        "idx_traces_ticket_timestamp",
        "request_traces",
        ["ticket_id_hash", "timestamp"],
    )
    op.create_index(
        "idx_traces_delivery_status_timestamp",
        "request_traces",
        ["delivery_status", "timestamp"],
    )
    op.create_index(
        "idx_traces_eval_run_case",
        "request_traces",
        ["eval_run_id", "eval_case_id"],
    )
    op.create_index(
        "idx_traces_ticket_upstream_event",
        "request_traces",
        ["ticket_id_hash", "upstream_event_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_traces_ticket_upstream_event", table_name="request_traces")
    op.drop_index("idx_traces_eval_run_case", table_name="request_traces")
    op.drop_index("idx_traces_delivery_status_timestamp", table_name="request_traces")
    op.drop_index("idx_traces_ticket_timestamp", table_name="request_traces")
    op.drop_column("request_traces", "delivered_at")
    op.drop_column("request_traces", "delivery_error_code")
    op.drop_column("request_traces", "delivery_retry_after_seconds")
    op.drop_column("request_traces", "delivery_http_status")
    op.drop_column("request_traces", "delivery_attempted")
    op.drop_column("request_traces", "delivery_status")
    op.drop_column("request_traces", "ticket_outcome")
    op.drop_column("request_traces", "eval_case_id")
    op.drop_column("request_traces", "eval_run_id")
    op.drop_column("request_traces", "ticket_id_hash")
    op.drop_column("request_traces", "upstream_event_id_source")
    op.drop_column("request_traces", "upstream_event_id")
