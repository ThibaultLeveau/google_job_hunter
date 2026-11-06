from dataclasses import replace
from datetime import timedelta
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from http_fixture import http_response
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_crawling import URL, setup
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_worker import FakeClock, immediate

from job_hunter.application.ai import AIError, Limits, Reply
from job_hunter.application.cleaning import CleanJob
from job_hunter.application.embeddings import EmbedJobs
from job_hunter.application.job_metadata import ExtractMetadata
from job_hunter.application.requirements import ExtractRequirements
from job_hunter.application.worker import (
    DeferredTaskError,
    PermanentTaskError,
    RetryableTaskError,
    Worker,
)
from job_hunter.domain.chunks import ChunkPolicy
from job_hunter.domain.snapshots import FetchedSnapshot
from job_hunter.domain.tasks import StaleLease, Task, TaskError
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.ai import AISettings, FixtureProvider
from job_hunter.infrastructure.cleaning import CleaningCallRecord, create_cleaner
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.embeddings import EmbeddingCallRecord
from job_hunter.infrastructure.job_metadata import MetadataCallRecord, create_metadata
from job_hunter.infrastructure.jobs import JobSourceRecord, PostgresJobRegistry
from job_hunter.infrastructure.pipeline import (
    JobFixture,
    PostgresPipeline,
    create_pipeline,
)
from job_hunter.infrastructure.requirements import (
    RequirementsCallRecord,
    create_requirements,
)
from job_hunter.infrastructure.searches import SavedSearchRecord, SearchRunRecord
from job_hunter.infrastructure.snapshots import PostgresSnapshots
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord, snapshot
from job_hunter.infrastructure.worker import PostgresWorkerStore

pytestmark = pytest.mark.integration
owners, runs = registry_owners, registry_runs
SETTINGS = AISettings(
    provider="fixture", environment="test", limits=Limits(input_tokens=64000)
)


def build(
    database: Database, owner: UUID, run: UUID
) -> tuple[PostgresPipeline, FakeClock, UUID]:
    crawler, _, clock, _, task = setup(
        database,
        owner,
        run,
        http_response(
            b"Engineer position. " * 220, headers=((b"Content-Type", b"text/plain"),)
        ),
    )
    service = create_pipeline(database, crawler, SETTINGS)
    pipeline = PostgresPipeline(
        database,
        crawler,
        service.cleaner,
        service.metadata,
        service.requirements,
        service.embeddings,
        ChunkPolicy(characters=128, overlap=0),
    )
    return pipeline, clock, task.key.subject_id


def worker(pipeline: PostgresPipeline, clock: FakeClock) -> Worker:
    handlers = pipeline.handlers()
    return Worker(
        PostgresWorkerStore(pipeline.database, handlers),
        {kind: handler.execute for kind, handler in handlers.items()},
        clock,
        immediate,
        Event(),
        lambda: 1,
        concurrency=2,
    )


def drain(pipeline: PostgresPipeline, clock: FakeClock) -> None:
    process = worker(pipeline, clock)
    for _ in range(100):
        process.tick()
        with pipeline.database.transaction() as session:
            remaining = session.scalar(
                select(func.count())
                .select_from(TaskRecord)
                .where(
                    TaskRecord.kind.in_(tuple(pipeline.handlers())),
                    TaskRecord.status.in_(("pending", "running")),
                )
            )
        if not remaining and not process.running:
            return
        clock.sleep(1)
    raise AssertionError("Pipeline did not finish within bounded fixture steps")


def claim_stage(
    pipeline: PostgresPipeline, clock: FakeClock, stage: str
) -> tuple[PostgresWorkerStore, Task, object]:
    store = PostgresWorkerStore(pipeline.database, pipeline.handlers())
    for _ in range(30):
        task = store.claim(
            tuple(pipeline.handlers()), clock.now(), timedelta(seconds=60)
        )
        assert task is not None
        try:
            result = pipeline.handlers()[task.key.kind].execute(task)
        except DeferredTaskError as error:
            store.postpone(task, error.retry_at, clock.now())
            clock.current = error.retry_at
            continue
        if task.key.kind == pipeline.kinds[stage]:
            return store, task, result
        store.publish(task, result, clock.now())
    raise AssertionError("Stage was not reached")


def counts(session: Session) -> list[int]:
    return [
        session.scalar(select(func.count()).select_from(model)) or 0
        for model in (
            CleaningCallRecord,
            MetadataCallRecord,
            RequirementsCallRecord,
            EmbeddingCallRecord,
        )
    ]


def current(pipeline: PostgresPipeline, source: UUID) -> tuple[UUID, dict[str, object]]:
    with pipeline.database.transaction() as session:
        row = session.get(JobSourceRecord, source)
        assert row is not None and row.current_snapshot_id is not None
        return row.current_snapshot_id, pipeline.status(
            session, row.current_snapshot_id
        )


def test_real_queue_end_to_end_and_committed_work_reuse(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, clock, source = build(isolated_database, owners[0], runs[0])
    generate, embed = JobFixture.generate, FixtureProvider.embed

    async def chat(
        adapter: JobFixture, prompt: str, data: str, schema: dict[str, Any]
    ) -> Reply:
        assert isolated_database.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        return await generate(adapter, prompt, data, schema)

    async def vectors(adapter: FixtureProvider, texts: tuple[str, ...]) -> Reply:
        assert isolated_database.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        assert len(texts) <= 16
        return await embed(adapter, texts)

    monkeypatch.setattr(JobFixture, "generate", chat)
    monkeypatch.setattr(FixtureProvider, "embed", vectors)
    drain(pipeline, clock)
    snapshot_id, state = current(pipeline, source)
    assert (
        state["content_ready"] and state["analysis_ready"] and state["embeddings_ready"]
    )
    with isolated_database.transaction() as session:
        before = counts(session)
        assert before[:3] == [1, 1, 1] and before[3] >= 3
        stages = state["stages"]
        assert isinstance(stages, dict) and len(stages) == 6
        assert all(
            stage["updated_at"] and stage["dependency_id"] for stage in stages.values()
        )
        pipeline.enqueue(session, snapshot_id, clock.now())
    pipeline = PostgresPipeline(
        isolated_database,
        pipeline.crawler,
        create_cleaner(SETTINGS),
        create_metadata(SETTINGS),
        create_requirements(SETTINGS),
        pipeline.embeddings,
        pipeline.policy,
    )
    assert pipeline.backfill(None, clock.now()) == source
    assert pipeline.backfill(source, clock.now()) is None
    drain(pipeline, clock)
    assert current(pipeline, source)[1]["analysis_ready"]
    with isolated_database.transaction() as session:
        assert counts(session) == before


@pytest.mark.parametrize(
    "stage", ["text", "clean", "chunks", "embed", "metadata", "requirements"]
)
def test_atomic_publication_crash_duplicate_and_stale_lease(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    pipeline, clock, source = build(isolated_database, owners[0], runs[0])
    store, task, result = claim_stage(pipeline, clock, stage)

    def outage(*args: object) -> None:
        raise SQLAlchemyError("database unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "artifacts", outage)
        with pytest.raises(RetryableTaskError):
            pipeline.execute(stage, task)

    with isolated_database.transaction() as session:
        before = counts(session)
        tasks_before = session.scalar(select(func.count()).select_from(TaskRecord))

    def crash(*args: object) -> None:
        raise RuntimeError("Crash after artifact writes, before successor enqueue")

    enqueue = pipeline.enqueue
    monkeypatch.setattr(pipeline, "enqueue", crash)
    with pytest.raises(RuntimeError):
        store.publish(task, result, clock.now())
    with isolated_database.transaction() as session:
        assert counts(session) == before
        assert (
            session.scalar(select(func.count()).select_from(TaskRecord)) == tasks_before
        )
        assert session.get(TaskRecord, task.id).status == "running"  # type: ignore[union-attr]
    monkeypatch.setattr(pipeline, "enqueue", enqueue)
    with pytest.raises(StaleLease):
        store.publish(replace(task, lease_token=uuid4()), result, clock.now())
    clock.sleep(61)
    store.recover(tuple(pipeline.handlers()), clock.now())
    with pytest.raises(StaleLease):
        store.publish(task, result, clock.now())
    replacement = store.claim((task.key.kind,), clock.now(), timedelta(seconds=60))
    assert replacement is not None
    store.publish(replacement, result, clock.now())
    with pytest.raises(StaleLease):
        store.publish(replacement, result, clock.now())
    drain(pipeline, clock)
    assert current(pipeline, source)[1]["embeddings_ready"]
    assert current(pipeline, source)[1]["analysis_ready"]


@pytest.mark.parametrize("failed_stage", ["clean", "metadata", "requirements", "embed"])
def test_branch_failure_and_selective_retry(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
    failed_stage: str,
) -> None:
    pipeline, clock, source = build(isolated_database, owners[0], runs[0])
    generate, embed = JobFixture.generate, FixtureProvider.embed

    async def chat(
        adapter: JobFixture, prompt: str, data: str, schema: dict[str, Any]
    ) -> Reply:
        stage = (
            "clean"
            if "ordered" in schema["properties"]
            else "metadata"
            if "selection" in schema["properties"]
            else "requirements"
        )
        if stage == failed_stage:
            raise AIError("rate_limited")
        return await generate(adapter, prompt, data, schema)

    calls = 0

    async def vectors(adapter: FixtureProvider, texts: tuple[str, ...]) -> Reply:
        nonlocal calls
        calls += 1
        if failed_stage == "embed" and calls == 2:
            raise AIError("provider_failure")
        return await embed(adapter, texts)

    monkeypatch.setattr(JobFixture, "generate", chat)
    monkeypatch.setattr(FixtureProvider, "embed", vectors)
    drain(pipeline, clock)
    _, state = current(pipeline, source)
    assert state["content_ready"]
    assert state["analysis_ready"] == (failed_stage in {"clean", "embed"})
    assert state["embeddings_ready"] == (failed_stage != "embed")
    with isolated_database.transaction() as session:
        before = counts(session)
        failed = session.scalar(
            select(TaskRecord).where(
                TaskRecord.kind == pipeline.kinds[failed_stage],
                TaskRecord.status == "failed",
            )
        )
        if failed_stage == "clean":
            assert failed is None
        else:
            assert failed is not None and failed.error_code == "permanent_failure"
            PostgresTaskQueue(session).retry(
                snapshot(failed).key, failed.generation, clock.now()
            )
    monkeypatch.setattr(JobFixture, "generate", generate)
    drain(pipeline, clock)
    state = current(pipeline, source)[1]
    assert state["analysis_ready"] and state["embeddings_ready"]
    with isolated_database.transaction() as session:
        after = counts(session)
        assert after[0] == before[0] == 1
        if failed_stage in {"requirements", "embed"}:
            assert after[1] == before[1] == 1
        if failed_stage == "embed":
            assert calls == after[3] == 4  # First committed batch was not repeated.


@pytest.mark.parametrize("changed", ["clean", "metadata", "requirements", "embed"])
def test_revision_changes_reuse_unaffected_branches(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    changed: str,
) -> None:
    old, clock, source = build(isolated_database, owners[0], runs[0])
    drain(old, clock)
    with isolated_database.transaction() as session:
        before = counts(session)
    pipeline = PostgresPipeline(
        isolated_database,
        old.crawler,
        CleanJob(old.cleaner.factory, "changed prompt")
        if changed == "clean"
        else old.cleaner,
        ExtractMetadata(old.metadata.factory, "changed prompt")
        if changed == "metadata"
        else old.metadata,
        ExtractRequirements(old.requirements.factory, "changed prompt")
        if changed == "requirements"
        else old.requirements,
        EmbedJobs(
            old.embeddings.store,
            old.embeddings.space.model_copy(update={"revision": "changed-model"}),
            old.embeddings.factory,
        )
        if changed == "embed"
        else old.embeddings,
        old.policy,
    )
    pipeline.backfill(None, clock.now())
    drain(pipeline, clock)
    assert current(pipeline, source)[1]["analysis_ready"]
    assert current(pipeline, source)[1]["embeddings_ready"]
    with isolated_database.transaction() as session:
        after = counts(session)
        assert after[0] == before[0] + int(changed == "clean")
        assert after[1] == before[1] + int(changed == "metadata")
        assert after[2] == before[2] + int(changed in {"metadata", "requirements"})
        assert after[3] == before[3] * (2 if changed in {"embed", "clean"} else 1)


@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize("removed", ["user", "search", "run"])
def test_cancellation_rechecked_before_publication(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    shared: bool,
    removed: str,
) -> None:
    pipeline, clock, source = build(isolated_database, owners[0], runs[0])
    store, task, result = claim_stage(pipeline, clock, "clean")
    with isolated_database.transaction() as session:
        if shared:
            PostgresJobRegistry(session).register(
                owners[1], runs[1], URL, 1, clock.now()
            )
        run = session.get(SearchRunRecord, runs[0])
        assert run is not None
        row = (
            session.get(UserRecord, owners[0])
            if removed == "user"
            else session.get(SavedSearchRecord, run.search_id)
        )
        if removed == "run":
            run.status = "cancelled"
        else:
            assert row is not None
            row.deleted_at = clock.now()
    if shared:
        store.publish(task, result, clock.now())
    else:
        with pytest.raises(PermanentTaskError):
            store.publish(task, result, clock.now())
        store.reject(task, TaskError.PERMANENT, None, clock.now())
    drain(pipeline, clock)
    with isolated_database.transaction() as session:
        assert (
            pipeline.artifacts(session, task.payload_ref).cleaned is not None
        ) == shared
    assert current(pipeline, source)[1]["embeddings_ready"] == shared
    if not shared:
        with isolated_database.transaction() as session:
            PostgresJobRegistry(session).register(
                owners[1], runs[1], URL, 1, clock.now()
            )
            for failed in session.scalars(
                select(TaskRecord).where(TaskRecord.status == "failed")
            ):
                PostgresTaskQueue(session).retry(
                    snapshot(failed).key, failed.generation, clock.now()
                )
        drain(pipeline, clock)
        assert current(pipeline, source)[1]["analysis_ready"]


def test_new_snapshot_discards_late_result(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    pipeline, clock, source = build(isolated_database, owners[0], runs[0])
    store, task, result = claim_stage(pipeline, clock, "clean")
    clock.sleep(1)
    with isolated_database.transaction() as session:
        newer = PostgresSnapshots(session).publish(
            source,
            uuid4(),
            0,
            FetchedSnapshot(
                URL, URL, 200, "text/plain", b"A different position.", clock.now()
            ),
        )
        pipeline.enqueue(session, newer.id, clock.now())
    with pytest.raises(PermanentTaskError):
        store.publish(task, result, clock.now())
    store.reject(task, TaskError.PERMANENT, None, clock.now())
    drain(pipeline, clock)
    with isolated_database.transaction() as session:
        assert pipeline.artifacts(session, task.payload_ref).cleaned is None
        assert not pipeline.status(session, task.payload_ref)["content_ready"]
    assert current(pipeline, source)[0] == newer.id
    assert current(pipeline, source)[1]["analysis_ready"]
