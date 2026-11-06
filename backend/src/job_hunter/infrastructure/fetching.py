import asyncio
import ipaddress
import logging
import re
import ssl
import threading
import time
import unicodedata
import zlib
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import ceil
from typing import Literal
from urllib.parse import urljoin

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpcore
import httpx
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from job_hunter.application.fetching import FetchError, FetchRequest, FetchResponse
from job_hunter.settings import ConfigurationError

FETCH_HTTP = ContextVar("private_fetch_http", default=False)
Resolver = Callable[[str, float], Awaitable[tuple[str, ...]]]


class FetchLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not FETCH_HTTP.get()


for name in ("httpcore.connection", "httpcore.http11", "httpcore.http2"):
    logging.getLogger(name).addFilter(FetchLogs())


class FetchSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOB_HUNTER_FETCH_", frozen=True, hide_input_in_errors=True
    )
    allowed_ports: tuple[Literal[80, 443], ...] = Field(
        default=(80, 443), min_length=1, max_length=2
    )
    max_redirects: int = Field(default=5, ge=0, le=10)
    connect_seconds: float = Field(default=5, ge=0.01, le=30)
    read_seconds: float = Field(default=5, ge=0.01, le=30)
    total_seconds: float = Field(default=20, ge=0.01, le=60)
    max_wire_bytes: int = Field(default=2097152, ge=1, le=8388608)
    max_body_bytes: int = Field(default=4194304, ge=1, le=16777216)
    max_connections: int = Field(default=4, ge=1, le=16)


def load_fetch_settings() -> FetchSettings:
    try:
        return FetchSettings()
    except ValidationError, SettingsError:
        raise ConfigurationError("Invalid JOB_HUNTER_FETCH_ settings.") from None


def public_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise FetchError("unsafe_address") from None
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or str(address) == "168.63.129.16"  # Azure's host-node WireServer address.
        or "%" in value
        or isinstance(address, ipaddress.IPv6Address)
        and (
            address.ipv4_mapped
            or address.sixtofour
            or address.teredo
            or address not in ipaddress.IPv6Network("2000::/3")
        )
    ):
        raise FetchError("unsafe_address")
    return str(address)


def public_url(value: str, settings: FetchSettings) -> httpx.URL:
    try:
        if (
            not 1 <= len(value.encode()) <= 2048
            or any(c.isspace() or unicodedata.category(c)[0] == "C" for c in value)
            or any(c in value for c in '\\<>"{}|^`')
            or re.search(r"%(?![0-9a-fA-F]{2})", value)
        ):
            raise ValueError
        url = httpx.URL(value)
        port = (
            url.port
            if url.port is not None
            else {"http": 80, "https": 443}.get(url.scheme)
        )
        if (
            not url.is_absolute_url
            or url.scheme not in {"http", "https"}
            or url.userinfo
            or "@" in value.split("/", 3)[2]
            or not url.host
            or "%" in url.host
            or port not in settings.allowed_ports
            or port != {"http": 80, "https": 443}[url.scheme]
            or value.split("/", 3)[2].endswith(":")
        ):
            raise ValueError
        try:
            ipaddress.ip_address(url.host)
        except ValueError:
            if "." not in url.host or re.fullmatch(
                r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*",
                url.host.rstrip("."),
            ):
                raise ValueError from None
        else:
            public_address(url.host)
        return url.copy_with(fragment=None)
    except ValueError, UnicodeError, httpx.InvalidURL:
        raise FetchError("unsafe_url") from None


async def resolve_public(host: str, timeout: float) -> tuple[str, ...]:
    try:
        answers = await dns.asyncresolver.resolve_name(
            host.rstrip(".") + ".", lifetime=timeout, search=False
        )
        return tuple(answers.addresses())
    except dns.resolver.NXDOMAIN, dns.resolver.NoAnswer:
        raise FetchError("dns") from None
    except dns.exception.Timeout:
        raise FetchError("timeout", transient=True) from None
    except dns.exception.DNSException:
        raise FetchError("dns", transient=True) from None


class PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self, host: str, port: int, address: str, backend: httpcore.AsyncNetworkBackend
    ) -> None:
        self.host, self.port, self.address, self.backend = host, port, address, backend

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if (host, port) != (self.host, self.port):
            raise FetchError("unsafe_address")
        return await self.backend.connect_tcp(
            self.address, port, timeout, local_address, socket_options
        )


class HttpFetcher:
    def __init__(
        self,
        settings: FetchSettings,
        resolver: Resolver = resolve_public,
        backend: httpcore.AsyncNetworkBackend | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings, self.resolver = settings, resolver
        self.backend = backend or httpcore.AnyIOBackend()
        self.slots = threading.BoundedSemaphore(settings.max_connections)
        self.now = now

    def retry_after(self, value: bytes) -> int | None:
        if value.isdigit() and len(value) <= 8:
            return min(int(value), 3600)
        try:
            parsed = parsedate_to_datetime(value.decode("ascii"))
            return max(0, min(3600, ceil((parsed - self.now()).total_seconds())))
        except ValueError, TypeError, OverflowError, UnicodeError:
            return None

    async def destination(self, url: httpx.URL) -> str:
        host = url.raw_host.decode("ascii")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            answers = await self.resolver(host, self.settings.connect_seconds)
        else:
            answers = (host,)
        if not 1 <= len(answers) <= 64:
            raise FetchError("dns")
        addresses = tuple(public_address(answer) for answer in answers)
        return addresses[0]

    def fetch(self, request: FetchRequest) -> FetchResponse:
        deadline = time.monotonic() + self.settings.total_seconds
        if not self.slots.acquire(timeout=self.settings.total_seconds):
            raise FetchError("busy", transient=True)
        token = FETCH_HTTP.set(True)
        try:
            return asyncio.run(self.hop(request, deadline))
        except TimeoutError, httpcore.TimeoutException:
            raise FetchError("timeout", transient=True) from None
        except httpcore.NetworkError as error:
            if isinstance(error.__cause__ or error.__context__, ssl.SSLError):
                raise FetchError("tls") from None
            raise FetchError("network", transient=True) from None
        except httpcore.ProtocolError, zlib.error, UnicodeError:
            raise FetchError("invalid_response") from None
        finally:
            FETCH_HTTP.reset(token)
            self.slots.release()

    async def hop(self, request: FetchRequest, deadline: float) -> FetchResponse:
        config = self.settings
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            url = public_url(request.url, config)
            if len(request.visited) > config.max_redirects:
                raise FetchError("redirect_limit")
            visited = tuple(str(public_url(item, config)) for item in request.visited)
            if str(url) in visited:
                raise FetchError("redirect_loop")
            address = await self.destination(url)
            backend = PinnedBackend(
                url.raw_host.decode("ascii"),
                url.port or (443 if url.scheme == "https" else 80),
                address,
                self.backend,
            )
            async with httpcore.AsyncConnectionPool(
                network_backend=backend,
                max_connections=1,
                max_keepalive_connections=0,
                retries=0,
                http2=False,
            ) as pool:
                async with pool.stream(
                    "GET",
                    str(url),
                    headers={
                        "Host": url.netloc.decode("ascii"),
                        "User-Agent": "JobHunter/0.1",
                        "Accept-Encoding": "gzip",
                        "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
                    },
                    extensions={
                        "timeout": {
                            "connect": config.connect_seconds,
                            "read": config.read_seconds,
                            "write": config.connect_seconds,
                            "pool": config.connect_seconds,
                        }
                    },
                ) as response:
                    headers: dict[bytes, bytes] = {}
                    if sum(len(k) + len(v) for k, v in response.headers) > 16384:
                        raise FetchError("too_large")
                    for key, value in response.headers:
                        key = key.lower()
                        if key in headers and key in {
                            b"location",
                            b"content-encoding",
                            b"content-type",
                        }:
                            raise FetchError("invalid_response")
                        headers[key] = value
                    if response.status in {301, 302, 303, 307, 308}:
                        if len(visited) >= config.max_redirects:
                            raise FetchError("redirect_limit")
                        location = headers.get(b"location", b"").decode("ascii")
                        if not location or any(c.isspace() for c in location):
                            raise FetchError("invalid_response")
                        target = public_url(urljoin(str(url), location), config)
                        history = (*visited, str(url))
                        if str(target) in history:
                            raise FetchError("redirect_loop")
                        await self.destination(target)
                        return FetchResponse(
                            str(url),
                            response.status,
                            None,
                            b"",
                            0,
                            FetchRequest(str(target), history),
                        )
                    if not 200 <= response.status < 300:
                        raise FetchError(
                            "http_status",
                            response.status >= 500
                            or response.status in {408, 425, 429},
                            response.status,
                            self.retry_after(headers.get(b"retry-after", b"")),
                        )
                    encoding = headers.get(b"content-encoding", b"identity").lower()
                    if encoding not in {b"identity", b"gzip"}:
                        raise FetchError("encoding")
                    decoder = zlib.decompressobj(31) if encoding == b"gzip" else None
                    body, wire_bytes = bytearray(), 0
                    async for chunk in response.aiter_stream():
                        await asyncio.sleep(0)
                        wire_bytes += len(chunk)
                        if wire_bytes > config.max_wire_bytes:
                            raise FetchError("too_large")
                        data = (
                            decoder.decompress(
                                chunk, config.max_body_bytes - len(body) + 1
                            )
                            if decoder
                            else chunk
                        )
                        if len(body) + len(data) > config.max_body_bytes:
                            raise FetchError("too_large")
                        body.extend(data)
                    if decoder and (not decoder.eof or decoder.unused_data):
                        raise FetchError("invalid_response")
                    content_type = headers.get(b"content-type", b"").decode("ascii")
                    if len(content_type) > 256 or any(
                        ord(c) < 32 or ord(c) == 127 for c in content_type
                    ):
                        raise FetchError("invalid_response")
                    return FetchResponse(
                        str(url),
                        response.status,
                        content_type or None,
                        bytes(body),
                        wire_bytes,
                    )
