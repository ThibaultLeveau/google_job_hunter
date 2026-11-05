from collections.abc import Callable
from datetime import datetime
from typing import Protocol

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
