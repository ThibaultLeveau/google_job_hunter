import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

from job_hunter.application.sessions import token_hash
from job_hunter.domain.accounts import Unauthenticated

ISSUER = "https://accounts.google.com"
Intent = Literal["sign_in", "reauthenticate", "link"]
UNKNOWN_AUTH_TIME = datetime(1970, 1, 1, tzinfo=UTC)
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class LinkingRequired(Exception):
    pass


class ConsentCancelled(Exception):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str = field(repr=False)
    authenticated_at: datetime = UNKNOWN_AUTH_TIME


@dataclass(frozen=True)
class OidcFlow:
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    intent: Intent = "sign_in"
    owner: str = ""
    action: str = ""
    started_at: int = 0


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

    def start(
        self,
        previous: str,
        intent: Intent = "sign_in",
        owner: str = "",
        action: str = "",
    ) -> tuple[str, str]:
        state, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        flow = OidcFlow(
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(48),
            intent,
            owner,
            action,
            int(self.now().timestamp()),
        )
        url = self.provider.authorization(state, flow)
        self.store.save(
            token_hash(state),
            token_hash(binding),
            token_hash(previous),
            flow,
            self.now(),
        )
        return url, binding

    def exchange(
        self,
        state: str,
        binding: str,
        previous: str,
        code: str,
        error: str,
        intent: Intent = "sign_in",
        owner: str = "",
    ) -> tuple[GoogleIdentity, OidcFlow]:
        if not TOKEN.fullmatch(state) or not TOKEN.fullmatch(binding):
            raise Unauthenticated()
        prior = token_hash(previous)
        flow = self.store.take(
            token_hash(state), token_hash(binding), prior, self.now()
        )
        if flow.intent != intent or flow.owner != owner:
            raise Unauthenticated()
        if error == "access_denied":
            raise ConsentCancelled()
        if error or not 1 <= len(code) <= 2048:
            raise Unauthenticated()
        return self.provider.exchange(code, flow, self.now()), flow

    def finish(
        self, state: str, binding: str, previous: str, code: str, error: str
    ) -> str:
        identity, _ = self.exchange(state, binding, previous, code, error)
        token = secrets.token_urlsafe(32)
        self.store.sign_in(
            identity, token_hash(token), token_hash(previous), self.now()
        )
        return token
