"""Production container commands; fixture harnesses use a separate image target."""

import argparse
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.exc import SQLAlchemyError

from job_hunter.infrastructure.database import Database
from job_hunter.settings import ConfigurationError, Settings, load_settings

COMMANDS = {
    "api": [
        "uvicorn",
        "job_hunter.app:create_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--no-access-log",
        "--no-proxy-headers",
        "--timeout-graceful-shutdown",
        "75",
    ],
    "worker": ["job_hunter.worker", "--concurrency", "2"],
    "migrate": ["alembic", "upgrade", "head"],
}


def production_settings() -> Settings:
    from job_hunter.infrastructure.accounts import load_mail_settings
    from job_hunter.infrastructure.ai import load_ai_settings
    from job_hunter.infrastructure.oidc import load_google_settings
    from job_hunter.infrastructure.search_provider import load_search_settings

    settings = load_settings()
    if settings.database_url is None:
        raise ConfigurationError("JOB_HUNTER_DATABASE_URL is required.")
    ai, search = load_ai_settings(), load_search_settings()
    if ai.environment != "production" or search.environment != "production":
        raise ConfigurationError(
            "Production commands reject development/test adapters."
        )
    if search.provider == "serpapi" and (
        search.api_key is None or not search.api_key.get_secret_value().strip()
    ):
        raise ConfigurationError("SerpApi requires JOB_HUNTER_SEARCH_API_KEY.")
    mail = load_mail_settings()
    if (
        not settings.allowed_origins
        or any(not origin.startswith("https://") for origin in settings.allowed_origins)
        or mail.public_url not in settings.allowed_origins
        or urlsplit(mail.public_url).hostname not in settings.trusted_hosts
        or "127.0.0.1" not in settings.trusted_hosts
    ):
        raise ConfigurationError("Configure canonical HTTPS origins and trusted hosts.")
    load_google_settings()
    private_storage()
    return settings


def private_storage() -> None:
    from job_hunter.infrastructure.documents import LocalStorage, load_storage_settings

    root = load_storage_settings().root
    if root is None:
        raise ConfigurationError("JOB_HUNTER_DOCUMENT_ROOT is required.")
    LocalStorage(root)
    with tempfile.TemporaryFile(dir=root):
        pass


def schema_ready(settings: Settings) -> None:
    database = Database(settings)
    try:
        config = Config(
            toml_file=Path(__file__).resolve().parents[1] / "pyproject.toml"
        )
        expected = ScriptDirectory.from_config(config).get_heads()
        with database.engine.connect() as connection:
            actual = MigrationContext.configure(connection).get_current_heads()
        if len(expected) != 1 or set(actual) != set(expected):
            raise ConfigurationError(
                "Database schema is not current; run migrate once."
            )
    finally:
        database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=[*COMMANDS, "api-health", "worker-health"])
    command = parser.parse_args(argv).command
    try:
        if command == "migrate":
            if load_settings().database_url is None:
                raise ConfigurationError("JOB_HUNTER_DATABASE_URL is required.")
        elif command.endswith("-health"):
            schema_ready(load_settings())
        else:
            schema_ready(production_settings())
        if command == "api-health":
            with httpx.Client(timeout=2, trust_env=False) as client:
                client.get("http://127.0.0.1:8000/api/health/ready").raise_for_status()
        elif command == "worker-health":
            if b"job_hunter.worker" not in Path("/proc/1/cmdline").read_bytes().split(
                b"\0"
            ):
                raise ConfigurationError("Worker process is unavailable.")
        else:
            os.execv(sys.executable, [sys.executable, "-m", *COMMANDS[command]])
        return 0
    except ConfigurationError as error:
        print(f"Runtime configuration: {error}", file=sys.stderr)
    except OSError, SQLAlchemyError, httpx.HTTPError:
        print(
            "Runtime unavailable: check database, storage and process health.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
