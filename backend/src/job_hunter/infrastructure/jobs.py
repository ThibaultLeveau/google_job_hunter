from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    delete,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.domain.errors import DomainError
from job_hunter.domain.jobs import (
    CANONICAL_VERSION,
    Discovery,
    JobSource,
    Registration,
    canonical_url,
)
from job_hunter.domain.tasks import require_utc
from job_hunter.infrastructure.database import Base
from job_hunter.infrastructure.searches import SearchRunRecord


class JobSourceRecord(Base):
    __tablename__ = "job_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id", "primary_url"],
            ["job_urls.source_id", "job_urls.canonical_url"],
            name="fk_job_primary_url",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("canonical_version > 0", name="ck_job_source_version"),
        CheckConstraint(
            "crawl_state IN ('pending','fetched','blocked','removed',"
            "'unsupported','non_job','failed')",
            name="ck_job_crawl_state",
        ),
        ForeignKeyConstraint(
            ["id", "current_snapshot_id"],
            ["job_snapshots.source_id", "job_snapshots.id"],
            name="fk_job_current_snapshot",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    primary_url: Mapped[str] = mapped_column(String(2048))
    canonical_version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    current_snapshot_id: Mapped[UUID | None]
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crawl_state: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    crawl_error: Mapped[str | None] = mapped_column(String(32))
    crawl_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobURLRecord(Base):
    __tablename__ = "job_urls"
    __table_args__ = (
        UniqueConstraint("source_id", "canonical_url", name="uq_job_url_source"),
        CheckConstraint(
            "octet_length(canonical_url) BETWEEN 1 AND 2048", name="ck_job_url_length"
        ),
        CheckConstraint("canonical_version > 0", name="ck_job_url_version"),
    )
    canonical_url: Mapped[str] = mapped_column(String(2048), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_sources.id", ondelete="CASCADE")
    )
    canonical_version: Mapped[int]


class RunJobRecord(Base):
    __tablename__ = "run_jobs"
    __table_args__ = (
        CheckConstraint("rank BETWEEN 1 AND 100", name="ck_run_job_rank"),
        CheckConstraint(
            "octet_length(original_url) BETWEEN 1 AND 2048", name="ck_run_job_url"
        ),
        Index("ix_run_job_source", "source_id", "run_id"),
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("search_runs.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_sources.id"), primary_key=True
    )
    original_url: Mapped[str] = mapped_column(String(2048))
    rank: Mapped[int]
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def source_view(row: JobSourceRecord) -> JobSource:
    return JobSource(row.id, row.primary_url, row.canonical_version, row.created_at)


def lock_job_namespace(session: Session, *, exclusive: bool = False) -> None:
    # Discoveries share the namespace; reconciliation excludes writers before run locks.
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    session.execute(text(f"SELECT {function}(817492351)"))


class PostgresJobRegistry:
    def __init__(self, session: Session) -> None:
        self.session = session

    def register(
        self,
        owner: UUID,
        run_id: UUID,
        original_url: str,
        rank: int,
        now: datetime,
    ) -> Registration:
        require_utc(now)
        url = canonical_url(original_url)
        if type(rank) is not int or not 1 <= rank <= 100:
            raise ValueError("Use a rank from 1 to 100.")
        lock_job_namespace(self.session)
        run = self.session.scalar(
            select(SearchRunRecord)
            .where(
                SearchRunRecord.id == run_id,
                SearchRunRecord.owner_id == owner,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise DomainError("not_found")
        if run.status not in {"pending", "running"}:
            raise DomainError("conflict")
        source_id = self.session.scalar(
            select(JobURLRecord.source_id).where(JobURLRecord.canonical_url == url)
        )
        created = False
        if source_id is None:
            candidate = uuid4()
            self.session.add(
                JobSourceRecord(
                    id=candidate,
                    primary_url=url,
                    canonical_version=CANONICAL_VERSION,
                    created_at=now,
                )
            )
            self.session.flush()
            inserted = self.session.scalar(
                insert(JobURLRecord)
                .values(
                    canonical_url=url,
                    source_id=candidate,
                    canonical_version=CANONICAL_VERSION,
                )
                .on_conflict_do_nothing(index_elements=["canonical_url"])
                .returning(JobURLRecord.source_id)
            )
            created = inserted is not None
            source_id = self.session.scalars(
                select(JobURLRecord.source_id).where(JobURLRecord.canonical_url == url)
            ).one()
            if not created:
                self.session.execute(
                    delete(JobSourceRecord).where(JobSourceRecord.id == candidate)
                )
        attached = self.session.scalar(
            insert(RunJobRecord)
            .values(
                run_id=run_id,
                source_id=source_id,
                original_url=original_url,
                rank=rank,
                discovered_at=now,
            )
            .on_conflict_do_nothing(index_elements=["run_id", "source_id"])
            .returning(RunJobRecord.source_id)
        )
        return Registration(source_id, created, attached is not None)

    def add_alias(self, source_id: UUID, url: str) -> None:
        canonical = canonical_url(url)
        lock_job_namespace(self.session)
        if self.session.get(JobSourceRecord, source_id) is None:
            raise DomainError("not_found")
        self.session.execute(
            insert(JobURLRecord)
            .values(
                canonical_url=canonical,
                source_id=source_id,
                canonical_version=CANONICAL_VERSION,
            )
            .on_conflict_do_nothing(index_elements=["canonical_url"])
        )
        actual = self.session.scalar(
            select(JobURLRecord.source_id).where(
                JobURLRecord.canonical_url == canonical
            )
        )
        if actual != source_id:
            raise DomainError("conflict")

    def get(self, owner: UUID, source_id: UUID) -> JobSource:
        row = self.session.scalar(
            select(JobSourceRecord).where(
                JobSourceRecord.id == source_id,
                select(RunJobRecord.run_id)
                .join(SearchRunRecord)
                .where(
                    RunJobRecord.source_id == source_id,
                    SearchRunRecord.owner_id == owner,
                )
                .exists(),
            )
        )
        if row is None:
            raise DomainError("not_found")
        return source_view(row)

    def discoveries(
        self, owner: UUID, run_id: UUID, limit: int = 100, offset: int = 0
    ) -> tuple[Discovery, ...]:
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
            raise ValueError("Use bounded discovery pagination.")
        if (
            self.session.scalar(
                select(SearchRunRecord.id).where(
                    SearchRunRecord.id == run_id,
                    SearchRunRecord.owner_id == owner,
                )
            )
            is None
        ):
            raise DomainError("not_found")
        rows = self.session.execute(
            select(JobSourceRecord, RunJobRecord)
            .join(RunJobRecord)
            .where(
                RunJobRecord.run_id == run_id,
            )
            .order_by(RunJobRecord.rank, RunJobRecord.source_id)
            .limit(limit)
            .offset(offset)
        )
        return tuple(
            Discovery(
                source_view(source),
                run_id,
                row.original_url,
                row.rank,
                row.discovered_at,
            )
            for source, row in rows
        )
