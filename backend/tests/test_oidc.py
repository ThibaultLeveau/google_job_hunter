import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import SecretStr
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from job_hunter.app import create_app
from job_hunter.application.oidc import (
    ISSUER,
    ConsentCancelled,
    GoogleSignIn,
    LinkingRequired,
    OidcFlow,
)
from job_hunter.application.sessions import Sessions
from job_hunter.domain.accounts import Registration, Unauthenticated
from job_hunter.infrastructure.accounts import (
    Argon2Passwords,
    AuthIdentityRecord,
    OidcFlowRecord,
    PostgresAccounts,
    PostgresSessions,
    SessionRecord,
    UserRecord,
    load_mail_settings,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.oidc import (
    CALLBACK,
    DISCOVERY,
    ENDPOINTS,
    GoogleProvider,
    GoogleSettings,
    PostgresOidc,
    load_google_settings,
)
from job_hunter.settings import ConfigurationError, Settings

NOW = datetime(2026, 1, 1, tzinfo=UTC)
CONFIG = GoogleSettings(
    client_id=SecretStr("fixture-client"),
    client_secret=SecretStr("fixture-secret"),
    redirect_uri="https://testserver" + CALLBACK,
)


class OidcTransport:
    """Reusable local discovery/JWKS/code exchange; no wall clock or network."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.key = RSAKey.generate_key(2048, parameters={"kid": "fixture"})
        self.claims: dict[str, Any] = {}
        self.flows: dict[str, dict[str, str]] = {}
        self.exchanges = 0
        self.invalid_signature = False
        self.discovery = {
            "issuer": ISSUER,
            **ENDPOINTS,
            "code_challenge_methods_supported": ["S256"],
        }
        self.database: Database | None = None

    def authorize(self, url: str) -> tuple[str, str]:
        values = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert values["scope"] == "openid email profile"
        assert values["response_type"] == "code"
        assert values["code_challenge_method"] == "S256"
        code = "fixture-code-" + str(len(self.flows))
        self.flows[code] = values
        return values["state"], code

    def transport(self) -> httpx.BaseTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.database:
            assert cast(QueuePool, self.database.engine.pool).checkedout() == 0
        data: dict[str, Any]
        if str(request.url) == DISCOVERY:
            data = self.discovery
        elif str(request.url) == ENDPOINTS["jwks_uri"]:
            assert "authorization" not in request.headers
            data = {"keys": [self.key.as_dict(private=False)]}
        else:
            assert str(request.url) == ENDPOINTS["token_endpoint"]
            form = parse_qs(request.content.decode())
            flow = self.flows[form["code"][0]]
            assert form["redirect_uri"] == [flow["redirect_uri"]]
            assert form["client_id"] == ["fixture-client"]
            assert form["client_secret"] == ["fixture-secret"]
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(form["code_verifier"][0].encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            assert challenge == flow["code_challenge"]
            claims = {
                "iss": ISSUER,
                "sub": "google-subject",
                "aud": "fixture-client",
                "iat": int(self.now.timestamp()),
                "exp": int(self.now.timestamp()) + 300,
                "nonce": flow["nonce"],
                "email": "user@example.org",
                "email_verified": True,
                **self.claims,
            }
            signed = jwt.encode({"alg": "RS256", "kid": "fixture"}, claims, self.key)
            if self.invalid_signature:
                parts = signed.split(".")
                parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
                signed = ".".join(parts)
            data = {
                "id_token": signed,
                "access_token": "private-access-canary",
                "refresh_token": "private-refresh-canary",
                "token_type": "Bearer",
            }
            self.exchanges += 1
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(data).encode()))


@pytest.fixture
def oidc() -> OidcTransport:
    return OidcTransport()


def service(database: Database, oidc: OidcTransport) -> GoogleSignIn:
    oidc.database = database
    return GoogleSignIn(
        PostgresOidc(database, load_mail_settings().key.get_secret_value()),
        GoogleProvider(CONFIG, oidc.transport),
        lambda: NOW,
    )


@pytest.mark.parametrize(
    "claims,signature",
    [
        ({"iss": "https://evil.example"}, False),
        ({"aud": "another-client"}, False),
        ({"exp": 0}, False),
        ({"nonce": "wrong"}, False),
        ({}, True),
        ({"email_verified": False}, False),
        ({"email_verified": "true"}, False),
        ({"email": "invalid"}, False),
        ({"sub": ""}, False),
        ({"azp": "another-client"}, False),
        ({"iat": 9999999999}, False),
    ],
)
def test_invalid_provider_tokens(
    oidc: OidcTransport, claims: dict[str, Any], signature: bool
) -> None:
    oidc.claims, oidc.invalid_signature = claims, signature
    provider, flow = GoogleProvider(CONFIG, oidc.transport), OidcFlow("nonce", "v" * 64)
    _, code = oidc.authorize(provider.authorization("s" * 43, flow))
    with pytest.raises(Unauthenticated):
        provider.exchange(code, flow, NOW)


@pytest.mark.integration
def test_first_login_rotation_existing_identity_and_private_state(
    isolated_database: Database,
    oidc: OidcTransport,
) -> None:
    sign_in = service(isolated_database, oidc)
    url, binding = sign_in.start("")
    state, code = oidc.authorize(url)
    with isolated_database.transaction() as session:
        stored = str(session.execute(text("TABLE oidc_flows")).all())
        assert all(
            value not in stored for value in (state, binding, oidc.flows[code]["nonce"])
        )
    token = sign_in.finish(state, binding, "", code, "")
    sessions = Sessions(
        PostgresSessions(isolated_database), Argon2Passwords().verify, lambda: NOW
    )
    user = sessions.current(token)
    assert user.email_verified and user.auth_method == "google"
    with isolated_database.transaction() as session:
        session.execute(update(UserRecord).values(email_verified_at=None))
    oidc.claims = {"iss": "accounts.google.com", "email": "different@example.org"}
    url, binding = sign_in.start(token)
    state, code = oidc.authorize(url)
    new = sign_in.finish(state, binding, token, code, "")
    existing = sessions.current(new)
    assert existing.user_id == user.user_id
    assert existing.email == user.email and not existing.email_verified
    with pytest.raises(Unauthenticated):
        sessions.current(token)
    with isolated_database.transaction() as session:
        identity = session.scalars(select(AuthIdentityRecord)).one()
        assert identity.issuer == ISSUER and identity.password_hash is None
        assert session.scalar(select(func.count()).select_from(OidcFlowRecord)) == 0
        stored = str(session.execute(text("TABLE sessions")).all())
        assert token not in stored and new not in stored and "canary" not in stored


@pytest.mark.integration
def test_binding_expiry_cancellation_and_replay(
    isolated_database: Database, oidc: OidcTransport
) -> None:
    sign_in = service(isolated_database, oidc)
    url, binding = sign_in.start("p" * 43)
    state, code = oidc.authorize(url)
    for values in [
        ("bad", binding, "p" * 43),
        (state, "b" * 43, "p" * 43),
        (state, binding, ""),
    ]:
        with pytest.raises(Unauthenticated):
            sign_in.finish(*values, code, "")
    with pytest.raises(ConsentCancelled):
        sign_in.finish(state, binding, "p" * 43, "", "access_denied")
    with pytest.raises(Unauthenticated):
        sign_in.finish(state, binding, "p" * 43, code, "")
    for error, code_value in [("server_error", code), ("", ""), ("", "x" * 2049)]:
        url, binding = sign_in.start("")
        state, _ = oidc.authorize(url)
        with pytest.raises(Unauthenticated):
            sign_in.finish(state, binding, "", code_value, error)
    url, binding = sign_in.start("")
    state, code = oidc.authorize(url)
    sign_in.now = lambda: NOW + timedelta(minutes=5)
    with pytest.raises(Unauthenticated):
        sign_in.finish(state, binding, "", code, "")
    sign_in.start("")
    assert oidc.exchanges == 0
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(OidcFlowRecord)) == 1


@pytest.mark.integration
@pytest.mark.parametrize("same_flow", [True, False])
def test_concurrent_first_sign_in(
    isolated_database: Database, oidc: OidcTransport, same_flow: bool
) -> None:
    sign_in = service(isolated_database, oidc)
    flows = []
    for _ in range(1 if same_flow else 2):
        url, binding = sign_in.start("")
        state, code = oidc.authorize(url)
        flows.append((state, binding, "", code, ""))
    other = Database(Settings(database_pool_size=2))
    parallel = service(other, oidc)
    oidc.database = None
    barrier = Barrier(2)

    def finish(index: int) -> bool:
        barrier.wait(timeout=5)
        try:
            parallel.finish(*flows[0 if same_flow else index])
            return True
        except Unauthenticated:
            return False

    original = Session.scalar

    def synchronized_lookup(
        session: Session, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        result = original(session, statement, *args, **kwargs)
        if (
            not same_flow
            and result is None
            and str(statement).startswith("SELECT auth_identities.user_id")
        ):
            barrier.wait(timeout=5)
        return result

    try:
        with (
            patch.object(Session, "scalar", synchronized_lookup),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(finish, range(2)))
        assert sum(results) == (1 if same_flow else 2)
    finally:
        other.close()
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(UserRecord)) == 1
        assert session.scalar(select(func.count()).select_from(AuthIdentityRecord)) == 1
        assert session.scalar(select(func.count()).select_from(SessionRecord)) == sum(
            results
        )


@pytest.mark.integration
def test_password_conflict_never_links(
    isolated_database: Database, oidc: OidcTransport
) -> None:
    PostgresAccounts(isolated_database).create(
        Registration("user@example.org", "private-password-canary"),
        Argon2Passwords().hash("private-password-canary"),
        NOW,
    )
    sign_in = service(isolated_database, oidc)
    url, binding = sign_in.start("")
    state, code = oidc.authorize(url)
    with isolated_database.transaction() as session:
        before = session.execute(text("TABLE auth_identities")).one()
    with pytest.raises(LinkingRequired):
        sign_in.finish(state, binding, "", code, "")
    with isolated_database.transaction() as session:
        assert session.execute(text("TABLE auth_identities")).one() == before
        assert session.scalar(select(UserRecord.email_verified_at)) is None
        assert session.scalar(select(func.count()).select_from(SessionRecord)) == 0


@pytest.mark.integration
def test_callback_api(
    isolated_database: Database, oidc: OidcTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("job_hunter.app.load_google_settings", lambda: CONFIG)
    app = create_app()
    app.state.google_sign_in = service(isolated_database, oidc)
    app.state.sessions.now = lambda: NOW
    with TestClient(
        app, base_url="https://testserver", follow_redirects=False
    ) as client:
        assert client.get("/api/auth/providers").json() == {"google": True}
        for outcome in ("signed_in", "cancelled", "failed", "linking_required"):
            response = client.get("/api/auth/google/start")
            assert response.status_code == 302
            cookie = response.headers["set-cookie"]
            assert all(
                flag in cookie
                for flag in ("Secure", "HttpOnly", "SameSite=lax", "Max-Age=300")
            )
            state, code = oidc.authorize(response.headers["location"])
            params = {"state": state, "code": code}
            if outcome == "cancelled":
                params["error"] = "access_denied"
            if outcome == "failed":
                params["state"] = "bad"
            if outcome == "linking_required":
                oidc.claims["sub"] = "different-subject"
            response = client.get(CALLBACK, params=params)
            assert response.status_code == 303
            assert (
                response.headers["location"]
                == "https://app.example.org/login#google=" + outcome
            )
            assert "job_hunter_oidc" not in client.cookies
            assert client.get("/api/auth/me").json()["email_verified"] is True
        assert (
            client.get(CALLBACK + "?code=a&code=b")
            .headers["location"]
            .endswith("failed")
        )
        assert client.get(CALLBACK + "?code=" + "x" * 5000).status_code == 303


def test_disabled_and_invalid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(create_app(Settings())) as client:
        assert client.get("/api/auth/providers").json() == {"google": False}
        assert client.get("/api/auth/google/start").status_code == 503
        assert (
            client.get(CALLBACK, follow_redirects=False).headers["location"]
            == "/login#google=failed"
        )
    monkeypatch.setenv("JOB_HUNTER_GOOGLE_CLIENT_ID", "private-config-canary")
    with pytest.raises(ConfigurationError, match="Invalid JOB_HUNTER_GOOGLE_"):
        load_google_settings()
    monkeypatch.setenv("JOB_HUNTER_GOOGLE_CLIENT_SECRET", "fixture-secret")
    for uri in (
        "http://example.org" + CALLBACK,
        "https://example.org/wrong",
        "https://example.org" + CALLBACK + "?code=x",
    ):
        monkeypatch.setenv("JOB_HUNTER_GOOGLE_REDIRECT_URI", uri)
        with pytest.raises(ConfigurationError):
            load_google_settings()


@pytest.mark.parametrize("failure", ["metadata", "oversized", "compressed"])
def test_provider_failures_are_bounded(oidc: OidcTransport, failure: str) -> None:
    oidc.discovery["issuer"] = "https://evil.example"

    def response(request: httpx.Request) -> httpx.Response:
        if failure == "metadata":
            return oidc.handle(request)
        return httpx.Response(
            200,
            headers={
                "content-encoding": "gzip" if failure == "compressed" else "identity"
            },
            stream=httpx.ByteStream(b"x" * 65537),
        )

    provider = GoogleProvider(CONFIG, lambda: httpx.MockTransport(response))
    with pytest.raises(Unauthenticated):
        provider.authorization("s" * 43, OidcFlow("nonce", "v" * 64))
