from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(ValueError):
    """Invalid startup configuration with no submitted values in the message."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOB_HUNTER_",
        frozen=True,
        hide_input_in_errors=True,
    )

    docs_enabled: bool = False


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
