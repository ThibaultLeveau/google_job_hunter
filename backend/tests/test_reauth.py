from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import cast

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.pool import QueuePool
from test_health import migration_config
from test_oidc import CONFIG, NOW, OidcTransport
from test_oidc import service as google_service
from test_worker import FakeClock

from job_hunter.api.sessions import COOKIE
from job_hunter.app import create_app
from job_hunter.application.oidc import ISSUER, UNKNOWN_AUTH_TIME, GoogleIdentity
from job_hunter.application.reauth import Reauthentication, ReauthenticationRequired
from job_hunter.application.sessions import Sessions, csrf_token, token_hash
from job_hunter.domain.accounts import Registration, Unauthenticated
from job_hunter.domain.errors import DomainError
from job_hunter.infrastructure.accounts import (
    Argon2Passwords,
    AuthIdentityRecord,
    PostgresAccounts,
    PostgresSessions,
    SessionRecord,
    UserRecord,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.reauth import PostgresProofs, recency_seconds
from job_hunter.settings import ConfigurationError, Settings

PASSWORD = "private-proof-password"
ORIGIN = "https://testserver"


def account(
    database: Database, clock: FakeClock, email: str = "owner@example.org"
) -> tuple[Sessions, str]:
    passwords = Argon2Passwords()
    PostgresAccounts(database).create(
        Registration(email, PASSWORD), passwords.hash(PASSWORD), clock.now()
    )
    sessions = Sessions(PostgresSessions(database), passwords.verify, clock.now)
    return sessions, sessions.login(email, PASSWORD, "")


def proofs(
    database: Database, clock: FakeClock, oidc: OidcTransport
) -> Reauthentication:
    google = google_service(database, oidc)
    google.now = clock.now
    return Reauthentication(
        PostgresProofs(database), google, Argon2Passwords().verify, clock.now, 300
    )


@pytest.mark.integration
def test_password_recency_is_not_activity_and_credentials_are_fenced(
    isolated_database: Database,
) -> None:
    clock, oidc = FakeClock(), OidcTransport()
    sessions, token = account(isolated_database, clock)
    proof = proofs(isolated_database, clock, oidc)
    proof.require(sessions.current(token), "delete_account")
    with pytest.raises(Unauthenticated):
        proof.start(sessions.current(token), token, "reauthenticate", "settings")
    clock.sleep(300)
    user = sessions.current(token)
    assert user.authenticated_at == NOW
    with pytest.raises(ReauthenticationRequired):
        proof.require(user, "delete_account")
    with pytest.raises(Unauthenticated):
        proof.password(user, token, "invalid")
    proof.password(user, token, PASSWORD)
    proof.require(sessions.current(token), "delete_account")
    assert sessions.current(token).authenticated_at == clock.now()

    def change_credential(encoded: str | None, submitted: str) -> bool:
        assert cast(QueuePool, isolated_database.engine.pool).checkedout() == 0
        with isolated_database.transaction() as session:
            session.execute(
                update(AuthIdentityRecord).values(
                    password_hash=Argon2Passwords().hash("replacement-password")
                )
            )
        return True

    proof.verify = change_credential
    with pytest.raises(Unauthenticated):
        proof.password(user, token, PASSWORD)
    proof.verify = Argon2Passwords().verify
    sessions.logout(token)
    with pytest.raises(Unauthenticated):
        proof.password(user, token, "replacement-password")


@pytest.mark.integration
@pytest.mark.parametrize(
    "case", ["missing", "old", "future", "boolean", "wrong_sub", "valid"]
)
def test_google_only_requires_fresh_provider_proof(
    isolated_database: Database, case: str
) -> None:
    clock, oidc = FakeClock(), OidcTransport()
    proof = proofs(isolated_database, clock, oidc)
    assert proof.google is not None
    url, binding = proof.google.start("")
    state, code = oidc.authorize(url)
    token = proof.google.finish(state, binding, "", code, "")
    sessions = Sessions(
        PostgresSessions(isolated_database), Argon2Passwords().verify, clock.now
    )
    user = sessions.current(token)
    assert user.authenticated_at == UNKNOWN_AUTH_TIME
    with pytest.raises(ReauthenticationRequired):
        proof.require(user, "export_data")
    with pytest.raises(Unauthenticated):
        proof.password(user, token, PASSWORD)
    url, binding = proof.start(user, token, "reauthenticate", "export_data")
    state, code = oidc.authorize(url)
    assert oidc.flows[code]["max_age"] == "0"
    assert "auth_time" in oidc.flows[code]["claims"]
    assert oidc.flows[code]["redirect_uri"].endswith("/reauthenticate/callback")
    stamp = int(NOW.timestamp())
    oidc.claims = (
        {}
        if case == "missing"
        else {
            "auth_time": {"old": stamp - 1, "future": stamp + 1, "boolean": True}.get(
                case, stamp
            )
        }
    )
    if case == "wrong_sub":
        oidc.claims["sub"] = "different-google-subject"
    if case != "valid":
        with pytest.raises(Unauthenticated):
            proof.finish(user, token, "reauthenticate", state, binding, code, "")
        assert sessions.current(token).authenticated_at == UNKNOWN_AUTH_TIME
    else:
        assert (
            proof.finish(user, token, "reauthenticate", state, binding, code, "")
            == "export_data"
        )
        proof.require(sessions.current(token), "export_data")
        assert sessions.current(token).authenticated_at == NOW
    with pytest.raises(Unauthenticated):
        proof.finish(user, token, "reauthenticate", state, binding, code, "")


@pytest.mark.integration
def test_linking_intent_session_binding_and_commit_recency(
    isolated_database: Database,
) -> None:
    clock, oidc = FakeClock(), OidcTransport()
    sessions, token = account(isolated_database, clock)
    proof = proofs(isolated_database, clock, oidc)
    user = sessions.current(token)
    assert proof.google is not None
    for changed in ("intent", "sign_in", "session", "owner", "stale", "valid"):
        url, binding = (
            proof.google.start(token)
            if changed == "sign_in"
            else proof.start(user, token, "link", "link_google")
        )
        state, code = oidc.authorize(url)
        if changed == "intent":
            with pytest.raises(Unauthenticated):
                proof.google.finish(state, binding, token, code, "")
        elif changed == "sign_in":
            with pytest.raises(Unauthenticated):
                proof.finish(user, token, "link", state, binding, code, "")
        elif changed == "session":
            other = sessions.login(user.email, PASSWORD, "")
            with pytest.raises(Unauthenticated):
                proof.finish(
                    sessions.current(other), other, "link", state, binding, code, ""
                )
        elif changed == "owner":
            _, other = account(isolated_database, clock, "other@example.org")
            with pytest.raises(Unauthenticated):
                proof.finish(
                    sessions.current(other), token, "link", state, binding, code, ""
                )
        elif changed == "stale":
            proof.seconds = 60
            clock.sleep(61)
            with pytest.raises(ReauthenticationRequired):
                proof.finish(user, token, "link", state, binding, code, "")
            proof.password(user, token, PASSWORD)
            user = sessions.current(token)
        else:
            assert (
                proof.finish(user, token, "link", state, binding, code, "")
                == "link_google"
            )
        with isolated_database.transaction() as session:
            assert session.scalar(
                select(func.count())
                .select_from(AuthIdentityRecord)
                .where(AuthIdentityRecord.issuer == ISSUER)
            ) == (1 if changed == "valid" else 0)
    assert proof.store.methods(user.user_id) == ["password", "google"]
    assert sessions.current(token).email == user.email
    assert not sessions.current(token).email_verified


@pytest.mark.integration
@pytest.mark.parametrize("same_owner", [True, False])
def test_concurrent_linking_is_unique_and_never_merges(
    isolated_database: Database, same_owner: bool
) -> None:
    clock = FakeClock()
    sessions, first = account(isolated_database, clock)
    _, second = account(isolated_database, clock, "other@example.org")
    tokens = [first, first if same_owner else second]
    owners = [sessions.current(token).user_id for token in tokens]
    database, barrier = Database(Settings(database_pool_size=2)), Barrier(2)

    def link(index: int) -> bool:
        barrier.wait(timeout=5)
        try:
            PostgresProofs(database).google(
                owners[index],
                token_hash(tokens[index]),
                GoogleIdentity("shared-sub", "any@example.org"),
                "link",
                NOW,
                300,
            )
            return True
        except DomainError:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(link, range(2)))
        assert sum(results) == (2 if same_owner else 1)
    finally:
        database.close()
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(UserRecord)) == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuthIdentityRecord)
                .where(AuthIdentityRecord.issuer == ISSUER)
            )
            == 1
        )


@pytest.mark.integration
def test_api_contract_csrf_and_shared_limits(
    isolated_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, oidc = FakeClock(), OidcTransport()
    sessions, token = account(isolated_database, clock)
    proof = proofs(isolated_database, clock, oidc)
    monkeypatch.setattr("job_hunter.app.load_google_settings", lambda: CONFIG)
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions, app.state.reauthentication = sessions, proof
    app.state.auth_limits.now = clock.now
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(token)}
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        client.cookies.set(COOKIE, token)
        assert client.get("/api/auth/methods").json()["methods"] == ["password"]
        clock.sleep(300)
        response = client.get("/api/auth/actions/delete_account")
        assert response.status_code == 403
        assert response.json()["code"] == "reauthentication_required"
        assert response.json()["action"] == "delete_account"
        body = {"action": "delete_account", "password": PASSWORD}
        assert (
            client.post("/api/auth/reauthenticate/password", json=body).status_code
            == 403
        )
        for _ in range(5):
            assert (
                client.post(
                    "/api/auth/reauthenticate/password", json=body, headers=headers
                ).status_code
                == 200
            )
        assert (
            client.post(
                "/api/auth/reauthenticate/password", json=body, headers=headers
            ).status_code
            == 429
        )
        assert client.get("/api/auth/actions/delete_account").status_code == 200
        for _ in range(30):
            response = client.post(
                "/api/auth/google/link", json={"action": "link_google"}, headers=headers
            )
            assert response.status_code == 200
        assert (
            client.post(
                "/api/auth/google/link", json={"action": "link_google"}, headers=headers
            ).status_code
            == 429
        )
        state, code = oidc.authorize(response.json()["authorization_url"])
        oidc.now = clock.now()
        response = client.get(
            "/api/auth/google/link/callback", params={"state": state, "code": code}
        )
        assert response.status_code == 303
        assert (
            response.headers["location"]
            == "https://app.example.org/login#google_link=linked&action=link_google"
        )
        assert client.get("/api/auth/methods").json()["methods"] == [
            "password",
            "google",
        ]
        assert (
            client.get(
                "/api/auth/google/link/callback", params={"state": state, "code": code}
            )
            .headers["location"]
            .endswith("failed&action=")
        )


@pytest.mark.integration
def test_migration_removes_legacy_google_recency(isolated_database: Database) -> None:
    clock = FakeClock()
    _, token = account(isolated_database, clock)
    with isolated_database.transaction() as session:
        session.execute(update(SessionRecord).values(auth_method="google"))
        expires = session.scalar(select(SessionRecord.expires_at))
    command.downgrade(migration_config(), "0008_google_oidc")
    command.upgrade(migration_config(), "head")
    with isolated_database.transaction() as session:
        row = session.scalars(select(SessionRecord)).one()
        assert row.authenticated_at == UNKNOWN_AUTH_TIME and row.expires_at == expires
    assert PostgresSessions(isolated_database).current(token_hash(token), NOW)


def test_configured_recency_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("59", "601", "private-invalid-canary"):
        monkeypatch.setenv("JOB_HUNTER_RECENT_AUTH_SECONDS", value)
        with pytest.raises(
            ConfigurationError, match="Invalid JOB_HUNTER_RECENT_AUTH_SECONDS"
        ):
            recency_seconds()
    monkeypatch.setenv("JOB_HUNTER_RECENT_AUTH_SECONDS", "60")
    assert recency_seconds() == 60
