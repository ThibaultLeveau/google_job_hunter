"""Private saved definitions, immutable run inputs and bounded progress counters."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_saved_search_runs"
down_revision = "0015_document_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("id", "owner_id", name="uq_saved_search_owner"),
        sa.CheckConstraint("revision > 0", name="ck_saved_search_revision"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_saved_search_name"),
        sa.CheckConstraint(
            "jsonb_typeof(definition) = 'object'", name="ck_saved_search_definition"
        ),
    )
    op.create_index(
        "ix_saved_search_owner", "saved_searches", ["owner_id", "created_at", "id"]
    )
    op.create_table(
        "search_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("search_id", sa.Uuid(), nullable=False),
        sa.Column("search_revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        *(
            sa.Column(name, sa.Integer(), nullable=False)
            for name in (
                "found_results",
                "new_urls",
                "known_urls",
                "skipped_results",
                "processing_total",
                "processing_ready",
            )
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["search_id", "owner_id"],
            ["saved_searches.id", "saved_searches.owner_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_search_run_key"),
        sa.CheckConstraint("search_revision > 0", name="ck_search_run_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'", name="ck_search_run_snapshot"
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','partial',"
            "'failed','cancelled')",
            name="ck_search_run_status",
        ),
        sa.CheckConstraint(
            "found_results >= 0 AND new_urls >= 0 AND known_urls >= 0 "
            "AND skipped_results >= 0 "
            "AND new_urls + known_urls + skipped_results <= found_results "
            "AND processing_total >= 0 "
            "AND processing_ready BETWEEN 0 AND processing_total",
            name="ck_search_run_counters",
        ),
    )
    op.create_index(
        "ix_search_run_owner", "search_runs", ["owner_id", "created_at", "id"]
    )
    op.execute("""
        CREATE FUNCTION preserve_search_run_inputs() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.id, NEW.owner_id, NEW.search_id, NEW.search_revision,
                   NEW.idempotency_key, NEW.snapshot, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.owner_id, OLD.search_id, OLD.search_revision,
                   OLD.idempotency_key, OLD.snapshot, OLD.created_at) THEN
                RAISE EXCEPTION 'Search run inputs are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER search_run_inputs BEFORE UPDATE ON search_runs
        FOR EACH ROW EXECUTE FUNCTION preserve_search_run_inputs();
    """)


def downgrade() -> None:
    op.drop_table("search_runs")
    op.execute("DROP FUNCTION preserve_search_run_inputs()")
    op.drop_table("saved_searches")
