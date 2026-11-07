import os
import socket
import sys
from collections.abc import Iterator
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from job_hunter.infrastructure.database import Database
from job_hunter.settings import Settings

DENIED_NETWORK: list[str] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-database", action="store_true", help="Fail without TEST_DATABASE_URL"
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("require_database") and not os.environ.get("TEST_DATABASE_URL"):
        raise pytest.UsageError("--require-database needs TEST_DATABASE_URL")
    if os.environ.get("TEST_DATABASE_URL") and config.getoption(
        "numprocesses", default=0
    ):
        raise pytest.UsageError(
            "Database tests must run sequentially; omit xdist workers"
        )

    database = make_url(os.environ.get("TEST_DATABASE_URL", "postgresql://localhost"))

    def allowed(host: object, port: object = None) -> bool:
        if host is None or host in ("localhost", b"localhost"):
            return True
        if isinstance(host, bytes):
            host = host.decode("ascii", errors="replace")
        if not isinstance(host, str):
            return False
        if host == database.host and port in (None, database.port or 5432):
            return True
        try:
            return ip_address(host).is_loopback
        except ValueError:
            return False

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event in {"socket.connect", "socket.sendto", "socket.sendmsg"}:
            if args[0].family == socket.AF_UNIX or args[1] is None:
                return
            permitted = allowed(*args[1][:2])
        elif event == "socket.getaddrinfo":
            permitted = allowed(*args[:2])
        elif event in {"socket.gethostbyname", "socket.gethostbyaddr"}:
            permitted = allowed(args[0])
        elif event == "socket.getnameinfo":
            permitted = allowed(*args[0][:2])
        else:
            return
        if not permitted:
            DENIED_NETWORK.append(event)
            raise RuntimeError("Unexpected external test network: " + event)

    sys.addaudithook(audit)


@pytest.fixture(autouse=True)
def no_external_network() -> Iterator[None]:
    yield
    denied, DENIED_NETWORK[:] = DENIED_NETWORK[:], []
    assert not denied, f"Unexpected external test network (even if caught): {denied}"


def pytest_sessionfinish(session: pytest.Session) -> None:
    if DENIED_NETWORK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.startswith("JOB_HUNTER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv(
        "JOB_HUNTER_TRUSTED_HOSTS", '["testserver", "localhost", "127.0.0.1"]'
    )
    monkeypatch.setenv("JOB_HUNTER_MAIL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JOB_HUNTER_MAIL_SENDER", "accounts@example.org")
    monkeypatch.setenv("JOB_HUNTER_MAIL_PUBLIC_URL", "https://app.example.org")
    monkeypatch.setenv("JOB_HUNTER_MAIL_SMTP_HOST", "smtp.example.org")


@pytest.fixture
def empty_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[Database]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("Set TEST_DATABASE_URL for real PostgreSQL; no SQLite fallback")
    Settings(database_url=SecretStr(admin_url))
    name = "test_" + uuid4().hex
    admin = create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
        connect_args={"connect_timeout": 2},
    )
    try:
        with admin.connect() as control:
            control.execute(text(f'CREATE DATABASE "{name}"'))
            try:
                url = (
                    make_url(admin_url)
                    .set(database=name)
                    .render_as_string(hide_password=False)
                )
                database = Database(
                    Settings(database_url=SecretStr(url), database_pool_size=1)
                )
                monkeypatch.setenv("JOB_HUNTER_DATABASE_URL", url)
                try:
                    yield database
                finally:
                    database.close()
            finally:
                control.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@pytest.fixture
def isolated_database(empty_database: Database) -> Database:
    config = Config(toml_file=Path(__file__).resolve().parents[1] / "pyproject.toml")
    command.upgrade(config, "head")
    return empty_database
