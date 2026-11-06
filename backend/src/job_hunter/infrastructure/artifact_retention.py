import argparse
import json
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    String,
    Table,
    delete,
    func,
    or_,
    select,
    text,
    tuple_,
)
from sqlalchemy import (
    cast as sql_cast,
)
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from job_hunter.application.artifact_retention import (
    ArtifactBatch,
    ArtifactCheckpoint,
    ArtifactPolicy,
)
from job_hunter.infrastructure.chunks import ChunkBatchRecord, ChunkRecord
from job_hunter.infrastructure.cleaning import CleaningCallRecord, CleaningRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.embeddings import EmbeddingCallRecord, EmbeddingRecord
from job_hunter.infrastructure.job_metadata import MetadataCallRecord, MetadataRecord
from job_hunter.infrastructure.jobs import JobSourceRecord
from job_hunter.infrastructure.matches import MatchIdentityRecord
from job_hunter.infrastructure.preparation import PreparationRecord
from job_hunter.infrastructure.requirements import (
    RequirementsCallRecord,
    RequirementsRecord,
)
from job_hunter.infrastructure.snapshots import CrawlDeliveryRecord, SnapshotRecord
from job_hunter.infrastructure.tasks import TaskRecord
from job_hunter.settings import load_settings


class Protected(Exception):
    pass


class Oversized(Exception):
    pass


def graph(snapshot: UUID) -> list[tuple[Table, ColumnElement[bool]]]:
    prepared = select(PreparationRecord.id).where(
        PreparationRecord.snapshot_id == snapshot
    )
    cleaned = select(CleaningRecord.id).where(
        CleaningRecord.preparation_id.in_(prepared)
    )
    metadata = select(MetadataRecord.id).where(
        MetadataRecord.preparation_id.in_(prepared)
    )
    chunks = select(ChunkRecord.id).where(ChunkRecord.content_id.in_(cleaned))
    chunk_strings = (
        select(func.array_agg(sql_cast(ChunkRecord.id, String)))
        .where(ChunkRecord.id.in_(chunks))
        .scalar_subquery()
    )
    # Parents are locked first; deletion runs in reverse dependency order.
    return [
        (cast(Table, model.__table__), condition)
        for model, condition in (
            (PreparationRecord, PreparationRecord.id.in_(prepared)),
            (CleaningRecord, CleaningRecord.id.in_(cleaned)),
            (MetadataRecord, MetadataRecord.id.in_(metadata)),
            (RequirementsRecord, RequirementsRecord.metadata_id.in_(metadata)),
            (CleaningCallRecord, CleaningCallRecord.preparation_id.in_(prepared)),
            (MetadataCallRecord, MetadataCallRecord.preparation_id.in_(prepared)),
            (RequirementsCallRecord, RequirementsCallRecord.metadata_id.in_(metadata)),
            (ChunkBatchRecord, ChunkBatchRecord.content_id.in_(cleaned)),
            (ChunkRecord, ChunkRecord.id.in_(chunks)),
            (
                EmbeddingCallRecord,
                EmbeddingCallRecord.chunk_ids.op("?|")(chunk_strings),
            ),
            (EmbeddingRecord, EmbeddingRecord.chunk_id.in_(chunks)),
        )
    ]


def collect(
    session: Session, root: SnapshotRecord, cutoff: datetime, budget: int
) -> list[tuple[Table, list[tuple[object, ...]]]]:
    if root.fetched_at > cutoff:
        raise Protected()
    # The current-snapshot foreign key is deferred until commit.
    session.get(JobSourceRecord, root.source_id, with_for_update={"nowait": True})
    if session.scalar(
        select(JobSourceRecord.id)
        .where(JobSourceRecord.current_snapshot_id == root.id)
        .limit(1)
    ):
        raise Protected()
    plans: list[tuple[Table, list[tuple[object, ...]]]] = []
    references = {root.id, root.source_id}
    for table, condition in graph(root.id):
        keys = list(table.primary_key.columns)
        rows = [
            tuple(row)
            for row in session.execute(
                select(*keys)
                .where(condition)
                .order_by(*keys)
                .limit(budget + 1)
                .with_for_update(nowait=True)
            )
        ]
        budget -= len(rows)
        if budget < 0:
            raise Oversized()
        plans.append((table, rows))
        references.update(
            value for row in rows for value in row if isinstance(value, UUID)
        )
    # Task keys remain durable. Lock even terminal rows against a simultaneous retry.
    tasks = list(
        session.scalars(
            select(TaskRecord)
            .where(
                or_(
                    TaskRecord.subject_id.in_(references),
                    TaskRecord.payload_ref.in_(references),
                )
            )
            .limit(1001)
            .with_for_update(nowait=True)
        )
    )
    if len(tasks) > 1000 or any(
        task.status not in ("succeeded", "cancelled") or task.updated_at > cutoff
        for task in tasks
    ):
        raise Protected()
    if session.scalar(
        select(MatchIdentityRecord.id)
        .where(
            or_(
                MatchIdentityRecord.content_id.in_(references),
                MatchIdentityRecord.requirements_id.in_(references),
            )
        )
        .limit(1)
    ):
        raise Protected()
    chunk_ids = {
        str(row[0])
        for table, rows in plans
        if table.name == "job_chunks"
        for row in rows
    }
    calls = [
        row[0]
        for table, rows in plans
        if table.name == "job_embedding_calls"
        for row in rows
    ]
    for ids in session.scalars(
        select(EmbeddingCallRecord.chunk_ids).where(EmbeddingCallRecord.id.in_(calls))
    ):
        if not set(ids) <= chunk_ids:
            raise Protected()
    # Lineage and crawl receipts still reference their immutable source bytes.
    referenced = session.scalar(
        select(SnapshotRecord.id).where(SnapshotRecord.previous_id == root.id).limit(1)
    ) or session.scalar(
        select(CrawlDeliveryRecord.task_id)
        .where(CrawlDeliveryRecord.snapshot_id == root.id)
        .limit(1)
    )
    if not referenced:
        if budget < 1:
            raise Oversized()
        plans.insert(0, (cast(Table, SnapshotRecord.__table__), [(root.id,)]))
    return plans


class PostgresArtifactRetention:
    def __init__(self, database: Database, now: Callable[[], datetime]) -> None:
        self.database, self.now = database, now

    def batch(
        self,
        policy: ArtifactPolicy,
        *,
        dry_run: bool = True,
        checkpoint: str | None = None,
    ) -> ArtifactBatch:
        cursor = ArtifactCheckpoint.start(self.now(), policy, dry_run, checkpoint)
        counts: dict[str, int] = {}
        scanned = protected = oversized = contended = 0
        after = cursor.after
        done = False
        with self.database.transaction() as session:
            # Producers may already hold task/row locks before taking the namespace.
            # Never wait in the reverse order; leave busy candidates for the next pass.
            if not session.scalar(text("SELECT pg_try_advisory_xact_lock(817492351)")):
                return ArtifactBatch(dry_run, {}, 0, 0, 0, 1, cursor.encode())
            for _ in range(policy.scan_limit):
                query = select(SnapshotRecord.id).where(
                    SnapshotRecord.fetched_at <= cursor.cutoff
                )
                if after is not None:
                    query = query.where(SnapshotRecord.id > after)
                candidate = session.scalar(query.order_by(SnapshotRecord.id).limit(1))
                if candidate is None:
                    done = True
                    break
                try:
                    with session.begin_nested():
                        root = session.get(
                            SnapshotRecord, candidate, with_for_update={"nowait": True}
                        )
                        if root is None:
                            raise Protected()
                        remaining = policy.row_limit - sum(counts.values())
                        plans = collect(session, root, cursor.cutoff, remaining)
                        if not dry_run:
                            for table, rows in reversed(plans):
                                if rows:
                                    session.execute(
                                        delete(table).where(
                                            tuple_(*table.primary_key.columns).in_(rows)
                                        )
                                    )
                    for table, rows in plans:
                        if rows:
                            counts[table.name] = counts.get(table.name, 0) + len(rows)
                except Protected:
                    protected += 1
                except Oversized:
                    if counts:
                        break
                    oversized += 1
                except DBAPIError as error:
                    if getattr(error.orig, "sqlstate", None) not in ("55P03", "23503"):
                        raise
                    contended += 1
                scanned += 1
                after = candidate
        next_cursor = (
            None if done else cursor.model_copy(update={"after": after}).encode()
        )
        return ArtifactBatch(
            dry_run, counts, scanned, protected, oversized, contended, next_cursor
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reclaim unreferenced old public processing artifacts."
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=os.environ.get("JOB_HUNTER_ARTIFACT_RETENTION_DAYS", "30"),
    )
    parser.add_argument("--scan-limit", type=int, default=20)
    parser.add_argument("--row-limit", type=int, default=10000)
    parser.add_argument("--checkpoint")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete eligible artifacts; otherwise only report counts.",
    )
    options = parser.parse_args()
    database = None
    try:
        policy = ArtifactPolicy(
            options.retention_days, options.scan_limit, options.row_limit
        )
        database = Database(load_settings())
        result = PostgresArtifactRetention(database, lambda: datetime.now(UTC)).batch(
            policy, dry_run=not options.apply, checkpoint=options.checkpoint
        )
        print(json.dumps(asdict(result), sort_keys=True))
    except ValueError:
        parser.error("Invalid retention settings or checkpoint.")
    except SQLAlchemyError:
        parser.exit(1, "Artifact cleanup failed; retry the same checkpoint.\n")
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    main()
