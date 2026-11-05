import json
import re
import time
from collections.abc import Callable
from dataclasses import astuple
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2 as httpx
from authlib.integrations.httpx_client import OAuth2Client  # type: ignore[import-untyped]
from authlib.oidc.core import CodeIDToken  # type: ignore[import-untyped]
from cryptography.fernet import Fernet
from joserfc import jwt
from joserfc.jwk import KeySet
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from job_hunter.application.oidc import (
    ISSUER,
    UNKNOWN_AUTH_TIME,
    GoogleIdentity,
    Intent,
    LinkingRequired,
    OidcFlow,
    OidcProvider,
    OidcStore,
)
from job_hunter.application.sessions import LIFETIME
from job_hunter.domain.accounts import Unauthenticated, normalize_email
from job_hunter.infrastructure.accounts import (
    AuthIdentityRecord,
    OidcFlowRecord,
    SessionRecord,
    UserRecord,
)
from job_hunter.infrastructure.database import Database
from job_hunter.settings import ConfigurationError, Settings

DISCOVERY = ISSUER + "/.well-known/openid-configuration"
ENDPOINTS = {
    "authorization_endpoint": ISSUER + "/o/oauth2/v2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",
    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
}
CALLBACK = "/api/auth/google/callback"


class GoogleSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOB_HUNTER_GOOGLE_", frozen=True, hide_input_in_errors=True
    )
    client_id: SecretStr | None = None
    client_secret: SecretStr | None = None
    redirect_uri: str | None = None


def load_google_settings() -> GoogleSettings | None:
    try:
        config = GoogleSettings()
        if not config.model_fields_set:
            return None
        if not config.redirect_uri or not all(
            value and value.get_secret_value().strip()
            for value in (config.client_id, config.client_secret)
        ):
            raise ValueError()
        url = urlsplit(config.redirect_uri)
        origin = f"{url.scheme}://{url.netloc}"
        Settings.validate_origins((origin,))
        if config.redirect_uri != origin + CALLBACK or (
            url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1"}
        ):
            raise ValueError()
        return config
    except ValueError:
        raise ConfigurationError("Invalid JOB_HUNTER_GOOGLE_ configuration.") from None


class BoundedTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        deadline = time.monotonic() + 15
        response = self.inner.handle_request(request)
        try:
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError()
            body = bytearray()
            for chunk in response.iter_raw():
                if len(body) + len(chunk) > 65536 or time.monotonic() > deadline:
                    raise ValueError()
                body.extend(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(body),
                request=request,
            )
        finally:
            response.close()

    def close(self) -> None:
        self.inner.close()


class GoogleProvider(OidcProvider):
    def __init__(
        self,
        settings: GoogleSettings,
        transport: Callable[[], httpx.BaseTransport] = httpx.HTTPTransport,
    ) -> None:
        self.settings, self.transport = settings, transport

    def client(self, intent: Intent = "sign_in") -> OAuth2Client:
        assert self.settings.client_id and self.settings.client_secret
        assert self.settings.redirect_uri is not None
        redirect = self.settings.redirect_uri
        if intent != "sign_in":
            redirect = redirect.removesuffix("/callback") + f"/{intent}/callback"
        return OAuth2Client(
            self.settings.client_id.get_secret_value(),
            self.settings.client_secret.get_secret_value(),
            redirect_uri=redirect,
            scope="openid email profile",
            code_challenge_method="S256",
            token_endpoint_auth_method="client_secret_post",
            transport=BoundedTransport(self.transport()),
            timeout=5,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept-Encoding": "identity"},
        )

    @staticmethod
    def discover(client: OAuth2Client) -> None:
        response = client.request("GET", DISCOVERY, withhold_token=True)
        response.raise_for_status()
        data = response.json()
        if (
            data.get("issuer") != ISSUER
            or any(data.get(k) != v for k, v in ENDPOINTS.items())
            or "S256" not in data.get("code_challenge_methods_supported", [])
        ):
            raise ValueError()

    def authorization(self, state: str, flow: OidcFlow) -> str:
        try:
            with self.client(flow.intent) as client:
                self.discover(client)
                url, _ = client.create_authorization_url(
                    ENDPOINTS["authorization_endpoint"],
                    state=state,
                    nonce=flow.nonce,
                    code_verifier=flow.verifier,
                    claims=json.dumps({"id_token": {"auth_time": {"essential": True}}}),
                    **({"max_age": 0} if flow.intent == "reauthenticate" else {}),
                )
                return cast(str, url)
        except Exception:
            raise Unauthenticated() from None

    def exchange(self, code: str, flow: OidcFlow, now: datetime) -> GoogleIdentity:
        try:
            with self.client(flow.intent) as client:
                self.discover(client)
                token = client.fetch_token(
                    ENDPOINTS["token_endpoint"],
                    code=code,
                    code_verifier=flow.verifier,
                    grant_type="authorization_code",
                )
                try:
                    response = client.request(
                        "GET", ENDPOINTS["jwks_uri"], withhold_token=True
                    )
                    response.raise_for_status()
                    decoded = jwt.decode(
                        token["id_token"],
                        KeySet.import_key_set(response.json()),
                        algorithms=["RS256"],
                    )
                    claims = CodeIDToken(
                        decoded.claims,
                        decoded.header,
                        options={
                            "iss": {"values": [ISSUER, "accounts.google.com"]},
                            "aud": {"value": client.client_id},
                        },
                        params={
                            "nonce": flow.nonce,
                            "client_id": client.client_id,
                            "access_token": token.get("access_token"),
                        },
                    )
                    claims.validate(now=int(now.timestamp()))
                    subject, email = claims["sub"], claims.get("email")
                    if (
                        claims.get("email_verified") is not True
                        or not isinstance(subject, str)
                        or not re.fullmatch(r"[\x21-\x7e]{1,255}", subject)
                        or not isinstance(email, str)
                    ):
                        raise ValueError()
                    normalize_email(email)
                    authenticated_at = UNKNOWN_AUTH_TIME
                    proof = claims.get("auth_time")
                    if proof is not None:
                        if type(proof) not in {int, float}:
                            raise ValueError()
                        authenticated_at = datetime.fromtimestamp(proof, UTC)
                        if not UNKNOWN_AUTH_TIME <= authenticated_at <= now:
                            raise ValueError()
                    if flow.intent == "reauthenticate" and (
                        proof is None or authenticated_at.timestamp() < flow.started_at
                    ):
                        raise ValueError()
                    return GoogleIdentity(subject, email, authenticated_at)
                finally:
                    token.clear()
                    client.token = None
        except Exception:
            raise Unauthenticated() from None


class PostgresOidc(OidcStore):
    def __init__(self, database: Database, key: str) -> None:
        self.database, self.cipher = database, Fernet(key)

    def save(
        self, state: str, binding: str, previous: str, flow: OidcFlow, now: datetime
    ) -> None:
        with self.database.transaction() as session:
            expired = (
                select(OidcFlowRecord.id)
                .where(OidcFlowRecord.expires_at <= now)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            session.execute(
                delete(OidcFlowRecord).where(OidcFlowRecord.id.in_(expired))
            )
            session.add(
                OidcFlowRecord(
                    id=uuid4(),
                    state_hash=state,
                    binding_hash=binding,
                    previous_hash=previous,
                    encrypted_flow=self.cipher.encrypt_at_time(
                        json.dumps(astuple(flow)).encode(),
                        int(now.timestamp()),
                    ),
                    expires_at=now + timedelta(minutes=5),
                )
            )

    def take(self, state: str, binding: str, previous: str, now: datetime) -> OidcFlow:
        with self.database.transaction() as session:
            encrypted = session.scalar(
                delete(OidcFlowRecord)
                .where(
                    OidcFlowRecord.state_hash == state,
                    OidcFlowRecord.binding_hash == binding,
                    OidcFlowRecord.previous_hash == previous,
                    OidcFlowRecord.expires_at > now,
                )
                .returning(OidcFlowRecord.encrypted_flow)
            )
        if encrypted is None:
            raise Unauthenticated()
        return OidcFlow(*json.loads(self.cipher.decrypt(encrypted)))

    def sign_in(
        self, identity: GoogleIdentity, digest: str, previous: str, now: datetime
    ) -> None:
        for _ in range(2):
            try:
                with self.database.transaction() as session:
                    owner = session.scalar(
                        select(AuthIdentityRecord.user_id)
                        .where(
                            AuthIdentityRecord.issuer == ISSUER,
                            AuthIdentityRecord.subject == identity.subject,
                        )
                        .with_for_update()
                    )
                    if owner is None:
                        email = normalize_email(identity.email)
                        owner = uuid4()
                        session.add(
                            UserRecord(
                                id=owner,
                                email=identity.email,
                                email_normalized=email,
                                email_verified_at=now,
                                created_at=now,
                            )
                        )
                        session.flush()
                        session.add(
                            AuthIdentityRecord(
                                id=uuid4(),
                                user_id=owner,
                                issuer=ISSUER,
                                subject=identity.subject,
                                created_at=now,
                            )
                        )
                    session.execute(
                        update(SessionRecord)
                        .where(SessionRecord.token_hash == previous)
                        .values(revoked_at=now)
                    )
                    session.add(
                        SessionRecord(
                            id=uuid4(),
                            user_id=owner,
                            token_hash=digest,
                            expires_at=now + LIFETIME,
                            authenticated_at=identity.authenticated_at,
                            last_seen_at=now,
                            auth_method="google",
                        )
                    )
                return
            except IntegrityError as error:
                if getattr(
                    getattr(error.orig, "diag", None), "constraint_name", None
                ) not in {"uq_users_email", "uq_auth_identities_provider"}:
                    raise
        raise LinkingRequired()
