"""add durable HDE inbox and outbox

Revision ID: 008_hde_durable_transport
Revises: 007_hde_delivery_telemetry
Create Date: 2026-07-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "008_hde_durable_transport"
down_revision = "007_hde_delivery_telemetry"
branch_labels = None
depends_on = None


INBOX_STATUSES = "'pending', 'processing', 'retry', 'processed', 'dead_letter'"
OUTBOX_STATUSES = "'pending', 'sending', 'retry', 'delivered', 'dead_letter'"


def upgrade() -> None:
    op.create_table(
        "hde_inbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("ticket_key", sa.String(length=64), nullable=False),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("upstream_event_id_source", sa.String(length=100), nullable=False),
        # Only PII-masked message fields are allowed in safe_payload.  The
        # reversible HDE ticket reference is encrypted with pgcrypto by the
        # repository and is cleared as soon as processing creates the outbox.
        sa.Column("safe_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ticket_ref_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("locked_by", sa.String(length=100)),
        sa.Column("last_error_code", sa.String(length=100)),
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
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("event_key", name="uq_hde_inbox_event_key"),
        sa.UniqueConstraint("request_id", name="uq_hde_inbox_request_id"),
        sa.CheckConstraint(
            f"status IN ({INBOX_STATUSES})",
            name="ck_hde_inbox_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_hde_inbox_attempts",
        ),
        sa.CheckConstraint(
            "upstream_event_id_source <> 'request_id_fallback'",
            name="ck_hde_inbox_stable_event_source",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(safe_payload) = 'object' AND ("
            "safe_payload = jsonb_build_object("
            "'schema_version', 1, 'purged', true) OR ("
            "safe_payload ?& ARRAY["
            "'schema_version', 'channel', 'message_masked', 'timestamp', "
            "'has_attachments', 'forum_context_masked'"
            "] AND safe_payload - ARRAY["
            "'schema_version', 'channel', 'message_masked', 'timestamp', "
            "'has_attachments', 'forum_context_masked'"
            "] = '{}'::jsonb "
            "AND safe_payload->'schema_version' = '1'::jsonb "
            "AND safe_payload->>'channel' = 'hde' "
            "AND jsonb_typeof(safe_payload->'message_masked') = 'string' "
            "AND jsonb_typeof(safe_payload->'timestamp') = 'string' "
            "AND jsonb_typeof(safe_payload->'has_attachments') = 'boolean' "
            "AND jsonb_typeof(safe_payload->'forum_context_masked') "
            "IN ('string', 'null')"
            "))",
            name="ck_hde_inbox_safe_payload_keys",
        ),
        sa.CheckConstraint(
            "(status = 'processed' AND ticket_ref_ciphertext IS NULL) OR "
            "(status <> 'processed' AND ticket_ref_ciphertext IS NOT NULL)",
            name="ck_hde_inbox_ticket_ref_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'processing') = (locked_at IS NOT NULL AND locked_by IS NOT NULL)",
            name="ck_hde_inbox_lease_lifecycle",
        ),
    )
    op.create_index(
        "idx_hde_inbox_ready",
        "hde_inbox",
        ["status", "next_attempt_at", "id"],
    )
    op.create_index(
        "idx_hde_inbox_ticket_order",
        "hde_inbox",
        ["ticket_key", "id", "status"],
    )
    op.create_index(
        "idx_hde_inbox_locked_at",
        "hde_inbox",
        ["locked_at"],
    )

    op.create_table(
        "hde_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "inbox_id",
            sa.BigInteger(),
            sa.ForeignKey("hde_inbox.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("ticket_key", sa.String(length=64), nullable=False),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # ticket_id and response_text live only inside this pgcrypto envelope.
        # The ciphertext is cleared immediately after confirmed delivery.
        sa.Column("delivery_envelope_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="8",
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("locked_by", sa.String(length=100)),
        sa.Column("last_error_code", sa.String(length=100)),
        sa.Column("delivery_http_status", sa.Integer()),
        sa.Column("retry_after_seconds", sa.Float()),
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
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("inbox_id", name="uq_hde_outbox_inbox_id"),
        sa.UniqueConstraint("event_key", name="uq_hde_outbox_event_key"),
        sa.UniqueConstraint("request_id", name="uq_hde_outbox_request_id"),
        sa.CheckConstraint(
            f"status IN ({OUTBOX_STATUSES})",
            name="ck_hde_outbox_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_hde_outbox_attempts",
        ),
        sa.CheckConstraint(
            "(status = 'delivered' AND delivery_envelope_ciphertext IS NULL) OR "
            "(status <> 'delivered' AND delivery_envelope_ciphertext IS NOT NULL)",
            name="ck_hde_outbox_envelope_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'sending') = (locked_at IS NOT NULL AND locked_by IS NOT NULL)",
            name="ck_hde_outbox_lease_lifecycle",
        ),
    )
    op.create_index(
        "idx_hde_outbox_ready",
        "hde_outbox",
        ["status", "next_attempt_at", "id"],
    )
    op.create_index(
        "idx_hde_outbox_ticket_order",
        "hde_outbox",
        ["ticket_key", "id", "status"],
    )
    op.create_index(
        "idx_hde_outbox_locked_at",
        "hde_outbox",
        ["locked_at"],
    )

    op.create_table(
        "hde_transport_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("queue", sa.String(length=16), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("ticket_key", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("operator_id", sa.String(length=100), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("previous_status", sa.String(length=24), nullable=False),
        sa.Column("resulting_status", sa.String(length=24), nullable=False),
        sa.Column("previous_attempt_count", sa.Integer(), nullable=False),
        sa.Column("previous_error_code", sa.String(length=100)),
        sa.Column("previous_dead_lettered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_delivery_http_status", sa.Integer()),
        sa.Column("delivery_http_status", sa.Integer()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "queue IN ('inbox', 'outbox')",
            name="ck_hde_transport_audit_queue",
        ),
        sa.CheckConstraint(
            "operator_id ~ '^[A-Za-z0-9_.@-]{2,100}$'",
            name="ck_hde_transport_audit_operator",
        ),
        sa.CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_hde_transport_audit_evidence",
        ),
        sa.CheckConstraint(
            "previous_attempt_count >= 0",
            name="ck_hde_transport_audit_previous_attempts",
        ),
        sa.CheckConstraint(
            "(action = 'requeue_inbox' "
            "AND reason_code = 'side_effects_reviewed_safe_to_resume' "
            "AND queue = 'inbox' AND previous_status = 'dead_letter' "
            "AND resulting_status = 'retry') OR "
            "(action = 'requeue_outbox' "
            "AND reason_code = 'provider_confirmed_not_delivered' "
            "AND queue = 'outbox' AND previous_status = 'dead_letter' "
            "AND resulting_status = 'retry') OR "
            "(action = 'reconcile_outbox_delivered' "
            "AND reason_code = 'provider_confirmed_delivered' "
            "AND queue = 'outbox' AND previous_status = 'dead_letter' "
            "AND resulting_status = 'delivered')",
            name="ck_hde_transport_audit_action_reason",
        ),
    )
    op.create_index(
        "idx_hde_transport_audit_created_at",
        "hde_transport_audit",
        ["created_at"],
    )
    op.create_index(
        "idx_hde_transport_audit_job",
        "hde_transport_audit",
        ["queue", "job_id", "created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION reject_hde_transport_audit_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'hde_transport_audit is append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_hde_transport_audit_append_only
        BEFORE UPDATE OR DELETE ON hde_transport_audit
        FOR EACH ROW EXECUTE FUNCTION reject_hde_transport_audit_mutation()
        """
    )


def downgrade() -> None:
    # ``IF EXISTS`` also supports development databases that briefly applied an
    # earlier uncommitted form of revision 008 before the audit table was added.
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('hde_transport_audit') IS NOT NULL THEN
                DROP TRIGGER IF EXISTS trg_hde_transport_audit_append_only
                ON hde_transport_audit;
            END IF;
        END;
        $$;
        """
    )
    op.execute("DROP FUNCTION IF EXISTS reject_hde_transport_audit_mutation()")
    op.execute("DROP INDEX IF EXISTS idx_hde_transport_audit_job")
    op.execute("DROP INDEX IF EXISTS idx_hde_transport_audit_created_at")
    op.execute("DROP TABLE IF EXISTS hde_transport_audit")

    op.drop_index("idx_hde_outbox_locked_at", table_name="hde_outbox")
    op.drop_index("idx_hde_outbox_ticket_order", table_name="hde_outbox")
    op.drop_index("idx_hde_outbox_ready", table_name="hde_outbox")
    op.drop_table("hde_outbox")

    op.drop_index("idx_hde_inbox_locked_at", table_name="hde_inbox")
    op.drop_index("idx_hde_inbox_ticket_order", table_name="hde_inbox")
    op.drop_index("idx_hde_inbox_ready", table_name="hde_inbox")
    op.drop_table("hde_inbox")
