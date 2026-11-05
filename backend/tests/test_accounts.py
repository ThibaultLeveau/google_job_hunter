import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import HashingError
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import QueuePool

from job_hunter.app import RegistrationRequest, create_app
from job_hunter.application.accounts import RegisterAccount
from job_hunter.domain.accounts import InvalidRegistration, Registration
from job_hunter.infrastructure.accounts import (
    Argon2Passwords,
    AuthIdentityRecord,
    PostgresAccounts,
    UserRecord,
)
from job_hunter.infrastructure.database import Database
from job_hunter.settings import Settings

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PASSWORD = "private-password-canary"


@pytest.mark.parametrize(
    ("email", "normalized"),
    [
        ("Person.Name+Jobs@EXAMPLE.ORG", "person.name+jobs@example.org"),
        ("PersonName@example.org", "personname@example.org"),
        ("Person.Name@example.org", "person.name@example.org"),
        ("ÉLISE@EXAMPLE.ORG", "élise@example.org"),
        ("User@xn--bcher-kva.de", "user@bücher.de"),
    ],
)
def test_email_normalization_preserves_submitted_form(
    email: str, normalized: str
) -> None:
    registration = Registration(email, PASSWORD)
    assert registration.email == email and registration.normalized_email == normalized
    assert PASSWORD not in repr(registration) and email not in repr(registration)
    assert PASSWORD not in repr(
        RegistrationRequest(email=email, password=SecretStr(PASSWORD))
    )


@pytest.mark.parametrize(
    ("email", "password", "field"),
    [
        ("private-email-canary", PASSWORD, "email"),
        ("a" * 321, PASSWORD, "email"),
        ("user@example.org", "a" * 14, "password"),
        ("user@example.org", "a" * 129, "password"),
        ("user@example.org", "private-canary\ud800", "password"),
    ],
)
def test_invalid_registration_is_redacted(
    email: str, password: str, field: str
) -> None:
    with pytest.raises(InvalidRegistration) as raised:
        Registration(email, password)
    assert raised.value.field_name == field
    assert "canary" not in "".join(traceback.format_exception(raised.value))


def test_argon2id_verification_salts_and_password_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passwords = Argon2Passwords()
    for password in ("a" * 15, "🙂" * 128, " spaces are retained "):
        encoded = passwords.hash(password)
        assert password not in encoded and passwords.verify(encoded, password)
        assert not passwords.verify(encoded, "another valid password")
        parameters = extract_parameters(encoded)
        assert parameters.type is Type.ID
        assert (
            parameters.memory_cost,
            parameters.time_cost,
            parameters.parallelism,
        ) == (65536, 3, 4)
    assert passwords.hash(PASSWORD) != passwords.hash(PASSWORD)
    assert not passwords.verify("private-hash-canary", PASSWORD)
    assert not passwords.verify("private-hash-canary", "short")
    with pytest.raises(InvalidRegistration):
        passwords.hash("short")

    def unavailable(_self: PasswordHasher, _: str) -> str:
        raise HashingError("private-hash-canary")

    monkeypatch.setattr(PasswordHasher, "hash", unavailable)
    with pytest.raises(RuntimeError, match="hashing unavailable") as raised:
        passwords.hash(PASSWORD)
    assert "private-hash-canary" not in "".join(
        traceback.format_exception(raised.value)
    )


@pytest.mark.integration
def test_registration_api_is_safe_unverified_and_preserves_existing_credentials(
    isolated_database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with TestClient(create_app()) as client:
        for email, password in (
            ("User@EXAMPLE.ORG", PASSWORD),
            ("USER@example.org", "replacement-password"),
        ):
            response = client.post(
                "/api/auth/register", json={"email": email, "password": password}
            )
            assert response.status_code == 202 and response.json() == {
                "status": "accepted"
            }
            assert "set-cookie" not in response.headers
    with isolated_database.transaction() as session:
        user = session.scalars(select(UserRecord)).one()
        identity = session.scalars(select(AuthIdentityRecord)).one()
        assert (
            user.email == "User@EXAMPLE.ORG"
            and user.email_normalized == "user@example.org"
        )
        assert (
            user.email_verified_at is None
            and user.created_at.utcoffset() == timedelta(0)
        )
        assert identity.user_id == user.id and identity.subject == str(user.id)
        assert identity.issuer == "password" and identity.password_hash is not None
        assert Argon2Passwords().verify(identity.password_hash, PASSWORD)
        assert not Argon2Passwords().verify(
            identity.password_hash, "replacement-password"
        )
        persisted = str(
            session.execute(
                text("SELECT row_to_json(auth_identities) FROM auth_identities")
            ).all()
        )
        assert PASSWORD not in persisted
        assert identity.password_hash not in caplog.text + response.text
    assert PASSWORD not in caplog.text + response.text


@pytest.mark.parametrize(
    "values",
    [
        {"email": "private-email-canary", "password": PASSWORD},
        {"email": "user@example.org", "password": "short"},
        {"email": "user@example.org", "password": "a" * 129},
        {"email": "user@example.org", "password": "private-canary\ud800"},
        {"email": "user@example.org", "password": 123},
        {"email": "user@example.org", "password": PASSWORD, "owner_id": str(uuid4())},
    ],
)
def test_api_validation_without_private_input(
    values: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/auth/register",
            content=json.dumps(values),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert response.json()["field_errors"]
    assert "canary" not in response.text + caplog.text


def test_registration_schema_and_unconfigured_database() -> None:
    app = create_app()
    schema = app.openapi()
    assert "password_hash" not in str(schema)
    request_schema = schema["components"]["schemas"]["RegistrationRequest"][
        "properties"
    ]
    assert request_schema["password"]["writeOnly"] is True
    assert request_schema["password"]["maxLength"] == 128
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/auth/register",
                json={"email": "user@example.org", "password": PASSWORD},
            ).status_code
            == 503
        )


@pytest.mark.integration
def test_concurrent_case_variants_create_one_account_without_hashing_in_transaction(
    isolated_database: Database,
) -> None:
    database = Database(Settings(database_pool_size=2))
    barrier = Barrier(2)

    class ConcurrentPasswords(Argon2Passwords):
        def hash(self, password: str) -> str:
            assert isinstance(database.engine.pool, QueuePool)
            assert database.engine.pool.checkedout() == 0
            result = super().hash(password)
            barrier.wait(timeout=5)
            return result

    service = RegisterAccount(
        PostgresAccounts(database), ConcurrentPasswords(), lambda: NOW
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(service.execute, Registration(email, PASSWORD))
                for email in ("User@Example.org", "USER@example.org")
            ]
            assert [future.result(timeout=10) for future in futures] == [None, None]
        with database.transaction() as session:
            assert session.scalar(select(func.count()).select_from(UserRecord)) == 1
            assert (
                session.scalar(select(func.count()).select_from(AuthIdentityRecord))
                == 1
            )
    finally:
        database.close()


@pytest.mark.integration
def test_distinct_aliases_and_international_case_uniqueness(
    isolated_database: Database,
) -> None:
    service = RegisterAccount(
        PostgresAccounts(isolated_database), Argon2Passwords(), lambda: NOW
    )
    for email in (
        "person@gmail.com",
        "per.son@gmail.com",
        "person+jobs@gmail.com",
        "person+other@gmail.com",
        "ÉLISE@example.org",
        "élise@example.org",
    ):
        service.execute(Registration(email, PASSWORD))
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(UserRecord)) == 5


@pytest.mark.integration
def test_identity_constraints_and_atomic_account_rollback(
    isolated_database: Database,
) -> None:
    accounts = PostgresAccounts(isolated_database)
    canary = "private-hash-canary"
    with pytest.raises(RuntimeError, match="Account storage unavailable") as raised:
        accounts.create(Registration("user@example.org", PASSWORD), canary, NOW)
    assert "private-hash-canary" not in "".join(
        traceback.format_exception(raised.value)
    )
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(UserRecord)) == 0
    RegisterAccount(accounts, Argon2Passwords(), lambda: NOW).execute(
        Registration("user@example.org", PASSWORD)
    )
    with isolated_database.transaction() as session:
        user_id = session.scalars(select(UserRecord.id)).one()
        session.add(
            AuthIdentityRecord(
                id=uuid4(),
                user_id=user_id,
                issuer="https://issuer.example",
                subject="subject",
                created_at=NOW,
            )
        )
    for issuer, subject, owner, password_hash in (
        ("https://issuer.example", "subject", user_id, None),
        ("https://issuer.example", "other", uuid4(), None),
        ("", "other", user_id, None),
        ("password", "other", user_id, None),
        ("https://issuer.example", "other", user_id, "private-hash-canary"),
    ):
        with pytest.raises(IntegrityError):
            with isolated_database.transaction() as session:
                session.add(
                    AuthIdentityRecord(
                        id=uuid4(),
                        user_id=owner,
                        issuer=issuer,
                        subject=subject,
                        password_hash=password_hash,
                        created_at=NOW,
                    )
                )
    with pytest.raises(IntegrityError):
        with isolated_database.transaction() as session:
            session.add(
                UserRecord(
                    id=uuid4(),
                    email="user@example.org",
                    email_normalized="user@example.org",
                    created_at=NOW,
                )
            )
    with pytest.raises(IntegrityError):
        with isolated_database.transaction() as session:
            session.execute(
                text("UPDATE users SET email_normalized='UPPER@example.org'")
            )
