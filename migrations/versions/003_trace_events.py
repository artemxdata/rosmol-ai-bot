"""persist graph trace events

Revision ID: 003_trace_events
Revises: 002_operational_indexes
Create Date: 2026-06-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "003_trace_events"
down_revision = "002_operational_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_traces",
        sa.Column(
            "trace_events",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("request_traces", "trace_events", server_default=None)


def downgrade() -> None:
    op.drop_column("request_traces", "trace_events")
