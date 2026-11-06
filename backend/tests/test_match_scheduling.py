from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_matches import Provider, setup
from test_pipeline import SETTINGS, build
from test_pipeline import drain as drain_public
from test_profiles import NOW
from test_snapshots import page, publish, register
from test_worker import FakeClock, immediate, read

from job_hunter.application.ai import AIError
from job_hunter.application.worker import PermanentTaskError, RetryableTaskError, Worker
from job_hunter.domain.errors import DomainError
from job_hunter.domain.profiles import Skill
from job_hunter.domain.tasks import StaleLease, Task, TaskError
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.match_events import (
    EVALUATE,
    SCAN,
    MatchScanRecord,
    enqueue_scan,
)
from job_hunter.infrastructure.match_scheduling import (
    MatchSchedule,
    UnknownMatch,
    create_schedule,
)
from job_hunter.infrastructure.matches import MatchArtifactRecord
from job_hunter.infrastructure.profiles import PostgresProfiles
from job_hunter.infrastructure.searches import SearchRunRecord
from job_hunter.infrastructure.tasks import TaskRecord
from job_hunter.infrastructure.worker import PostgresWorkerStore
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
owners, runs = registry_owners, registry_runs


def service(
    db: Database, owner: UUID, run: UUID
) -> tuple[MatchSchedule, FakeClock, UUID, Provider]:
    repository, job, provider = setup(db, owner, run)
    clock = FakeClock()
    clock.current = NOW
    schedule = MatchSchedule(repository, clock.now)
    return schedule, clock, job, provider


def drain(schedule: MatchSchedule, clock: FakeClock) -> None:
    handlers = schedule.handlers()
    worker = Worker(
        PostgresWorkerStore(schedule.database, handlers),
        {kind: value.execute for kind, value in handlers.items()},
        clock,
        immediate,
        Event(),
        lambda: 1,
        concurrency=1,
    )
    for _ in range(100):
        worker.tick()
        with schedule.database.transaction() as session:
            pending = session.scalar(
                select(func.count())
                .select_from(TaskRecord)
                .where(
                    TaskRecord.kind.in_(handlers),
                    TaskRecord.status.in_(("pending", "running")),
                )
            )
        if not pending and not worker.running:
            return
        clock.sleep(1)
    raise AssertionError("Matching did not finish")


def claim(
    schedule: MatchSchedule, clock: FakeClock, kind: str
) -> tuple[PostgresWorkerStore, Task]:
    store = PostgresWorkerStore(schedule.database, schedule.handlers())
    task = store.claim((kind,), clock.now(), timedelta(seconds=60))
    assert task is not None
    return store, task


def test_private_discovery_profile_changes_and_repeated_events(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    db = isolated_database
    schedule, clock, job, provider = service(db, owners[0], runs[0])
    assert schedule.status(owners[0], job)["state"] == "pending"
    for method in (
        lambda: schedule.status(owners[1], job),
        lambda: schedule.request(owners[1], job, uuid4()),
    ):
        with pytest.raises(DomainError, match="not_found"):
            method()
    drain(schedule, clock)
    first = schedule.status(owners[0], job)
    assert first["state"] == "current" and provider.calls == 1
    profiles = PostgresProfiles(db, clock.now)
    profiles.put(owners[1], 0, Skill(name="SQL"))
    assert register(db, owners[1], runs[1]) == job
    drain(schedule, clock)
    other = schedule.status(owners[1], job)
    assert other["state"] == "current" and other["result"] != first["result"]
    for _ in range(2):
        register(db, owners[1], runs[1])
        last = schedule.backfill()
        assert last is not None and schedule.backfill(last) is None
        drain(schedule, clock)
    assert provider.calls == 2
    profiles.put(owners[0], 1, Skill(name="Rust"))
    stale = schedule.status(owners[0], job)
    assert (
        stale["state"] == "stale"
        and stale["pending"]
        and stale["result"] == first["result"]
    )
    drain(schedule, clock)
    assert schedule.status(owners[0], job)["state"] == "current"
    assert schedule.status(owners[1], job) == other
    assert provider.calls == 3


@pytest.mark.parametrize(
    "change", ["profile", "job", "job_after", "deleted", "new_request"]
)
def test_changes_during_provider_call_prevent_publication(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    change: str,
) -> None:
    db = isolated_database
    schedule, clock, job, provider = service(db, owners[0], runs[0])
    schedule.request(owners[0], job, uuid4())
    store, task = claim(schedule, clock, EVALUATE)

    def mutate() -> None:
        if change == "profile":
            PostgresProfiles(db, clock.now).put(owners[0], 1, Skill(name="Rust"))
        elif change.startswith("job"):
            publish(
                db,
                job,
                replace(page(body=b"Changed job"), fetched_at=NOW + timedelta(hours=1)),
            )
        elif change == "deleted":
            with db.transaction() as session:
                session.execute(
                    update(UserRecord)
                    .where(UserRecord.id == owners[0])
                    .values(deleted_at=clock.now())
                )
        else:
            schedule.request(owners[0], job, uuid4())

    provider.before = mutate if change != "job_after" else lambda: None
    if change == "job":
        with pytest.raises(DomainError):
            schedule.execute(task)
    else:
        result = schedule.execute(task)
        if change == "job_after":
            mutate()
        with pytest.raises((DomainError, PermanentTaskError)):
            store.publish(task, result, clock.now())
    assert read(db, task).status == "running"
    with db.transaction() as session:
        assert (
            session.scalar(select(func.count()).select_from(MatchArtifactRecord)) == 0
        )
    provider.before = lambda: None


def test_paged_fanout_crash_rollback_and_restart(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = isolated_database
    schedule, clock, job, _ = service(db, owners[0], runs[0])
    register(db, owners[1], runs[1])
    monkeypatch.setattr("job_hunter.infrastructure.match_scheduling.PAGE", 1)
    with db.transaction() as session:
        enqueue_scan(session, None, job, "ready", clock.now())
        scan_id = session.scalars(
            select(MatchScanRecord.id).where(MatchScanRecord.owner_id.is_(None))
        ).one()
        session.execute(
            update(TaskRecord)
            .where(TaskRecord.subject_id != scan_id)
            .values(status="cancelled")
        )
    store, task = claim(schedule, clock, SCAN)
    original = schedule.schedule

    def crash(*args: object) -> None:
        original(*args)  # type: ignore[arg-type]
        raise RuntimeError("crash before cursor commit")

    with monkeypatch.context() as patch:
        patch.setattr(schedule, "schedule", crash)
        with pytest.raises(RuntimeError):
            store.publish(task, None, clock.now())
    with db.transaction() as session:
        assert session.get(MatchScanRecord, scan_id).cursor is None  # type: ignore[union-attr]
        assert (
            session.scalar(
                select(func.count())
                .select_from(TaskRecord)
                .where(TaskRecord.kind == EVALUATE)
            )
            == 0
        )
    store.publish(task, None, clock.now())
    with db.transaction() as session:
        assert session.get(MatchScanRecord, scan_id).step == 2  # type: ignore[union-attr]
        assert (
            session.scalar(
                select(func.count())
                .select_from(TaskRecord)
                .where(TaskRecord.kind == EVALUATE)
            )
            == 1
        )
    drain(schedule, clock)
    assert all(schedule.status(owner, job)["state"] == "current" for owner in owners)
    with db.transaction() as session:
        assert session.get(MatchScanRecord, scan_id).step == 3  # type: ignore[union-attr]


def test_evaluation_crash_lease_recovery_and_safe_failure(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = isolated_database
    schedule, clock, job, provider = service(db, owners[0], runs[0])
    schedule.request(owners[0], job, uuid4())
    store, task = claim(schedule, clock, EVALUATE)
    result = schedule.execute(task)
    latest = schedule.latest

    def crash(*args: object) -> None:
        raise SQLAlchemyError("publication outage")

    with monkeypatch.context() as patch:
        patch.setattr(schedule, "latest", crash)
        with pytest.raises(RetryableTaskError):
            store.publish(task, result, clock.now())
        patch.setattr(schedule.repository, "load", crash)
        with pytest.raises(RetryableTaskError):
            schedule.execute(task)
    assert schedule.latest == latest and read(db, task).status == "running"
    with db.transaction() as session:
        assert (
            session.scalar(select(func.count()).select_from(MatchArtifactRecord)) == 0
        )
    clock.sleep(61)
    store.recover((EVALUATE,), clock.now())
    for action in (
        lambda: schedule.execute(task),
        lambda: store.publish(task, result, clock.now()),
    ):
        with pytest.raises(StaleLease):
            action()
    _, replacement = claim(schedule, clock, EVALUATE)
    store.publish(replacement, result, clock.now())
    assert schedule.status(owners[0], job)["state"] == "current"
    with pytest.raises(StaleLease):
        store.publish(replacement, result, clock.now())

    async def failed(*args: object) -> None:
        raise AIError("provider_failure")

    monkeypatch.setattr(provider, "generate", failed)
    schedule.request(owners[0], job, uuid4())
    drain(schedule, clock)
    status = schedule.status(owners[0], job)
    assert (
        status["state"] == "failed" and not status["pending"] and not status["current"]
    )
    assert status["reason"] == TaskError.PERMANENT
    monkeypatch.setattr(provider, "generate", UnknownMatch(SETTINGS, "{}").generate)
    schedule.request(owners[0], job, uuid4())
    drain(schedule, clock)
    recovered = schedule.status(owners[0], job)
    assert recovered["state"] == "current" and isinstance(recovered["result"], dict)
    assert recovered["result"]["state"] == "unknown"
    assert recovered["result"]["score"]["percent"] is None


def test_reevaluation_idempotency_concurrent_budget_and_window(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    db = isolated_database
    schedule, clock, job, provider = service(db, owners[0], runs[0])
    schedule.repository.run(owners[0], job, clock.now(), 4)
    drain(schedule, clock)
    assert schedule.status(owners[0], job)["state"] == "current" and provider.calls == 1
    key = uuid4()
    first = schedule.request(owners[0], job, key)
    assert first is not None and first.key.subject_version == 5
    assert schedule.request(owners[0], job, key).id == first.id  # type: ignore[union-attr]
    other = register(db, owners[0], runs[0], "https://jobs.example.org/other")
    with pytest.raises(DomainError, match="conflict"):
        schedule.request(owners[0], other, key)
    with db.transaction() as session:
        enqueue_scan(session, owners[0], other, "not-ready", clock.now())
    assert schedule.status(owners[0], other)["state"] == "pending"
    parallel = Database(Settings(database_pool_size=4))
    original = schedule.database
    schedule.database = parallel
    barrier = Barrier(4)

    def request(_: int) -> Task | None:
        barrier.wait(timeout=10)
        return schedule.request(owners[0], job, uuid4())

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(request, range(4)))
        assert sum(result is not None for result in results) == 2
    finally:
        schedule.database = original
        parallel.close()
    assert schedule.status(owners[0], job)["reevaluations_remaining"] == 0
    assert provider.calls == 1
    PostgresProfiles(db, clock.now).put(owners[0], 1, Skill(name="Rust"))
    assert schedule.request(owners[0], job, key).id == first.id  # type: ignore[union-attr]
    clock.sleep(86401)
    assert schedule.request(owners[0], job, uuid4()) is not None
    assert schedule.status(owners[0], job)["reevaluations_remaining"] == 2


def test_public_readiness_wires_worker_and_migration(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    db = isolated_database
    config = Config(toml_file="pyproject.toml")
    command.downgrade(config, "0028_private_matches")
    command.upgrade(config, "head")
    command.check(config)
    pipeline, clock, job = build(db, owners[0], runs[0])
    PostgresProfiles(db, clock.now).put(owners[0], 0, Skill(name="Python"))
    schedule = create_schedule(pipeline, SETTINGS, clock.now)
    assert isinstance(
        schedule.repository.evaluator.factory(schedule.repository.ai.accounting).chat,
        UnknownMatch,
    )
    assert schedule.status(owners[0], job)["state"] == "pending"
    drain_public(pipeline, clock)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(MatchScanRecord)) == 1
    drain(schedule, clock)
    result = schedule.status(owners[0], job)
    assert result["state"] == "current" and isinstance(result["result"], dict)
    assert result["result"]["score"]["percent"] is None


def test_busy_discovery_is_retryable_and_profile_enqueue_is_atomic(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = isolated_database
    schedule, clock, job, _ = service(db, owners[0], runs[0])
    schedule.request(owners[0], job, uuid4())
    _, task = claim(schedule, clock, EVALUATE)
    observer = Database(Settings(database_pool_size=1))
    try:
        with observer.transaction() as other:
            other.scalar(
                select(SearchRunRecord)
                .where(SearchRunRecord.id == runs[0])
                .with_for_update()
            )
            with pytest.raises(RetryableTaskError):
                schedule.execute(task)
        assert schedule.execute(task).content.state == "ready"
    finally:
        observer.close()

    def crash(*args: object) -> None:
        raise RuntimeError("enqueue failed")

    profiles = PostgresProfiles(db, clock.now)
    with monkeypatch.context() as patch:
        patch.setattr("job_hunter.infrastructure.profiles.enqueue_scan", crash)
        with pytest.raises(RuntimeError):
            profiles.put(owners[0], 1, Skill(name="Rust"))
    assert profiles.read(owners[0]).revision == 1
