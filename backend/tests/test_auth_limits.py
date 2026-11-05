from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Annotated, cast

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from job_hunter.api.sessions import authenticated_user, limit_auth, verified_user
from job_hunter.app import create_app
from job_hunter.application.auth_limits import AuthLimits
from job_hunter.domain.accounts import (
    AUTH_POLICIES,
    AuthAction,
    AuthLimitExceeded,
    SessionUser,
)
from job_hunter.infrastructure.accounts import (
    AuthRateBucket,
    AuthRateKey,
    PostgresAuthCounter,
    UserRecord,
)
from job_hunter.infrastructure.database import Database
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
NOW = datetime(2026, 1, 1, tzinfo=UTC)
ORIGIN = "https://testserver"
PASSWORD = "private-rate-password"


def test_competing_instances_share_one_budget_and_key(
    isolated_database: Database,
) -> None:
    databases = [Database(Settings(database_pool_size=4)) for _ in range(2)]
    limiters = [
        AuthLimits(PostgresAuthCounter(database), lambda: NOW) for database in databases
    ]
    barrier = Barrier(8)

    def consume(index: int) -> bool:
        barrier.wait(timeout=5)
        try:
            limiters[index % 2].check(
                "login",
                "203.0.113.8",
                "USER@example.org" if index % 2 else "user@example.org",
            )
            return True
        except AuthLimitExceeded as error:
            assert error.retry_after == 300
            return False

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert sum(pool.map(consume, range(8))) == 5
        with isolated_database.transaction() as session:
            assert session.scalar(select(func.count()).select_from(AuthRateKey)) == 1
            assert sorted(session.scalars(select(AuthRateBucket.count))) == [5, 8]
            stored = str(
                session.execute(
                    text("SELECT row_to_json(auth_rate_buckets) FROM auth_rate_buckets")
                ).all()
            )
            assert "example.org" not in stored and "203.0.113" not in stored
    finally:
        for database in databases:
            database.close()


@pytest.mark.parametrize("action", list(AUTH_POLICIES))
def test_each_action_is_bounded_and_recovers_at_window_end(
    isolated_database: Database, action: AuthAction
) -> None:
    clock = [NOW]
    limiter = AuthLimits(PostgresAuthCounter(isolated_database), lambda: clock[0])
    policy = AUTH_POLICIES[action]
    for _ in range(policy.account_limit or policy.ip_limit):
        limiter.check(action, "203.0.113.9", "user@example.org")
    clock[0] += timedelta(seconds=policy.seconds, microseconds=-1)
    with pytest.raises(AuthLimitExceeded) as error:
        limiter.check(action, "203.0.113.9", "user@example.org")
    assert error.value.retry_after == 1
    clock[0] += timedelta(microseconds=1)
    limiter.check(action, "203.0.113.9", "user@example.org")
    with isolated_database.transaction() as session:
        assert all(
            count == 1 for count in session.scalars(select(AuthRateBucket.count))
        )


def test_ip_canonicalization_unknown_peers_and_bounded_cleanup(
    isolated_database: Database,
) -> None:
    limiter = AuthLimits(PostgresAuthCounter(isolated_database), lambda: NOW)
    for peers in (
        ("203.0.113.9", "::ffff:203.0.113.9"),
        ("2001:db8::1", "2001:db8::2"),
        ("", "untrusted"),
    ):
        for index in range(30):
            limiter.check("oauth_start", peers[index % 2])
        with pytest.raises(AuthLimitExceeded):
            limiter.check("oauth_start", peers[0])
    limiter.check("oauth_start", "2001:db8:1::1")
    # More than one cleanup batch; consumption must still reset an expired key.
    for index in range(110):
        limiter.check("recovery", f"192.0.2.{index}", f"person{index}@example.org")
    limiter.now = lambda: NOW + timedelta(hours=2)
    limiter.check("recovery", "192.0.2.109", "person109@example.org")
    with isolated_database.transaction() as session:
        count = session.scalar(select(func.count()).select_from(AuthRateBucket))
        assert count is not None and 124 <= count <= 126
    limiter.check("recovery", "192.0.2.109", "person109@example.org")
    limiter.check("recovery", "192.0.2.109", "person109@example.org")
    with pytest.raises(AuthLimitExceeded):
        limiter.check("recovery", "192.0.2.109", "person109@example.org")


def test_http_limits_retry_after_and_forwarded_header_spoofing(
    isolated_database: Database,
) -> None:
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    cast(AuthLimits, app.state.auth_limits).now = lambda: NOW

    @app.post("/api/auth/fixture-oauth")
    def oauth(request: Request) -> None:
        limit_auth(request, "oauth_start")

    with TestClient(app, base_url=ORIGIN, client=("203.0.113.5", 1234)) as client:
        for index in range(30):
            response = client.post(
                "/api/auth/fixture-oauth",
                headers={
                    "Origin": ORIGIN,
                    "X-Forwarded-For": f"192.0.2.{index}",
                    "Forwarded": f"for=192.0.2.{index}",
                },
            )
            assert response.status_code == 200
        response = client.post("/api/auth/fixture-oauth", headers={"Origin": ORIGIN})
        assert response.status_code == 429 and response.headers["retry-after"] == "300"
        assert response.json()["code"] == "rate_limited"
        assert "Retry-After" in response.headers["access-control-expose-headers"]
        assert "203.0.113.5" not in response.text
        app.state.auth_limits = None
        assert client.post("/api/auth/fixture-oauth").status_code == 503


def test_registration_and_login_limits_do_not_disclose_accounts(
    isolated_database: Database,
) -> None:
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    cast(AuthLimits, app.state.auth_limits).now = lambda: NOW
    with TestClient(app, base_url=ORIGIN) as client:
        for _ in range(3):
            assert (
                client.post(
                    "/api/auth/register",
                    json={"email": "known@example.org", "password": PASSWORD},
                ).status_code
                == 202
            )
        response = client.post(
            "/api/auth/register",
            json={"email": "KNOWN@example.org", "password": PASSWORD},
        )
        assert response.status_code == 429 and response.headers["retry-after"] == "3600"
        for email in ("known@example.org", "unknown@example.org"):
            for _ in range(5):
                response = client.post(
                    "/api/auth/login",
                    headers={"Origin": ORIGIN},
                    json={"email": email, "password": "incorrect-password"},
                )
                assert (
                    response.status_code == 401
                    and response.json()["code"] == "unauthenticated"
                )
            response = client.post(
                "/api/auth/login",
                headers={"Origin": ORIGIN},
                json={"email": email, "password": PASSWORD},
            )
            assert (
                response.status_code == 429 and response.headers["retry-after"] == "300"
            )
            assert email not in response.text and PASSWORD not in response.text
        assert client.post("/api/auth/login", content=b"x" * 8193).status_code == 413


def test_verified_policy_preserves_account_and_limited_resend_access(
    isolated_database: Database,
) -> None:
    app = create_app(Settings(allowed_origins=(ORIGIN,)))

    @app.post("/api/fixture-paid")
    def paid(user: Annotated[SessionUser, Depends(verified_user)]) -> None:
        pass

    @app.post("/api/auth/fixture-resend")
    def resend(
        request: Request, user: Annotated[SessionUser, Depends(authenticated_user)]
    ) -> None:
        limit_auth(request, "verification_resend", user.email)

    with TestClient(app, base_url=ORIGIN) as client:
        assert client.post("/api/fixture-paid").status_code == 401
        client.post(
            "/api/auth/register",
            json={"email": "user@example.org", "password": PASSWORD},
        )
        response = client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={"email": "user@example.org", "password": PASSWORD},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}
        assert client.get("/api/auth/me").status_code == 200
        assert client.post("/api/fixture-paid", headers=headers).status_code == 403
        for _ in range(3):
            assert (
                client.post("/api/auth/fixture-resend", headers=headers).status_code
                == 200
            )
        assert (
            client.post("/api/auth/fixture-resend", headers=headers).status_code == 429
        )
        with isolated_database.transaction() as session:
            session.execute(update(UserRecord).values(email_verified_at=NOW))
        assert client.post("/api/fixture-paid", headers=headers).status_code == 200
        assert client.post("/api/auth/logout", headers=headers).status_code == 204
