"""Discard Google session timestamps that were not backed by provider auth_time."""

from alembic import op

revision = "0009_authentication_recency"
down_revision = "0008_google_oidc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE sessions SET authenticated_at = '1970-01-01T00:00:00Z' "
        "WHERE auth_method = 'google'"
    )


def downgrade() -> None:
    # Discarded authentication evidence cannot be reconstructed safely.
    pass
