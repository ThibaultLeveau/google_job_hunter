"""Record owner-scoped idempotency and rolling public retry admission."""

import sqlalchemy as sa
from alembic import op

revision = "0032_job_retries"
down_revision = "0031_job_listing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_retry_requests",
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_job_retry_generation"),
    )
    op.execute("""
        CREATE TRIGGER immutable_job_retries BEFORE UPDATE ON job_retry_requests
        FOR EACH ROW EXECUTE FUNCTION immutable_private_match()
    """)


def downgrade() -> None:
    op.drop_table("job_retry_requests")
