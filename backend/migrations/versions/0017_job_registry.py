"""One public URL namespace and private run discovery provenance."""

import sqlalchemy as sa
from alembic import op

revision = "0017_job_registry"
down_revision = "0016_saved_search_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("primary_url", sa.String(2048), nullable=False),
        sa.Column("canonical_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("canonical_version > 0", name="ck_job_source_version"),
    )
    op.create_table(
        "job_urls",
        sa.Column("canonical_url", sa.String(2048), primary_key=True),
        sa.Column(
            "source_id",
            sa.Uuid(),
            sa.ForeignKey("job_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("canonical_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("source_id", "canonical_url", name="uq_job_url_source"),
        sa.CheckConstraint(
            "octet_length(canonical_url) BETWEEN 1 AND 2048", name="ck_job_url_length"
        ),
        sa.CheckConstraint("canonical_version > 0", name="ck_job_url_version"),
    )
    op.create_foreign_key(
        "fk_job_primary_url",
        "job_sources",
        "job_urls",
        ["id", "primary_url"],
        ["source_id", "canonical_url"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "run_jobs",
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("search_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "source_id", sa.Uuid(), sa.ForeignKey("job_sources.id"), primary_key=True
        ),
        sa.Column("original_url", sa.String(2048), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rank BETWEEN 1 AND 100", name="ck_run_job_rank"),
        sa.CheckConstraint(
            "octet_length(original_url) BETWEEN 1 AND 2048", name="ck_run_job_url"
        ),
    )
    op.create_index("ix_run_job_source", "run_jobs", ["source_id", "run_id"])


def downgrade() -> None:
    op.drop_table("run_jobs")
    op.drop_constraint("fk_job_primary_url", "job_sources", type_="foreignkey")
    op.drop_table("job_urls")
    op.drop_table("job_sources")
