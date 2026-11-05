import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from job_hunter.domain.accounts import (
    InvalidRegistration,
    Registration,
    SessionUser,
    Unauthenticated,
)

LIFETIME = timedelta(hours=12)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(token: str) -> str:
    return hmac.digest(token.encode(), b"csrf-token", "sha256").hex()


@dataclass(frozen=True)
class Credential:
    identity_id: UUID
    user_id: UUID
    password_hash: str = field(repr=False)


class SessionStore(Protocol):
    def credential(self, email: str) -> Credential | None: ...
    def rotate(
        self,
        credential: Credential,
        previous: str,
        digest: str,
        now: datetime,
        expires: datetime,
    ) -> None: ...
    def current(self, digest: str, now: datetime) -> SessionUser: ...
    def revoke(self, digest: str, now: datetime) -> None: ...


class Sessions:
    def __init__(
        self,
        store: SessionStore,
        verify: Callable[[str | None, str], bool],
        now: Callable[[], datetime],
    ) -> None:
        self.store, self.verify, self.now = store, verify, now

    def login(self, email: str, password: str, previous: str) -> str:
        try:
            registration = Registration(email, password)
        except InvalidRegistration:
            raise Unauthenticated() from None
        credential = self.store.credential(registration.normalized_email)
        verified = self.verify(
            credential.password_hash if credential else None, password
        )
        if credential is None or not verified:
            raise Unauthenticated()
        token, now = secrets.token_urlsafe(32), self.now()
        self.store.rotate(
            credential, token_hash(previous), token_hash(token), now, now + LIFETIME
        )
        return token

    def current(self, token: str) -> SessionUser:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise Unauthenticated()
        return self.store.current(token_hash(token), self.now())

    def logout(self, token: str) -> None:
        self.store.revoke(token_hash(token), self.now())
