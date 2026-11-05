from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from job_hunter.application.worker import (
    Execute,
    PermanentTaskError,
    RetryableTaskError,
    Submit,
    Worker,
)
from job_hunter.domain.tasks import NewTask, StaleLease, Task, TaskError, TaskKey
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord, snapshot
from job_hunter.infrastructure.worker import Handler, PostgresWorkerStore
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
NOW = datetime(2026, 1, 1, tzinfo=UTC)
LEASE = timedelta(seconds=6)


class FakeClock:
    def __init__(self) -> None:
        self.current = NOW
        self.on_sleep: Callable[[], None] = lambda: None

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        self.on_sleep()


def immediate(execute: Execute, task: Task) -> Future[object]:
    future: Future[object] = Future()
    try:
        future.set_result(execute(task))
    except Exception as error:
        future.set_exception(error)
    return future


def enqueue(database: Database, kind: str = "stage", attempts: int = 3) -> Task:
    with database.transaction() as session:
        return PostgresTaskQueue(session).enqueue(
            NewTask(TaskKey(kind, uuid4(), 1), uuid4(), NOW, attempts), NOW
        )


def read(database: Database, task: Task) -> Task:
    with database.transaction() as session:
        return snapshot(
            session.scalars(select(TaskRecord).where(TaskRecord.id == task.id)).one()
        )


def make_worker(
    database: Database,
    execute: Execute = lambda task: "result",
    *,
    publish: Callable[[Session, Task, object, datetime], None] = lambda *args: None,
    submit: Submit = immediate,
    concurrency: int = 1,
) -> tuple[Worker, FakeClock, PostgresWorkerStore]:
    clock = FakeClock()
    store = PostgresWorkerStore(database, {"stage": Handler(execute, publish)})
    worker = Worker(
        store,
        {"stage": execute},
        clock,
        submit,
        Event(),
        lambda: 0.5,
        concurrency=concurrency,
        lease=LEASE,
    )
    return worker, clock, store


def test_restart_after_claim_and_unknown_kind(isolated_database: Database) -> None:
    task = enqueue(isolated_database)
    unknown = enqueue(isolated_database, "future_stage")
    worker, clock, store = make_worker(isolated_database)
    old = store.claim(("stage",), NOW, LEASE)
    assert old is not None
    worker.tick()
    assert not worker.running
    clock.sleep(6)
    worker.tick()
    assert worker.running[0].task.attempts == 2
    with pytest.raises(StaleLease):
        store.publish(old, "obsolete", clock.now())
    worker.tick()
    assert read(isolated_database, task).status == "succeeded"
    assert read(isolated_database, unknown).attempts == 0
    assert read(isolated_database, unknown).status == "pending"


def test_heartbeat_and_shutdown_drain_without_claiming(
    isolated_database: Database,
) -> None:
    enqueue(isolated_database)
    enqueue(isolated_database)
    pending: Future[object] = Future()
    worker, clock, store = make_worker(isolated_database, submit=lambda *_: pending)
    worker.tick()
    active = worker.running[0].task
    clock.sleep(3)
    worker.tick()
    assert read(isolated_database, active).lease_expires_at == NOW + timedelta(
        seconds=9
    )
    store.recover(("stage",), NOW + LEASE)
    assert read(isolated_database, active).status == "running"
    worker.stop.set()
    clock.on_sleep = lambda: pending.set_result("done")
    worker.run()
    assert not worker.running and read(isolated_database, active).status == "succeeded"
    with isolated_database.transaction() as session:
        states = session.scalars(select(TaskRecord.status)).all()
        assert sorted(states) == ["pending", "succeeded"]


@pytest.mark.parametrize(
    "failure", [RetryableTaskError, PermanentTaskError, RuntimeError]
)
def test_retry_budget_backoff_and_safe_terminal_errors(
    isolated_database: Database,
    failure: type[Exception],
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = enqueue(isolated_database, attempts=9)

    def execute(_: Task) -> object:
        raise failure("private-canary")

    worker, clock, _ = make_worker(isolated_database, execute)
    for attempt in range(1, 10 if failure is RetryableTaskError else 2):
        worker.tick()
        worker.tick()
        state = read(isolated_database, task)
        assert state.attempts == attempt
        if state.status == "failed":
            break
        delay = min(60, 2 ** (attempt - 1)) * 0.5
        assert state.run_at == clock.now() + timedelta(seconds=delay)
        worker.tick()
        assert not worker.running
        clock.sleep(delay)
    assert state.status == "failed"
    assert state.error_code == (
        TaskError.EXHAUSTED if failure is RetryableTaskError else TaskError.PERMANENT
    )
    assert "private-canary" not in str(state) + caplog.text


def test_expired_heartbeat_discards_result_and_holds_concurrency_slot(
    isolated_database: Database,
) -> None:
    task = enqueue(isolated_database)
    pending: Future[object] = Future()
    worker, clock, store = make_worker(isolated_database, submit=lambda *_: pending)
    worker.tick()
    old = worker.running[0].task
    clock.sleep(7)
    worker.tick()
    assert worker.running[0].lost
    assert read(isolated_database, task).status == "pending"
    assert read(isolated_database, task).attempts == 1
    worker.stop.set()
    pending.set_result("obsolete")
    worker.tick()
    assert not worker.running and read(isolated_database, task).status == "pending"
    with pytest.raises(StaleLease):
        store.renew(old, clock.now(), LEASE)


def test_crash_before_ack_rolls_back_stage_and_next_task(
    isolated_database: Database,
) -> None:
    task = enqueue(isolated_database)
    with isolated_database.transaction() as session:
        session.execute(text("CREATE TABLE artifact (id uuid PRIMARY KEY, value text)"))
    calls = 0
    crash = True

    class Crash(BaseException):
        pass

    def execute(_: Task) -> object:
        nonlocal calls
        calls += 1
        return "validated"

    def publish(session: Session, owned: Task, result: object, now: datetime) -> None:
        session.execute(
            text("INSERT INTO artifact VALUES (:id, :value)"),
            {"id": owned.id, "value": result},
        )
        PostgresTaskQueue(session).enqueue(
            NewTask(TaskKey("next_stage", owned.key.subject_id, 1), owned.id, now), now
        )
        if crash:
            raise Crash()

    worker, clock, _ = make_worker(isolated_database, execute, publish=publish)
    worker.tick()
    with pytest.raises(Crash):
        worker.tick()
    with isolated_database.transaction() as session:
        assert session.scalar(text("SELECT count(*) FROM artifact")) == 0
        assert session.scalar(text("SELECT count(*) FROM tasks")) == 1
    assert read(isolated_database, task).status == "running"
    crash = False
    restarted, clock, _ = make_worker(isolated_database, execute, publish=publish)
    clock.sleep(7)
    restarted.tick()
    restarted.tick()
    restarted.tick()
    assert calls == 2 and read(isolated_database, task).status == "succeeded"
    with isolated_database.transaction() as session:
        assert session.scalar(text("SELECT count(*) FROM artifact")) == 1
        assert session.scalar(text("SELECT count(*) FROM tasks")) == 2
    next_worker, _, _ = make_worker(isolated_database, execute, publish=publish)
    next_worker.tick()
    assert not next_worker.running and calls == 2

    def resume(owned: Task) -> object:
        with isolated_database.transaction() as session:
            return session.scalar(
                text("SELECT value FROM artifact WHERE id=:id"),
                {"id": owned.payload_ref},
            )

    results: list[object] = []
    registry = {
        "next_stage": Handler(
            resume, lambda _session, _task, value, _now: results.append(value)
        )
    }
    resumed = Worker(
        PostgresWorkerStore(isolated_database, registry),
        {"next_stage": resume},
        clock,
        immediate,
        Event(),
        lambda: 0.5,
    )
    resumed.tick()
    resumed.tick()
    assert results == ["validated"] and calls == 2


def test_recovery_exhaustion_and_invalid_renewal(isolated_database: Database) -> None:
    task = enqueue(isolated_database, attempts=1)
    _, _, store = make_worker(isolated_database)
    owned = store.claim(("stage",), NOW, LEASE)
    assert owned is not None
    with isolated_database.transaction() as session:
        queue = PostgresTaskQueue(session)
        with pytest.raises(ValueError):
            queue.renew(owned, NOW, timedelta(0))
        with pytest.raises(ValueError):
            queue.defer(owned, NOW - LEASE, NOW)
    store.recover(("unregistered",), NOW + LEASE)
    assert read(isolated_database, task).status == "running"
    store.recover(("stage",), NOW + LEASE)
    state = read(isolated_database, task)
    assert state.status == "failed" and state.error_code == TaskError.EXHAUSTED
    task = enqueue(isolated_database, attempts=1)
    owned = store.claim(("stage",), NOW, LEASE)
    assert owned is not None
    store.reject(owned, TaskError.TRANSIENT, NOW + LEASE, NOW)
    assert read(isolated_database, task).status == "failed"


def test_competing_workers_and_external_work_without_locks(
    isolated_database: Database,
) -> None:
    tasks = [enqueue(isolated_database) for _ in range(3)]
    database = Database(Settings(database_pool_size=4))
    barrier = Barrier(2)
    release = Event()

    def execute(task: Task) -> object:
        with database.transaction() as session:
            assert (
                session.scalar(
                    select(TaskRecord.id)
                    .where(TaskRecord.id == task.id)
                    .with_for_update(nowait=True)
                )
                == task.id
            )
        barrier.wait(timeout=5)
        assert release.wait(timeout=5)
        return "done"

    try:
        with (
            ThreadPoolExecutor(max_workers=2) as external,
            ThreadPoolExecutor(max_workers=2) as claimers,
        ):
            workers = [
                make_worker(database, execute, submit=external.submit)[0]
                for _ in range(2)
            ]
            for future in [claimers.submit(worker.tick) for worker in workers]:
                future.result(timeout=5)
            assert len({worker.running[0].task.id for worker in workers}) == 2
            for worker in workers:
                worker.tick()
                assert len(worker.running) == 1
                worker.stop.set()
            release.set()
            for worker in workers:
                worker.running[0].future.result(timeout=5)
                worker.tick()
        assert sorted(read(database, task).status for task in tasks) == [
            "pending",
            "succeeded",
            "succeeded",
        ]
    finally:
        release.set()
        database.close()


def test_storage_failure_recovery_and_redaction(
    isolated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = enqueue(isolated_database)
    worker, clock, store = make_worker(isolated_database)

    def unavailable(*_: object) -> None:
        raise RuntimeError("private-canary")

    with monkeypatch.context() as patch:
        patch.setattr(store, "recover", unavailable)
        clock.on_sleep = worker.stop.set
        worker.run()
    assert read(isolated_database, task).attempts == 0
    worker.stop.clear()
    worker.tick()
    with monkeypatch.context() as patch:
        patch.setattr(store, "publish", unavailable)
        patch.setattr(store, "reject", unavailable)
        worker.tick()
    assert read(isolated_database, task).status == "running"
    assert "private-canary" not in caplog.text and "lease recovery" in caplog.text
    clock.on_sleep = lambda: None
    clock.sleep(7)
    worker.tick()
    worker.tick()
    assert read(isolated_database, task).status == "succeeded"


def test_stale_finished_result_and_rejection_are_discarded(
    isolated_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue(isolated_database)
    worker, clock, store = make_worker(isolated_database)
    worker.tick()
    clock.sleep(7)
    worker.stop.set()
    worker.tick()
    assert not worker.running
    worker.stop.clear()
    worker.tick()

    def stale(*_: object) -> None:
        raise StaleLease()

    def permanent(*_: object) -> None:
        raise PermanentTaskError()

    with monkeypatch.context() as patch:
        patch.setattr(store, "reject", stale)
        patch.setattr(store, "publish", permanent)
        worker.tick()
    assert not worker.running


@pytest.mark.parametrize("concurrency", [0, 17])
def test_concurrency_bounds(isolated_database: Database, concurrency: int) -> None:
    with pytest.raises(ValueError):
        make_worker(isolated_database, concurrency=concurrency)


def test_publication_database_failure_uses_bounded_retry(
    isolated_database: Database,
) -> None:
    task = enqueue(isolated_database)

    def publish(session: Session, *_: object) -> None:
        session.execute(text("SELECT * FROM unavailable_artifact"))

    worker, _, _ = make_worker(isolated_database, publish=publish)
    worker.tick()
    worker.tick()
    state = read(isolated_database, task)
    assert state.status == "pending" and state.error_code == TaskError.TRANSIENT
    assert state.attempts == 1
