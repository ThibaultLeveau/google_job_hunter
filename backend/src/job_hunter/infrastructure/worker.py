from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from job_hunter.application.worker import Execute, RetryableTaskError, WorkerStore
from job_hunter.domain.tasks import Task, TaskError
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord, snapshot


@dataclass(frozen=True)
class Handler:
    execute: Execute
    # Recheck the live subject/version; publish and enqueue without external I/O.
    publish: Callable[[Session, Task, object, datetime], None]
    failure: Callable[[object], TaskError | None] = lambda _: None
    rejected: Callable[[Session, Task, TaskError, datetime], None] | None = None


class PostgresWorkerStore(WorkerStore):
    def __init__(self, database: Database, handlers: Mapping[str, Handler]) -> None:
        self.database, self.handlers = database, dict(handlers)

    def claim(
        self, kinds: tuple[str, ...], now: datetime, lease: timedelta
    ) -> Task | None:
        with self.database.transaction() as session:
            return PostgresTaskQueue(session).claim(kinds, now, lease)

    def recover(self, kinds: tuple[str, ...], now: datetime) -> None:
        with self.database.transaction() as session:
            PostgresTaskQueue(session).recover(kinds, now)
            for row in session.scalars(
                select(TaskRecord)
                .where(
                    TaskRecord.kind.in_(kinds),
                    TaskRecord.status == "failed",
                    TaskRecord.error_code == TaskError.EXHAUSTED,
                    TaskRecord.updated_at == now,
                )
                .limit(100)
                .with_for_update(skip_locked=True)
            ):
                rejected = self.handlers[row.kind].rejected
                if rejected is not None:
                    rejected(session, snapshot(row), TaskError.EXHAUSTED, now)

    def renew(self, task: Task, now: datetime, lease: timedelta) -> None:
        with self.database.transaction() as session:
            PostgresTaskQueue(session).renew(task, now, lease)

    def publish(self, task: Task, result: object, now: datetime) -> None:
        try:
            with self.database.transaction() as session:
                handler = self.handlers[task.key.kind]
                error = handler.failure(result)
                queue = PostgresTaskQueue(session)
                if error is None:
                    queue.complete(task, now)
                else:
                    queue.fail(task, error, now)
                handler.publish(session, task, result, now)
        except SQLAlchemyError:
            raise RetryableTaskError() from None

    def reject(
        self, task: Task, error: TaskError, retry_at: datetime | None, now: datetime
    ) -> None:
        with self.database.transaction() as session:
            queue = PostgresTaskQueue(session)
            if retry_at is None:
                queue.fail(task, error, now)
                rejected = self.handlers[task.key.kind].rejected
                if rejected is not None:
                    rejected(session, task, error, now)
            else:
                queue.defer(task, retry_at, now)

    def postpone(self, task: Task, retry_at: datetime, now: datetime) -> None:
        with self.database.transaction() as session:
            result = PostgresTaskQueue(session).postpone(task, retry_at, now)
            rejected = self.handlers[task.key.kind].rejected
            if result.status == "failed" and rejected is not None:
                rejected(session, task, TaskError.EXHAUSTED, now)
