"""Remove document metadata after cleanup, retaining owner-scoped receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0033_document_removal"
down_revision = "0032_job_retries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_deletions", sa.Column("owner_id", sa.Uuid()))
    op.execute("""
        UPDATE document_deletions AS receipt SET owner_id = document.owner_id
        FROM documents AS document WHERE document.id = receipt.document_id
    """)
    op.alter_column("document_deletions", "owner_id", nullable=False)
    op.create_foreign_key(
        "document_deletions_owner_id_fkey",
        "document_deletions",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_document_deletions_owner_id", "document_deletions", ["owner_id"]
    )
    op.drop_constraint(
        "document_deletions_document_id_fkey", "document_deletions", type_="foreignkey"
    )
    op.drop_constraint(
        "unavailable_profile_sources_document_id_fkey",
        "unavailable_profile_sources",
        type_="foreignkey",
    )
    # Completed cleanup already removed the blob and parsed/extracted content.
    op.execute("""
        DELETE FROM documents USING document_deletions AS receipt
        WHERE documents.id = receipt.document_id AND receipt.completed_at IS NOT NULL
          AND documents.deleted_at IS NOT NULL
    """)


def downgrade() -> None:
    # Erased metadata cannot be restored; drop orphan receipts/markers, not facts.
    for table in ("document_deletions", "unavailable_profile_sources"):
        op.execute(
            f"DELETE FROM {table} WHERE document_id NOT IN (SELECT id FROM documents)"
        )
        op.create_foreign_key(
            f"{table}_document_id_fkey",
            table,
            "documents",
            ["document_id"],
            ["id"],
            ondelete="CASCADE" if table == "document_deletions" else None,
        )
    op.drop_index("ix_document_deletions_owner_id", "document_deletions")
    op.drop_column("document_deletions", "owner_id")
