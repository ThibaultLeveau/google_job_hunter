from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from job_hunter.application.oidc import GoogleIdentity, GoogleSignIn, Intent
from job_hunter.application.sessions import Credential, token_hash
from job_hunter.domain.accounts import SessionUser, Unauthenticated

Action = Literal["link_google", "export_data", "delete_account", "settings"]
Method = Literal["password", "google"]


def is_recent(authenticated_at: datetime, now: datetime, seconds: int) -> bool:
    return 0 <= (now - authenticated_at).total_seconds() < seconds


class ReauthenticationRequired(Exception):
    def __init__(self, action: Action) -> None:
        self.action = action
        super().__init__("Recent authentication is required.")


class ProofStore(Protocol):
    def methods(self, owner: UUID) -> list[Method]: ...
    def credential(self, owner: UUID) -> Credential | None: ...
    def password(self, credential: Credential, digest: str, now: datetime) -> None: ...
    def google(
        self,
        owner: UUID,
        digest: str,
        identity: GoogleIdentity,
        intent: Intent,
        now: datetime,
        seconds: int,
    ) -> None: ...


class Reauthentication:
    def __init__(
        self,
        store: ProofStore,
        google: GoogleSignIn | None,
        verify: Callable[[str | None, str], bool],
        now: Callable[[], datetime],
        seconds: int,
    ) -> None:
        self.store, self.google, self.verify = store, google, verify
        self.now, self.seconds = now, seconds

    def require(self, user: SessionUser, action: Action) -> None:
        if not is_recent(user.authenticated_at, self.now(), self.seconds):
            raise ReauthenticationRequired(action)

    def password(self, user: SessionUser, token: str, password: str) -> None:
        credential = self.store.credential(user.user_id)
        valid = self.verify(credential.password_hash if credential else None, password)
        if credential is None or not valid:
            raise Unauthenticated()
        self.store.password(credential, token_hash(token), self.now())

    def start(
        self, user: SessionUser, token: str, intent: Intent, action: Action
    ) -> tuple[str, str]:
        if intent == "link":
            self.require(user, "link_google")
        elif "google" not in self.store.methods(user.user_id):
            raise Unauthenticated()
        assert self.google is not None
        return self.google.start(token, intent, str(user.user_id), action)

    def finish(
        self,
        user: SessionUser,
        token: str,
        intent: Intent,
        state: str,
        binding: str,
        code: str,
        error: str,
    ) -> str:
        assert self.google is not None
        identity, flow = self.google.exchange(
            state, binding, token, code, error, intent, str(user.user_id)
        )
        if intent == "reauthenticate" and not is_recent(
            identity.authenticated_at, self.now(), self.seconds
        ):
            raise ReauthenticationRequired(cast(Action, flow.action))
        self.store.google(
            user.user_id, token_hash(token), identity, intent, self.now(), self.seconds
        )
        return flow.action
