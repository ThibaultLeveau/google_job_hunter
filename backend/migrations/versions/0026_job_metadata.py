"""Preserve source-verified metadata and provider call accounting."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0026_job_metadata"
down_revision = "0025_job_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("job_metadata", "job_metadata_calls"):
        op.create_table(
            name,
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
    op.create_unique_constraint(
        "uq_metadata_revision", "job_metadata", ["preparation_id", "version"]
    )
    op.create_check_constraint(
        "ck_metadata_payload", "job_metadata", "jsonb_typeof(payload) = 'object'"
    )
    op.execute("""
        CREATE FUNCTION immutable_job_metadata() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Metadata artifacts are immutable';
        END $$
    """)
    for name in ("job_metadata", "job_metadata_calls"):
        op.execute(f"""
            CREATE TRIGGER immutable_{name} BEFORE UPDATE ON {name}
            FOR EACH ROW EXECUTE FUNCTION immutable_job_metadata()
        """)


def downgrade() -> None:
    op.drop_table("job_metadata_calls")
    op.drop_table("job_metadata")
    op.execute("DROP FUNCTION immutable_job_metadata()")
