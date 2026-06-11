"""add llm usage accounting

Revision ID: 004_llm_usage_accounting
Revises: 003_trace_events
Create Date: 2026-06-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "004_llm_usage_accounting"
down_revision = "003_trace_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_traces",
        sa.Column(
            "llm_usage",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "request_traces",
        sa.Column("llm_prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "request_traces",
        sa.Column("llm_completion_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "request_traces",
        sa.Column("llm_total_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "request_traces",
        sa.Column(
            "llm_estimated_cost_rub",
            sa.Numeric(precision=12, scale=6),
            nullable=False,
            server_default="0",
        ),
    )
    op.alter_column("request_traces", "llm_usage", server_default=None)
    op.alter_column("request_traces", "llm_prompt_tokens", server_default=None)
    op.alter_column("request_traces", "llm_completion_tokens", server_default=None)
    op.alter_column("request_traces", "llm_total_tokens", server_default=None)
    op.alter_column("request_traces", "llm_estimated_cost_rub", server_default=None)
    op.create_index("idx_traces_llm_total_tokens", "request_traces", ["llm_total_tokens"])


def downgrade() -> None:
    op.drop_index("idx_traces_llm_total_tokens", table_name="request_traces")
    op.drop_column("request_traces", "llm_estimated_cost_rub")
    op.drop_column("request_traces", "llm_total_tokens")
    op.drop_column("request_traces", "llm_completion_tokens")
    op.drop_column("request_traces", "llm_prompt_tokens")
    op.drop_column("request_traces", "llm_usage")
