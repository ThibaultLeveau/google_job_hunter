from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from email_validator import EmailNotValidError, validate_email

from job_hunter.domain.errors import DomainError


class InvalidRegistration(ValueError):
    def __init__(self, field_name: Literal["email", "password"]) -> None:
        self.field_name = field_name
        super().__init__("Invalid registration field.")


def validate_password(password: str) -> None:
    if not 15 <= len(password) <= 128:
        raise InvalidRegistration("password")
    try:
        password.encode("utf-8")
    except UnicodeError:
        raise InvalidRegistration("password") from None


@dataclass(frozen=True)
class Registration:
    email: str = field(repr=False)
    password: str = field(repr=False)
    normalized_email: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validate_password(self.password)
        object.__setattr__(self, "normalized_email", normalize_email(self.email))


class Unauthenticated(Exception):
    def __init__(self) -> None:
        super().__init__("Authentication is required.")


@dataclass(frozen=True)
class SessionUser:
    user_id: UUID
    email: str = field(repr=False)
    email_verified: bool
    authenticated_at: datetime
    auth_method: str

    def require_owner(self, owner_id: UUID) -> None:
        if self.user_id != owner_id:
            raise DomainError("not_found")


def normalize_email(email: str) -> str:
    if len(email) > 320:
        raise InvalidRegistration("email")
    try:
        return validate_email(email, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        raise InvalidRegistration("email") from None


type AuthAction = Literal[
    "registration",
    "login",
    "recovery",
    "oauth_start",
    "verification",
    "verification_resend",
]


@dataclass(frozen=True)
class AuthPolicy:
    seconds: int
    ip_limit: int
    account_limit: int


AUTH_POLICIES: dict[AuthAction, AuthPolicy] = {
    "registration": AuthPolicy(3600, 20, 3),
    "login": AuthPolicy(300, 30, 5),
    "recovery": AuthPolicy(3600, 20, 3),
    "oauth_start": AuthPolicy(300, 30, 0),
    "verification": AuthPolicy(300, 30, 10),
    "verification_resend": AuthPolicy(3600, 20, 3),
}


@dataclass(frozen=True)
class RateBucket:
    identifier: str = field(repr=False)
    limit: int


class AuthLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("Too many requests. Try again later.")
