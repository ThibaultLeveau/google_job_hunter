"""Transactional review decisions and accepted field provenance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014_profile_review"
down_revision = "0013_profile_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "profile_facts",
        sa.Column("user_fields", JSONB(), nullable=False, server_default="[]"),
    )
    op.execute(
        "UPDATE profile_facts SET user_fields = (SELECT jsonb_agg(key ORDER BY key) "
        "FROM jsonb_object_keys(data) AS key WHERE key <> 'kind')"
    )
    op.alter_column("profile_facts", "user_fields", server_default=None)
    op.create_table(
        "profile_review_requests",
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("profiles.owner_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.Uuid(), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "profile_review_decisions",
        sa.Column(
            "proposal_id",
            sa.Uuid(),
            sa.ForeignKey("profile_proposals.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "profile_fact_sources",
        sa.Column(
            "fact_id",
            sa.Uuid(),
            sa.ForeignKey("profile_facts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("proposal_id", sa.Uuid(), primary_key=True),
        sa.Column("field", sa.String(16), primary_key=True),
        sa.Column("start", sa.Integer(), primary_key=True),
        sa.Column("end", sa.Integer(), primary_key=True),
        sa.ForeignKeyConstraint(
            ["proposal_id", "field", "start", "end"],
            [
                "proposal_sources.proposal_id",
                "proposal_sources.field",
                "proposal_sources.start",
                "proposal_sources.end",
            ],
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("profile_fact_sources")
    op.drop_table("profile_review_decisions")
    op.drop_table("profile_review_requests")
    op.drop_column("profile_facts", "user_fields")
