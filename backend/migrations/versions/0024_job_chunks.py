"""Persist complete deterministic chunk batches with immutable evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024_job_chunks"
down_revision = "0023_job_cleaning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_chunk_batches",
        sa.Column(
            "content_id", sa.Uuid(), sa.ForeignKey("job_cleanings.id"), primary_key=True
        ),
        sa.Column("policy_version", sa.String(64), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("policy", JSONB(), nullable=False),
    )
    op.create_table(
        "job_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["content_id", "policy_version"],
            ["job_chunk_batches.content_id", "job_chunk_batches.policy_version"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "content_id", "policy_version", "index", name="uq_chunk_index"
        ),
        sa.CheckConstraint('"index" BETWEEN 0 AND 2047', name="ck_chunk_index"),
        sa.CheckConstraint(
            "payload->>'text' IS NOT NULL "
            "AND length(payload->>'text') BETWEEN 1 AND 2000 "
            "AND octet_length(payload->>'text') <= 6000 AND content_hash = "
            "encode(sha256(convert_to(payload->>'text', 'UTF8')), 'hex')",
            name="ck_chunk_text",
        ),
    )
    op.execute("""
        CREATE FUNCTION immutable_job_chunk() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Chunk artifacts are immutable';
        END $$
    """)
    for table in ("job_chunks", "job_chunk_batches"):
        op.execute(f"""
            CREATE TRIGGER immutable_{table} BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION immutable_job_chunk()
        """)


def downgrade() -> None:
    op.drop_table("job_chunks")
    op.drop_table("job_chunk_batches")
    op.execute("DROP FUNCTION immutable_job_chunk()")
