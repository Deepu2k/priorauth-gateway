"""Initial schema: prior_auth_requests and status_history.

Revision ID: 0001
Revises:
Create Date: 2026-11-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATUSES = "('received','sent','approved','denied','failed')"


def upgrade() -> None:
    op.create_table(
        "prior_auth_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("patient", postgresql.JSONB(), nullable=True),
        sa.Column("patient_reference", sa.String(128), nullable=True),
        sa.Column("coverage", postgresql.JSONB(), nullable=False),
        sa.Column("service_request", postgresql.JSONB(), nullable=False),
        sa.Column("service_code", sa.String(32), nullable=False),
        sa.Column("submitted_by", sa.String(128), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("authorization_number", sa.String(64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"status IN {STATUSES}", name="ck_requests_status"),
        sa.CheckConstraint("patient IS NOT NULL OR patient_reference IS NOT NULL", name="ck_requests_patient"),
    )
    op.create_index("ix_requests_status", "prior_auth_requests", ["status"])
    op.create_index("ix_requests_service_code", "prior_auth_requests", ["service_code"])
    op.create_index("ix_requests_submitted_by_created", "prior_auth_requests", ["submitted_by", "created_at"])

    op.create_table(
        "status_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("prior_auth_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("event", sa.String(48), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"to_status IN {STATUSES}", name="ck_history_to_status"),
    )
    op.create_index("ix_history_request_id", "status_history", ["request_id", "id"])


def downgrade() -> None:
    op.drop_table("status_history")
    op.drop_table("prior_auth_requests")
