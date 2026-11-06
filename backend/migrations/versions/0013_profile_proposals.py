"""Immutable document proposals and constrained source associations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013_profile_proposals"
down_revision = "0012_parsed_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profile_extractions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "parsed_id",
            sa.Uuid(),
            sa.ForeignKey("parsed_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_key", sa.String(64), nullable=False),
        sa.Column("accounting", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("parsed_id", "version_key", name="uq_extraction_version"),
    )
    op.create_table(
        "profile_proposals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "extraction_id",
            sa.Uuid(),
            sa.ForeignKey("profile_extractions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("data", JSONB(), nullable=False),
        sa.UniqueConstraint("extraction_id", "ordinal", name="uq_proposal_ordinal"),
        sa.CheckConstraint("ordinal >= 0", name="ck_proposal_ordinal"),
    )
    op.create_table(
        "proposal_sources",
        sa.Column(
            "proposal_id",
            sa.Uuid(),
            sa.ForeignKey("profile_proposals.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("field", sa.String(16), primary_key=True),
        sa.Column("start", sa.Integer(), primary_key=True),
        sa.Column("end", sa.Integer(), primary_key=True),
        sa.Column("locator", sa.String(32), nullable=False),
        sa.Column("quote", sa.String(4000), nullable=False),
        sa.CheckConstraint('start >= 0 AND "end" > start', name="ck_proposal_span"),
    )


def downgrade() -> None:
    op.drop_table("proposal_sources")
    op.drop_table("profile_proposals")
    op.drop_table("profile_extractions")
