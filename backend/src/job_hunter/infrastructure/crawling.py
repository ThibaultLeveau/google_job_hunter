from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.crawling import CRAWL, FAILED_STATES, CrawlResult, classify
from job_hunter.application.fetching import FetchError, FetchRequest
from job_hunter.application.worker import Clock, DeferredTaskError, RetryableTaskError
from job_hunter.domain.snapshots import FetchedSnapshot
from job_hunter.domain.tasks import StaleLease, Task, TaskError
from job_hunter.infrastructure.crawl_policy import PoliteFetcher, RobotsDenied
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.jobs import (
    JobSourceRecord,
    RunJobRecord,
    lock_job_namespace,
)
from job_hunter.infrastructure.searches import SearchRunRecord
from job_hunter.infrastructure.snapshots import PostgresSnapshots
from job_hunter.infrastructure.tasks import TaskRecord
from job_hunter.infrastructure.worker import Handler


class CrawlProgressRecord(Base):
    __tablename__ = "crawl_progress"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_crawl_progress_generation"),
        CheckConstraint(
            "jsonb_typeof(visited) = 'array' AND jsonb_array_length(visited) <= 10",
            name="ck_crawl_progress_visited",
        ),
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(primary_key=True)
    requested_url: Mapped[str] = mapped_column(String(2048))
    next_url: Mapped[str] = mapped_column(String(2048))
    visited: Mapped[list[str]] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def refresh_counts(session: Session, source_id: UUID) -> None:
    runs = session.scalars(
        select(SearchRunRecord)
        .join(RunJobRecord)
        .where(RunJobRecord.source_id == source_id)
        .order_by(SearchRunRecord.id)
        .with_for_update(of=SearchRunRecord)
    )
    for run in runs:
        run.processing_failed = (
            session.scalar(
                select(func.count())
                .select_from(RunJobRecord)
                .join(JobSourceRecord)
                .where(
                    RunJobRecord.run_id == run.id,
                    JobSourceRecord.crawl_state.in_(FAILED_STATES),
                )
            )
            or 0
        )


class PostgresCrawler:
    def __init__(
        self, database: Database, fetcher: PoliteFetcher, clock: Clock
    ) -> None:
        self.database, self.fetcher, self.clock = database, fetcher, clock

    def live(self, session: Session, task: Task) -> None:
        if (
            session.scalar(
                select(TaskRecord.id)
                .where(
                    TaskRecord.id == task.id,
                    TaskRecord.status == "running",
                    TaskRecord.generation == task.generation,
                    TaskRecord.lease_token == task.lease_token,
                    TaskRecord.lease_expires_at > self.clock.now(),
                )
                .with_for_update()
            )
            is None
        ):
            raise StaleLease()

    def execute(self, task: Task) -> CrawlResult | None:
        try:
            return self.hop(task)
        except SQLAlchemyError:
            raise RetryableTaskError() from None

    def hop(self, task: Task) -> CrawlResult | None:
        with self.database.transaction() as session:
            self.live(session, task)
            lock_job_namespace(session)
            snapshots = PostgresSnapshots(session)
            source_id = snapshots.resolve(task.key.subject_id)
            source = session.get(JobSourceRecord, source_id)
            if source is None:
                return None
            if task.payload_ref != task.key.subject_id:
                return CrawlResult("failed", "invalid_task")
            if task.key.subject_version == 1 and task.generation == 0:
                if current := snapshots.current(source_id):
                    return classify(
                        FetchedSnapshot(
                            current.requested_url,
                            current.final_url,
                            current.status,
                            current.content_type,
                            current.body,
                            current.fetched_at,
                        )
                    )
            progress = session.get(CrawlProgressRecord, (task.id, task.generation))
            if progress is None:
                progress = CrawlProgressRecord(
                    task_id=task.id,
                    generation=task.generation,
                    requested_url=source.primary_url,
                    next_url=source.primary_url,
                    visited=[],
                    started_at=self.clock.now(),
                )
                session.add(progress)
            progress.started_at = self.clock.now()
            request = FetchRequest(progress.next_url, tuple(progress.visited))
            initial = progress.requested_url
        requested_at = progress.started_at
        try:
            response = self.fetcher.fetch(request)
        except RobotsDenied:
            return CrawlResult("blocked", "robots_denied")
        except FetchError as error:
            if error.status is not None:
                return classify(
                    FetchedSnapshot(
                        initial,
                        request.url,
                        error.status,
                        None,
                        b"",
                        requested_at,
                        request.visited,
                    )
                )
            return CrawlResult("failed", error.code)
        if response.redirect is not None:
            with self.database.transaction() as session:
                self.live(session, task)
                progress = session.get(CrawlProgressRecord, (task.id, task.generation))
                assert progress is not None
                progress.next_url = response.redirect.url
                progress.visited = list(response.redirect.visited)
            raise DeferredTaskError(self.clock.now() + timedelta(seconds=1))
        return classify(
            FetchedSnapshot(
                initial,
                response.url,
                response.status,
                response.content_type,
                response.body,
                requested_at,
                request.visited,
            )
        )

    def publish(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        if not isinstance(result, CrawlResult):
            return
        lock_job_namespace(session, exclusive=True)
        snapshots = PostgresSnapshots(session)
        source_id = snapshots.resolve(task.key.subject_id)
        if result.snapshot is not None:
            snapshot = snapshots.publish(
                source_id, task.id, task.generation, result.snapshot
            )
            source_id = snapshot.source_id
        source = session.get(JobSourceRecord, source_id)
        if source is None:
            return
        progress = session.get(CrawlProgressRecord, (task.id, task.generation))
        observed = (
            result.snapshot.fetched_at
            if result.snapshot
            else progress.started_at
            if progress
            else now
        )
        if any(
            value is not None and value > observed
            for value in (source.last_fetched_at, source.crawl_observed_at)
        ):
            return
        source.crawl_state, source.crawl_error = result.state, result.error
        source.crawl_observed_at = observed
        session.flush()
        refresh_counts(session, source_id)

    def rejected(
        self, session: Session, task: Task, error: TaskError, now: datetime
    ) -> None:
        lock_job_namespace(session, exclusive=True)
        source = session.get(JobSourceRecord, task.key.subject_id)
        progress = session.get(CrawlProgressRecord, (task.id, task.generation))
        observed = progress.started_at if progress else now
        if source is None or any(
            value is not None and value >= observed
            for value in (source.last_fetched_at, source.crawl_observed_at)
        ):
            return
        source.crawl_state, source.crawl_error = "failed", error.value
        source.crawl_observed_at = observed
        session.flush()
        refresh_counts(session, source.id)

    def handlers(self) -> dict[str, Handler]:
        return {
            CRAWL: Handler(
                self.execute,
                self.publish,
                lambda result: (
                    TaskError.PERMANENT
                    if isinstance(result, CrawlResult) and result.state != "fetched"
                    else None
                ),
                self.rejected,
            )
        }
