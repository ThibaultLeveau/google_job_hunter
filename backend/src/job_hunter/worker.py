import argparse
import logging
import random
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

from job_hunter.application.extraction import ExtractProfile
from job_hunter.application.worker import Worker
from job_hunter.infrastructure.accounts import (
    PostgresVerification,
    SmtpMail,
    load_mail_settings,
)
from job_hunter.infrastructure.ai import create_ai, load_ai_settings
from job_hunter.infrastructure.crawl_policy import CrawlPolicyStore, PoliteFetcher
from job_hunter.infrastructure.crawling import PostgresCrawler
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.discovery import PostgresDiscovery
from job_hunter.infrastructure.documents import (
    DocumentParsing,
    LocalStorage,
    PostgresExtraction,
    load_storage_settings,
)
from job_hunter.infrastructure.fetching import HttpFetcher, load_fetch_settings
from job_hunter.infrastructure.parsing import IsolatedParser
from job_hunter.infrastructure.recovery import PostgresRecovery
from job_hunter.infrastructure.search_provider import (
    create_search_provider,
    load_search_settings,
)
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
    parser.add_argument(
        "--profile-fixture", help="Explicit synthetic JSON response for fixture AI."
    )
    args = parser.parse_args()
    ai_config = load_ai_settings()
    search_provider = create_search_provider(load_search_settings())
    if ai_config.provider == "fixture" and args.profile_fixture is None:
        parser.error("Fixture AI requires --profile-fixture.")
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signal, _frame: stop.set())
    database = Database(load_settings())
    try:
        clock = SystemClock()
        mail_settings = load_mail_settings()
        verification = PostgresVerification(database, mail_settings)
        recovery = PostgresRecovery(database, mail_settings)
        storage = load_storage_settings()
        extraction = PostgresExtraction(
            database,
            ExtractProfile(
                lambda accounting: create_ai(
                    ai_config,
                    accounting,
                    cancelled=stop.is_set,
                    fixture_response=args.profile_fixture,
                ),
                ai_config.model_dump_json(
                    exclude={"api_key", "environment", "embedding_model", "limits"}
                ),
            ),
            clock.now,
            ai_config.provider != "disabled",
        )
        while not stop.is_set() and extraction.backfill():
            pass
        handlers = {
            **HANDLERS,
            **PostgresCrawler(
                database,
                PoliteFetcher(
                    CrawlPolicyStore(database),
                    HttpFetcher(load_fetch_settings(), now=clock.now),
                    clock,
                ),
                clock,
            ).handlers(),
            **PostgresDiscovery(database, search_provider, clock.now).handlers(),
            **extraction.handlers(),
            **verification.handlers(SmtpMail(mail_settings), clock.now),
            **recovery.handlers(
                SmtpMail(mail_settings, "Reset your password"), clock.now
            ),
        }
        if storage.root is not None:
            handlers.update(
                DocumentParsing(
                    database,
                    LocalStorage(storage.root),
                    IsolatedParser(cancelled=stop.is_set),
                    clock.now,
                    extraction.enqueue,
                ).handlers()
            )
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
