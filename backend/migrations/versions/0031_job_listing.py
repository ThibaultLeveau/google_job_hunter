"""Index owner-specific match history and source alias lookup for job listing."""

from alembic import op

revision = "0031_job_listing"
down_revision = "0030_provider_budgets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_match_owner_job", "private_matches", ["owner_id", "job_id"])
    op.create_index(
        "ix_source_merge_target", "job_source_merges", ["source_id", "old_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_source_merge_target", "job_source_merges")
    op.drop_index("ix_match_owner_job", "private_matches")
