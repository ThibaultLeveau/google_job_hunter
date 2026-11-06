"""Preserve public content lineage and transactional source reconciliation."""

import sqlalchemy as sa
from alembic import op

revision = "0020_job_snapshots"
down_revision = "0019_crawl_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_id", sa.Uuid(), sa.ForeignKey("job_sources.id"), nullable=False
        ),
        sa.Column("lineage_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), sa.Identity(), nullable=False, unique=True),
        sa.Column(
            "previous_id", sa.Uuid(), sa.ForeignKey("job_snapshots.id"), nullable=True
        ),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("requested_url", sa.String(2048), nullable=False),
        sa.Column("final_url", sa.String(2048), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(256), nullable=True),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "id", name="uq_snapshot_source"),
        sa.CheckConstraint("octet_length(body) <= 16777216", name="ck_snapshot_size"),
        sa.CheckConstraint(
            "status BETWEEN 200 AND 599 AND format_version > 0",
            name="ck_snapshot_status",
        ),
        sa.CheckConstraint(
            "content_hash = encode(sha256(body), 'hex')", name="ck_snapshot_hash"
        ),
        sa.CheckConstraint(
            "octet_length(requested_url) BETWEEN 1 AND 2048 "
            "AND octet_length(final_url) BETWEEN 1 AND 2048",
            name="ck_snapshot_urls",
        ),
    )
    op.add_column(
        "job_sources", sa.Column("current_snapshot_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "job_sources",
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_job_current_snapshot",
        "job_sources",
        "job_snapshots",
        ["id", "current_snapshot_id"],
        ["source_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "crawl_deliveries",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column(
            "snapshot_id", sa.Uuid(), sa.ForeignKey("job_snapshots.id"), nullable=False
        ),
        sa.Column("requested_url", sa.String(2048), nullable=False),
        sa.Column("final_url", sa.String(2048), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 0", name="ck_crawl_delivery_generation"),
    )
    op.create_table(
        "job_source_merges",
        sa.Column("old_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_id", sa.Uuid(), sa.ForeignKey("job_sources.id"), nullable=False
        ),
        sa.CheckConstraint("old_id <> source_id", name="ck_source_merge_identity"),
    )
    op.create_table(
        "job_discovery_history",
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("search_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("original_source_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_id", sa.Uuid(), sa.ForeignKey("job_sources.id"), nullable=False
        ),
        sa.Column("original_url", sa.String(2048), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("""
        CREATE FUNCTION immutable_job_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.id, NEW.lineage_id, NEW.version, NEW.previous_id,
                NEW.format_version,
                NEW.requested_url, NEW.final_url, NEW.status, NEW.content_type,
                NEW.body, NEW.content_hash, NEW.fetched_at) IS DISTINCT FROM
               (OLD.id, OLD.lineage_id, OLD.version, OLD.previous_id,
                OLD.format_version,
                OLD.requested_url, OLD.final_url, OLD.status, OLD.content_type,
                OLD.body, OLD.content_hash, OLD.fetched_at) THEN
                RAISE EXCEPTION 'Job snapshots are immutable';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER immutable_job_snapshot BEFORE UPDATE ON job_snapshots
        FOR EACH ROW EXECUTE FUNCTION immutable_job_snapshot();
    """)


def downgrade() -> None:
    op.drop_table("job_discovery_history")
    op.drop_table("job_source_merges")
    op.drop_table("crawl_deliveries")
    op.drop_constraint("fk_job_current_snapshot", "job_sources", type_="foreignkey")
    op.drop_column("job_sources", "current_snapshot_id")
    op.drop_column("job_sources", "last_fetched_at")
    op.drop_table("job_snapshots")
    op.execute("DROP FUNCTION immutable_job_snapshot()")
