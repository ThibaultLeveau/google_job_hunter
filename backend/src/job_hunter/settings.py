import re
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from job_hunter.domain.searches import Domain


class ConfigurationError(ValueError):
    """Invalid startup configuration with no submitted values in the message."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOB_HUNTER_",
        frozen=True,
        hide_input_in_errors=True,
    )

    docs_enabled: bool = False
    database_url: SecretStr | None = None
    database_pool_size: int = Field(default=5, ge=1, le=20)
    trusted_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    allowed_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    request_max_bytes: int = Field(default=1048576, ge=1, le=16777216)
    search_allowed_domains: tuple[Domain, ...] = Field(
        default=("linkedin.com", "indeed.com", "jobs.lever.co", "boards.greenhouse.io"),
        max_length=100,
    )

    @field_validator("trusted_hosts")
    @classmethod
    def validate_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", value) for value in values
        ):
            raise ValueError(
                "Use exact lowercase hostnames without wildcards or ports."
            )
        return values

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            origin = urlsplit(value)
            _ = origin.port
            if (
                origin.scheme not in {"http", "https"}
                or not origin.hostname
                or origin.username is not None
                or "*" in value
                or value != f"{origin.scheme}://{origin.netloc}"
            ):
                raise ValueError(
                    "Use exact HTTP(S) origins without paths or credentials."
                )
        return values

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            url = make_url(value.get_secret_value())
            valid = (
                url.drivername == "postgresql+psycopg"
                and bool(url.host)
                and "," not in (url.host or "")
                and bool(url.database)
                and set(url.query) <= {"sslmode", "sslrootcert"}
            )
            _ = url.port
        except ArgumentError, ValueError:
            valid = False
        if not valid:
            raise ValueError("Use a single-host postgresql+psycopg URL.")
        return value


def load_settings() -> Settings:
    try:
        return Settings()
    except SettingsError:
        raise ConfigurationError(
            "Invalid application settings. Check JOB_HUNTER_ variables."
        ) from None
    except ValidationError as error:
        # Report known field names only; validator messages may contain raw input.
        fields = sorted(
            {
                str(detail["loc"][0])
                for detail in error.errors(include_input=False, include_context=False)
                if detail["loc"] and detail["loc"][0] in Settings.model_fields
            }
        )
        raise ConfigurationError(
            f"Invalid application settings: {', '.join(fields)}. "
            "Check the corresponding JOB_HUNTER_ environment variables."
        ) from None
