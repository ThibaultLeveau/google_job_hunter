import asyncio
import gzip
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from threading import Barrier
from unittest.mock import AsyncMock
from uuid import uuid4

import dns.exception
import dns.resolver
import httpcore
import pytest
from alembic import command
from alembic.config import Config
from http_fixture import FixtureHTTP, FixtureStream, http_response
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_worker import NOW, FakeClock, enqueue, make_worker, read

from job_hunter.application.fetching import (
    FetchError,
    FetchRequest,
    FetchResponse,
    PublicFetcher,
)
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
from job_hunter.infrastructure.fetching import (
    FetchSettings,
    HttpFetcher,
    PinnedBackend,
    load_fetch_settings,
    public_url,
    resolve_public,
)
from job_hunter.infrastructure.tasks import PostgresTaskQueue
from job_hunter.settings import ConfigurationError, Settings

URL = "https://jobs.example.org/job?a=1&a=2"


def service(fixture: FixtureHTTP, settings: FetchSettings | None = None) -> HttpFetcher:
    return HttpFetcher(settings or FetchSettings(), fixture.resolve, fixture)


@pytest.mark.parametrize("failure", [httpcore.ConnectError, httpcore.ConnectTimeout])
def test_unreachable_first_address_falls_back_without_resolving_again(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    addresses = ("2606:4700:4700::1111", "93.184.216.34")
    fixture = FixtureHTTP({URL: http_response()}, addresses)
    original = fixture.connect_tcp
    attempted: list[str] = []

    async def connect(
        host: str, port: int, timeout: float | None = None, *args: object
    ) -> httpcore.AsyncNetworkStream:
        attempted.append(host)
        if host == addresses[0]:
            fixture.addresses = ("127.0.0.1",)
            raise failure("unreachable")
        return await original(host, port, timeout)

    monkeypatch.setattr(fixture, "connect_tcp", connect)
    assert service(fixture).fetch(FetchRequest(URL)).body == b"public job"
    assert attempted == list(addresses) and fixture.dns == ["jobs.example.org"]
    assert fixture.tls == ["jobs.example.org"] and len(fixture.requests) == 1


def test_address_fallback_shares_connect_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FixtureHTTP({})
    elapsed = [10.0]
    timeouts: list[float] = []
    monkeypatch.setattr(
        "job_hunter.infrastructure.fetching.time.monotonic", lambda: elapsed[0]
    )

    async def connect(
        host: str, port: int, timeout: float | None = None, *args: object
    ) -> httpcore.AsyncNetworkStream:
        assert timeout is not None
        timeouts.append(timeout)
        elapsed[0] += timeout
        raise httpcore.ConnectTimeout()

    monkeypatch.setattr(fixture, "connect_tcp", connect)
    backend = PinnedBackend(
        "jobs.example.org", 443, ("2606:4700:4700::1111", "93.184.216.34"), fixture
    )
    with pytest.raises(httpcore.ConnectTimeout):
        asyncio.run(backend.connect_tcp("jobs.example.org", 443, 5))
    assert timeouts == [2.5, 2.5] and elapsed[0] == 15


def test_http_failure_never_replays_on_another_address() -> None:
    fixture = FixtureHTTP(
        {URL: http_response(status=503)}, ("2606:4700:4700::1111", "93.184.216.34")
    )
    with pytest.raises(FetchError, match="http_status"):
        service(fixture).fetch(FetchRequest(URL))
    assert len(fixture.connections) == len(fixture.requests) == 1


@pytest.mark.parametrize(
    "host",
    """
    127.0.0.1 0.0.0.0 10.1.2.3 172.16.0.1 192.168.1.1 168.63.129.16
    169.254.169.254 100.100.100.200 100.64.0.1 192.0.2.1 198.18.0.1
    224.0.0.1 240.0.0.1 255.255.255.255 [::1] [::] [fc00::1] [fe80::1]
    [ff02::1] [2001:db8::1] [::ffff:127.0.0.1] [::ffff:8.8.8.8]
    [64:ff9b::a00:1] [2002:7f00:1::] [2001:0000:4136:e378:8000:63bf:3fff:fdd2]
    [fe80::1%25eth0] 2130706433 127.1 0177.0.0.1 0x7f000001
    """.split(),
)
def test_unsafe_addresses_never_connect(host: str) -> None:
    fixture = FixtureHTTP({})
    with pytest.raises(FetchError):
        service(fixture).fetch(FetchRequest(f"http://{host}/"))
    assert not fixture.connections and not fixture.dns


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://jobs.example.org/",
        "/relative",
        "http://localhost/",
        "https://user:secret@jobs.example.org/",
        "https://@jobs.example.org/",
        "https://jobs.example.org:8443/",
        "https://jobs.example.org:80/",
        "https://jobs.example.org:0/",
        "https://jobs.example.org:/",
        "https://jobs.example.org:443@127.0.0.1/",
        "http://127%2e0%2e0%2e1/",
        "https://jobs.example.org\\@127.0.0.1/",
        "\nhttps://jobs.example.org/",
        "https://jobs.example.org/\x00",
        "https://jobs.example.org/%xx",
        "https://jobs.example.org/" + "x" * 2048,
    ],
)
def test_url_tricks_are_rejected(url: str) -> None:
    fixture = FixtureHTTP({})
    with pytest.raises(FetchError, match="unsafe"):
        service(fixture).fetch(FetchRequest(url))
    assert not fixture.connections


@pytest.mark.parametrize(
    "addresses,code",
    [
        (("93.184.216.34", "10.0.0.1"), "unsafe_address"),
        (("93.184.216.34", "168.63.129.16"), "unsafe_address"),
        (("93.184.216.34", "::ffff:127.0.0.1"), "unsafe_address"),
        (("not-an-address",), "unsafe_address"),
        ((), "dns"),
        (("93.184.216.34",) * 65, "dns"),
    ],
)
def test_every_dns_answer_is_checked(addresses: tuple[str, ...], code: str) -> None:
    fixture = FixtureHTTP({}, addresses)
    with pytest.raises(FetchError, match=code):
        service(fixture).fetch(FetchRequest(URL))
    assert not fixture.connections


def test_pinned_address_preserves_tls_host_and_ignores_ambient_proxies(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://secret:password@127.0.0.1:8888")
    fixture = FixtureHTTP({URL: http_response(headers=((b"Set-Cookie", b"secret=1"),))})

    async def rebind(host: str, timeout: float) -> tuple[str, ...]:
        result = await fixture.resolve(host, timeout)
        fixture.addresses = ("127.0.0.1",)
        return result

    fetcher: PublicFetcher = HttpFetcher(FetchSettings(), rebind, fixture)
    caplog.set_level(logging.DEBUG, logger="httpcore")
    result = fetcher.fetch(FetchRequest(URL + "#fragment"))
    assert (
        result.url == URL and result.body == b"public job" and result.wire_bytes == 10
    )
    assert result.content_type is None and result.redirect is None
    assert len(fixture.connections) == 1
    host, port, timeout = fixture.connections[0]
    assert (host, port) == ("93.184.216.34", 443)
    assert timeout is not None and 4.9 < timeout <= 5
    assert fixture.tls == ["jobs.example.org"] and len(fixture.dns) == 1
    request = fixture.requests[0].lower()
    assert b"host: jobs.example.org\r\n" in request
    assert b"get /job?a=1&a=2 http/1.1" in request
    assert b"cookie:" not in request and b"authorization:" not in request
    assert "secret" not in caplog.text and "jobs.example.org" not in caplog.text
    with pytest.raises(FetchError, match="unsafe_address"):
        fetcher.fetch(FetchRequest(URL))
    assert len(fixture.connections) == 1 and fixture.streams[0].closed
    assert str(public_url("https://cafe.de/", FetchSettings())) == "https://cafe.de/"
    fixture = FixtureHTTP({"https://xn--bcher-kva.example/": http_response()})
    assert service(fixture).fetch(FetchRequest("https://bücher.example/")).status == 200
    assert fixture.dns == ["xn--bcher-kva.example"]
    assert fixture.tls == ["xn--bcher-kva.example"]


def test_public_literals_do_not_resolve_and_port_configuration_is_enforced() -> None:
    for url in ("http://8.8.8.8/", "https://[2606:4700:4700::1111]/"):
        fixture = FixtureHTTP({url: http_response()})
        assert service(fixture).fetch(FetchRequest(url)).status == 200
        assert not fixture.dns
    with pytest.raises(FetchError, match="unsafe_url"):
        public_url("http://jobs.example.org/", FetchSettings(allowed_ports=(443,)))


def test_redirects_are_validated_one_hop_at_a_time_and_rebound_targets_fail() -> None:
    target = "https://next.example.org/job"
    fixture = FixtureHTTP(
        {
            URL: http_response(status=302, headers=((b"Location", target.encode()),)),
            target: http_response(),
        }
    )
    fetcher = service(fixture)
    first = fetcher.fetch(FetchRequest(URL))
    assert first.redirect == FetchRequest(target, (URL,)) and len(fixture.requests) == 1
    assert fixture.dns == ["jobs.example.org", "next.example.org"]
    fixture.addresses = ("169.254.169.254",)
    with pytest.raises(FetchError, match="unsafe_address"):
        fetcher.fetch(first.redirect)
    assert len(fixture.requests) == 1
    fixture.addresses = ("93.184.216.34",)
    assert fetcher.fetch(first.redirect).body == b"public job"


@pytest.mark.parametrize(
    "location,code",
    [
        (b"http://127.0.0.1/", "unsafe_address"),
        (b"http://169.254.169.254/", "unsafe_address"),
        (b"http://user:pass@jobs.example.org/", "unsafe_url"),
        (b"#same", "redirect_loop"),
        (b"", "invalid_response"),
    ],
)
def test_bad_redirects_never_trigger_another_connection(
    location: bytes, code: str
) -> None:
    fixture = FixtureHTTP(
        {URL: http_response(status=301, headers=((b"Location", location),))}
    )
    with pytest.raises(FetchError, match=code):
        service(fixture).fetch(FetchRequest(URL))
    assert len(fixture.connections) == 1 and fixture.streams[0].closed


def test_relative_redirects_loops_and_hop_limit() -> None:
    other = "https://jobs.example.org/other"
    fixture = FixtureHTTP(
        {
            URL: http_response(status=307, headers=((b"Location", b"/other"),)),
            other: http_response(status=308, headers=((b"Location", URL.encode()),)),
        }
    )
    first = service(fixture).fetch(FetchRequest(URL))
    assert first.redirect == FetchRequest(other, (URL,))
    with pytest.raises(FetchError, match="redirect_loop"):
        service(fixture).fetch(first.redirect)
    with pytest.raises(FetchError, match="redirect_limit"):
        service(fixture, FetchSettings(max_redirects=0)).fetch(FetchRequest(URL))
    with pytest.raises(FetchError, match="redirect_limit"):
        service(fixture, FetchSettings(max_redirects=0)).fetch(
            FetchRequest(URL, (other,))
        )
    with pytest.raises(FetchError, match="redirect_loop"):
        service(fixture).fetch(FetchRequest(URL, (URL,)))


@pytest.mark.parametrize(
    "status,transient", [(403, False), (404, False), (429, True), (503, True)]
)
def test_http_failure_metadata_is_safe_and_typed(status: int, transient: bool) -> None:
    fixture = FixtureHTTP(
        {URL: http_response(b"private detail", status, ((b"Retry-After", b"99999"),))}
    )
    with pytest.raises(FetchError) as result:
        service(fixture).fetch(FetchRequest(URL))
    assert (result.value.status, result.value.transient, result.value.retry_after) == (
        status,
        transient,
        3600,
    )
    assert str(result.value) == "http_status" and "detail" not in repr(result.value)


@pytest.mark.parametrize(
    "kind",
    [
        "wire",
        "body",
        "gzip",
        "truncated",
        "concat",
        "unsupported",
        "malformed",
        "headers",
    ],
)
def test_streamed_byte_limits_and_compression_fail_closed(kind: str) -> None:
    body = (
        gzip.compress(b"x" * 200)
        if kind in {"gzip", "truncated", "concat"}
        else b"x" * 10
    )
    if kind == "truncated":
        body = body[:-4]
    if kind == "concat":
        body += gzip.compress(b"second")
    encoding = (
        b"gzip"
        if kind in {"gzip", "truncated", "concat", "malformed"}
        else b"br"
        if kind == "unsupported"
        else b"identity"
    )
    headers = ((b"Content-Encoding", encoding),) + (
        ((b"X-Large", b"x" * 17000),) if kind == "headers" else ()
    )
    fixture = FixtureHTTP({URL: http_response(body, headers=headers)})
    config = FetchSettings(
        max_wire_bytes=5 if kind == "wire" else 500,
        max_body_bytes=5 if kind == "body" else 100 if kind == "gzip" else 500,
    )
    with pytest.raises(FetchError) as result:
        service(fixture, config).fetch(FetchRequest(URL))
    assert result.value.code == (
        "too_large"
        if kind in {"wire", "body", "gzip", "headers"}
        else "encoding"
        if kind == "unsupported"
        else "invalid_response"
    )
    assert fixture.streams[0].closed


def test_valid_gzip_chunks_and_redacted_representation() -> None:
    raw = "Développeur Python".encode()
    packed = gzip.compress(raw)
    chunks = http_response(
        packed,
        headers=(
            (b"Content-Encoding", b"gzip"),
            (b"Content-Type", b"text/html; charset=utf-8"),
        ),
    )
    chunks[1:] = [packed[:5], packed[5:10], packed[10:]]
    fixture = FixtureHTTP({URL: chunks})
    result = service(
        fixture, FetchSettings(max_body_bytes=len(raw), max_wire_bytes=len(packed))
    ).fetch(FetchRequest(URL))
    assert result.body == raw and result.wire_bytes == len(packed)
    assert result.content_type == "text/html; charset=utf-8" and "Python" not in repr(
        result
    )


@pytest.mark.parametrize("operation", ["dns", "connect", "read"])
def test_total_deadline_cancels_slow_operations(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FixtureHTTP({URL: http_response()})

    async def slow(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    if operation == "dns":
        monkeypatch.setattr(fixture, "resolve", slow)
    elif operation == "connect":
        monkeypatch.setattr(fixture, "connect_tcp", slow)
    else:
        monkeypatch.setattr(FixtureStream, "read", slow)
    with pytest.raises(FetchError, match="timeout") as error:
        service(fixture, FetchSettings(total_seconds=0.01)).fetch(FetchRequest(URL))
    assert error.value.transient
    assert all(stream.closed for stream in fixture.streams)


@pytest.mark.parametrize(
    "error,code",
    [
        (httpcore.ReadTimeout(), "timeout"),
        (httpcore.ConnectError(), "network"),
        (httpcore.RemoteProtocolError(), "invalid_response"),
    ],
)
def test_transport_errors_are_redacted(
    error: Exception, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FixtureHTTP({})
    monkeypatch.setattr(fixture, "connect_tcp", AsyncMock(side_effect=error))
    with pytest.raises(FetchError, match=code):
        service(fixture).fetch(FetchRequest(URL))


def test_dns_default_queries_both_families_without_search_and_classifies_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = dns.resolver.HostAnswers()
    resolve = AsyncMock(return_value=answers)
    monkeypatch.setattr(dns.asyncresolver, "resolve_name", resolve)
    assert asyncio.run(resolve_public("example.org", 2)) == ()
    resolve.assert_awaited_once_with("example.org.", lifetime=2, search=False)
    for error, transient in [
        (dns.resolver.NXDOMAIN, False),
        (dns.resolver.NoAnswer, False),
        (dns.exception.Timeout, True),
        (dns.resolver.NoNameservers, True),
    ]:
        resolve.side_effect = error
        with pytest.raises(FetchError) as caught:
            asyncio.run(resolve_public("example.org", 2))
        assert caught.value.transient is transient


def test_socket_slots_are_shared_by_sync_workers_and_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FixtureHTTP({URL: http_response()})
    fetcher = service(fixture, FetchSettings(max_connections=1, total_seconds=0.02))
    entered, release = threading.Event(), threading.Event()
    original = fetcher.hop

    async def gated(request: FetchRequest, deadline: float) -> FetchResponse:
        entered.set()
        assert release.wait(2)
        return await original(request, deadline + 2)

    monkeypatch.setattr(fetcher, "hop", gated)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(fetcher.fetch, FetchRequest(URL))
        assert entered.wait(2)
        try:
            with pytest.raises(FetchError, match="busy"):
                fetcher.fetch(FetchRequest(URL))
        finally:
            release.set()
        assert first.result().status == 200
    monkeypatch.setattr(fetcher, "hop", original)
    fetcher.settings = FetchSettings(max_connections=1, total_seconds=2)
    assert fetcher.fetch(FetchRequest(URL)).status == 200


def test_settings_and_pinned_backend_reject_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError):
        FetchSettings(allowed_ports=(8080,))  # type: ignore[arg-type]
    monkeypatch.setenv("JOB_HUNTER_FETCH_ALLOWED_PORTS", "secret-invalid")
    with pytest.raises(ConfigurationError) as error:
        load_fetch_settings()
    assert "secret-invalid" not in str(error.value)
    monkeypatch.delenv("JOB_HUNTER_FETCH_ALLOWED_PORTS")
    assert load_fetch_settings().max_redirects == 5
    pinned = PinnedBackend("jobs.example.org", 443, ("93.184.216.34",), FixtureHTTP({}))
    with pytest.raises(FetchError, match="unsafe_address"):
        asyncio.run(pinned.connect_tcp("other.example.org", 443))


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
@pytest.mark.integration
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


@pytest.mark.integration
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
    clock.sleep(86400)
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
@pytest.mark.integration
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
@pytest.mark.integration
def test_unsupported_robots_delay_denies(
    isolated_database: Database, delay: str
) -> None:
    service, _, _ = crawler(
        isolated_database,
        http_response(f"User-agent: *\nCrawl-delay: {delay}".encode()),
    )
    with pytest.raises(RobotsDenied):
        service.fetch(PAGE)


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
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
@pytest.mark.integration
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


@pytest.mark.integration
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
@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
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
@pytest.mark.integration
def test_retry_after_formats(
    isolated_database: Database, value: bytes, seconds: int | None
) -> None:
    service, _, _ = crawler(isolated_database)
    assert service.http.retry_after(value) == seconds


@pytest.mark.integration
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
@pytest.mark.integration
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


@pytest.mark.integration
def test_long_crawl_delay_does_not_refresh_robots_forever(
    isolated_database: Database,
) -> None:
    service, fixture, clock = crawler(
        isolated_database, http_response(b"User-agent: *\nCrawl-delay: 3600")
    )
    ready(service, clock)
    assert clock.now() == NOW + timedelta(hours=1)
    assert service.fetch(PAGE).body == b"public job"
    assert len(fixture.requests) == 2
