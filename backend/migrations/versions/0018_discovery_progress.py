"""Persist bounded discovery checkpoints and processing failure counts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_discovery_progress"
down_revision = "0017_job_registry"
branch_labels = None
depends_on = None


def counters(extra: str) -> str:
    return (
        "found_results >= 0 AND new_urls >= 0 AND known_urls >= 0 "
        "AND skipped_results >= 0 "
        "AND new_urls + known_urls + skipped_results <= found_results "
        "AND processing_total >= 0 AND " + extra
    )


def upgrade() -> None:
    op.add_column(
        "search_runs",
        sa.Column(
            "discovery",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "search_runs",
        sa.Column(
            "processing_failed", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_check_constraint(
        "ck_search_run_discovery", "search_runs", "jsonb_typeof(discovery) = 'object'"
    )
    op.drop_constraint("ck_search_run_counters", "search_runs", type_="check")
    op.create_check_constraint(
        "ck_search_run_counters",
        "search_runs",
        counters(
            "processing_ready >= 0 AND processing_failed >= 0 "
            "AND processing_ready + processing_failed <= processing_total"
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_search_run_counters", "search_runs", type_="check")
    op.create_check_constraint(
        "ck_search_run_counters",
        "search_runs",
        counters("processing_ready BETWEEN 0 AND processing_total"),
    )
    op.drop_constraint("ck_search_run_discovery", "search_runs", type_="check")
    op.drop_column("search_runs", "processing_failed")
    op.drop_column("search_runs", "discovery")
