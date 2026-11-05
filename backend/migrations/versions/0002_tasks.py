"""Persist reference-only tasks and fenced attempts."""

import sqlalchemy as sa
from alembic import op

revision = "0002_tasks"
down_revision = "0001_vector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("subject_version", sa.Integer(), nullable=False),
        sa.Column("payload_ref", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "kind", "subject_id", "subject_version", name="uq_tasks_key"
        ),
        sa.CheckConstraint("kind ~ '^[a-z][a-z0-9_]{0,63}$'", name="ck_tasks_kind"),
        sa.CheckConstraint(
            "subject_version > 0 AND generation >= 0", name="ck_tasks_versions"
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts "
            "AND max_attempts BETWEEN 1 AND 100",
            name="ck_tasks_attempts",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint(
            "(status = 'running') = (lease_expires_at IS NOT NULL)",
            name="ck_tasks_lease",
        ),
        sa.CheckConstraint(
            "error_code IN ('transient_failure','permanent_failure',"
            "'attempts_exhausted')",
            name="ck_tasks_error",
        ),
    )
    op.create_index("ix_tasks_due", "tasks", ["status", "run_at", "id"])


def downgrade() -> None:
    op.drop_table("tasks")
