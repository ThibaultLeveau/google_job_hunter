from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from job_hunter.settings import ConfigurationError, Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        if settings.database_url is None:
            raise ConfigurationError(
                "JOB_HUNTER_DATABASE_URL is required for database use."
            )
        self.engine = create_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.database_pool_size,
            max_overflow=0,
            pool_timeout=2,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 2,
                "options": (
                    "-c statement_timeout=2000 -c lock_timeout=2000 -c timezone=UTC"
                ),
                "tcp_user_timeout": 2000,
                "keepalives_idle": 2,
                "keepalives_interval": 1,
                "keepalives_count": 2,
            },
        )
        self.sessions = sessionmaker(
            self.engine,
            expire_on_commit=False,
            autobegin=False,
            close_resets_only=False,
        )

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        # The caller owns this entire unit of work; adapters must not commit it.
        with self.sessions.begin() as session:
            yield session

    def is_ready(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def close(self) -> None:
        self.engine.dispose()
