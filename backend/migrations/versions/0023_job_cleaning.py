"""Preserve verified AI cleaning and bounded call accounting."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0023_job_cleaning"
down_revision = "0022_job_preparation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_cleanings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "preparation_id",
            sa.Uuid(),
            sa.ForeignKey("job_preparations.id"),
            nullable=False,
        ),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.UniqueConstraint("preparation_id", "version", name="uq_cleaning_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_cleaning_payload"
        ),
    )
    op.execute("""
        CREATE FUNCTION immutable_job_cleaning() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Cleaning artifacts are immutable';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER immutable_job_cleaning BEFORE UPDATE ON job_cleanings
        FOR EACH ROW EXECUTE FUNCTION immutable_job_cleaning()
    """)
    op.create_table(
        "job_cleaning_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "preparation_id",
            sa.Uuid(),
            sa.ForeignKey("job_preparations.id"),
            nullable=False,
        ),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
    )
    op.execute("""
        CREATE TRIGGER immutable_job_cleaning_call BEFORE UPDATE ON job_cleaning_calls
        FOR EACH ROW EXECUTE FUNCTION immutable_job_cleaning()
    """)


def downgrade() -> None:
    op.drop_table("job_cleaning_calls")
    op.drop_table("job_cleanings")
    op.execute("DROP FUNCTION immutable_job_cleaning()")
