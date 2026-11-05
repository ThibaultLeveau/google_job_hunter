from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.tasks import TaskQueue
from job_hunter.domain.errors import DomainError
from job_hunter.domain.tasks import (
    NewTask,
    StaleLease,
    Task,
    TaskError,
    TaskKey,
    TaskStatus,
    require_utc,
)
from job_hunter.infrastructure.database import Base


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("kind", "subject_id", "subject_version", name="uq_tasks_key"),
        CheckConstraint("kind ~ '^[a-z][a-z0-9_]{0,63}$'", name="ck_tasks_kind"),
        CheckConstraint(
            "subject_version > 0 AND generation >= 0", name="ck_tasks_versions"
        ),
        CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts "
            "AND max_attempts BETWEEN 1 AND 100",
            name="ck_tasks_attempts",
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed')",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "(status = 'running') = (lease_expires_at IS NOT NULL)",
            name="ck_tasks_lease",
        ),
        CheckConstraint(
            "error_code IN ('transient_failure','permanent_failure',"
            "'attempts_exhausted')",
            name="ck_tasks_error",
        ),
        Index("ix_tasks_due", "status", "run_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[UUID]
    subject_version: Mapped[int]
    payload_ref: Mapped[UUID]
    status: Mapped[TaskStatus] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    max_attempts: Mapped[int]
    generation: Mapped[int] = mapped_column(default=0, server_default="0")
    lease_token: Mapped[UUID]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def snapshot(row: TaskRecord) -> Task:
    return Task(
        row.id,
        TaskKey(row.kind, row.subject_id, row.subject_version),
        row.payload_ref,
        row.status,
        row.run_at,
        row.attempts,
        row.max_attempts,
        row.generation,
        row.lease_token,
        row.lease_expires_at,
        TaskError(row.error_code) if row.error_code is not None else None,
    )


class PostgresTaskQueue(TaskQueue):
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(self, task: NewTask, now: datetime) -> Task:
        require_utc(now)
        statement = (
            insert(TaskRecord)
            .values(
                id=uuid4(),
                kind=task.key.kind,
                subject_id=task.key.subject_id,
                subject_version=task.key.subject_version,
                payload_ref=task.payload_ref,
                run_at=task.run_at,
                max_attempts=task.max_attempts,
                lease_token=uuid4(),
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_tasks_key")
            .returning(TaskRecord)
        )
        row = self.session.scalar(statement)
        if row is None:
            row = self.session.scalars(
                select(TaskRecord).where(
                    TaskRecord.kind == task.key.kind,
                    TaskRecord.subject_id == task.key.subject_id,
                    TaskRecord.subject_version == task.key.subject_version,
                )
            ).one()
        if row.payload_ref != task.payload_ref:
            raise DomainError("conflict")
        return snapshot(row)

    def claim(
        self, kinds: tuple[str, ...], now: datetime, lease: timedelta
    ) -> Task | None:
        require_utc(now)
        if not timedelta(0) < lease <= timedelta(minutes=5):
            raise ValueError("Lease must be positive and at most five minutes.")
        row = self.session.scalar(
            select(TaskRecord)
            .where(
                TaskRecord.kind.in_(kinds),
                TaskRecord.status == "pending",
                TaskRecord.run_at <= now,
                TaskRecord.attempts < TaskRecord.max_attempts,
            )
            .order_by(TaskRecord.run_at, TaskRecord.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        row.status, row.lease_token = "running", uuid4()
        row.lease_expires_at, row.updated_at = now + lease, now
        row.attempts += 1
        self.session.flush()
        return snapshot(row)

    def _transition(
        self,
        task: Task,
        now: datetime,
        status: TaskStatus,
        error: TaskError | None,
        *,
        run_at: datetime | None = None,
        lease: timedelta | None = None,
    ) -> Task:
        require_utc(now)
        row = self.session.scalar(
            update(TaskRecord)
            .where(
                TaskRecord.id == task.id,
                TaskRecord.status == "running",
                TaskRecord.generation == task.generation,
                TaskRecord.lease_token == task.lease_token,
                TaskRecord.lease_expires_at > now,
            )
            .values(
                status=status,
                lease_token=task.lease_token if lease else uuid4(),
                lease_expires_at=now + lease if lease else None,
                run_at=run_at if run_at is not None else TaskRecord.run_at,
                error_code=error.value if error is not None else None,
                updated_at=now,
            )
            .returning(TaskRecord)
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise StaleLease()
        return snapshot(row)

    def renew(self, task: Task, now: datetime, lease: timedelta) -> Task:
        if not timedelta(0) < lease <= timedelta(minutes=5):
            raise ValueError("Lease must be positive and at most five minutes.")
        return self._transition(task, now, "running", None, lease=lease)

    def defer(self, task: Task, retry_at: datetime, now: datetime) -> Task:
        require_utc(retry_at)
        if retry_at < now:
            raise ValueError("Retry cannot precede the current time.")
        if task.attempts >= task.max_attempts:
            return self.fail(task, TaskError.EXHAUSTED, now)
        return self._transition(
            task, now, "pending", TaskError.TRANSIENT, run_at=retry_at
        )

    def recover(self, kinds: tuple[str, ...], now: datetime) -> None:
        require_utc(now)
        rows = self.session.scalars(
            select(TaskRecord)
            .where(
                TaskRecord.kind.in_(kinds),
                TaskRecord.status == "running",
                TaskRecord.lease_expires_at <= now,
            )
            .order_by(TaskRecord.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
        for row in rows:
            exhausted = row.attempts >= row.max_attempts
            row.status = "failed" if exhausted else "pending"
            row.error_code = TaskError.EXHAUSTED if exhausted else TaskError.TRANSIENT
            row.lease_token, row.lease_expires_at = uuid4(), None
            row.run_at = row.updated_at = now
        self.session.flush()

    def complete(self, task: Task, now: datetime) -> Task:
        return self._transition(task, now, "succeeded", None)

    def fail(self, task: Task, error: TaskError, now: datetime) -> Task:
        if not isinstance(error, TaskError):
            raise ValueError("Use a safe task error code.")
        return self._transition(task, now, "failed", error)

    def retry(self, key: TaskKey, expected_generation: int, now: datetime) -> Task:
        require_utc(now)
        if expected_generation < 0:
            raise ValueError("Expected generation must be nonnegative.")
        predicate = (
            TaskRecord.kind == key.kind,
            TaskRecord.subject_id == key.subject_id,
            TaskRecord.subject_version == key.subject_version,
        )
        row = self.session.scalar(
            update(TaskRecord)
            .where(
                *predicate,
                TaskRecord.generation == expected_generation,
                TaskRecord.status == "failed",
            )
            .values(
                status="pending",
                generation=TaskRecord.generation + 1,
                attempts=0,
                lease_token=uuid4(),
                lease_expires_at=None,
                error_code=None,
                run_at=now,
                updated_at=now,
            )
            .returning(TaskRecord)
            .execution_options(populate_existing=True)
        )
        if row is None:
            row = self.session.scalar(
                select(TaskRecord)
                .where(*predicate)
                .execution_options(populate_existing=True)
            )
            if row is None or row.generation != expected_generation + 1:
                raise DomainError("conflict")
        return snapshot(row)
