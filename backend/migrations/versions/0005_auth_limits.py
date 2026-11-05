"""Share bounded authentication counters without storing raw identifiers."""

import sqlalchemy as sa
from alembic import op

revision = "0005_auth_limits"
down_revision = "0004_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_rate_key",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("secret", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "id = '00000000-0000-0000-0000-000000000000'::uuid "
            "AND octet_length(secret) = 32",
            name="ck_auth_rate_key",
        ),
    )
    op.create_table(
        "auth_rate_buckets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key_hash", name="uq_auth_rate_bucket"),
        sa.CheckConstraint("count BETWEEN 1 AND 30", name="ck_auth_rate_count"),
        sa.CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="ck_auth_rate_hash"),
    )
    op.create_index("ix_auth_rate_expiry", "auth_rate_buckets", ["expires_at"])


def downgrade() -> None:
    op.drop_table("auth_rate_buckets")
    op.drop_table("auth_rate_key")
