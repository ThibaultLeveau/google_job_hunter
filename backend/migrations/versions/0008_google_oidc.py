"""Single-use OIDC flow state and Google-authenticated sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0008_google_oidc"
down_revision = "0007_password_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oidc_flows",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("encrypted_flow", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("state_hash", name="uq_oidc_state"),
    )
    op.create_index("ix_oidc_expiry", "oidc_flows", ["expires_at"])
    op.drop_constraint("ck_sessions_method", "sessions", type_="check")
    op.create_check_constraint(
        "ck_sessions_method", "sessions", "auth_method IN ('password', 'google')"
    )


def downgrade() -> None:
    op.drop_table("oidc_flows")
    op.execute("DELETE FROM sessions WHERE auth_method = 'google'")
    op.drop_constraint("ck_sessions_method", "sessions", type_="check")
    op.create_check_constraint(
        "ck_sessions_method", "sessions", "auth_method = 'password'"
    )
