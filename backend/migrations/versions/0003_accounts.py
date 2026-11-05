"""Persist unverified users and uniquely bound authentication identities."""

import sqlalchemy as sa
from alembic import op

revision = "0003_accounts"
down_revision = "0002_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("email_normalized", sa.String(320), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email_normalized", name="uq_users_email"),
        sa.CheckConstraint(
            "email_normalized = lower(email_normalized)",
            name="ck_users_email_normalized",
        ),
    )
    op.create_table(
        "auth_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("issuer", "subject", name="uq_auth_identities_provider"),
        sa.CheckConstraint(
            "length(issuer) > 0 AND length(subject) > 0", name="ck_auth_identity_key"
        ),
        sa.CheckConstraint(
            "(issuer = 'password' AND password_hash IS NOT NULL "
            "AND password_hash LIKE '$argon2id$%') OR "
            "(issuer <> 'password' AND password_hash IS NULL)",
            name="ck_auth_identity_password",
        ),
    )
    op.create_index("ix_auth_identities_user_id", "auth_identities", ["user_id"])


def downgrade() -> None:
    op.drop_table("auth_identities")
    op.drop_table("users")
