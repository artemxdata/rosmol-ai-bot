"""persist routing hint in request traces

Revision ID: 005_routing_hint_trace
Revises: 004_llm_usage_accounting
Create Date: 2026-06-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "005_routing_hint_trace"
down_revision = "004_llm_usage_accounting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_traces",
        sa.Column(
            "routing_hint",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("request_traces", "routing_hint", server_default=None)


def downgrade() -> None:
    op.drop_column("request_traces", "routing_hint")
