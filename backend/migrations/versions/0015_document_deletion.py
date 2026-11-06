"""Fence document work and retain text-free unavailable-source markers."""

import sqlalchemy as sa
from alembic import op

revision = "0015_document_deletion"
down_revision = "0014_profile_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_tasks_status", "tasks", type_="check")
    op.create_check_constraint(
        "ck_tasks_status",
        "tasks",
        "status IN ('pending','running','succeeded','failed','cancelled')",
    )
    op.create_table(
        "document_deletions",
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("step > 0", name="ck_document_deletion_step"),
    )
    op.create_table(
        "unavailable_profile_sources",
        sa.Column(
            "fact_id",
            sa.Uuid(),
            sa.ForeignKey("profile_facts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "document_id", sa.Uuid(), sa.ForeignKey("documents.id"), primary_key=True
        ),
    )


def downgrade() -> None:
    op.drop_table("unavailable_profile_sources")
    op.drop_table("document_deletions")
    op.execute(
        "UPDATE tasks SET status='failed', error_code='permanent_failure' "
        "WHERE status='cancelled'"
    )
    op.drop_constraint("ck_tasks_status", "tasks", type_="check")
    op.create_check_constraint(
        "ck_tasks_status",
        "tasks",
        "status IN ('pending','running','succeeded','failed')",
    )
