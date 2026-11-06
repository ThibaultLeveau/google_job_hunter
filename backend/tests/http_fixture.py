import ssl
from collections.abc import Iterable
from dataclasses import dataclass, field

import httpcore


def http_response(
    body: bytes = b"public job",
    status: int = 200,
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> list[bytes]:
    head = [
        f"HTTP/1.1 {status} Fixture".encode(),
        b"Content-Length: " + str(len(body)).encode(),
        b"Connection: close",
    ]
    head.extend(key + b": " + value for key, value in headers)
    return [b"\r\n".join(head) + b"\r\n\r\n", body]


@dataclass
class FixtureHTTP(httpcore.AsyncNetworkBackend):
    responses: dict[str, list[bytes]]
    addresses: tuple[str, ...] = ("93.184.216.34",)
    connections: list[tuple[str, int, float | None]] = field(default_factory=list)
    requests: list[bytes] = field(default_factory=list)
    dns: list[str] = field(default_factory=list)
    tls: list[str | None] = field(default_factory=list)
    streams: list[FixtureStream] = field(default_factory=list)

    async def resolve(self, host: str, timeout: float) -> tuple[str, ...]:
        self.dns.append(host)
        return self.addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connections.append((host, port, timeout))
        stream = FixtureStream(self)
        self.streams.append(stream)
        return stream


class FixtureStream(httpcore.AsyncMockStream):
    def __init__(self, fixture: FixtureHTTP) -> None:
        super().__init__([])
        self.fixture, self.sent, self.secure, self.closed = fixture, b"", False, False

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.sent += buffer
        if not buffer or not self.sent.endswith(b"\r\n\r\n"):
            return
        self.fixture.requests.append(self.sent)
        lines = self.sent.split(b"\r\n")
        target = lines[0].split(b" ")[1].decode()
        host = next(
            line[6:].decode() for line in lines if line.lower().startswith(b"host: ")
        )
        url = ("https" if self.secure else "http") + "://" + host + target
        if url not in self.fixture.responses:
            raise AssertionError("Unconfigured fixture URL")
        self._buffer = list(self.fixture.responses[url])

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        assert (
            ssl_context.check_hostname and ssl_context.verify_mode == ssl.CERT_REQUIRED
        )
        self.secure = True
        self.fixture.tls.append(server_hostname)
        return self

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()
