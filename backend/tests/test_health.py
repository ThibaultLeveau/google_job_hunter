import asyncio
import json
import os
import smtplib
import socket
import ssl
import subprocess
import sys
import traceback
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from threading import Barrier, Event
from typing import Annotated, cast
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4
from zipfile import ZipFile

import httpx2 as httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InvalidRequestError,
    SQLAlchemyError,
)
from sqlalchemy.pool import QueuePool
from test_profiles import owners as profile_owners
from test_worker import FakeClock, immediate

from job_hunter.api.sessions import COOKIE, authenticated_user
from job_hunter.app import create_app
from job_hunter.application.accounts import Mail, RegisterAccount
from job_hunter.application.documents import DocumentUploads
from job_hunter.application.sessions import LIFETIME, Sessions, csrf_token, token_hash
from job_hunter.application.worker import RetryableTaskError, Worker
from job_hunter.domain.accounts import Registration, SessionUser, Unauthenticated
from job_hunter.domain.documents import TYPES, InvalidDocument, validate_content
from job_hunter.domain.errors import DomainError
from job_hunter.domain.tasks import StaleLease
from job_hunter.infrastructure.accounts import (
    Argon2Passwords,
    AuthIdentityRecord,
    MailDeliveryRecord,
    PostgresAccounts,
    PostgresSessions,
    PostgresVerification,
    SessionRecord,
    SmtpMail,
    UserRecord,
    VerificationRecord,
    load_mail_settings,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import (
    DocumentRecord,
    LocalStorage,
    PostgresDocuments,
    StorageSettings,
    load_storage_settings,
)
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.infrastructure.worker import PostgresWorkerStore
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
    assert list(first.openapi()["paths"]) == [
        "/api/v1/searches/preview",
        "/api/v1/searches/provider",
        "/api/v1/searches",
        "/api/v1/searches/runs",
        "/api/v1/searches/runs/{run_id}",
        "/api/v1/searches/runs/{run_id}/cancel",
        "/api/v1/searches/{search_id}",
        "/api/v1/searches/{search_id}/runs",
        "/api/v1/profile",
        "/api/v1/profile/facts/{fact_id}",
        "/api/v1/profile/facts",
        "/api/v1/profile/review",
        "/api/v1/documents",
        "/api/v1/documents/{document_id}",
        "/api/v1/documents/{document_id}/download",
        "/api/v1/documents/{document_id}/proposals",
        "/api/health/live",
        "/api/health/ready",
        "/api/auth/providers",
        "/api/auth/google/start",
        "/api/auth/google/callback",
        "/api/auth/register",
        "/api/auth/verify",
        "/api/auth/resend-verification",
        "/api/auth/forgot-password",
        "/api/auth/reset-password",
        "/api/auth/login",
        "/api/auth/me",
        "/api/auth/logout",
        "/api/auth/methods",
        "/api/auth/actions/{action}",
        "/api/auth/reauthenticate/password",
        "/api/auth/google/{intent}",
        "/api/auth/google/{intent}/callback",
    ]
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


NOW = datetime(2026, 1, 1, tzinfo=UTC)
ORIGIN = "https://testserver"
PASSWORD = "private-session-password"
OWN_RECORD, FOREIGN_RECORD = UUID(int=1), UUID(int=2)


@pytest.fixture
def client(isolated_database: Database) -> Iterator[TestClient]:
    PostgresAccounts(isolated_database).create(
        Registration("user@example.org", PASSWORD),
        Argon2Passwords().hash(PASSWORD),
        NOW,
    )
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    cast(Sessions, app.state.sessions).now = lambda: NOW
    with isolated_database.transaction() as session:
        owner = session.scalars(select(UserRecord.id)).one()
    records = {OWN_RECORD: owner, FOREIGN_RECORD: uuid4()}

    @app.post("/api/private/{record_id}")
    def owned(
        record_id: UUID, user: Annotated[SessionUser, Depends(authenticated_user)]
    ) -> dict[str, UUID]:
        record_owner = records.get(record_id)
        if record_owner is None:
            raise DomainError("not_found")
        user.require_owner(record_owner)
        return {"owner_id": user.user_id}

    with TestClient(app, base_url=ORIGIN) as browser:
        yield browser


def login(client: TestClient, **headers: str) -> tuple[str, str, UUID]:
    response = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN, **headers},
        json={"email": "USER@example.org", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return (
        client.cookies[COOKIE],
        response.json()["csrf_token"],
        UUID(response.json()["id"]),
    )


@pytest.mark.integration
def test_cookie_hash_storage_rotation_and_revocation(
    client: TestClient, isolated_database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    client.cookies.set(
        COOKIE, "chosen-fixed-value", domain="testserver.local", path="/api"
    )
    response = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={"email": "user@example.org", "password": PASSWORD},
    )
    assert response.status_code == 200
    flags = response.headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/api", "Max-Age=43200"):
        assert flag in flags
    assert "Domain=" not in flags
    token = client.cookies[COOKIE]
    assert len(token) == 43 and token != "chosen-fixed-value"
    csrf = response.json()["csrf_token"]
    assert token not in response.text + caplog.text
    with isolated_database.transaction() as session:
        stored = session.scalars(select(SessionRecord)).one()
        assert stored.token_hash == token_hash(token)
        assert stored.authenticated_at == stored.last_seen_at == NOW
        assert stored.expires_at == NOW + LIFETIME and stored.auth_method == "password"
        assert token not in str(
            session.execute(text("SELECT row_to_json(sessions) FROM sessions")).all()
        )
    new_token, new_csrf, _ = login(client, **{"X-CSRF-Token": csrf})
    assert new_token != token and new_csrf != csrf
    service = cast(Sessions, cast(FastAPI, client.app).state.sessions)
    with pytest.raises(Unauthenticated):
        service.current(token)
    assert client.get("/api/auth/me").json()["csrf_token"] == new_csrf
    assert client.get("/api/auth/logout").status_code == 405
    response = client.post(
        "/api/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": new_csrf}
    )
    assert response.status_code == 204 and "Max-Age=0" in response.headers["set-cookie"]
    assert COOKIE not in client.cookies
    with pytest.raises(Unauthenticated):
        service.current(new_token)
    assert client.get("/api/auth/me").json()["code"] == "unauthenticated"


@pytest.mark.integration
def test_expiry_last_seen_and_authentication_recency(
    client: TestClient, isolated_database: Database
) -> None:
    token, _, _ = login(client)
    service = cast(Sessions, cast(FastAPI, client.app).state.sessions)
    service.now = lambda: NOW + timedelta(minutes=10)
    assert client.get("/api/auth/me").status_code == 200
    with isolated_database.transaction() as session:
        stored = session.scalars(select(SessionRecord)).one()
        assert stored.last_seen_at == service.now()
        assert stored.authenticated_at == NOW and stored.expires_at == NOW + LIFETIME
    service.now = lambda: NOW + LIFETIME
    assert client.get("/api/auth/me").status_code == 401
    assert (
        client.post(
            "/api/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token(token)},
        ).status_code
        == 401
    )
    # Expired cookies cannot fix the identity of a new password login.
    login(client)


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("user@example.org", "incorrect-password"),
        ("missing@example.org", PASSWORD),
        ("missing@example.org", "dummy-verification-only-value"),
        ("not-an-email", PASSWORD),
        ("user@example.org", "short"),
    ],
)
@pytest.mark.integration
def test_invalid_credentials_have_one_safe_result(
    client: TestClient, email: str, password: str, caplog: pytest.LogCaptureFixture
) -> None:
    response = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={"email": email, "password": password},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
    assert response.json()["message"] == "Authentication is required."
    assert "set-cookie" not in response.headers
    assert password not in response.text + caplog.text


@pytest.mark.integration
def test_unknown_and_known_credentials_use_verification_outside_transaction(
    client: TestClient, isolated_database: Database
) -> None:
    database = Database(Settings(database_pool_size=1))
    calls: list[bool] = []

    def verify(encoded: str | None, password: str) -> bool:
        assert (
            isinstance(database.engine.pool, QueuePool)
            and database.engine.pool.checkedout() == 0
        )
        calls.append(encoded is not None)
        return Argon2Passwords().verify(encoded, password)

    service = Sessions(PostgresSessions(database), verify, lambda: NOW)
    try:
        for email in ("user@example.org", "missing@example.org"):
            with pytest.raises(Unauthenticated):
                service.login(email, "incorrect-password", "")
        assert calls == [True, False]
    finally:
        database.close()


@pytest.mark.parametrize(
    "headers", [{}, {"Origin": "https://foreign.example"}, {"Origin": "null"}]
)
@pytest.mark.integration
def test_login_csrf_is_rejected(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post(
        "/api/auth/login",
        headers=headers,
        json={"email": "user@example.org", "password": PASSWORD},
    )
    assert response.status_code == 403 and COOKIE not in client.cookies


@pytest.mark.integration
def test_csrf_is_bound_to_session_and_origin_for_all_authenticated_writes(
    client: TestClient,
) -> None:
    token, csrf, owner_id = login(client)
    for headers in (
        {},
        {"Origin": ORIGIN},
        {"Origin": ORIGIN, "X-CSRF-Token": "x" * 64},
        {"X-CSRF-Token": csrf},
        {"Origin": "https://foreign.example", "X-CSRF-Token": csrf},
    ):
        assert (
            client.post(f"/api/private/{OWN_RECORD}", headers=headers).status_code
            == 403
        )
    service = cast(Sessions, cast(FastAPI, client.app).state.sessions)
    other_token = service.login("user@example.org", PASSWORD, "")
    assert (
        client.post(
            f"/api/private/{OWN_RECORD}",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token(other_token)},
        ).status_code
        == 403
    )
    valid = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    assert client.post(f"/api/private/{OWN_RECORD}", headers=valid).json() == {
        "owner_id": str(owner_id)
    }
    assert client.post(f"/api/private/{uuid4()}", headers=valid).status_code == 404
    assert (
        client.post(f"/api/private/{FOREIGN_RECORD}", headers=valid).status_code == 404
    )
    assert (
        client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={"email": "user@example.org", "password": PASSWORD},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/auth/register",
            json={"email": "other@example.org", "password": PASSWORD},
        ).status_code
        == 403
    )
    assert service.current(token).user_id == owner_id


@pytest.mark.integration
def test_anonymous_routes_and_cookie_only_authentication(client: TestClient) -> None:
    assert client.get("/api/health/live").status_code == 200
    assert (
        client.post(
            "/api/auth/register",
            json={"email": "other@example.org", "password": PASSWORD},
        ).status_code
        == 202
    )
    token, _, _ = login(client)
    client.cookies.clear()
    assert (
        client.get(
            "/api/auth/me", headers={"Authorization": "Bearer " + token}
        ).status_code
        == 401
    )
    assert client.get("/api/auth/me", params={"token": token}).status_code == 401
    client.cookies.set(COOKIE, "malformed", domain="testserver.local", path="/api")
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/health/live").status_code == 200
    preflight = client.options(
        "/api/auth/logout",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, X-CSRF-Token",
        },
    )
    assert preflight.status_code == 204
    assert "x-csrf-token" in preflight.headers["access-control-allow-headers"]


@pytest.mark.integration
def test_credential_change_race_does_not_create_session(
    client: TestClient, isolated_database: Database
) -> None:
    store = PostgresSessions(isolated_database)
    credential = store.credential("user@example.org")
    assert credential is not None
    changed_hash = Argon2Passwords().hash("changed-password-value")
    with isolated_database.transaction() as session:
        session.execute(update(AuthIdentityRecord).values(password_hash=changed_hash))
    with pytest.raises(Unauthenticated):
        store.rotate(credential, "", token_hash("new"), NOW, NOW + LIFETIME)
    with isolated_database.transaction() as session:
        assert session.scalars(select(SessionRecord)).all() == []


@pytest.mark.integration
def test_session_constraints_and_missing_configuration(
    client: TestClient, isolated_database: Database
) -> None:
    login(client)
    for expression in (
        "token_hash='plaintext'",
        "expires_at=authenticated_at",
        "auth_method='untrusted'",
    ):
        with pytest.raises(IntegrityError):
            with isolated_database.transaction() as session:
                session.execute(text(f"UPDATE sessions SET {expression}"))
    with TestClient(
        create_app(Settings(database_url=None, allowed_origins=(ORIGIN,))),
        base_url=ORIGIN,
    ) as unavailable:
        assert unavailable.get("/api/auth/me").status_code == 503


@pytest.mark.integration
def test_login_requires_json_and_rejects_mass_assignment(client: TestClient) -> None:
    values = {"email": "user@example.org", "password": PASSWORD}
    response = client.post(
        "/api/auth/login", content=json.dumps(values), headers={"Origin": ORIGIN}
    )
    assert response.status_code == 422
    response = client.post(
        "/api/auth/login",
        content=json.dumps(values),
        headers={"Origin": ORIGIN, "Content-Type": "application/problem+json"},
    )
    assert response.status_code == 415
    response = client.post("/api/auth/login", data=values, headers={"Origin": ORIGIN})
    assert response.status_code == 422
    response = client.post(
        "/api/auth/login",
        json={**values, "user_id": str(uuid4())},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 422 and COOKIE not in client.cookies


@pytest.mark.integration
def test_failed_rotation_preserves_previous_session(
    client: TestClient, isolated_database: Database
) -> None:
    token, _, owner = login(client)
    store = PostgresSessions(isolated_database)
    credential = store.credential("user@example.org")
    assert credential is not None
    with pytest.raises(IntegrityError):
        store.rotate(
            credential, token_hash(token), token_hash(token), NOW, NOW + LIFETIME
        )
    assert store.current(token_hash(token), NOW).user_id == owner


class MemoryMail(Mail):
    def __init__(self, database: Database, failures: int = 0) -> None:
        self.database, self.failures = database, failures
        self.messages: list[tuple[str, str, UUID]] = []
        self.attempts = 0

    def send(self, recipient: str, link: str, message_id: UUID) -> None:
        assert isinstance(self.database.engine.pool, QueuePool)
        assert self.database.engine.pool.checkedout() == 0
        self.attempts += 1
        if self.failures:
            self.failures -= 1
            raise RetryableTaskError(f"private-mail-canary {link}")
        self.messages.append((recipient, link, message_id))


def queued_verification(database: Database) -> tuple[PostgresVerification, UUID, str]:
    accounts = PostgresAccounts(database)
    RegisterAccount(accounts, Argon2Passwords(), lambda: NOW).execute(
        Registration("User@example.org", PASSWORD)
    )
    with database.transaction() as session:
        record = session.scalars(select(VerificationRecord)).one()
        encrypted = session.scalars(select(MailDeliveryRecord.encrypted_token)).one()
        token = accounts.verification.cipher.decrypt(encrypted).decode()
        assert len(token) == 43 and record.token_hash == token_hash(token)
        persisted = str(
            session.execute(
                text(
                    "SELECT row_to_json(verification_deliveries) "
                    "FROM verification_deliveries"
                )
            ).all()
        ) + str(session.execute(text("SELECT row_to_json(tasks) FROM tasks")).all())
        assert token not in persisted and "User@example.org" not in persisted
        assert session.scalar(select(func.count()).select_from(TaskRecord)) == 2
        return accounts.verification, record.user_id, token


@pytest.mark.integration
def test_verification_is_owner_scoped_single_use_and_concurrent(
    isolated_database: Database,
) -> None:
    service, user_id, token = queued_verification(isolated_database)
    assert not service.consume(uuid4(), token_hash(token), NOW)
    database = Database(Settings(database_pool_size=2))
    other = PostgresVerification(database, load_mail_settings())
    barrier = Barrier(2)

    def consume(index: int) -> bool:
        barrier.wait(timeout=5)
        return (service if index else other).consume(user_id, token_hash(token), NOW)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(consume, range(2))) == 1
    finally:
        database.close()
    assert not service.consume(user_id, token_hash(token), NOW + timedelta(seconds=1))
    service.resend(user_id, NOW)
    service.resend(uuid4(), NOW)
    with isolated_database.transaction() as session:
        assert session.scalar(select(UserRecord.email_verified_at)) == NOW
        assert session.scalar(select(func.count()).select_from(VerificationRecord)) == 0
        assert session.scalar(select(func.count()).select_from(MailDeliveryRecord)) == 0


@pytest.mark.integration
@pytest.mark.parametrize("failures", [1, 3, "corrupt"])
def test_mail_retry_and_expiry_delete_secrets_even_after_failure(
    isolated_database: Database,
    failures: int | str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, user_id, token = queued_verification(isolated_database)
    mail = MemoryMail(isolated_database, failures if isinstance(failures, int) else 0)
    clock = FakeClock()
    if failures == "corrupt":
        with isolated_database.transaction() as session:
            session.execute(
                update(MailDeliveryRecord).values(
                    encrypted_token=b"private-ciphertext-canary"
                )
            )
    handlers = service.handlers(mail, clock.now)
    store = PostgresWorkerStore(isolated_database, handlers)
    worker = Worker(
        store,
        {k: h.execute for k, h in handlers.items()},
        clock,
        immediate,
        Event(),
        lambda: 0.5,
    )
    for _ in range(4):
        worker.tick()
        worker.tick()
        clock.sleep(2)
    assert mail.attempts == {1: 2, 3: 3, "corrupt": 0}[failures]
    with isolated_database.transaction() as session:
        assert session.scalar(
            select(TaskRecord.status).where(TaskRecord.kind == "verification_mail")
        ) == ("succeeded" if failures == 1 else "failed")
        assert session.scalar(select(func.count()).select_from(MailDeliveryRecord)) == (
            0 if failures == 1 else 1
        )
    if mail.messages:
        assert mail.messages[0][0] == "User@example.org"
        assert (
            mail.messages[0][1] == f"https://app.example.org/verify-email#token={token}"
        )
    clock.current = NOW + timedelta(hours=1)
    assert not service.consume(user_id, token_hash(token), clock.now())
    worker.tick()
    worker.tick()
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(VerificationRecord)) == 0
        assert session.scalar(select(func.count()).select_from(MailDeliveryRecord)) == 0
        assert session.scalar(select(UserRecord.email_verified_at)) is None
    assert token not in caplog.text and "private-mail-canary" not in caplog.text


@pytest.mark.integration
def test_mail_crash_recovery_fences_cleanup_and_reuses_link(
    isolated_database: Database,
) -> None:
    service, user_id, token = queued_verification(isolated_database)
    mail, clock = MemoryMail(isolated_database), FakeClock()
    handlers = service.handlers(mail, clock.now)
    store = PostgresWorkerStore(isolated_database, handlers)
    task = store.claim(("verification_mail",), NOW, timedelta(seconds=1))
    assert task is not None
    with patch.object(
        service.database,
        "transaction",
        side_effect=SQLAlchemyError("private-mail-canary"),
    ):
        with pytest.raises(RetryableTaskError):
            handlers[task.key.kind].execute(task)
    assert mail.attempts == 0
    handlers[task.key.kind].execute(task)
    clock.sleep(1)
    store.recover(("verification_mail",), clock.now())
    retried = store.claim(("verification_mail",), clock.now(), timedelta(seconds=1))
    assert retried is not None
    handlers[retried.key.kind].execute(retried)
    assert mail.messages[0] == mail.messages[1]
    with pytest.raises(StaleLease):
        store.publish(task, None, clock.now())
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(MailDeliveryRecord)) == 1
    store.publish(retried, None, clock.now())
    handlers[retried.key.kind].execute(retried)
    assert len(mail.messages) == 2
    assert service.consume(user_id, token_hash(token), clock.now())


@pytest.mark.integration
def test_mail_enqueue_failure_rolls_back_account(
    isolated_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = PostgresAccounts(isolated_database)

    def unavailable(*args: object) -> None:
        raise SQLAlchemyError("private-mail-canary")

    monkeypatch.setattr(PostgresTaskQueue, "enqueue", unavailable)
    with pytest.raises(RuntimeError, match="Account storage unavailable"):
        RegisterAccount(accounts, Argon2Passwords(), lambda: NOW).execute(
            Registration("user@example.org", PASSWORD)
        )
    with isolated_database.transaction() as session:
        for table in (
            UserRecord,
            AuthIdentityRecord,
            VerificationRecord,
            MailDeliveryRecord,
            TaskRecord,
        ):
            assert session.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.integration
def test_verify_resend_api_throttles_and_redacts(
    isolated_database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    origin = "https://testserver"
    cipher = PostgresAccounts(isolated_database).verification.cipher

    def current_token() -> str:
        with isolated_database.transaction() as session:
            return cipher.decrypt(
                session.scalars(select(MailDeliveryRecord.encrypted_token)).one()
            ).decode()

    with TestClient(
        create_app(Settings(allowed_origins=(origin,))), base_url=origin
    ) as client:
        assert client.post("/api/auth/resend-verification").status_code == 401
        assert (
            client.post("/api/auth/verify", json={"token": "x" * 43}).status_code == 401
        )
        values = {"email": "user@example.org", "password": PASSWORD}
        assert client.post("/api/auth/register", json=values).status_code == 202
        original = current_token()
        client.post("/api/auth/register", json=values)
        assert current_token() == original
        login = client.post("/api/auth/login", json=values, headers={"Origin": origin})
        headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
        assert (
            client.post("/api/auth/verify", json={"token": original}).status_code == 403
        )
        for _ in range(3):
            assert client.post(
                "/api/auth/resend-verification", headers=headers
            ).json() == {"status": "accepted"}
        limited = client.post("/api/auth/resend-verification", headers=headers)
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 3500
        for token in (original, "x" * 43, "\ud800" * 43):
            assert client.post(
                "/api/auth/verify",
                headers={**headers, "Content-Type": "application/json"},
                content=json.dumps({"token": token}),
            ).json() == {"status": "accepted"}
        assert client.get("/api/auth/me").json()["email_verified"] is False
        latest = current_token()
        for _ in range(2):
            verified = client.post(
                "/api/auth/verify", headers=headers, json={"token": latest}
            )
            assert verified.status_code == 202 and verified.json() == {
                "status": "accepted"
            }
            assert latest not in verified.text + caplog.text
        assert client.get("/api/auth/me").json()["email_verified"] is True
        invalid = client.post(
            "/api/auth/verify",
            headers=headers,
            json={"token": "private-token-canary", "user_id": str(uuid4())},
        )
        assert invalid.status_code == 422 and "private-token-canary" not in invalid.text


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KEY", "private-key-canary"),
        ("KEY", None),
        ("SENDER", "private-sender-canary\n"),
        ("PUBLIC_URL", "http://private-host-canary.org"),
        ("PUBLIC_URL", "https://app.example.org/path"),
        ("SMTP_USERNAME", "private-user-canary"),
        ("SMTP_PORT", "99999"),
    ],
)
def test_invalid_mail_configuration_is_safe(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(f"JOB_HUNTER_MAIL_{name}")
    else:
        monkeypatch.setenv(f"JOB_HUNTER_MAIL_{name}", value)
    with pytest.raises(ConfigurationError) as raised:
        load_mail_settings()
    assert "canary" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("subject", ["Verify your email", "Reset your password"])
def test_smtp_uses_tls_canonical_sender_and_safe_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
    subject: str,
) -> None:
    transport = MagicMock()
    monkeypatch.setattr(smtplib, "SMTP_SSL", transport)
    SmtpMail(load_mail_settings()).send(
        "user@example.org", "https://app.example.org", uuid4()
    )
    transport.return_value.__enter__.return_value.login.assert_not_called()
    transport.reset_mock()
    monkeypatch.setenv("JOB_HUNTER_MAIL_SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("JOB_HUNTER_MAIL_SMTP_PASSWORD", "private-smtp-canary")
    mail = SmtpMail(load_mail_settings(), subject)
    message_id, link = (
        uuid4(),
        "https://app.example.org/verify-email#token=private-token-canary",
    )
    mail.send("user@example.org", link, message_id)
    connection = transport.return_value.__enter__.return_value
    context = transport.call_args.kwargs["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert transport.call_args.kwargs["timeout"] == 5
    connection.login.assert_called_once_with("smtp-user", "private-smtp-canary")
    message = connection.send_message.call_args.args[0]
    assert (
        message["From"] == "accounts@example.org"
        and message["To"] == "user@example.org"
    )
    assert message["Subject"] == subject
    assert message["Message-ID"] == f"<{message_id}@app.example.org>"
    assert link in message.get_content() and message.get_content_type() == "text/plain"
    connection.send_message.side_effect = smtplib.SMTPException("private-token-canary")
    with pytest.raises(RetryableTaskError) as raised:
        mail.send("user@example.org", link, message_id)
    assert "private-token-canary" not in "".join(
        traceback.format_exception(raised.value)
    )


owners = profile_owners
DOCUMENTS = "/api/v1/documents"
DOCUMENT_HEADERS = {
    "Origin": ORIGIN,
    "X-CSRF-Token": csrf_token("a" * 43),
    "Content-Type": "text/plain",
    "X-Document-Name": "../../private%0D%0Aname.txt",
}


def post_document(client: TestClient, body: bytes, **headers: str) -> httpx.Response:
    return client.post(DOCUMENTS, content=body, headers={**DOCUMENT_HEADERS, **headers})


@pytest.fixture
def document_client(
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("JOB_HUNTER_DOCUMENT_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("JOB_HUNTER_DOCUMENT_MAX_BYTES", "1024")
    monkeypatch.setenv("JOB_HUNTER_DOCUMENT_QUOTA_COUNT", "2")
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    cast(Sessions, app.state.sessions).now = lambda: NOW
    with TestClient(app, base_url=ORIGIN) as browser:
        browser.cookies.set(COOKIE, "a" * 43)
        yield browser


@pytest.mark.parametrize(
    ("kind", "content", "valid"),
    [
        ("text/plain", "Développeur\nPython".encode(), True),
        ("text/plain", b"\xef\xbb\xbfhello", True),
        ("text/plain", b"", False),
        ("text/plain", b"a\x00b", False),
        ("text/plain", b"\xff", False),
        ("text/plain", b"\xc3", False),
        ("text/plain", b"%PDF-1.7", False),
        ("application/pdf", b"%PDF-1.7\nexample\n%%EOF\n", True),
        ("application/pdf", b"%PDF-1.7\ntruncated", False),
        ("application/pdf", b"plain text", False),
        (list(TYPES)[1], b"PK\x03\x04broken", False),
        (list(TYPES)[1], b"plain text", False),
    ],
)
def test_document_content_validation(kind: str, content: bytes, valid: bool) -> None:
    with nullcontext() if valid else pytest.raises(InvalidDocument):
        validate_content(BytesIO(content), kind, len(content))


def test_docx_container_and_entry_bounds() -> None:
    for names, valid in [
        (["missing"], False),
        (["[Content_Types].xml", "_rels/.rels", "word/document.xml"], True),
        ([str(index) for index in range(1001)], False),
    ]:
        file = BytesIO()
        with ZipFile(file, "w") as archive:
            for name in names:
                archive.writestr(name, "<root/>")
        with nullcontext() if valid else pytest.raises(InvalidDocument):
            validate_content(file, list(TYPES)[1], file.tell())


@pytest.mark.integration
def test_document_api_ownership_quota_tombstones_and_download(
    document_client: TestClient,
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = document_client
    assert client.post(DOCUMENTS, content=b"private-canary").status_code == 403
    created = post_document(client, b"private-canary")
    assert created.status_code == 201, created.text
    doc = created.json()
    assert doc["filename"] == "private__name.txt" and doc["status"] == "queued"
    assert doc["version"] == 1 and doc["deleted_at"] is None
    assert "storage" not in created.text and "content_hash" not in created.text
    path = DOCUMENTS + "/" + doc["id"]
    assert client.get(path).json() == doc
    assert client.get(DOCUMENTS).json() == [doc]
    assert client.get(DOCUMENTS + "?offset=1").json() == []
    download = client.get(path + "/download")
    assert download.content == b"private-canary"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "no-store"
    assert post_document(client, b"private-canary").status_code == 200
    assert post_document(client, b"second").status_code == 201
    assert post_document(client, b"third").status_code == 409
    client.cookies.set(COOKIE, "b" * 43)
    assert client.get(DOCUMENTS).json() == []
    assert client.get(path).status_code == 404
    assert client.get(path + "/download").status_code == 404
    foreign = {"X-CSRF-Token": csrf_token("b" * 43)}
    other = post_document(client, b"private-canary", **foreign)
    assert other.status_code == 201 and other.json()["id"] != doc["id"]
    with pytest.raises(IntegrityError), isolated_database.transaction() as session:
        session.execute(
            update(DocumentRecord)
            .where(DocumentRecord.owner_id == owners[1])
            .values(owner_id=owners[0])
        )
    with isolated_database.transaction() as session:
        session.execute(
            update(UserRecord)
            .where(UserRecord.id == owners[1])
            .values(email_verified_at=None)
        )
        session.execute(
            update(DocumentRecord)
            .where(DocumentRecord.id == UUID(doc["id"]))
            .values(deleted_at=NOW)
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(TaskRecord)
                .where(TaskRecord.kind == "document_parse")
            )
            == 3
        )
    assert post_document(client, b"other", **foreign).status_code == 403
    client.cookies.set(COOKIE, "a" * 43)
    assert client.get(path).status_code == 404
    assert client.get(path + "/download").status_code == 404
    assert post_document(client, b"private-canary").status_code == 409
    client.cookies.clear()
    assert client.get(DOCUMENTS).status_code == 401
    assert len(list((tmp_path / "private").iterdir())) == 3
    assert "private-canary" not in caplog.text


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["stage", "write", "publish", "enqueue"])
def test_document_failures_leave_no_records_tasks_or_files(
    document_client: TestClient,
    isolated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    from job_hunter.application.documents import PendingUpload

    target = {"enqueue": PostgresTaskQueue, "write": PendingUpload}.get(
        failure, LocalStorage
    )
    monkeypatch.setattr(
        target, failure, MagicMock(side_effect=OSError("private-failure-canary"))
    )
    response = post_document(document_client, b"private")
    assert response.status_code == 503 and "canary" not in response.text
    records = PostgresDocuments(isolated_database, StorageSettings(), lambda: NOW)
    LocalStorage(tmp_path / "private").reap(
        records, datetime.now(UTC) + timedelta(hours=2)
    )
    assert list((tmp_path / "private").iterdir()) == []
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(DocumentRecord)) == 0
        assert session.scalar(select(func.count()).select_from(TaskRecord)) == 0


@pytest.mark.integration
@pytest.mark.parametrize("case", ["oversized", "timeout", "valid"])
def test_document_body_streams_without_buffering_and_cleans_partial_files(
    document_client: TestClient,
    tmp_path: Path,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = asyncio.timeout
    if case == "timeout":
        monkeypatch.setattr(asyncio, "timeout", lambda seconds: timeout(0))

    async def exercise() -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            yield b"first"
            assert list((tmp_path / "private").glob("*.part"))
            yield b"x" * (1024 if case == "oversized" else 8)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=document_client.app), base_url=ORIGIN
        ) as client:
            return await client.post(
                DOCUMENTS,
                content=body(),
                headers={**DOCUMENT_HEADERS, "Cookie": COOKIE + "=" + "a" * 43},
            )

    response = asyncio.run(exercise())
    monkeypatch.setattr(asyncio, "timeout", timeout)
    assert (
        response.status_code == {"oversized": 413, "timeout": 408, "valid": 201}[case]
    )
    assert not list((tmp_path / "private").glob("*.part"))
    if case != "valid":
        assert not list((tmp_path / "private").iterdir())
    assert post_document(document_client, b"a" * 1025).status_code == 413
    for extra in [
        {"Content-Type": "image/png"},
        {"Content-Type": "application/pdf"},
        {"X-Document-Name": "%ff"},
    ]:
        assert post_document(document_client, b"text", **extra).status_code == 415


@pytest.mark.integration
@pytest.mark.parametrize("same", [True, False])
def test_concurrent_uploads_deduplicate_and_enforce_quota(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    same: bool,
) -> None:
    database = Database(Settings(database_pool_size=2))
    settings = StorageSettings(quota_bytes=4)
    records = PostgresDocuments(database, settings, lambda: NOW)
    storage = LocalStorage(tmp_path / "private")
    service = DocumentUploads(records, storage, 1024)
    barrier = Barrier(2)

    def upload(index: int) -> tuple[str, bool]:
        with service.receive("resume", "text/plain") as pending:
            pending.write(b"same" if same else str(index).encode() * 4)
            barrier.wait(timeout=5)
            try:
                document, created = service.finish(owners[0], pending)
                return str(document.id), created
            except DomainError:
                return "conflict", False

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(upload, [0, 1]))
        assert sum(created for _, created in results) == 1
        assert (results[0][0] == results[1][0]) is same
        if not same:
            assert any(key == "conflict" for key, _ in results)
        with database.transaction() as session:
            assert session.scalar(select(func.count()).select_from(DocumentRecord)) == 1
            task = session.scalars(select(TaskRecord)).one()
            assert task.kind == "document_parse" and task.status == "pending"
            assert task.payload_ref == task.subject_id
            assert (
                PostgresTaskQueue(session).claim(
                    ("verification_mail",), NOW, timedelta(seconds=30)
                )
                is None
            )
        assert len(list(storage.root.iterdir())) == 1
    finally:
        database.close()


@pytest.mark.integration
def test_orphan_sweep_preserves_active_committed_and_uncertain_writes(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path / "private")
    records = PostgresDocuments(isolated_database, StorageSettings(), lambda: NOW)
    service = DocumentUploads(records, storage, 1024)
    with service.receive("resume", "text/plain") as pending:
        pending.write(b"resume")
        doc, _ = service.finish(owners[0], pending)
    with service.receive("active", "text/plain") as pending:
        storage.publish(pending.key, pending.file)
        os.utime(storage.path(pending.key), (0, 0))
        key, file = storage.stage()
        file.close()  # Simulate process death before publishing a reference.
        os.utime(storage.path(key, ".part"), (0, 0))
        (storage.root / "unrelated").touch()
        storage.reap(records, datetime.now(UTC) + timedelta(hours=2))
        assert storage.path(pending.key).exists()
        assert not storage.path(key, ".part").exists()
        assert records.get(owners[0], doc.id)[0] == doc
    with monkeypatch.context() as patcher:
        add = records.add

        def uncertain(*args: object) -> None:
            add(*args)  # type: ignore[arg-type]
            raise OSError("Lost commit acknowledgement")

        patcher.setattr(records, "add", uncertain)
        with (
            pytest.raises(OSError),
            service.receive("uncertain", "text/plain") as pending,
        ):
            pending.write(b"uncertain")
            service.finish(owners[0], pending)
    storage.reap(records, datetime.now(UTC) + timedelta(hours=2))
    assert len(list(storage.root.glob("*.blob"))) == 2
    assert (storage.root / "unrelated").exists()
    with pytest.raises(ValueError):
        storage.open("../escape")
    with monkeypatch.context() as patcher:
        patcher.setenv("JOB_HUNTER_DOCUMENT_ROOT", str(storage.root))
        subprocess.run(
            [sys.executable, "-m", "job_hunter.infrastructure.documents"],
            check=True,
            capture_output=True,
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ROOT", "relative-private-canary"),
        ("MAX_BYTES", "0"),
        ("QUOTA_COUNT", "1001"),
        ("QUOTA_BYTES", "bad"),
    ],
)
def test_storage_configuration_rejects_invalid_limits_safely(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv("JOB_HUNTER_DOCUMENT_" + name, value)
    with pytest.raises(ConfigurationError) as error:
        load_storage_settings()
    assert "canary" not in str(error.value)
