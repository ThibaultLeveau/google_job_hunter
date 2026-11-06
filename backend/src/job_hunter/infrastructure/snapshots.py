from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    LargeBinary,
    String,
    UniqueConstraint,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.domain.errors import DomainError
from job_hunter.domain.jobs import CANONICAL_VERSION, canonical_url
from job_hunter.domain.snapshots import FetchedSnapshot
from job_hunter.infrastructure.database import Base
from job_hunter.infrastructure.jobs import (
    JobSourceRecord,
    JobURLRecord,
    RunJobRecord,
    lock_job_namespace,
)
from job_hunter.infrastructure.searches import SearchRunRecord


class SnapshotRecord(Base):
    __tablename__ = "job_snapshots"
    __table_args__ = (
        UniqueConstraint("source_id", "id", name="uq_snapshot_source"),
        CheckConstraint("octet_length(body) <= 16777216", name="ck_snapshot_size"),
        CheckConstraint(
            "status BETWEEN 200 AND 599 AND format_version > 0",
            name="ck_snapshot_status",
        ),
        CheckConstraint(
            "content_hash = encode(sha256(body), 'hex')", name="ck_snapshot_hash"
        ),
        CheckConstraint(
            "octet_length(requested_url) BETWEEN 1 AND 2048 "
            "AND octet_length(final_url) BETWEEN 1 AND 2048",
            name="ck_snapshot_urls",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("job_sources.id"))
    lineage_id: Mapped[UUID]
    version: Mapped[int] = mapped_column(Identity(), unique=True)
    previous_id: Mapped[UUID | None] = mapped_column(ForeignKey("job_snapshots.id"))
    format_version: Mapped[int]
    requested_url: Mapped[str] = mapped_column(String(2048))
    final_url: Mapped[str] = mapped_column(String(2048))
    status: Mapped[int]
    content_type: Mapped[str | None] = mapped_column(String(256))
    body: Mapped[bytes] = mapped_column(LargeBinary)
    content_hash: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CrawlDeliveryRecord(Base):
    __tablename__ = "crawl_deliveries"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_crawl_delivery_generation"),
    )
    task_id: Mapped[UUID] = mapped_column(primary_key=True)
    generation: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("job_snapshots.id"))
    requested_url: Mapped[str] = mapped_column(String(2048))
    final_url: Mapped[str] = mapped_column(String(2048))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceMergeRecord(Base):
    __tablename__ = "job_source_merges"
    __table_args__ = (
        CheckConstraint("old_id <> source_id", name="ck_source_merge_identity"),
    )
    old_id: Mapped[UUID] = mapped_column(primary_key=True)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("job_sources.id"))


class DiscoveryHistoryRecord(Base):
    __tablename__ = "job_discovery_history"
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("search_runs.id", ondelete="CASCADE"), primary_key=True
    )
    original_source_id: Mapped[UUID] = mapped_column(primary_key=True)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("job_sources.id"))
    original_url: Mapped[str] = mapped_column(String(2048))
    rank: Mapped[int]
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PostgresSnapshots:
    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve(self, source_id: UUID) -> UUID:
        return (
            self.session.scalar(
                select(SourceMergeRecord.source_id).where(
                    SourceMergeRecord.old_id == source_id
                )
            )
            or source_id
        )

    def delivered(self, task_id: UUID, generation: int) -> SnapshotRecord | None:
        return self.session.scalar(
            select(SnapshotRecord)
            .join(CrawlDeliveryRecord)
            .where(
                CrawlDeliveryRecord.task_id == task_id,
                CrawlDeliveryRecord.generation == generation,
            )
        )

    def current(self, source_id: UUID) -> SnapshotRecord | None:
        return self.session.scalar(
            select(SnapshotRecord)
            .join(
                JobSourceRecord,
                JobSourceRecord.current_snapshot_id == SnapshotRecord.id,
            )
            .where(JobSourceRecord.id == self.resolve(source_id))
        )

    def publish(
        self, source_id: UUID, task_id: UUID, generation: int, fetched: FetchedSnapshot
    ) -> SnapshotRecord:
        lock_job_namespace(self.session, exclusive=True)
        if previous := self.delivered(task_id, generation):
            return previous
        source_id = self.resolve(source_id)
        source = self.session.get(JobSourceRecord, source_id)
        if source is None:
            raise DomainError("not_found")
        # Private discovery tracking values must never enter shared public snapshots.
        if fetched.requested_url != canonical_url(fetched.requested_url):
            raise DomainError("conflict")
        if (
            self.session.scalar(
                select(JobURLRecord.source_id).where(
                    JobURLRecord.canonical_url == canonical_url(fetched.requested_url)
                )
            )
            != source_id
        ):
            raise DomainError("conflict")
        fresh = (
            source.last_fetched_at is None
            or fetched.fetched_at >= source.last_fetched_at
        )
        if fresh:
            final = canonical_url(fetched.final_url)
            aliases = sorted(
                {final, *(canonical_url(url) for url in fetched.redirects)}
            )
            existing = dict(
                self.session.execute(
                    select(JobURLRecord.canonical_url, JobURLRecord.source_id).where(
                        JobURLRecord.canonical_url.in_(aliases)
                    )
                ).all()
            )
            target_id = existing.get(final, source_id)
            for old_id in sorted({source_id, *existing.values()} - {target_id}):
                self._merge(old_id, target_id)
            source = self.session.get(JobSourceRecord, target_id)
            assert source is not None
            for alias in aliases:
                self.session.execute(
                    insert(JobURLRecord)
                    .values(
                        canonical_url=alias,
                        source_id=target_id,
                        canonical_version=CANONICAL_VERSION,
                    )
                    .on_conflict_do_nothing()
                )
        current = self.current(source.id)
        if current is not None and (
            current.content_hash,
            current.status,
            current.content_type,
            current.format_version,
        ) == (fetched.content_hash, fetched.status, fetched.content_type, 1):
            result = current
        else:
            result = SnapshotRecord(
                id=uuid4(),
                source_id=source.id,
                lineage_id=source.id,
                previous_id=current.id if current else None,
                format_version=1,
                requested_url=fetched.requested_url,
                final_url=fetched.final_url,
                status=fetched.status,
                content_type=fetched.content_type,
                body=fetched.body,
                content_hash=fetched.content_hash,
                fetched_at=fetched.fetched_at,
            )
            self.session.add(result)
            self.session.flush()
        if (
            source.last_fetched_at is None
            or fetched.fetched_at >= source.last_fetched_at
        ):
            source.current_snapshot_id = result.id
            source.last_fetched_at = fetched.fetched_at
        self.session.add(
            CrawlDeliveryRecord(
                task_id=task_id,
                generation=generation,
                snapshot_id=result.id,
                requested_url=fetched.requested_url,
                final_url=fetched.final_url,
                fetched_at=fetched.fetched_at,
            )
        )
        self.session.flush()
        return result

    def _merge(self, old_id: UUID, target_id: UUID) -> None:
        # The namespace lock precedes run/source changes; there is no external I/O.
        old = self.session.get(JobSourceRecord, old_id)
        target = self.session.get(JobSourceRecord, target_id)
        assert old is not None and target is not None
        rows = self.session.scalars(
            select(RunJobRecord).where(RunJobRecord.source_id.in_((old_id, target_id)))
        ).all()
        preserved = set(
            self.session.execute(
                select(
                    DiscoveryHistoryRecord.run_id, DiscoveryHistoryRecord.source_id
                ).where(DiscoveryHistoryRecord.source_id.in_((old_id, target_id)))
            )
        )
        for row in rows:
            if (row.run_id, row.source_id) not in preserved:
                self.session.execute(
                    insert(DiscoveryHistoryRecord)
                    .values(
                        run_id=row.run_id,
                        original_source_id=row.source_id,
                        source_id=target_id,
                        original_url=row.original_url,
                        rank=row.rank,
                        discovered_at=row.discovered_at,
                    )
                    .on_conflict_do_nothing()
                )
            if row.source_id == old_id:
                self.session.execute(
                    insert(RunJobRecord)
                    .values(
                        run_id=row.run_id,
                        source_id=target_id,
                        original_url=row.original_url,
                        rank=row.rank,
                        discovered_at=row.discovered_at,
                    )
                    .on_conflict_do_nothing()
                )
        self.session.execute(
            delete(RunJobRecord).where(RunJobRecord.source_id == old_id)
        )
        for model in (
            SnapshotRecord,
            JobURLRecord,
            DiscoveryHistoryRecord,
            SourceMergeRecord,
        ):
            self.session.execute(
                update(model)
                .where(model.source_id == old_id)
                .values(source_id=target_id)
            )
        self.session.add(SourceMergeRecord(old_id=old_id, source_id=target_id))
        if target.current_snapshot_id is None:
            target.current_snapshot_id = old.current_snapshot_id
            target.last_fetched_at = old.last_fetched_at
        self.session.delete(old)
        self.session.flush()
        for run_id in sorted({row.run_id for row in rows}):
            run = self.session.get(SearchRunRecord, run_id, with_for_update=True)
            assert run is not None
            run.processing_total = (
                self.session.scalar(
                    select(func.count())
                    .select_from(RunJobRecord)
                    .where(RunJobRecord.run_id == run_id)
                )
                or 0
            )
            run.processing_ready = min(run.processing_ready, run.processing_total)
            run.processing_failed = min(
                run.processing_failed, run.processing_total - run.processing_ready
            )
