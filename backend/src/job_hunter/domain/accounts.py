from dataclasses import dataclass, field
from typing import Literal

from email_validator import EmailNotValidError, validate_email


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
        if len(self.email) > 320:
            raise InvalidRegistration("email")
        try:
            address = validate_email(self.email, check_deliverability=False)
        except EmailNotValidError:
            raise InvalidRegistration("email") from None
        object.__setattr__(self, "normalized_email", address.normalized.lower())
