import re
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from job_hunter.application.sessions import token_hash
from job_hunter.domain.accounts import Registration


class Passwords(Protocol):
    def hash(self, password: str) -> str: ...


class AccountWriter(Protocol):
    def create(
        self, registration: Registration, password_hash: str, now: datetime
    ) -> None: ...


class RegisterAccount:
    def __init__(
        self, accounts: AccountWriter, passwords: Passwords, now: Callable[[], datetime]
    ) -> None:
        self.accounts, self.passwords, self.now = accounts, passwords, now

    def execute(self, registration: Registration) -> None:
        # Hash even duplicate submissions before opening the short account transaction.
        password_hash = self.passwords.hash(registration.password)
        self.accounts.create(registration, password_hash, self.now())


class Mail(Protocol):
    def send(self, recipient: str, link: str, message_id: UUID) -> None: ...


class VerificationStore(Protocol):
    def resend(self, user_id: UUID, now: datetime) -> None: ...
    def consume(self, user_id: UUID, digest: str, now: datetime) -> bool: ...


class VerifyEmail:
    def __init__(self, store: VerificationStore, now: Callable[[], datetime]) -> None:
        self.store, self.now = store, now

    def verify(self, user_id: UUID, token: str) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            self.store.consume(user_id, token_hash(token), self.now())

    def resend(self, user_id: UUID) -> None:
        self.store.resend(user_id, self.now())
