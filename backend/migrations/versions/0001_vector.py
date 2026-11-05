"""Enable pgvector without creating feature tables."""

from alembic import op

revision = "0001_vector"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION vector")
