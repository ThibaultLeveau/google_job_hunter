from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from http_fixture import FixtureHTTP, http_response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_worker import NOW, FakeClock, enqueue, make_worker, read

from job_hunter.application.fetching import FetchError, FetchRequest, FetchResponse
from job_hunter.application.worker import DeferredTaskError, RetryableTaskError
from job_hunter.domain.tasks import StaleLease, Task, TaskError
from job_hunter.infrastructure.crawl_policy import (
    CrawlPolicyStore,
    HostLease,
    HostRecord,
    PoliteFetcher,
    RobotsDenied,
    RobotsPolicy,
    RobotsRecord,
    RobotsUnavailable,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.fetching import FetchSettings, HttpFetcher
from job_hunter.infrastructure.tasks import PostgresTaskQueue
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
ORIGIN = "https://jobs.example.org"
PAGE = FetchRequest(ORIGIN + "/jobs/1")


def crawler(
    database: Database, robots: list[bytes] | None = None
) -> tuple[PoliteFetcher, FixtureHTTP, FakeClock]:
    fixture = FixtureHTTP(
        {
            ORIGIN + "/robots.txt": robots or http_response(b"User-agent: *\nAllow: /"),
            PAGE.url: http_response(),
        }
    )
    clock = FakeClock()
    service = PoliteFetcher(
        CrawlPolicyStore(database),
        HttpFetcher(FetchSettings(), fixture.resolve, fixture, clock.now),
        clock,
    )
    return service, fixture, clock


def ready(
    service: PoliteFetcher, clock: FakeClock, request: FetchRequest = PAGE
) -> None:
    with pytest.raises(DeferredTaskError) as wait:
        service.fetch(request)
    clock.current = wait.value.retry_at


@pytest.mark.parametrize(
    "status,decision",
    [
        (404, "allow"),
        (410, "allow"),
        (401, "deny"),
        (403, "deny"),
        (429, "unavailable"),
        (503, "unavailable"),
    ],
)
def test_robots_status_and_cached_decisions(
    isolated_database: Database, status: int, decision: str
) -> None:
    service, fixture, clock = crawler(isolated_database, http_response(status=status))
    policy = service.robots(ORIGIN, "jobs.example.org")
    assert policy.decision == decision
    assert service.robots(ORIGIN, "jobs.example.org") == policy
    assert len(fixture.requests) == 1
    clock.sleep(1)
    if decision == "allow":
        assert service.fetch(PAGE).body == b"public job"
    else:
        with pytest.raises(RobotsDenied if decision == "deny" else RobotsUnavailable):
            service.fetch(PAGE)
        assert len(fixture.requests) == 1


def test_rules_cache_expiry_and_declared_pacing(isolated_database: Database) -> None:
    body = (
        b"User-agent: *\nDisallow: /\nUser-agent: JobHunter\nDisallow: /jobs/*\n"
        b"Allow: /jobs/1$\nCrawl-delay: 2\nRequest-rate: 1/5"
    )
    service, fixture, clock = crawler(isolated_database, http_response(body))
    ready(service, clock)
    assert clock.now() == NOW + timedelta(seconds=5)
    assert service.fetch(PAGE).body == b"public job"
    assert b"User-Agent: JobHunter/0.1" in fixture.requests[0]
    with pytest.raises(RobotsDenied):
        service.fetch(FetchRequest(ORIGIN + "/jobs/2"))
    assert len(fixture.requests) == 2
    fixture.responses[ORIGIN + "/robots.txt"] = http_response(
        b"User-agent: *\nDisallow: /"
    )
    clock.sleep(3600)
    with pytest.raises(RobotsDenied):
        service.fetch(PAGE)
    assert len(fixture.requests) == 3


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"\xff", ()),
        (b"a" * 512001, ()),
        (b"<html>Sign in</html>", ((b"Content-Type", b"text/html"),)),
    ],
)
def test_invalid_robots_fails_closed(
    isolated_database: Database, body: bytes, headers: tuple[tuple[bytes, bytes], ...]
) -> None:
    service, fixture, _ = crawler(
        isolated_database, http_response(body, headers=headers)
    )
    with pytest.raises(RobotsUnavailable):
        service.fetch(PAGE)
    assert len(fixture.requests) == 1


@pytest.mark.parametrize("delay", ["3601", "9999999999999"])
def test_unsupported_robots_delay_denies(
    isolated_database: Database, delay: str
) -> None:
    service, _, _ = crawler(
        isolated_database,
        http_response(f"User-agent: *\nCrawl-delay: {delay}".encode()),
    )
    with pytest.raises(RobotsDenied):
        service.fetch(PAGE)


def test_robots_redirect_and_dns_do_not_bypass_policy(
    isolated_database: Database,
) -> None:
    service, fixture, clock = crawler(
        isolated_database,
        http_response(
            status=302,
            headers=((b"Location", b"https://elsewhere.example.org/robots.txt"),),
        ),
    )
    with pytest.raises(RobotsUnavailable):
        service.fetch(PAGE)
    assert len(fixture.requests) == 1
    clock.sleep(60)
    fixture.addresses = ("127.0.0.1",)
    with pytest.raises(RobotsUnavailable):
        service.fetch(PAGE)
    assert len(fixture.requests) == 1


def test_shared_host_claims_crash_recovery_and_fenced_release(
    isolated_database: Database,
) -> None:
    database = Database(Settings(database_pool_size=4))
    store = CrawlPolicyStore(database)
    barrier = Barrier(2)

    def claim() -> HostLease | datetime:
        barrier.wait(timeout=5)
        try:
            return store.claim("jobs.example.org", NOW, timedelta(seconds=25))
        except DeferredTaskError as error:
            return error.retry_at

    try:
        with ThreadPoolExecutor(2) as pool:
            results = [
                future.result(timeout=5)
                for future in [pool.submit(claim), pool.submit(claim)]
            ]
        leases = [result for result in results if isinstance(result, HostLease)]
        assert len(leases) == 1 and NOW + timedelta(seconds=25) in results
        replacement = store.claim(
            "jobs.example.org", NOW + timedelta(seconds=25), timedelta(seconds=25)
        )
        with pytest.raises(StaleLease):
            store.release(
                leases[0],
                NOW + timedelta(seconds=26),
                1,
                (ORIGIN, RobotsPolicy("allow", "", NOW + timedelta(hours=1))),
            )
        assert store.cached(ORIGIN, NOW) is None
        store.release(replacement, NOW + timedelta(seconds=26), 3)
        with pytest.raises(DeferredTaskError) as wait:
            store.claim(
                "jobs.example.org", NOW + timedelta(seconds=28), timedelta(seconds=25)
            )
        assert wait.value.retry_at == NOW + timedelta(seconds=29)
        with pytest.raises(StaleLease):
            store.release(replacement, NOW + timedelta(seconds=27), 1)
    finally:
        database.close()


def test_network_runs_without_locks_and_stale_cache_cannot_publish(
    isolated_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, fixture, clock = crawler(isolated_database)
    real_fetch = service.http.fetch

    def fetch(request: FetchRequest) -> FetchResponse:
        with isolated_database.transaction() as session:
            assert (
                session.scalar(select(HostRecord.host).with_for_update(nowait=True))
                == "jobs.example.org"
            )
        response = real_fetch(request)
        clock.sleep(26)
        return response

    monkeypatch.setattr(service.http, "fetch", fetch)
    with pytest.raises(StaleLease):
        service.fetch(PAGE)
    assert service.store.cached(ORIGIN, clock.now()) is None
    assert len(fixture.requests) == 1


@pytest.mark.parametrize("denied", [False, True])
def test_redirect_handoff_checks_destination_before_page(
    isolated_database: Database, denied: bool
) -> None:
    service, fixture, clock = crawler(isolated_database)
    target = "https://destination.example.org/job"
    fixture.responses[PAGE.url] = http_response(
        status=302, headers=((b"Location", target.encode()),)
    )
    fixture.responses["https://destination.example.org/robots.txt"] = http_response(
        b"User-agent: *\nDisallow: /" if denied else b""
    )
    fixture.responses[target] = http_response(b"destination")
    ready(service, clock)
    redirect = service.fetch(PAGE).redirect
    assert redirect is not None and redirect.visited == (PAGE.url,)
    with pytest.raises(RobotsDenied if denied else DeferredTaskError):
        service.fetch(redirect)
    assert len(fixture.requests) == 3
    if not denied:
        clock.sleep(1)
        assert service.fetch(redirect).body == b"destination"
        clock.sleep(1)
        with pytest.raises(FetchError, match="redirect_loop"):
            service.fetch(FetchRequest(target, (target,)))


def test_busy_redirect_and_unsafe_redirect_make_no_target_request(
    isolated_database: Database,
) -> None:
    service, fixture, clock = crawler(isolated_database)
    target = "https://busy.example.org/job"
    fixture.responses[PAGE.url] = http_response(
        status=302, headers=((b"Location", target.encode()),)
    )
    ready(service, clock)
    redirect = service.fetch(PAGE).redirect
    assert redirect is not None
    service.store.claim("busy.example.org", clock.now(), timedelta(seconds=25))
    with pytest.raises(DeferredTaskError):
        service.fetch(redirect)
    assert len(fixture.requests) == 2
    clock.sleep(1)
    fixture.responses[PAGE.url] = http_response(
        status=302, headers=((b"Location", b"http://127.0.0.1/"),)
    )
    with pytest.raises(FetchError, match="unsafe_address"):
        service.fetch(PAGE)
    assert len(fixture.requests) == 3


@pytest.mark.parametrize("robots", [False, True])
def test_retry_after_is_durable_and_exhausts_budget(
    isolated_database: Database, robots: bool
) -> None:
    service, fixture, clock = crawler(isolated_database)
    fixture.responses[ORIGIN + "/robots.txt" if robots else PAGE.url] = http_response(
        status=429, headers=((b"Retry-After", b"120"),)
    )
    task = enqueue(isolated_database, attempts=2)
    worker, worker_clock, _ = make_worker(
        isolated_database, lambda _: service.fetch(PAGE)
    )
    service.clock = worker_clock
    if not robots:
        ready(service, worker_clock)
    for attempt in (1, 2):
        now = worker_clock.now()
        worker.tick()
        worker.tick()
        state = read(isolated_database, task)
        assert state.attempts == attempt
        if attempt == 1:
            assert state.run_at == now + timedelta(seconds=120)
            assert state.status == "pending"
            worker_clock.sleep(119)
            worker.tick()
            assert not worker.running
            worker_clock.sleep(1)
        else:
            assert state.status == "failed" and state.error_code == TaskError.EXHAUSTED


def test_capacity_waits_refund_attempts_but_expire_and_fence(
    isolated_database: Database,
) -> None:
    task = enqueue(isolated_database, attempts=1)
    attempts: list[Task] = []
    worker, clock, store = make_worker(isolated_database)

    def busy(owned: Task) -> object:
        attempts.append(owned)
        raise DeferredTaskError(clock.now() + timedelta(seconds=2))

    worker.handlers["stage"] = busy
    for _ in range(4):
        worker.tick()
        worker.tick()
        state = read(isolated_database, task)
        assert (
            state.attempts == 0
            and state.status == "pending"
            and state.error_code is None
        )
        clock.sleep(2)
    with pytest.raises(StaleLease):
        store.postpone(attempts[0], clock.now() + timedelta(seconds=2), clock.now())
    clock.sleep(86400)
    worker.tick()
    worker.tick()
    assert read(isolated_database, task).error_code == TaskError.EXHAUSTED
    with isolated_database.transaction() as session, pytest.raises(ValueError):
        PostgresTaskQueue(session).postpone(task, NOW, NOW)
    with isolated_database.transaction() as session:
        PostgresTaskQueue(session).retry(task.key, 0, clock.now())
    worker.tick()
    worker.tick()
    state = read(isolated_database, task)
    assert state.status == "pending" and state.attempts == 0 and state.generation == 1


def test_future_retry_is_capped_and_database_errors_retry(
    isolated_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _ = crawler(isolated_database)
    task = enqueue(isolated_database)

    def unavailable(_: Task) -> object:
        raise RetryableTaskError(retry_at=NOW + timedelta(days=7))

    worker, _, _ = make_worker(isolated_database, unavailable)
    worker.tick()
    worker.tick()
    assert read(isolated_database, task).run_at == NOW + timedelta(hours=1)
    with isolated_database.transaction() as session:
        session.execute(text("DROP TABLE crawl_hosts"))
    with pytest.raises(RetryableTaskError):
        service.fetch(PAGE)


def test_policy_migration_constraints_and_round_trip(
    isolated_database: Database,
) -> None:
    with pytest.raises(IntegrityError), isolated_database.transaction() as session:
        session.add(
            HostRecord(host="bad.example.org", next_at=NOW, lease_token=uuid4())
        )
    for decision, body in [("bogus", ""), ("rules", "a" * 512001)]:
        with pytest.raises(IntegrityError), isolated_database.transaction() as session:
            session.add(
                RobotsRecord(
                    origin=ORIGIN, decision=decision, body=body, expires_at=NOW
                )
            )
    config = Config(toml_file=Path(__file__).resolve().parents[1] / "pyproject.toml")
    command.downgrade(config, "0018_discovery_progress")
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.parametrize(
    "value,seconds",
    [
        (b"0", 0),
        (b"99999999", 3600),
        (b"nonsense", None),
        (b"\xff", None),
        (b"Wed, 01 Jan 2026 00:00:00", None),
        (format_datetime(NOW + timedelta(seconds=123), usegmt=True).encode(), 123),
        (format_datetime(NOW - timedelta(seconds=1), usegmt=True).encode(), 0),
        (format_datetime(NOW + timedelta(days=5), usegmt=True).encode(), 3600),
    ],
)
def test_retry_after_formats(
    isolated_database: Database, value: bytes, seconds: int | None
) -> None:
    service, _, _ = crawler(isolated_database)
    assert service.http.retry_after(value) == seconds


def test_http_date_retry_and_shared_http_https_pacing(
    isolated_database: Database,
) -> None:
    service, fixture, clock = crawler(isolated_database)
    ready(service, clock)
    fixture.responses[PAGE.url] = http_response(
        status=503,
        headers=(
            (
                b"Retry-After",
                format_datetime(
                    clock.now() + timedelta(seconds=123), usegmt=True
                ).encode(),
            ),
        ),
    )
    with pytest.raises(RetryableTaskError) as failure:
        service.fetch(PAGE)
    assert failure.value.retry_at == clock.now() + timedelta(seconds=123)
    with pytest.raises(DeferredTaskError) as wait:
        service.fetch(FetchRequest("http://jobs.example.org/jobs/1"))
    assert wait.value.retry_at == failure.value.retry_at
    assert len(fixture.requests) == 2


@pytest.mark.parametrize(
    "rule,allowed",
    [
        ("Crawl-delay: inf", True),
        ("Request-rate: 1/7200", False),
        ("Request-rate: 1/5 0400-0600", False),
    ],
)
def test_malformed_and_unsupported_extensions(
    isolated_database: Database, rule: str, allowed: bool
) -> None:
    service, _, clock = crawler(
        isolated_database, http_response(f"User-agent: *\n{rule}".encode())
    )
    if allowed:
        ready(service, clock)
        assert service.fetch(PAGE).status == 200
    else:
        with pytest.raises(RobotsDenied):
            service.fetch(PAGE)
