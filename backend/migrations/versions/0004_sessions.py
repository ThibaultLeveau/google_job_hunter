"""Persist revocable opaque sessions and authentication recency."""

import sqlalchemy as sa
from alembic import op

revision = "0004_sessions"
down_revision = "0003_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authenticated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("auth_method", sa.String(16), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token"),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_sessions_token"),
        sa.CheckConstraint("expires_at > authenticated_at", name="ck_sessions_expiry"),
        sa.CheckConstraint("auth_method = 'password'", name="ck_sessions_method"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])


def downgrade() -> None:
    op.drop_table("sessions")
