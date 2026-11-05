from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from job_hunter.domain.errors import DomainError
from job_hunter.domain.tasks import NewTask, StaleLease, Task, TaskError, TaskKey
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.settings import Settings

NOW = datetime(2026, 1, 1, tzinfo=UTC)
LEASE = timedelta(seconds=30)


def new_task(kind: str = "parse_document") -> NewTask:
    subject = uuid4()
    return NewTask(TaskKey(kind, subject, 1), subject, NOW)


def enqueue(database: Database, request: NewTask) -> Task:
    with database.transaction() as session:
        return PostgresTaskQueue(session).enqueue(request, NOW)


def claim(database: Database, now: datetime = NOW) -> Task:
    with database.transaction() as session:
        task = PostgresTaskQueue(session).claim(("parse_document",), now, LEASE)
        assert task is not None
        return task


@pytest.mark.parametrize(
    "invalid", ["kind", "subject", "version", "payload", "time", "attempts"]
)
def test_task_values_reject_content_and_invalid_bounds(invalid: str) -> None:
    request = new_task()
    with pytest.raises(ValueError):
        match invalid:
            case "kind":
                replace(request.key, kind="private content with spaces")
            case "subject":
                replace(request.key, subject_id=cast(UUID, "private-canary"))
            case "version":
                replace(request.key, subject_version=0)
            case "payload":
                replace(request, payload_ref=cast(UUID, {"content": "private-canary"}))
            case "time":
                replace(request, run_at=NOW.replace(tzinfo=None))
            case "attempts":
                replace(request, max_attempts=0)


@pytest.mark.integration
def test_migration_matches_model_and_constraints(isolated_database: Database) -> None:
    request = new_task()
    enqueue(isolated_database, request)
    with isolated_database.engine.connect() as connection:
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )
    for expression in (
        "attempts = -1",
        "status = 'running'",
        "error_code = 'private-canary'",
    ):
        with pytest.raises(IntegrityError):
            with isolated_database.transaction() as session:
                session.execute(text(f"UPDATE tasks SET {expression}"))


@pytest.mark.integration
@pytest.mark.parametrize("rollback", [False, True])
def test_business_write_and_enqueue_are_atomic(
    isolated_database: Database, rollback: bool
) -> None:
    request = new_task()
    with isolated_database.transaction() as session:
        session.execute(
            text("CREATE TABLE subject (id uuid PRIMARY KEY, content text)")
        )
    try:
        with isolated_database.transaction() as session:
            session.execute(
                text("INSERT INTO subject VALUES (:id, :content)"),
                {"id": request.key.subject_id, "content": "private-canary"},
            )
            PostgresTaskQueue(session).enqueue(request, NOW)
            if rollback:
                raise RuntimeError("Abort the unit of work")
    except RuntimeError:
        assert rollback
    with isolated_database.transaction() as session:
        assert session.scalar(text("SELECT count(*) FROM subject")) == int(not rollback)
        assert session.scalar(select(func.count()).select_from(TaskRecord)) == int(
            not rollback
        )
        serialized = str(
            session.execute(text("SELECT row_to_json(tasks) FROM tasks")).all()
        )
        assert "private-canary" not in serialized


@pytest.mark.integration
def test_enqueue_preserves_identity_and_rejects_changed_reference(
    isolated_database: Database,
) -> None:
    request = new_task()
    first = enqueue(isolated_database, request)
    assert (
        enqueue(isolated_database, replace(request, run_at=NOW + LEASE)).id == first.id
    )
    with pytest.raises(DomainError):
        enqueue(isolated_database, replace(request, payload_ref=uuid4()))
    with isolated_database.transaction() as session:
        queue = PostgresTaskQueue(session)
        assert queue.claim(("unregistered",), NOW, LEASE) is None
        assert queue.claim((request.key.kind,), NOW - LEASE, LEASE) is None
        for lease in (timedelta(0), timedelta(minutes=6)):
            with pytest.raises(ValueError):
                queue.claim((request.key.kind,), NOW, lease)
        assert queue.claim((request.key.kind,), NOW, LEASE) is not None
    with isolated_database.transaction() as session:
        session.execute(
            text(
                "UPDATE tasks SET status='pending', "
                "lease_expires_at=NULL, attempts=max_attempts"
            )
        )
        assert PostgresTaskQueue(session).claim((request.key.kind,), NOW, LEASE) is None


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["enqueue", "claim", "retry"])
def test_concurrent_queue_operations_have_one_result(
    isolated_database: Database, operation: str
) -> None:
    request = new_task()
    if operation != "enqueue":
        enqueue(isolated_database, request)
    if operation == "retry":
        owned = claim(isolated_database)
        with isolated_database.transaction() as session:
            PostgresTaskQueue(session).fail(owned, TaskError.TRANSIENT, NOW)
    concurrent = Database(
        Settings(
            database_url=SecretStr(
                isolated_database.engine.url.render_as_string(hide_password=False)
            ),
            database_pool_size=2,
        )
    )
    barrier = Barrier(2, timeout=5)

    def run() -> Task | None:
        barrier.wait()
        with concurrent.transaction() as session:
            queue = PostgresTaskQueue(session)
            if operation == "enqueue":
                return queue.enqueue(request, NOW)
            if operation == "retry":
                return queue.retry(request.key, 0, NOW)
            result = queue.claim((request.key.kind,), NOW, LEASE)
            # Keep both transactions open: SKIP LOCKED must not wait for the owner.
            barrier.wait()
            return result

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: run(), range(2)))
    finally:
        concurrent.close()
    owned_results = [result for result in results if result is not None]
    assert len(owned_results) == (1 if operation == "claim" else 2)
    assert len({result.id for result in owned_results}) == 1
    assert len({result.lease_token for result in owned_results}) == 1
    with isolated_database.transaction() as session:
        rows = session.scalars(select(TaskRecord)).all()
        assert len(rows) == 1
        assert rows[0].generation == int(operation == "retry")
        assert rows[0].attempts == int(operation == "claim")


@pytest.mark.integration
def test_expiry_and_stale_tokens_rollback_publication(
    isolated_database: Database,
) -> None:
    request = new_task()
    enqueue(isolated_database, request)
    owned = claim(isolated_database)
    with isolated_database.transaction() as session:
        session.execute(text("CREATE TABLE artifact (id integer PRIMARY KEY)"))
        session.execute(text("INSERT INTO artifact VALUES (1)"))
    for stale in (replace(owned, lease_token=uuid4()), replace(owned, generation=1)):
        with pytest.raises(StaleLease):
            with isolated_database.transaction() as session:
                session.execute(text("INSERT INTO artifact VALUES (2)"))
                PostgresTaskQueue(session).complete(stale, NOW)
    with pytest.raises(StaleLease):
        with isolated_database.transaction() as session:
            PostgresTaskQueue(session).complete(owned, NOW + LEASE)
    with isolated_database.transaction() as session:
        failed = PostgresTaskQueue(session).fail(owned, TaskError.PERMANENT, NOW)
        assert failed.status == "failed" and failed.error_code == TaskError.PERMANENT
    with isolated_database.transaction() as session:
        reopened = PostgresTaskQueue(session).retry(request.key, 0, NOW)
        assert (
            reopened.id == owned.id
            and reopened.generation == 1
            and reopened.attempts == 0
        )
        assert reopened.lease_token != owned.lease_token
        assert session.scalar(text("SELECT count(*) FROM artifact")) == 1
    for operation in ("complete", "fail"):
        with pytest.raises(StaleLease):
            with isolated_database.transaction() as session:
                queue = PostgresTaskQueue(session)
                if operation == "complete":
                    queue.complete(owned, NOW)
                else:
                    queue.fail(owned, TaskError.TRANSIENT, NOW)
    current = claim(isolated_database)
    with isolated_database.transaction() as session:
        queue = PostgresTaskQueue(session)
        with pytest.raises(ValueError):
            queue.fail(current, cast(TaskError, "private-canary"), NOW)
        completed = queue.complete(current, NOW)
        assert completed.status == "succeeded" and completed.lease_expires_at is None
    with isolated_database.transaction() as session:
        queue = PostgresTaskQueue(session)
        assert queue.retry(request.key, 0, NOW).generation == 1
        with pytest.raises(DomainError):
            queue.retry(request.key, 1, NOW)
        with pytest.raises(DomainError):
            queue.retry(new_task().key, 0, NOW)
        with pytest.raises(ValueError):
            queue.retry(request.key, -1, NOW)
        assert queue.claim((request.key.kind,), NOW, LEASE) is None
