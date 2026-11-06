"""Owner-scoped accepted facts and aggregate revision fencing."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_profile_facts"
down_revision = "0009_authentication_recency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_profile_revision"),
    )
    op.create_table(
        "profile_facts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("profiles.owner_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("exact_key", sa.String(64), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_id", "kind", "exact_key", name="uq_profile_fact_exact"
        ),
        sa.CheckConstraint(
            "kind IN ('skill', 'experience', 'project')", name="ck_profile_fact_kind"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(data) = 'object' AND coalesce(data->>'kind', '') = kind",
            name="ck_profile_fact_data",
        ),
    )


def downgrade() -> None:
    op.drop_table("profile_facts")
    op.drop_table("profiles")
