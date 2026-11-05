import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from job_hunter.infrastructure.database import Database
from job_hunter.settings import Settings


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


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.startswith("JOB_HUNTER_"):
            monkeypatch.delenv(name)


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
