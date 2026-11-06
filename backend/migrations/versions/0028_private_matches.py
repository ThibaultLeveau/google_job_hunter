"""Keep private match identities and immutable evaluation attempts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0028_private_matches"
down_revision = "0027_job_requirements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "private_matches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("profile_revision", sa.Integer(), nullable=False),
        sa.Column(
            "requirements_id",
            sa.Uuid(),
            sa.ForeignKey("job_requirements.id"),
            nullable=False,
        ),
        sa.Column(
            "content_id", sa.Uuid(), sa.ForeignKey("job_cleanings.id"), nullable=False
        ),
        sa.Column("chunk_version", sa.String(64), nullable=False),
        sa.Column("evaluator_version", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "owner_id",
            "profile_revision",
            "requirements_id",
            "content_id",
            "chunk_version",
            "evaluator_version",
            name="uq_private_match_revision",
        ),
        sa.CheckConstraint("profile_revision >= 0", name="ck_match_profile_revision"),
    )
    op.create_index("ix_private_matches_owner_id", "private_matches", ["owner_id"])
    op.create_table(
        "private_match_artifacts",
        sa.Column(
            "match_id",
            sa.Uuid(),
            sa.ForeignKey("private_matches.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("profile_facts", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_match_generation"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' "
            "AND jsonb_typeof(profile_facts) = 'array'",
            name="ck_match_payload",
        ),
    )
    op.create_table(
        "private_match_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "match_id",
            sa.Uuid(),
            sa.ForeignKey("private_matches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_match_call_generation"),
    )
    op.create_index(
        "ix_private_match_calls_match_id", "private_match_calls", ["match_id"]
    )
    op.execute("""
        CREATE FUNCTION immutable_private_match() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Private match artifacts are immutable';
        END $$
    """)
    for name in ("private_matches", "private_match_artifacts", "private_match_calls"):
        op.execute(f"""
            CREATE TRIGGER immutable_{name} BEFORE UPDATE ON {name}
            FOR EACH ROW EXECUTE FUNCTION immutable_private_match()
        """)


def downgrade() -> None:
    op.drop_table("private_match_calls")
    op.drop_table("private_match_artifacts")
    op.drop_table("private_matches")
    op.execute("DROP FUNCTION immutable_private_match()")
