import asyncio
import ssl
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpcore
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from http_fixture import FixtureHTTP, http_response

from job_hunter.application.fetching import FetchError, FetchRequest
from job_hunter.infrastructure.fetching import FetchSettings, HttpFetcher


def test_real_tls_verifies_original_hostname_through_pinned_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "jobs.example.org")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("jobs.example.org")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    requests: list[bytes] = []

    async def respond(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"".join(http_response()))
            await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def exercise() -> None:
        async with await asyncio.start_server(
            respond, "127.0.0.1", 0, ssl=server_context
        ) as server:
            port = server.sockets[0].getsockname()[1]
            backend = httpcore.AnyIOBackend()
            connect = backend.connect_tcp

            async def local(
                host: str,
                remote_port: int,
                timeout: float | None = None,
                local_address: str | None = None,
                socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
            ) -> httpcore.AsyncNetworkStream:
                assert host == "93.184.216.34" and remote_port == 443
                return await connect("127.0.0.1", port, timeout)

            monkeypatch.setattr(backend, "connect_tcp", local)
            fetcher = HttpFetcher(FetchSettings(), FixtureHTTP({}).resolve, backend)
            result = await asyncio.to_thread(
                fetcher.fetch, FetchRequest("https://jobs.example.org/")
            )
            assert result.body == b"public job"
            with pytest.raises(FetchError, match="tls") as error:
                await asyncio.to_thread(
                    fetcher.fetch, FetchRequest("https://wrong.example.org/")
                )
            assert not error.value.transient

    asyncio.run(exercise())
    assert len(requests) == 1 and b"Host: jobs.example.org\r\n" in requests[0]
