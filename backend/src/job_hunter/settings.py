from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


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
