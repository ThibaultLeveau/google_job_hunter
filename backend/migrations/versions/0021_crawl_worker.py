"""Persist crawl hop checkpoints and explicit public source outcomes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_crawl_worker"
down_revision = "0020_job_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_sources",
        sa.Column(
            "crawl_state", sa.String(16), nullable=False, server_default="pending"
        ),
    )
    op.add_column("job_sources", sa.Column("crawl_error", sa.String(32), nullable=True))
    op.add_column(
        "job_sources",
        sa.Column("crawl_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_job_crawl_state",
        "job_sources",
        "crawl_state IN ('pending','fetched','blocked','removed',"
        "'unsupported','non_job','failed')",
    )
    op.execute("""
        UPDATE job_sources SET crawl_state='failed', crawl_error=tasks.error_code
        FROM tasks WHERE kind='job_crawl' AND subject_id=job_sources.id
        AND status='failed'
    """)
    op.create_table(
        "crawl_progress",
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column("requested_url", sa.String(2048), nullable=False),
        sa.Column("next_url", sa.String(2048), nullable=False),
        sa.Column("visited", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 0", name="ck_crawl_progress_generation"),
        sa.CheckConstraint(
            "jsonb_typeof(visited) = 'array' AND jsonb_array_length(visited) <= 10",
            name="ck_crawl_progress_visited",
        ),
    )


def downgrade() -> None:
    op.drop_table("crawl_progress")
    op.drop_constraint("ck_job_crawl_state", "job_sources", type_="check")
    op.drop_column("job_sources", "crawl_error")
    op.drop_column("job_sources", "crawl_observed_at")
    op.drop_column("job_sources", "crawl_state")
