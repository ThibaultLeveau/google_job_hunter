import argparse
import logging
import random
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

from job_hunter.application.worker import Worker
from job_hunter.infrastructure.accounts import (
    PostgresVerification,
    SmtpMail,
    load_mail_settings,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.recovery import PostgresRecovery
from job_hunter.infrastructure.worker import Handler, PostgresWorkerStore
from job_hunter.settings import load_settings

HANDLERS: dict[str, Handler] = {}


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run registered durable task handlers."
    )
    parser.add_argument("--concurrency", type=int, choices=range(1, 17), default=2)
    args = parser.parse_args()
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signal, _frame: stop.set())
    database = Database(load_settings())
    try:
        clock = SystemClock()
        mail_settings = load_mail_settings()
        verification = PostgresVerification(database, mail_settings)
        recovery = PostgresRecovery(database, mail_settings)
        handlers = {
            **HANDLERS,
            **verification.handlers(SmtpMail(mail_settings), clock.now),
            **recovery.handlers(
                SmtpMail(mail_settings, "Reset your password"), clock.now
            ),
        }
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            logging.getLogger(__name__).info("Worker started.")
            Worker(
                PostgresWorkerStore(database, handlers),
                {kind: handler.execute for kind, handler in handlers.items()},
                clock,
                executor.submit,
                stop,
                random.random,
                concurrency=args.concurrency,
            ).run()
    finally:
        database.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
