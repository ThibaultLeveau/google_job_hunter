"""Persist shared host leases and bounded robots policy caches."""

import sqlalchemy as sa
from alembic import op

revision = "0019_crawl_policy"
down_revision = "0018_discovery_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("capacity_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "crawl_hosts",
        sa.Column("host", sa.String(253), primary_key=True),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_crawl_host_lease",
        ),
    )
    op.create_table(
        "crawl_robots",
        sa.Column("origin", sa.String(300), primary_key=True),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('rules','allow','deny','unavailable')",
            name="ck_crawl_robots_decision",
        ),
        sa.CheckConstraint("octet_length(body) <= 512000", name="ck_crawl_robots_size"),
    )


def downgrade() -> None:
    op.drop_table("crawl_robots")
    op.drop_table("crawl_hosts")
    op.drop_column("tasks", "capacity_deadline")
