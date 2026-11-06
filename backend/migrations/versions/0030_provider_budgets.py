"""Reserve provider capacity atomically and retain uncertain charges."""

import sqlalchemy as sa
from alembic import op

revision = "0030_provider_budgets"
down_revision = "0029_match_scheduling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_budget_buckets",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("unit", sa.String(8), primary_key=True),
        sa.Column("scope", sa.String(8), primary_key=True),
        sa.Column("subject", sa.Uuid(), primary_key=True),
        sa.Column("charged", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("unit IN ('search','tokens')", name="ck_budget_bucket_unit"),
        sa.CheckConstraint("scope IN ('global','owner','run')", name="ck_budget_scope"),
        sa.CheckConstraint("charged >= 0", name="ck_budget_bucket_charge"),
    )
    op.create_table(
        "provider_budget_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("unit", sa.String(8), nullable=False),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("search_runs.id", ondelete="SET NULL")
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("ceiling", sa.BigInteger(), nullable=False),
        sa.Column("charged", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "unit IN ('search','tokens')", name="ck_budget_reservation_unit"
        ),
        sa.CheckConstraint(
            "ceiling > 0 AND charged BETWEEN 0 AND ceiling", name="ck_budget_charge"
        ),
        sa.CheckConstraint(
            "state IN ('reserved','unknown','settled')", name="ck_budget_state"
        ),
        sa.CheckConstraint(
            "state = 'settled' OR charged = ceiling", name="ck_budget_uncertain"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_budget_expiry"),
    )
    op.create_index(
        "ix_provider_budget_reservations_expires_at",
        "provider_budget_reservations",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("provider_budget_reservations")
    op.drop_table("provider_budget_buckets")
