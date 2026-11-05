import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from job_hunter.application.sessions import token_hash
from job_hunter.domain.accounts import Unauthenticated

ISSUER = "https://accounts.google.com"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class LinkingRequired(Exception):
    pass


class ConsentCancelled(Exception):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str = field(repr=False)


@dataclass(frozen=True)
class OidcFlow:
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)


class OidcProvider(Protocol):
    def authorization(self, state: str, flow: OidcFlow) -> str: ...
    def exchange(self, code: str, flow: OidcFlow, now: datetime) -> GoogleIdentity: ...


class OidcStore(Protocol):
    def save(
        self, state: str, binding: str, previous: str, flow: OidcFlow, now: datetime
    ) -> None: ...
    def take(
        self, state: str, binding: str, previous: str, now: datetime
    ) -> OidcFlow: ...
    def sign_in(
        self, identity: GoogleIdentity, digest: str, previous: str, now: datetime
    ) -> None: ...


class GoogleSignIn:
    def __init__(
        self, store: OidcStore, provider: OidcProvider, now: Callable[[], datetime]
    ) -> None:
        self.store, self.provider, self.now = store, provider, now

    def start(self, previous: str) -> tuple[str, str]:
        state, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        flow = OidcFlow(secrets.token_urlsafe(32), secrets.token_urlsafe(48))
        url = self.provider.authorization(state, flow)
        self.store.save(
            token_hash(state),
            token_hash(binding),
            token_hash(previous),
            flow,
            self.now(),
        )
        return url, binding

    def finish(
        self, state: str, binding: str, previous: str, code: str, error: str
    ) -> str:
        if not TOKEN.fullmatch(state) or not TOKEN.fullmatch(binding):
            raise Unauthenticated()
        prior = token_hash(previous)
        flow = self.store.take(
            token_hash(state), token_hash(binding), prior, self.now()
        )
        if error == "access_denied":
            raise ConsentCancelled()
        if error or not 1 <= len(code) <= 2048:
            raise Unauthenticated()
        identity = self.provider.exchange(code, flow, self.now())
        token = secrets.token_urlsafe(32)
        self.store.sign_in(identity, token_hash(token), prior, self.now())
        return token
