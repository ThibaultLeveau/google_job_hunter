"""Single-use recovery bound to a password identity and encrypted delivery."""

import sqlalchemy as sa
from alembic import op

revision = "0007_password_recovery"
down_revision = "0006_email_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_recoveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "identity_id",
            sa.Uuid(),
            sa.ForeignKey("auth_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("credential_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_recovery_user"),
        sa.UniqueConstraint("token_hash", name="uq_recovery_token"),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_recovery_token"),
        sa.CheckConstraint("expires_at > created_at", name="ck_recovery_expiry"),
    )
    op.create_table(
        "recovery_deliveries",
        sa.Column(
            "id",
            sa.Uuid(),
            sa.ForeignKey("password_recoveries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("recovery_deliveries")
    op.drop_table("password_recoveries")
