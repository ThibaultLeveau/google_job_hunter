import os
import socket
import subprocess
import sys
import traceback
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InvalidRequestError

from job_hunter.app import create_app
from job_hunter.infrastructure.database import Database
from job_hunter.settings import ConfigurationError, Settings, load_settings


@pytest.fixture
def fake_settings() -> Settings:
    return Settings(docs_enabled=False)


def test_liveness_is_fixed_and_credential_free(fake_settings: Settings) -> None:
    with TestClient(create_app(fake_settings)) as client:
        response = client.get("/api/health/live")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_default_startup_requires_no_credentials() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/health/live").json() == {"status": "ok"}
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_app_creation_is_deterministic_and_isolated(fake_settings: Settings) -> None:
    first = create_app(fake_settings)
    second = create_app(fake_settings)

    assert first is not second
    assert first.openapi() == second.openapi()
    assert list(first.openapi()["paths"]) == ["/api/health/live", "/api/health/ready"]
    first.dependency_overrides[load_settings] = lambda: Settings(docs_enabled=True)
    assert second.dependency_overrides == {}


def test_explicit_settings_do_not_read_environment(
    fake_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOB_HUNTER_DOCS_ENABLED", "invalid-ambient-value")
    with TestClient(create_app(fake_settings)) as client:
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/docs").status_code == 404


@pytest.mark.parametrize("value", ["true", "1", "false", "0"])
def test_environment_controls_docs(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_HUNTER_DOCS_ENABLED", value)
    with TestClient(create_app()) as client:
        expected_status = 200 if value in {"true", "1"} else 404
        assert client.get("/docs").status_code == expected_status
        assert client.get("/openapi.json").status_code == expected_status
        assert client.get("/api/health/live").json() == {"status": "ok"}


def test_settings_are_loaded_per_app(monkeypatch: pytest.MonkeyPatch) -> None:
    first = create_app()
    monkeypatch.setenv("JOB_HUNTER_DOCS_ENABLED", "true")
    second = create_app()

    with TestClient(first) as client:
        assert client.get("/docs").status_code == 404
    with TestClient(second) as client:
        assert client.get("/docs").status_code == 200


def test_settings_are_immutable(fake_settings: Settings) -> None:
    with pytest.raises(ValidationError, match="Instance is frozen"):
        fake_settings.docs_enabled = True


@pytest.mark.parametrize("value", ["", "maybe", "private-credential-canary"])
def test_invalid_settings_fail_with_redacted_errors(
    value: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("JOB_HUNTER_DOCS_ENABLED", value)
    with pytest.raises(
        ConfigurationError, match="Invalid application settings"
    ) as raised:
        create_app()

    rendered = "".join(traceback.format_exception(raised.value))
    assert "docs_enabled" in str(raised.value)
    assert "JOB_HUNTER_" in str(raised.value)
    assert "input_value" not in rendered
    if value:
        assert value not in rendered + caplog.text


def test_server_startup_rejects_bad_settings_without_disclosing_input() -> None:
    canary = "private-startup-credential-canary"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "job_hunter.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ],
        env={**os.environ, "JOB_HUNTER_DOCS_ENABLED": canary},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "Invalid application settings: docs_enabled" in result.stderr
    assert canary not in result.stdout + result.stderr


def test_readiness_is_unavailable_when_database_is_unconfigured() -> None:
    with TestClient(create_app(Settings())) as client:
        assert client.get("/api/health/ready").status_code == 503
        assert client.get("/api/health/live").status_code == 200
    with pytest.raises(ConfigurationError, match="DATABASE_URL is required"):
        Database(Settings())


def test_database_down_is_redacted_and_liveness_remains_available(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        url = f"postgresql+psycopg://user:private-canary@127.0.0.1:{port.getsockname()[1]}/db"
        settings = Settings(database_url=SecretStr(url))
        with TestClient(create_app(settings)) as client:
            response = client.get("/api/health/ready")
            assert response.status_code == 503
            assert response.json() == {"status": "unavailable"}
            assert client.get("/api/health/live").status_code == 200
        assert "private-canary" not in response.text + caplog.text + repr(settings)


@pytest.mark.parametrize(
    "url",
    [
        "invalid-canary",
        "sqlite:///private-canary",
        "postgresql+psycopg:///db",
        "postgresql+psycopg://host",
        "postgresql+psycopg://a,b/db",
        "postgresql+psycopg://host:invalid/db",
        "postgresql+psycopg://host/db?options=canary",
    ],
)
def test_invalid_database_settings_are_redacted(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOB_HUNTER_DATABASE_URL", url)
    with pytest.raises(ConfigurationError) as raised:
        load_settings()
    rendered = "".join(traceback.format_exception(raised.value))
    assert "database_url" in rendered
    assert url not in rendered


@pytest.mark.parametrize("pool_size", [0, 21])
def test_database_pool_bounds(pool_size: int) -> None:
    with pytest.raises(ValidationError):
        Settings(database_pool_size=pool_size)
    assert Settings(database_url=None).database_url is None


def migration_config(output: StringIO | None = None) -> Config:
    return Config(
        toml_file=Path(__file__).resolve().parents[1] / "pyproject.toml",
        output_buffer=output,
    )


def test_initial_migration_has_one_head_and_offline_sql() -> None:
    output = StringIO()
    config = migration_config(output)
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1
    command.upgrade(config, "head", sql=True)
    assert "CREATE EXTENSION vector" in output.getvalue()
    command.downgrade(config, "head:base", sql=True)
    assert "DROP EXTENSION vector" in output.getvalue()


@pytest.mark.integration
def test_fresh_migration_and_readiness(empty_database: Database) -> None:
    config = migration_config()
    command.upgrade(config, "head")
    with empty_database.transaction() as session:
        assert session.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname='vector'")
        )
        assert (
            session.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(config).get_current_head()
        )
    with TestClient(create_app()) as client:
        response = client.get("/api/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    command.downgrade(config, "base")
    with empty_database.transaction() as session:
        assert (
            session.scalar(
                text("SELECT count(*) FROM pg_extension WHERE extname='vector'")
            )
            == 0
        )
    command.upgrade(config, "head")


@pytest.mark.integration
def test_transaction_rollback_and_session_cleanup(isolated_database: Database) -> None:
    with isolated_database.transaction() as setup:
        setup.execute(text("CREATE TABLE probe (id integer PRIMARY KEY)"))
    with pytest.raises(DBAPIError):
        with isolated_database.transaction() as failed:
            failed.execute(text("INSERT INTO probe VALUES (1)"))
            failed.execute(text("INSERT INTO probe VALUES (1)"))
    with isolated_database.transaction() as saved:
        assert saved.scalar(text("SELECT count(*) FROM probe")) == 0
        saved.execute(text("INSERT INTO probe VALUES (2)"))
    with isolated_database.transaction() as checked:
        assert checked.scalar(text("SELECT id FROM probe")) == 2
    assert len({id(setup), id(failed), id(saved), id(checked)}) == 4
    for closed in (setup, failed, saved, checked):
        with pytest.raises(InvalidRequestError):
            with closed.begin():
                closed.execute(text("SELECT 1"))


@pytest.mark.integration
def test_query_and_pool_wait_are_bounded(isolated_database: Database) -> None:
    with isolated_database.engine.connect() as held:
        assert held.scalar(text("SHOW statement_timeout")) == "2s"
        assert held.scalar(text("SHOW lock_timeout")) == "2s"
        assert held.scalar(text("SHOW timezone")) == "UTC"
        assert isolated_database.is_ready() is False
    with pytest.raises(DBAPIError, match="statement timeout"):
        with isolated_database.transaction() as session:
            session.execute(text("SELECT pg_sleep(10)"))
    assert isolated_database.is_ready() is True
