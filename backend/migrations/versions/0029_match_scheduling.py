"""Queue bounded private matching fan-out and idempotent reevaluation."""

import sqlalchemy as sa
from alembic import op

revision = "0029_match_scheduling"
down_revision = "0028_private_matches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "match_scans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("job_id", sa.Uuid()),
        sa.Column("cursor", sa.Uuid()),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "owner_id IS NOT NULL OR job_id IS NOT NULL", name="ck_match_scan_scope"
        ),
    )
    op.create_table(
        "match_requests",
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.Uuid(), primary_key=True),
        sa.Column(
            "match_id",
            sa.Uuid(),
            sa.ForeignKey("private_matches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("""
        CREATE TRIGGER immutable_match_requests BEFORE UPDATE ON match_requests
        FOR EACH ROW EXECUTE FUNCTION immutable_private_match()
    """)


def downgrade() -> None:
    op.drop_table("match_requests")
    op.drop_table("match_scans")
