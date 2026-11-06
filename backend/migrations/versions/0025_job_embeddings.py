"""Store fixed-dimension public vectors separately from transient private queries."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects.postgresql import JSONB

revision = "0025_job_embeddings"
down_revision = "0024_job_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_embedding_spaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("revision", sa.String(80), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.CheckConstraint("dimensions = 1536", name="ck_embedding_dimensions"),
    )
    op.create_table(
        "job_embeddings",
        sa.Column(
            "chunk_id", sa.Uuid(), sa.ForeignKey("job_chunks.id"), primary_key=True
        ),
        sa.Column(
            "space_id",
            sa.Uuid(),
            sa.ForeignKey("job_embedding_spaces.id"),
            primary_key=True,
        ),
        sa.Column("embedding", VECTOR(1536), nullable=False),
        sa.CheckConstraint("vector_norm(embedding) > 0", name="ck_embedding_nonzero"),
    )
    op.create_table(
        "job_embedding_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "space_id",
            sa.Uuid(),
            sa.ForeignKey("job_embedding_spaces.id"),
            nullable=False,
        ),
        sa.Column("chunk_ids", JSONB(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
    )
    op.execute("""
        CREATE FUNCTION immutable_job_embedding() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Embedding artifacts are immutable';
        END $$
    """)
    for table in ("job_embeddings", "job_embedding_spaces", "job_embedding_calls"):
        op.execute(f"""
            CREATE TRIGGER immutable_{table} BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION immutable_job_embedding()
        """)


def downgrade() -> None:
    op.drop_table("job_embedding_calls")
    op.drop_table("job_embeddings")
    op.drop_table("job_embedding_spaces")
    op.execute("DROP FUNCTION immutable_job_embedding()")
