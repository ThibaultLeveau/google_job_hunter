"""Keep full requirements alongside bounded display summaries."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0027_job_requirements"
down_revision = "0026_job_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("job_requirements", "job_requirements_calls"):
        op.create_table(
            name,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "metadata_id",
                sa.Uuid(),
                sa.ForeignKey("job_metadata.id"),
                nullable=False,
            ),
            sa.Column("version", sa.String(64), nullable=False),
            sa.Column("payload", JSONB(), nullable=False),
        )
    op.create_unique_constraint(
        "uq_requirements_revision", "job_requirements", ["metadata_id", "version"]
    )
    op.create_check_constraint(
        "ck_requirements_payload",
        "job_requirements",
        "jsonb_typeof(payload) = 'object'",
    )
    op.execute("""
        CREATE FUNCTION immutable_job_requirements() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Requirements artifacts are immutable';
        END $$
    """)
    for name in ("job_requirements", "job_requirements_calls"):
        op.execute(f"""
            CREATE TRIGGER immutable_{name} BEFORE UPDATE ON {name}
            FOR EACH ROW EXECUTE FUNCTION immutable_job_requirements()
        """)


def downgrade() -> None:
    op.drop_table("job_requirements_calls")
    op.drop_table("job_requirements")
    op.execute("DROP FUNCTION immutable_job_requirements()")
