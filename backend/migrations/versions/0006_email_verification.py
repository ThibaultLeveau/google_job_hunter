"""Keep verification hashes and encrypted, expiring mail delivery references."""

import sqlalchemy as sa
from alembic import op

revision = "0006_email_verification"
down_revision = "0005_auth_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_verifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_verification_user"),
        sa.UniqueConstraint("token_hash", name="uq_verification_token"),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name="ck_verification_token"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_verification_expiry"),
    )
    op.create_table(
        "verification_deliveries",
        sa.Column(
            "id",
            sa.Uuid(),
            sa.ForeignKey("email_verifications.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("verification_deliveries")
    op.drop_table("email_verifications")
