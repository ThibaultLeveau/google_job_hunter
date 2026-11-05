import re
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from job_hunter.application.accounts import Passwords
from job_hunter.application.sessions import token_hash
from job_hunter.domain.accounts import InvalidRegistration, normalize_email


class RecoveryStore(Protocol):
    def request(self, email: str, now: datetime) -> None: ...
    def reset(self, digest: str, password_hash: str, now: datetime) -> bool: ...


class PasswordRecovery:
    def __init__(
        self, store: RecoveryStore, passwords: Passwords, now: Callable[[], datetime]
    ) -> None:
        self.store, self.passwords, self.now = store, passwords, now

    def request(self, email: str) -> None:
        try:
            normalized = normalize_email(email)
        except InvalidRegistration:
            return
        self.store.request(normalized, self.now())

    def reset(self, token: str, password: str) -> None:
        # Hash before locking, including unknown tokens; the API bounds attempts.
        encoded = self.passwords.hash(password)
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            self.store.reset(token_hash(token), encoded, self.now())
