"""Private upload references and owner-scoped exact deduplication."""

import sqlalchemy as sa
from alembic import op

revision = "0011_private_documents"
down_revision = "0010_profile_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(160), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("owner_id", "content_hash", name="uq_document_content"),
        sa.UniqueConstraint("storage_key", name="uq_document_storage"),
        sa.CheckConstraint("size_bytes > 0 AND version > 0", name="ck_document_size"),
        sa.CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'", name="ck_document_hash"),
        sa.CheckConstraint("storage_key ~ '^[a-f0-9]{32}$'", name="ck_document_key"),
        sa.CheckConstraint(
            "status IN ('queued','processing','ready','failed')",
            name="ck_document_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("documents")
