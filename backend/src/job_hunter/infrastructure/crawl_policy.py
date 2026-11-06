from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from uuid import UUID, uuid4

from protego import Protego
from sqlalchemy import CheckConstraint, DateTime, String, Text, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from job_hunter.application.fetching import FetchError, FetchRequest, FetchResponse
from job_hunter.application.worker import (
    Clock,
    DeferredTaskError,
    PermanentTaskError,
    RetryableTaskError,
)
from job_hunter.domain.tasks import StaleLease
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.fetching import HttpFetcher, public_url

AGENT = "JobHunter"
ROBOTS_BYTES = 512_000


class HostRecord(Base):
    __tablename__ = "crawl_hosts"
    __table_args__ = (
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_crawl_host_lease",
        ),
    )
    host: Mapped[str] = mapped_column(String(253), primary_key=True)
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RobotsRecord(Base):
    __tablename__ = "crawl_robots"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('rules','allow','deny','unavailable')",
            name="ck_crawl_robots_decision",
        ),
        CheckConstraint("octet_length(body) <= 512000", name="ck_crawl_robots_size"),
    )
    origin: Mapped[str] = mapped_column(String(300), primary_key=True)
    decision: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RobotsDenied(PermanentTaskError):
    pass


class RobotsUnavailable(RetryableTaskError):
    pass


@dataclass(frozen=True)
class HostLease:
    host: str
    token: UUID


@dataclass(frozen=True)
class RobotsPolicy:
    decision: str
    body: str
    expires_at: datetime

    def delay(self, url: str) -> float:
        if self.decision == "unavailable":
            raise RobotsUnavailable(retry_at=self.expires_at)
        if self.decision == "deny":
            raise RobotsDenied()
        rules = Protego.parse(self.body)
        if not rules.can_fetch(url, AGENT):
            raise RobotsDenied()
        return self.pacing(rules)

    @staticmethod
    def pacing(rules: Protego) -> float:
        delay = rules.crawl_delay(AGENT) or 1.0
        rate = rules.request_rate(AGENT)
        if rate is not None:
            if rate.start_time is not None or rate.seconds > 3600 * rate.requests:
                raise RobotsDenied()
            delay = max(delay, rate.seconds / rate.requests)
        if not isfinite(delay) or delay > 3600:
            raise RobotsDenied()
        return max(1.0, delay)


class CrawlPolicyStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def claim(self, host: str, now: datetime, lease: timedelta) -> HostLease:
        with self.database.transaction() as session:
            session.execute(
                insert(HostRecord)
                .values(host=host, next_at=now)
                .on_conflict_do_nothing()
            )
            row = session.scalars(
                select(HostRecord).where(HostRecord.host == host).with_for_update()
            ).one()
            ready = max(row.next_at, row.lease_expires_at or now)
            if ready > now:
                raise DeferredTaskError(ready)
            token = uuid4()
            row.lease_token, row.lease_expires_at = token, now + lease
            return HostLease(host, token)

    def release(
        self,
        lease: HostLease,
        now: datetime,
        delay: float,
        cache: tuple[str, RobotsPolicy] | None = None,
    ) -> None:
        with self.database.transaction() as session:
            row = session.scalar(
                select(HostRecord)
                .where(
                    HostRecord.host == lease.host,
                    HostRecord.lease_token == lease.token,
                    HostRecord.lease_expires_at > now,
                )
                .with_for_update()
            )
            if row is None:
                raise StaleLease()
            row.next_at = now + timedelta(seconds=delay)
            row.lease_token = row.lease_expires_at = None
            if cache is not None:
                origin, policy = cache
                values = dict(
                    decision=policy.decision,
                    body=policy.body,
                    expires_at=policy.expires_at,
                )
                session.execute(
                    insert(RobotsRecord)
                    .values(origin=origin, **values)
                    .on_conflict_do_update(
                        index_elements=[RobotsRecord.origin], set_=values
                    )
                )

    def cached(self, origin: str, now: datetime) -> RobotsPolicy | None:
        with self.database.transaction() as session:
            row = session.scalar(
                select(RobotsRecord).where(
                    RobotsRecord.origin == origin, RobotsRecord.expires_at > now
                )
            )
            return RobotsPolicy(row.decision, row.body, row.expires_at) if row else None


class PoliteFetcher:
    """One checked hop; redirect callers must submit its returned request here again.

    One leased request per hostname across HTTP/HTTPS, with at least one second
    after completion. The lease exceeds the HTTP deadline by five seconds.
    Robots rules expire after one hour; unavailable files after 60 seconds or
    Retry-After (at most one hour). Only 404/410 imply permission; 401/403 deny.
    Redirected, oversized, invalid UTF-8 or non-text robots files fail closed.
    Rules use JobHunter (the HTTP agent is JobHunter/0.1), RFC wildcard matching,
    Crawl-delay and Request-rate. Delays above one hour/time windows deny access.
    Unrecognized malformed directives follow Protego's ignore semantics.
    Capacity waits use durable deferral; failed calls consume bounded attempts.
    Worker composition and page outcome classification belong to issue #44.
    """

    def __init__(
        self, store: CrawlPolicyStore, http: HttpFetcher, clock: Clock
    ) -> None:
        self.store, self.http, self.clock = store, http, clock
        self.lease = timedelta(seconds=http.settings.total_seconds + 5)

    def robots(self, origin: str, host: str) -> RobotsPolicy:
        cached = self.store.cached(origin, self.clock.now())
        if cached is not None:
            return cached
        lease = self.store.claim(host, self.clock.now(), self.lease)
        decision, body, ttl, delay = "unavailable", "", 60, 1.0
        try:
            response = self.http.fetch(FetchRequest(origin + "/robots.txt"))
            # Redirected robots files fail closed; no unchecked cross-origin exemption.
            if response.redirect is None and len(response.body) <= ROBOTS_BYTES:
                if (response.content_type or "text/plain").split(";", 1)[
                    0
                ].lower() == "text/plain":
                    body = response.body.decode("utf-8-sig", errors="strict")
                    try:
                        delay = RobotsPolicy.pacing(Protego.parse(body))
                        decision, ttl = "rules", 3600
                    except RobotsDenied:
                        decision, ttl = "deny", 3600
        except FetchError as error:
            if error.status in {404, 410}:
                decision, ttl = "allow", 3600
            elif error.status in {401, 403}:
                decision, ttl = "deny", 3600
            elif error.retry_after is not None:
                ttl = max(60, error.retry_after)
                delay = float(ttl)
        except UnicodeError:
            pass
        finally:
            policy = RobotsPolicy(
                decision, body, self.clock.now() + timedelta(seconds=ttl)
            )
            self.store.release(lease, self.clock.now(), delay, (origin, policy))
        return policy

    def fetch(self, request: FetchRequest) -> FetchResponse:
        try:
            return self.hop(request)
        except SQLAlchemyError:
            raise RetryableTaskError() from None

    def hop(self, request: FetchRequest) -> FetchResponse:
        url = public_url(request.url, self.http.settings)
        host = url.raw_host.decode("ascii").rstrip(".")
        origin = str(url.copy_with(path="", query=None, fragment=None)).rstrip("/")
        delay = self.robots(origin, host).delay(str(url))
        lease = self.store.claim(host, self.clock.now(), self.lease)
        try:
            return self.http.fetch(request)
        except FetchError as error:
            if error.transient:
                delay = max(delay, float(error.retry_after or 0))
                raise RetryableTaskError(
                    retry_at=self.clock.now() + timedelta(seconds=delay)
                ) from None
            raise
        finally:
            self.store.release(lease, self.clock.now(), delay)
