"""Versioned private text artifacts and publication tombstones."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_parsed_documents"
down_revision = "0011_private_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column("documents", sa.Column("parse_error", sa.String(32)))
    op.drop_constraint("ck_document_status", "documents")
    op.create_check_constraint(
        "ck_document_status",
        "documents",
        "status IN ('queued','processing','ready','failed','unsupported')",
    )
    op.create_table(
        "parsed_documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("parser_version", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "document_id",
            "document_version",
            "parser_version",
            name="uq_parsed_version",
        ),
        sa.CheckConstraint(
            "document_version > 0 AND parser_version > 0", name="ck_parsed_versions"
        ),
        sa.CheckConstraint(
            "source_hash ~ '^[a-f0-9]{64}$' AND content_hash ~ '^[a-f0-9]{64}$'",
            name="ck_parsed_hashes",
        ),
    )


def downgrade() -> None:
    op.drop_table("parsed_documents")
    op.execute("UPDATE documents SET status='failed' WHERE status='unsupported'")
    op.drop_constraint("ck_document_status", "documents")
    op.create_check_constraint(
        "ck_document_status",
        "documents",
        "status IN ('queued','processing','ready','failed')",
    )
    op.drop_column("documents", "parse_error")
    op.drop_column("users", "deleted_at")
