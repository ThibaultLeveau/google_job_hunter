from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import QueuePool
from test_health import MemoryMail
from test_worker import FakeClock, immediate

from job_hunter.app import create_app
from job_hunter.application.accounts import RegisterAccount
from job_hunter.application.recovery import PasswordRecovery
from job_hunter.application.sessions import Sessions, token_hash
from job_hunter.application.worker import RetryableTaskError, Worker
from job_hunter.domain.accounts import (
    InvalidRegistration,
    Registration,
    Unauthenticated,
)
from job_hunter.infrastructure.accounts import (
    Argon2Passwords,
    AuthIdentityRecord,
    PostgresAccounts,
    PostgresSessions,
    RecoveryDeliveryRecord,
    RecoveryRecord,
    load_mail_settings,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.recovery import PostgresRecovery
from job_hunter.infrastructure.tasks import TaskRecord
from job_hunter.infrastructure.worker import PostgresWorkerStore
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
NOW = datetime(2026, 1, 1, tzinfo=UTC)
PASSWORD = "private-original-password"
NEW_PASSWORD = "private-new-password"
EMAIL = "user@example.org"
ORIGIN = "https://testserver"


def account(database: Database) -> tuple[PostgresRecovery, Sessions]:
    RegisterAccount(PostgresAccounts(database), Argon2Passwords(), lambda: NOW).execute(
        Registration(EMAIL, PASSWORD)
    )
    return PostgresRecovery(database, load_mail_settings()), Sessions(
        PostgresSessions(database), Argon2Passwords().verify, lambda: NOW
    )


def raw_token(database: Database, store: PostgresRecovery) -> str:
    with database.transaction() as session:
        ciphertext = session.scalars(
            select(RecoveryDeliveryRecord.encrypted_token)
        ).one()
        token = store.cipher.decrypt(ciphertext).decode()
        assert session.scalar(select(RecoveryRecord.token_hash)) == token_hash(token)
        saved = str(
            session.execute(text("SELECT row_to_json(tasks) FROM tasks")).all()
        ) + str(ciphertext)
        assert token not in saved and EMAIL not in saved
        return token


def test_concurrent_resets_revoke_sessions_and_fence_old_login(
    isolated_database: Database,
) -> None:
    store, sessions = account(isolated_database)
    first, second = (
        sessions.login(EMAIL, PASSWORD, ""),
        sessions.login(EMAIL, PASSWORD, ""),
    )
    credential = PostgresSessions(isolated_database).credential(EMAIL)
    assert credential is not None
    store.request(EMAIL, NOW)
    token = raw_token(isolated_database, store)
    database = Database(Settings(database_pool_size=2))
    other = PostgresRecovery(database, load_mail_settings())
    barrier = Barrier(2)
    hashes: list[str] = []

    def reset(index: int) -> bool:
        encoded = Argon2Passwords().hash(NEW_PASSWORD + str(index))
        hashes.append(encoded)
        barrier.wait(timeout=5)
        return (store if index else other).reset(token_hash(token), encoded, NOW)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reset, range(2)))
        assert sum(outcomes) == 1
    finally:
        database.close()
    for previous in (first, second):
        with pytest.raises(Unauthenticated):
            sessions.current(previous)
    with pytest.raises(Unauthenticated):
        PostgresSessions(isolated_database).rotate(
            credential,
            token_hash(first),
            token_hash("new"),
            NOW,
            NOW + timedelta(hours=1),
        )
    assert not store.reset(token_hash(token), hashes[0], NOW)
    with pytest.raises(Unauthenticated):
        sessions.login(EMAIL, PASSWORD, "")
    assert sessions.login(EMAIL, NEW_PASSWORD + str(outcomes.index(True)), "")
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(RecoveryRecord)) == 0
        assert (
            session.scalar(select(func.count()).select_from(RecoveryDeliveryRecord))
            == 0
        )


def test_expiry_replacement_invalid_password_and_atomic_rollback(
    isolated_database: Database,
) -> None:
    store, sessions = account(isolated_database)
    session_token = sessions.login(EMAIL, PASSWORD, "")
    service = PasswordRecovery(store, Argon2Passwords(), lambda: NOW)
    service.request(EMAIL)
    original = raw_token(isolated_database, store)
    service.request(EMAIL.upper())
    token = raw_token(isolated_database, store)
    assert token != original
    assert not store.reset(token_hash(original), "unused", NOW)
    assert not store.reset(token_hash(token), "unused", NOW + timedelta(hours=1))
    with pytest.raises(InvalidRegistration):
        service.reset(token, "short")
    # A failed credential update must restore the token and sessions.
    with pytest.raises(IntegrityError):
        store.reset(token_hash(token), "invalid-hash", NOW)
    assert sessions.current(session_token)
    assert raw_token(isolated_database, store) == token

    class CheckedPasswords(Argon2Passwords):
        def hash(self, password: str) -> str:
            assert isinstance(isolated_database.engine.pool, QueuePool)
            assert isolated_database.engine.pool.checkedout() == 0
            return super().hash(password)

    PasswordRecovery(store, CheckedPasswords(), lambda: NOW).reset(token, NEW_PASSWORD)
    with pytest.raises(Unauthenticated):
        sessions.current(session_token)


def test_oauth_only_and_changed_credentials_never_gain_a_password(
    isolated_database: Database,
) -> None:
    store, _ = account(isolated_database)
    service = PasswordRecovery(store, Argon2Passwords(), lambda: NOW)
    service.request("unknown@example.org")
    service.request("not-an-email")
    service.request(EMAIL)
    token = raw_token(isolated_database, store)
    with isolated_database.transaction() as session:
        session.execute(
            update(AuthIdentityRecord).values(
                password_hash=Argon2Passwords().hash(NEW_PASSWORD)
            )
        )
    assert not store.reset(token_hash(token), "unused", NOW)
    mail = MemoryMail(isolated_database)
    handlers = store.handlers(mail, lambda: NOW)
    task = PostgresWorkerStore(isolated_database, handlers).claim(
        ("recovery_mail",), NOW, timedelta(seconds=5)
    )
    assert task is not None
    with patch.object(
        store.database, "transaction", side_effect=SQLAlchemyError("private-canary")
    ):
        with pytest.raises(RetryableTaskError):
            handlers[task.key.kind].execute(task)
    handlers[task.key.kind].execute(task)
    assert not mail.messages
    with isolated_database.transaction() as session:
        session.execute(
            update(AuthIdentityRecord).values(
                issuer="https://accounts.google.com",
                subject="google-subject",
                password_hash=None,
            )
        )
    service.request(EMAIL)
    assert not store.reset(token_hash(token), "unused", NOW)
    with isolated_database.transaction() as session:
        assert session.scalar(select(AuthIdentityRecord.password_hash)) is None
        assert session.scalar(select(func.count()).select_from(AuthIdentityRecord)) == 1
        session.execute(delete(RecoveryRecord))
    service.request(EMAIL)
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(RecoveryRecord)) == 0


@pytest.mark.parametrize("failures", [1, 3, "corrupt"])
def test_mail_retry_expiry_and_no_secret_logging(
    isolated_database: Database, failures: int | str, caplog: pytest.LogCaptureFixture
) -> None:
    store, _ = account(isolated_database)
    store.request(EMAIL, NOW)
    token = raw_token(isolated_database, store)
    mail = MemoryMail(isolated_database, failures if isinstance(failures, int) else 0)
    clock = FakeClock()
    if failures == "corrupt":
        with isolated_database.transaction() as session:
            session.execute(
                update(RecoveryDeliveryRecord).values(encrypted_token=b"private-canary")
            )
    handlers = store.handlers(mail, clock.now)
    worker_store = PostgresWorkerStore(isolated_database, handlers)
    worker = Worker(
        worker_store,
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
    if mail.messages:
        assert (
            mail.messages[0][1]
            == f"https://app.example.org/reset-password#token={token}"
        )
    with isolated_database.transaction() as session:
        assert session.scalar(
            select(func.count()).select_from(RecoveryDeliveryRecord)
        ) == (0 if failures == 1 else 1)
        assert session.scalar(
            select(TaskRecord.status).where(TaskRecord.kind == "recovery_mail")
        ) == ("succeeded" if failures == 1 else "failed")
    clock.current = NOW + timedelta(hours=1)
    worker.tick()
    worker.tick()
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(RecoveryRecord)) == 0
        assert (
            session.scalar(select(func.count()).select_from(RecoveryDeliveryRecord))
            == 0
        )
    assert token not in caplog.text and "private-canary" not in caplog.text


def test_recovery_api_is_generic_bounded_and_handles_revoked_cookies(
    isolated_database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    store, _ = account(isolated_database)
    with TestClient(
        create_app(Settings(allowed_origins=(ORIGIN,))), base_url=ORIGIN
    ) as client:
        headers = {"Origin": ORIGIN}
        assert (
            client.post("/api/auth/forgot-password", json={"email": EMAIL}).status_code
            == 403
        )
        for email in (EMAIL, "unknown@example.org"):
            result = client.post(
                "/api/auth/forgot-password", headers=headers, json={"email": email}
            )
            assert result.status_code == 202 and result.json() == {"status": "accepted"}
        token = raw_token(isolated_database, store)
        assert (
            client.post(
                "/api/auth/reset-password",
                headers=headers,
                json={"token": token, "password": "short"},
            ).status_code
            == 422
        )
        login = client.post(
            "/api/auth/login",
            headers=headers,
            json={"email": EMAIL, "password": PASSWORD},
        )
        assert (
            client.post(
                "/api/auth/reset-password",
                headers=headers,
                json={"token": token, "password": NEW_PASSWORD},
            ).status_code
            == 403
        )
        headers["X-CSRF-Token"] = login.json()["csrf_token"]
        for _ in range(2):
            result = client.post(
                "/api/auth/reset-password",
                headers=headers,
                json={"token": token, "password": NEW_PASSWORD},
            )
            assert result.status_code == 202 and result.json() == {"status": "accepted"}
        assert client.get("/api/auth/me").status_code == 401
        for _ in range(2):
            assert (
                client.post(
                    "/api/auth/forgot-password", headers=headers, json={"email": EMAIL}
                ).status_code
                == 202
            )
        limited = client.post(
            "/api/auth/forgot-password", headers=headers, json={"email": EMAIL}
        )
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 3500
        invalid = client.post(
            "/api/auth/reset-password",
            headers=headers,
            json={
                "token": "x" * 43,
                "password": NEW_PASSWORD,
                "redirect_url": "https://evil.example",
            },
        )
        assert invalid.status_code == 422 and "evil.example" not in invalid.text
        client.cookies.clear()
        assert (
            client.post(
                "/api/auth/login",
                headers={"Origin": ORIGIN},
                json={"email": EMAIL, "password": NEW_PASSWORD},
            ).status_code
            == 200
        )
        assert (
            token not in result.text + caplog.text
            and NEW_PASSWORD not in result.text + caplog.text
        )
