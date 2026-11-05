from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from job_hunter.application.worker import Execute, RetryableTaskError, WorkerStore
from job_hunter.domain.tasks import Task, TaskError
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.tasks import PostgresTaskQueue


@dataclass(frozen=True)
class Handler:
    execute: Execute
    # Recheck the live subject/version; publish and enqueue without external I/O.
    publish: Callable[[Session, Task, object, datetime], None]


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

    def renew(self, task: Task, now: datetime, lease: timedelta) -> None:
        with self.database.transaction() as session:
            PostgresTaskQueue(session).renew(task, now, lease)

    def publish(self, task: Task, result: object, now: datetime) -> None:
        try:
            with self.database.transaction() as session:
                PostgresTaskQueue(session).complete(task, now)
                self.handlers[task.key.kind].publish(session, task, result, now)
        except SQLAlchemyError:
            raise RetryableTaskError() from None

    def reject(
        self, task: Task, error: TaskError, retry_at: datetime | None, now: datetime
    ) -> None:
        with self.database.transaction() as session:
            queue = PostgresTaskQueue(session)
            if retry_at is None:
                queue.fail(task, error, now)
            else:
                queue.defer(task, retry_at, now)
