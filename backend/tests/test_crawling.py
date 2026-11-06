import os
import signal
import sys
from datetime import datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from http_fixture import FixtureHTTP, http_response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_discovery import Provider, page
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_profiles import HEADERS, NOW, ORIGIN, TOKEN
from test_worker import FakeClock, immediate, read

from job_hunter import worker as command_worker
from job_hunter.app import create_app
from job_hunter.application.crawling import CRAWL, CrawlResult
from job_hunter.application.fetching import FetchRequest, FetchResponse
from job_hunter.application.worker import DeferredTaskError, Worker
from job_hunter.domain.snapshots import FetchedSnapshot
from job_hunter.domain.tasks import NewTask, StaleLease, Task, TaskError, TaskKey
from job_hunter.infrastructure.crawl_policy import (
    CrawlPolicyStore,
    HostRecord,
    PoliteFetcher,
)
from job_hunter.infrastructure.crawling import CrawlProgressRecord, PostgresCrawler
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.fetching import FetchSettings, HttpFetcher
from job_hunter.infrastructure.jobs import JobSourceRecord, PostgresJobRegistry
from job_hunter.infrastructure.searches import SearchRunRecord
from job_hunter.infrastructure.snapshots import SnapshotRecord
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.infrastructure.worker import PostgresWorkerStore
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
owners, runs = registry_owners, registry_runs
URL = "https://jobs.example.org/job"
TARGET = "https://target.example.org/destination"
HTML = ((b"Content-Type", b"text/html"),)


def setup(
    database: Database,
    owner: UUID,
    run_id: UUID,
    response: list[bytes] | None = None,
    *,
    attempts: int = 3,
    redirects: int = 5,
) -> tuple[PostgresCrawler, Worker, FakeClock, FixtureHTTP, Task]:
    clock = FakeClock()
    fixture = FixtureHTTP(
        {
            URL: response or http_response(b"<p>Public job</p>", headers=HTML),
            "https://jobs.example.org/robots.txt": http_response(
                b"User-agent: *\nAllow: /"
            ),
            "https://target.example.org/robots.txt": http_response(
                b"User-agent: *\nAllow: /"
            ),
            TARGET: http_response(b"<p>Destination job</p>", headers=HTML),
        }
    )
    stage = PostgresCrawler(
        database,
        PoliteFetcher(
            CrawlPolicyStore(database),
            HttpFetcher(
                FetchSettings(max_redirects=redirects),
                fixture.resolve,
                fixture,
                clock.now,
            ),
            clock,
        ),
        clock,
    )
    with database.transaction() as session:
        found = PostgresJobRegistry(session).register(owner, run_id, URL, 1, NOW)
        run = session.get(SearchRunRecord, run_id)
        assert run is not None
        run.processing_total += int(found.new_association)
        task = PostgresTaskQueue(session).enqueue(
            NewTask(TaskKey(CRAWL, found.source_id, 1), found.source_id, NOW, attempts),
            NOW,
        )
    handlers = stage.handlers()
    process = Worker(
        PostgresWorkerStore(database, handlers),
        {CRAWL: stage.execute},
        clock,
        immediate,
        Event(),
        lambda: 1,
        concurrency=1,
    )
    return stage, process, clock, fixture, task


def finish(database: Database, process: Worker, clock: FakeClock, task: Task) -> Task:
    for _ in range(40):
        process.tick()
        state = read(database, task)
        if state.status in {"succeeded", "failed"}:
            return state
        if not process.running:
            clock.current = max(clock.now(), state.run_at)
    raise AssertionError("Crawl did not finish within bounded fixture steps")


@pytest.mark.parametrize(
    "status,mime,body,outcome",
    [
        (200, b"text/html", b"<p>Public job</p>", "fetched"),
        (403, b"text/html", b"private denial details", "blocked"),
        (404, b"text/html", b"missing", "removed"),
        (200, b"application/pdf", b"binary", "unsupported"),
        (200, b"text/plain", b"  ", "non_job"),
        (
            200,
            b"text/html",
            b'<div id="root"></div><noscript>You need to enable JavaScript</noscript>',
            "unsupported",
        ),
        (
            200,
            b"text/html",
            b"<p>Public job</p><noscript>Please enable JavaScript</noscript>",
            "fetched",
        ),
        (200, b"text/html", b'<input name="secret" type="password">', "blocked"),
        (200, b"text/html", b'<div class="g-recaptcha"></div>', "blocked"),
    ],
)
def test_worker_persists_explicit_outcomes(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    status: int,
    mime: bytes,
    body: bytes,
    outcome: str,
) -> None:
    _, process, clock, fixture, task = setup(
        isolated_database,
        owners[0],
        runs[0],
        http_response(body, status, ((b"Content-Type", mime),)),
    )
    state = finish(isolated_database, process, clock, task)
    assert state.status == ("succeeded" if outcome == "fetched" else "failed")
    assert state.attempts == 1 and len(fixture.requests) == 2
    with isolated_database.transaction() as session:
        source = session.get(JobSourceRecord, task.key.subject_id)
        assert source is not None and source.crawl_state == outcome
        snapshot = session.get(SnapshotRecord, source.current_snapshot_id)
        assert snapshot is not None and snapshot.status == status
        assert snapshot.body == (body if status == 200 else b"")
        assert session.scalar(
            select(SearchRunRecord.processing_failed).where(
                SearchRunRecord.id == runs[0]
            )
        ) == int(outcome != "fetched")


@pytest.mark.parametrize("denied", [True, False])
def test_redirect_checkpoint_applies_destination_policy_and_recovers_host_slot(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    denied: bool,
) -> None:
    stage, process, clock, fixture, task = setup(
        isolated_database,
        owners[0],
        runs[0],
        http_response(status=302, headers=((b"Location", TARGET.encode()),)),
    )
    lease = stage.fetcher.store.claim("target.example.org", NOW, timedelta(seconds=25))
    if denied:
        fixture.responses["https://target.example.org/robots.txt"] = http_response(
            b"User-agent: *\nDisallow: /"
        )
    state = finish(isolated_database, process, clock, task)
    assert state.status == ("failed" if denied else "succeeded")
    assert clock.now() >= NOW + timedelta(seconds=25)
    assert sum(request.startswith(b"GET /job ") for request in fixture.requests) == 1
    assert len(fixture.requests) == (3 if denied else 4)
    with pytest.raises(StaleLease):
        stage.fetcher.store.release(lease, clock.now(), 1)
    with isolated_database.transaction() as session:
        progress = session.get(CrawlProgressRecord, (task.id, 0))
        assert (
            progress is not None
            and progress.next_url == TARGET
            and progress.visited == [URL]
        )
        source = session.get(JobSourceRecord, task.key.subject_id)
        assert source is not None and source.crawl_error == (
            "robots_denied" if denied else None
        )


def test_retry_after_resumes_redirect_without_repeating_prior_hops(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    _, process, clock, fixture, task = setup(
        isolated_database,
        owners[0],
        runs[0],
        http_response(status=302, headers=((b"Location", TARGET.encode()),)),
    )
    fixture.responses[TARGET] = http_response(
        status=429, headers=((b"Retry-After", b"120"),)
    )
    for _ in range(20):
        process.tick()
        state = read(isolated_database, task)
        if state.status == "pending" and state.attempts == 1:
            break
        if not process.running:
            clock.current = state.run_at
    assert state.run_at == clock.now() + timedelta(seconds=120)
    fixture.responses[TARGET] = http_response(b"public job", headers=HTML)
    assert finish(isolated_database, process, clock, task).status == "succeeded"
    assert sum(request.startswith(b"GET /job ") for request in fixture.requests) == 1
    assert (
        sum(request.startswith(b"GET /destination ") for request in fixture.requests)
        == 2
    )


@pytest.mark.parametrize(
    "location,error",
    [
        (b"http://127.0.0.1/", "unsafe_address"),
        (b"https://third.example.org/", "redirect_limit"),
    ],
)
def test_redirect_budget_and_ssrf_span_task_claims(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    location: bytes,
    error: str,
) -> None:
    _, process, clock, fixture, task = setup(
        isolated_database,
        owners[0],
        runs[0],
        http_response(status=302, headers=((b"Location", TARGET.encode()),)),
        redirects=1 if error == "redirect_limit" else 5,
    )
    fixture.responses[TARGET] = http_response(
        status=302, headers=((b"Location", location),)
    )
    assert finish(isolated_database, process, clock, task).status == "failed"
    assert len(fixture.requests) == 4
    with isolated_database.transaction() as session:
        assert session.scalar(select(JobSourceRecord.crawl_error)) == error


def test_redelivery_and_manual_retry_preserve_snapshot_versions(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    stage, process, clock, fixture, task = setup(
        isolated_database, owners[0], runs[0], http_response(status=404)
    )
    finish(isolated_database, process, clock, task)
    with pytest.raises(StaleLease):
        stage.execute(task)
    with isolated_database.transaction() as session:
        first = session.scalars(select(SnapshotRecord)).one()
        PostgresTaskQueue(session).retry(task.key, 0, clock.now())
    fixture.responses[URL] = http_response(b"new public job", headers=HTML)
    assert finish(isolated_database, process, clock, task).status == "succeeded"
    with isolated_database.transaction() as session:
        rows = session.scalars(
            select(SnapshotRecord).order_by(SnapshotRecord.version)
        ).all()
        assert len(rows) == 2 and rows[1].previous_id == first.id
        assert (
            session.scalar(
                select(SearchRunRecord.processing_failed).where(
                    SearchRunRecord.id == runs[0]
                )
            )
            == 0
        )
    before = len(fixture.requests)
    process.tick()
    assert len(fixture.requests) == before


@pytest.mark.parametrize("crash", [False, True])
def test_exhaustion_and_crash_recovery_publish_failure_atomically(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    crash: bool,
) -> None:
    stage, process, clock, fixture, task = setup(
        isolated_database, owners[0], runs[0], attempts=1
    )
    if crash:
        owned = process.store.claim((CRAWL,), NOW, timedelta(seconds=60))
        assert owned is not None
        with pytest.raises(DeferredTaskError):
            stage.execute(owned)
        clock.sleep(61)
        process.store.recover((CRAWL,), clock.now())
        with pytest.raises(StaleLease):
            process.store.publish(owned, CrawlResult("fetched"), clock.now())
    else:
        fixture.responses[URL] = http_response(status=503)
        finish(isolated_database, process, clock, task)
    assert read(isolated_database, task).error_code == TaskError.EXHAUSTED
    with isolated_database.transaction() as session:
        assert session.scalar(select(JobSourceRecord.crawl_state)) == "failed"
        assert (
            session.scalar(
                select(SearchRunRecord.processing_failed).where(
                    SearchRunRecord.id == runs[0]
                )
            )
            == 1
        )


def test_no_database_locks_during_http_and_stale_result_cannot_publish(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage, process, clock, _, task = setup(isolated_database, owners[0], runs[0])
    process.tick()
    process.tick()
    clock.sleep(1)
    real_fetch = stage.fetcher.http.fetch

    def fetch(request: FetchRequest) -> FetchResponse:
        with isolated_database.transaction() as session:
            assert session.scalar(select(HostRecord.host).with_for_update(nowait=True))
            active = session.get(TaskRecord, task.id, with_for_update={"nowait": True})
            assert active is not None
            active.lease_token = uuid4()
        return real_fetch(request)

    monkeypatch.setattr(stage.fetcher.http, "fetch", fetch)
    process.tick()
    process.tick()
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SnapshotRecord)) == 0


def test_convergence_reuses_completed_snapshot_for_pending_canonical_task(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    _, process, clock, fixture, task = setup(
        isolated_database,
        owners[0],
        runs[0],
        http_response(status=302, headers=((b"Location", TARGET.encode()),)),
    )
    with isolated_database.transaction() as session:
        target = PostgresJobRegistry(session).register(
            owners[1], runs[1], TARGET, 1, NOW
        )
        run = session.get(SearchRunRecord, runs[1])
        assert run is not None
        run.processing_total = 1
        pending = PostgresTaskQueue(session).enqueue(
            NewTask(
                TaskKey(CRAWL, target.source_id, 1),
                target.source_id,
                NOW + timedelta(seconds=10),
            ),
            NOW,
        )
    assert finish(isolated_database, process, clock, task).status == "succeeded"
    count = len(fixture.requests)
    clock.sleep(10)
    assert finish(isolated_database, process, clock, pending).status == "succeeded"
    assert len(fixture.requests) == count
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SnapshotRecord)) == 1
        assert session.scalar(select(func.count()).select_from(JobSourceRecord)) == 1
        for owner in owners:
            assert (
                PostgresJobRegistry(session).get(owner, target.source_id).id
                == target.source_id
            )


def test_late_denial_preserves_newer_success(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage, process, clock, _, task = setup(
        isolated_database, owners[0], runs[0], http_response(status=403)
    )
    process.tick()
    process.tick()
    clock.sleep(1)
    original = stage.fetcher.http.fetch

    def fetch(request: FetchRequest) -> FetchResponse:
        clock.sleep(1)
        with isolated_database.transaction() as session:
            PostgresTaskQueue(session).enqueue(
                NewTask(
                    TaskKey(CRAWL, task.key.subject_id, 2),
                    task.payload_ref,
                    clock.now(),
                ),
                clock.now(),
            )
        newer = process.store.claim((CRAWL,), clock.now(), timedelta(seconds=60))
        assert newer is not None
        process.store.publish(
            newer,
            CrawlResult(
                "fetched",
                snapshot=FetchedSnapshot(
                    URL, URL, 200, "text/html", b"new job", clock.now()
                ),
            ),
            clock.now(),
        )
        clock.sleep(1)
        return original(request)

    monkeypatch.setattr(stage.fetcher.http, "fetch", fetch)
    assert finish(isolated_database, process, clock, task).status == "failed"
    with isolated_database.transaction() as session:
        source = session.get(JobSourceRecord, task.key.subject_id)
        assert source is not None and source.crawl_state == "fetched"
        current = session.get(SnapshotRecord, source.current_snapshot_id)
        assert current is not None and current.body == b"new job"
        assert (
            session.scalar(
                select(SearchRunRecord.processing_failed).where(
                    SearchRunRecord.id == runs[0]
                )
            )
            == 0
        )


def test_api_to_real_worker_and_database_journey(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FixtureHTTP(
        {
            "https://jobs.example.org/robots.txt": http_response(
                b"User-agent: *\nAllow: /"
            ),
            URL: http_response(
                b"pdf", headers=((b"Content-Type", b"application/pdf"),)
            ),
        }
    )
    monkeypatch.setattr(
        command_worker,
        "HttpFetcher",
        lambda settings, now: HttpFetcher(settings, fixture.resolve, fixture, now),
    )
    monkeypatch.setattr(
        command_worker, "create_search_provider", lambda _: Provider((page((URL,)),))
    )
    original = PostgresCrawler.publish

    def stop(
        stage: PostgresCrawler,
        session: Session,
        task: Task,
        result: object,
        now: datetime,
    ) -> None:
        original(stage, session, task, result, now)
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(PostgresCrawler, "publish", stop)
    monkeypatch.setattr(sys, "argv", ["worker", "--concurrency", "1"])
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = lambda: NOW
    with TestClient(app, base_url=ORIGIN, headers=HEADERS) as client:
        client.cookies.set("job_hunter_session", TOKEN)
        saved = client.post(
            "/api/v1/searches",
            json={"name": "Fixture", "definition": {"groups": [["Python"]]}},
        ).json()
        run = client.post(
            f"/api/v1/searches/{saved['id']}/runs",
            json={"expected_revision": 1, "idempotency_key": str(uuid4())},
        ).json()
        previous = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            command_worker.main()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        response = client.get(f"/api/v1/searches/runs/{run['id']}")
        assert response.status_code == 200 and response.json()["status"] == "completed"
        assert response.json()["counters"]["processing_failed"] == 1
        client.cookies.clear()
        client.cookies.set("job_hunter_session", "b" * 43)
        assert client.get(f"/api/v1/searches/runs/{run['id']}").status_code == 404
    assert len(fixture.requests) == 2
    config = Config(toml_file="pyproject.toml")
    command.downgrade(config, "0020_job_snapshots")
    command.upgrade(config, "head")
    command.check(config)
