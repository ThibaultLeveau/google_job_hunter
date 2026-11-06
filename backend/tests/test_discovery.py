import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from test_profiles import NOW, ORIGIN, TOKEN
from test_profiles import owners as profile_owners

from job_hunter import worker
from job_hunter.app import create_app
from job_hunter.application.search_provider import (
    SearchCapability,
    SearchError,
    SearchErrorCode,
    SearchPage,
    SearchRequest,
    SearchResult,
    SearchUsage,
)
from job_hunter.application.searches import SearchRun, SearchWrite
from job_hunter.application.worker import RetryableTaskError
from job_hunter.domain.searches import SearchDefinition
from job_hunter.domain.tasks import NewTask, StaleLease, Task
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.discovery import CRAWL, PostgresDiscovery
from job_hunter.infrastructure.jobs import (
    JobSourceRecord,
    PostgresJobRegistry,
    RunJobRecord,
)
from job_hunter.infrastructure.searches import (
    DISCOVERY,
    PostgresSearches,
    SearchRunRecord,
)
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.infrastructure.worker import PostgresWorkerStore
from job_hunter.settings import Settings

owners = profile_owners
A, B, C, D = (f"https://jobs.example.org/{n}" for n in range(4))


def page(
    urls: tuple[str, ...], index: int = 0, following: int | None = None
) -> SearchPage:
    return SearchPage(
        results=tuple(
            SearchResult(title="Unverified", source_url=url, rank=index * 10 + rank + 1)
            for rank, url in enumerate(urls)
        ),
        next_page=following,
        truncated=False,
        usage=SearchUsage(
            request_id=f"fixture-{index}", received=len(urls), returned=len(urls)
        ),
    )


class Provider:
    capability = SearchCapability(
        provider="fixture", status="fixture", max_pages=2, max_results=20
    )

    def __init__(self, pages: tuple[SearchPage | Exception, ...]) -> None:
        self.pages, self.calls = pages, list[SearchRequest]()

    def fetch(self, request: SearchRequest) -> SearchPage:
        self.calls.append(request)
        value = self.pages[request.page]
        if isinstance(value, Exception):
            raise value
        return value


def searches(database: Database) -> PostgresSearches:
    return PostgresSearches(database, lambda: NOW, ())


def start(database: Database, owner: UUID) -> SearchRun:
    service = searches(database)
    saved = service.save(
        owner,
        SearchWrite(
            name="Private", definition=SearchDefinition(groups=[["secret query"]])
        ),
    )
    return service.start(owner, saved.id, saved.revision, uuid4())


def claim(store: PostgresWorkerStore) -> Task:
    task = store.claim((DISCOVERY,), NOW, timedelta(seconds=60))
    assert task is not None
    return task


def process(stage: PostgresDiscovery) -> Task:
    store = PostgresWorkerStore(stage.database, stage.handlers())
    task = claim(store)
    store.publish(task, stage.execute(task), stage.now())
    return task


def test_checkpoints_snapshot_overlap_reuse_and_api_outcomes(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
) -> None:
    run = start(isolated_database, owners[0])
    searches(isolated_database).save(
        owners[0],
        SearchWrite(name="Edited", definition=SearchDefinition(groups=[["Go"]])),
        run.search_id,
        1,
    )
    provider = Provider(
        (
            page((A + "?utm_source=first", B, C), following=1),
            page((A, D, "https://bad_host.example/job"), 1),
        )
    )
    stage = PostgresDiscovery(isolated_database, provider, lambda: NOW)
    process(stage)
    halfway = searches(isolated_database).run(owners[0], run.id)
    assert halfway.status == "running" and halfway.discovery.pages_completed == 1
    assert halfway.counters.processing_total == 3
    process(stage)
    done = searches(isolated_database).run(owners[0], run.id)
    assert done.status == "completed" and done.discovery.pages_completed == 2
    assert done.counters.model_dump() == dict(
        found_results=6,
        new_urls=4,
        known_urls=1,
        skipped_results=1,
        processing_total=4,
        processing_ready=0,
        processing_failed=0,
    )
    assert all(request.query == run.snapshot.query for request in provider.calls)
    with isolated_database.transaction() as session:
        crawls = session.scalars(
            select(TaskRecord).where(TaskRecord.kind == CRAWL).order_by(TaskRecord.id)
        ).all()
        assert len(crawls) == 4
        crawl_ids = {t.id for t in crawls}
        crawls[0].status, crawls[1].status = "succeeded", "failed"
    second = start(isolated_database, owners[1])
    provider.pages = (page((A + "?utm_source=second", B, C, D, C)),)
    process(stage)
    reused = searches(isolated_database).run(owners[1], second.id)
    assert reused.counters.new_urls == 0 and reused.counters.known_urls == 5
    assert (
        reused.counters.processing_total == 4 and reused.counters.processing_failed == 1
    )
    with isolated_database.transaction() as session:
        assert (
            set(session.scalars(select(TaskRecord.id).where(TaskRecord.kind == CRAWL)))
            == crawl_ids
        )
        assert session.scalar(select(func.count()).select_from(JobSourceRecord)) == 4
        rows = PostgresJobRegistry(session).discoveries(owners[0], run.id)
        assert rows[0].original_url.endswith("utm_source=first")
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = lambda: NOW
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set("job_hunter_session", TOKEN)
        response = client.get(f"/api/v1/searches/runs/{run.id}")
        assert (
            response.status_code == 200
            and response.json()["discovery"]["pages_completed"] == 2
        )
        assert client.get(f"/api/v1/searches/runs/{second.id}").status_code == 404


@pytest.mark.parametrize(
    ("code", "partial"),
    [
        ("disabled", False),
        ("unconfigured", False),
        ("timeout", False),
        ("quota", True),
        ("rate_limited", True),
        ("provider_failure", True),
    ],
)
def test_safe_terminal_and_partial_errors(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    code: SearchErrorCode,
    partial: bool,
) -> None:
    run = start(isolated_database, owners[0])
    error = (
        RuntimeError("private-provider-error-canary")
        if code == "provider_failure"
        else SearchError(code)
    )
    provider = Provider((page((A,), following=1), error) if partial else (error,))
    stage = PostgresDiscovery(isolated_database, provider, lambda: NOW)
    if partial:
        process(stage)
    process(stage)
    result = searches(isolated_database).run(owners[0], run.id)
    assert result.status == ("partial" if partial else "failed")
    assert (
        result.discovery.error_code == code
        and result.discovery.pages_completed == int(partial)
    )
    assert result.counters.processing_total == int(partial)
    assert "private-provider-error-canary" not in result.model_dump_json()
    assert (
        PostgresWorkerStore(isolated_database, stage.handlers()).claim(
            (DISCOVERY,), NOW, timedelta(seconds=60)
        )
        is None
    )


@pytest.mark.parametrize("kind", ["results", "pages", "invalid"])
def test_caps_and_invalid_provider_contract(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    kind: str,
) -> None:
    malformed = kind == "invalid"
    run = start(isolated_database, owners[0])
    provider = Provider((page((A, B) if malformed else (A,), following=1),))
    provider.capability = provider.capability.model_copy(
        update={"max_pages": 1} if kind == "pages" else {"max_results": 1}
    )
    process(PostgresDiscovery(isolated_database, provider, lambda: NOW))
    result = searches(isolated_database).run(owners[0], run.id)
    assert result.status == ("failed" if malformed else "completed")
    assert result.discovery.limited is (not malformed) and len(provider.calls) == 1
    assert result.discovery.error_code == ("invalid_response" if malformed else None)


def test_crash_replay_atomic_enqueue_and_resume(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = start(isolated_database, owners[0])
    provider = Provider((page((A,), following=1), page((B,), 1)))
    provider.capability = provider.capability.model_copy(
        update={"provider": "serpapi", "status": "configured"}
    )
    now = [NOW]
    stage = PostgresDiscovery(isolated_database, provider, lambda: now[0])
    store = PostgresWorkerStore(isolated_database, stage.handlers())
    abandoned = claim(store)
    first = stage.execute(abandoned)
    now[0] += timedelta(seconds=61)
    store.recover((DISCOVERY,), now[0])
    with pytest.raises(StaleLease):
        store.publish(abandoned, first, now[0])
    task = store.claim((DISCOVERY,), now[0], timedelta(seconds=60))
    assert task is not None and task.attempts == 2
    result = stage.execute(task)
    enqueue = PostgresTaskQueue.enqueue

    def broken(queue: PostgresTaskQueue, next_task: NewTask, now: datetime) -> Task:
        enqueue(queue, next_task, now)
        raise SQLAlchemyError("private storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresTaskQueue, "enqueue", broken)
        with pytest.raises(RetryableTaskError):
            store.publish(task, result, now[0])
    assert PostgresTaskQueue.enqueue is enqueue
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(JobSourceRecord)) == 0
        assert session.scalar(select(func.count()).select_from(RunJobRecord)) == 0
        row = session.get(SearchRunRecord, run.id)
        assert row is not None and row.discovery["pages_completed"] == 0
        assert (
            session.scalar(select(TaskRecord.status).where(TaskRecord.id == task.id))
            == "running"
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(TaskRecord)
                .where(TaskRecord.kind == CRAWL)
            )
            == 0
        )
    store.publish(task, result, now[0])
    next_task = store.claim((DISCOVERY,), now[0], timedelta(seconds=60))
    assert next_task is not None and next_task.key.subject_version == 2
    restarted = PostgresDiscovery(isolated_database, provider, lambda: now[0])
    store.publish(next_task, restarted.execute(next_task), now[0])
    done = searches(isolated_database).run(owners[0], run.id)
    assert done.counters.new_urls == 2 and done.discovery.provider_requests == 3
    assert [request.page for request in provider.calls] == [0, 0, 1]
    assert stage.execute(abandoned) is None


@pytest.mark.parametrize("action", ["cancel", "tombstone", "delete"])
def test_late_response_cannot_publish_after_cancellation_or_owner_deletion(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = start(isolated_database, owners[0])
    database = Database(Settings(database_pool_size=2))
    entered, release = Event(), Event()
    provider = Provider((page((A,), following=1), page((B,), 1)))
    stage = PostgresDiscovery(database, provider, lambda: NOW)
    store = PostgresWorkerStore(database, stage.handlers())
    if action == "cancel":
        process(stage)

    def blocked(request: SearchRequest) -> SearchPage:
        entered.set()
        assert release.wait(timeout=5)
        return page((B,), request.page)

    monkeypatch.setattr(provider, "fetch", blocked)
    task = claim(store)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(stage.execute, task)
            assert entered.wait(timeout=5)
            with database.transaction() as session:
                session.execute(
                    select(SearchRunRecord)
                    .where(SearchRunRecord.id == run.id)
                    .with_for_update(nowait=True)
                )
                session.execute(
                    select(UserRecord)
                    .where(UserRecord.id == owners[0])
                    .with_for_update(nowait=True)
                )
            if action == "cancel":
                assert (
                    searches(database).cancel(owners[0], run.id).status == "cancelled"
                )
            else:
                with database.transaction() as session:
                    session.execute(
                        delete(UserRecord).where(UserRecord.id == owners[0])
                        if action == "delete"
                        else update(UserRecord)
                        .where(UserRecord.id == owners[0])
                        .values(deleted_at=NOW)
                    )
            release.set()
            result = future.result(timeout=5)
            if action == "cancel":
                with pytest.raises(StaleLease):
                    store.publish(task, result, NOW)
            else:
                store.publish(task, result, NOW)
        with database.transaction() as session:
            assert session.scalar(
                select(func.count()).select_from(JobSourceRecord)
            ) == int(action == "cancel")
            assert session.scalar(
                select(func.count()).select_from(RunJobRecord)
            ) == int(action == "cancel")
            assert session.scalar(
                select(func.count())
                .select_from(TaskRecord)
                .where(TaskRecord.kind == CRAWL)
            ) == int(action == "cancel")
    finally:
        release.set()
        database.close()


def test_exhausted_worker_outcome_is_visible_and_command_registers_discovery(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = start(isolated_database, owners[0])
    with isolated_database.transaction() as session:
        session.execute(
            update(TaskRecord)
            .where(TaskRecord.kind == DISCOVERY)
            .values(status="failed")
        )
    failed = searches(isolated_database).run(owners[0], run.id)
    assert failed.status == "failed" and failed.discovery.error_code == "worker_failure"
    second = start(isolated_database, owners[1])
    provider = Provider((page((A,)),))

    def stop_after_response(request: SearchRequest) -> SearchPage:
        os.kill(os.getpid(), signal.SIGTERM)
        return page((A,))

    monkeypatch.setattr(provider, "fetch", stop_after_response)
    monkeypatch.setattr(worker, "create_search_provider", lambda _: provider)
    monkeypatch.setattr(worker.SystemClock, "now", lambda _: NOW)
    monkeypatch.setattr(sys, "argv", ["worker", "--concurrency", "1"])
    previous = {
        signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        worker.main()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    assert searches(isolated_database).run(owners[1], second.id).status == "completed"
