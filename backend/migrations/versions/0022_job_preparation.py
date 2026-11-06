"""Preserve immutable deterministic text preparation per snapshot and engine version."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0022_job_preparation"
down_revision = "0021_crawl_worker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_preparations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "snapshot_id", sa.Uuid(), sa.ForeignKey("job_snapshots.id"), nullable=False
        ),
        sa.Column("version", sa.String(120), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.UniqueConstraint("snapshot_id", "version", name="uq_preparation_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_preparation_payload"
        ),
    )
    op.execute("""
        CREATE FUNCTION immutable_job_preparation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Preparation artifacts are immutable';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER immutable_job_preparation BEFORE UPDATE ON job_preparations
        FOR EACH ROW EXECUTE FUNCTION immutable_job_preparation()
    """)


def downgrade() -> None:
    op.drop_table("job_preparations")
    op.execute("DROP FUNCTION immutable_job_preparation()")
