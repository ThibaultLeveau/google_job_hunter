import asyncio
import gzip
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import dns.exception
import dns.resolver
import httpcore
import pytest
from http_fixture import FixtureHTTP, FixtureStream, http_response
from pydantic import ValidationError

from job_hunter.application.fetching import (
    FetchError,
    FetchRequest,
    FetchResponse,
    PublicFetcher,
)
from job_hunter.infrastructure.fetching import (
    FetchSettings,
    HttpFetcher,
    PinnedBackend,
    load_fetch_settings,
    public_url,
    resolve_public,
)
from job_hunter.settings import ConfigurationError

URL = "https://jobs.example.org/job?a=1&a=2"


def service(fixture: FixtureHTTP, settings: FetchSettings | None = None) -> HttpFetcher:
    return HttpFetcher(settings or FetchSettings(), fixture.resolve, fixture)


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
    assert fixture.connections == [("93.184.216.34", 443, 5)]
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
    pinned = PinnedBackend("jobs.example.org", 443, "93.184.216.34", FixtureHTTP({}))
    with pytest.raises(FetchError, match="unsafe_address"):
        asyncio.run(pinned.connect_tcp("other.example.org", 443))
