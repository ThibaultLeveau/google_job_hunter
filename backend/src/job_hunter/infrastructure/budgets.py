from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.domain.errors import DomainError
from job_hunter.domain.tasks import require_utc
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.searches import SavedSearchRecord, SearchRunRecord
from job_hunter.settings import ConfigurationError

Unit = Literal["search", "tokens"]
Bound = Annotated[int, Field(strict=True, ge=0, le=10**12)]
GLOBAL = UUID(int=0)


class Ceilings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    global_daily: Bound
    owner_daily: Bound
    run_daily: Bound


class BudgetSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOB_HUNTER_BUDGET_", frozen=True, hide_input_in_errors=True
    )
    search: Ceilings = Ceilings(global_daily=100, owner_daily=20, run_daily=10)
    tokens: Ceilings = Ceilings(
        global_daily=1_000_000, owner_daily=100_000, run_daily=50_000
    )
    reservation_seconds: int = Field(default=300, ge=1, le=3600)


def load_budget_settings() -> BudgetSettings:
    try:
        return BudgetSettings()
    except ValidationError, SettingsError:
        raise ConfigurationError("Invalid JOB_HUNTER_BUDGET_ settings.") from None


class BudgetError(Exception):
    def __init__(self, code: Literal["quota", "duplicate"]):
        self.code = code
        super().__init__(code)


class BudgetBucketRecord(Base):
    __tablename__ = "provider_budget_buckets"
    __table_args__ = (
        CheckConstraint("unit IN ('search','tokens')", name="ck_budget_bucket_unit"),
        CheckConstraint("scope IN ('global','owner','run')", name="ck_budget_scope"),
        CheckConstraint("charged >= 0", name="ck_budget_bucket_charge"),
    )
    day: Mapped[date] = mapped_column(primary_key=True)
    unit: Mapped[str] = mapped_column(String(8), primary_key=True)
    scope: Mapped[str] = mapped_column(String(8), primary_key=True)
    subject: Mapped[UUID] = mapped_column(primary_key=True)
    charged: Mapped[int] = mapped_column(BigInteger)


class BudgetReservationRecord(Base):
    __tablename__ = "provider_budget_reservations"
    __table_args__ = (
        CheckConstraint(
            "unit IN ('search','tokens')", name="ck_budget_reservation_unit"
        ),
        CheckConstraint(
            "ceiling > 0 AND charged BETWEEN 0 AND ceiling", name="ck_budget_charge"
        ),
        CheckConstraint(
            "state IN ('reserved','unknown','settled')", name="ck_budget_state"
        ),
        CheckConstraint(
            "state = 'settled' OR charged = ceiling", name="ck_budget_uncertain"
        ),
        CheckConstraint("expires_at > created_at", name="ck_budget_expiry"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    unit: Mapped[str] = mapped_column(String(8))
    owner_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("search_runs.id", ondelete="SET NULL")
    )
    day: Mapped[date]
    ceiling: Mapped[int] = mapped_column(BigInteger)
    charged: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


@dataclass(frozen=True)
class Availability:
    remaining: int
    resets_at: datetime


def scopes(owner: UUID | None, run: UUID | None) -> list[tuple[str, UUID]]:
    if (run is not None and owner is None) or any(
        value is not None and (not isinstance(value, UUID) or value == GLOBAL)
        for value in (owner, run)
    ):
        raise ValueError("Invalid budget scope.")
    return (
        [("global", GLOBAL)]
        + ([("owner", owner)] if owner else [])
        + ([("run", run)] if run else [])
    )


class PostgresBudgets:
    def __init__(
        self, database: Database, settings: BudgetSettings, now: Callable[[], datetime]
    ) -> None:
        self.database, self.settings, self.now = database, settings, now

    def limits(self, unit: Unit) -> Ceilings:
        if unit not in {"search", "tokens"}:
            raise ValueError("Invalid budget unit.")
        return self.settings.search if unit == "search" else self.settings.tokens

    def instant(self) -> datetime:
        now = self.now()
        require_utc(now)
        return now

    @staticmethod
    def authorize(session: Session, owner: UUID | None, run: UUID | None) -> None:
        if (
            owner is not None
            and session.scalar(
                select(UserRecord.id)
                .where(UserRecord.id == owner, UserRecord.deleted_at.is_(None))
                .with_for_update(read=True)
            )
            is None
        ):
            raise DomainError("not_found")
        if (
            run is not None
            and session.scalar(
                select(SearchRunRecord.id)
                .join(SavedSearchRecord)
                .where(
                    SearchRunRecord.id == run,
                    SearchRunRecord.owner_id == owner,
                    SearchRunRecord.status != "cancelled",
                    SavedSearchRecord.deleted_at.is_(None),
                )
                .with_for_update(
                    read=True, of=(SearchRunRecord, SavedSearchRecord), nowait=True
                )
            )
            is None
        ):
            raise DomainError("not_found")

    def reserve(
        self,
        key: UUID,
        unit: Unit,
        ceiling: int,
        owner: UUID | None = None,
        run: UUID | None = None,
    ) -> None:
        keys, limits, now = scopes(owner, run), self.limits(unit), self.instant()
        if (
            not isinstance(key, UUID)
            or type(ceiling) is not int
            or not 0 < ceiling <= 10**12
        ):
            raise ValueError("Invalid budget reservation.")
        with self.database.transaction() as session:
            self.authorize(session, owner, run)
            created = session.scalar(
                insert(BudgetReservationRecord)
                .values(
                    id=key,
                    unit=unit,
                    owner_id=owner,
                    run_id=run,
                    day=now.date(),
                    ceiling=ceiling,
                    charged=ceiling,
                    state="reserved",
                    created_at=now,
                    expires_at=now
                    + timedelta(seconds=self.settings.reservation_seconds),
                )
                .on_conflict_do_nothing()
                .returning(BudgetReservationRecord.id)
            )
            if created is None:
                existing = session.get(BudgetReservationRecord, key)
                assert existing is not None
                if (
                    existing.unit,
                    existing.owner_id,
                    existing.run_id,
                    existing.ceiling,
                ) != (unit, owner, run, ceiling):
                    raise ValueError("Reservation identity conflict.")
                raise BudgetError("duplicate")
            for scope, subject in keys:
                session.execute(
                    insert(BudgetBucketRecord)
                    .values(
                        day=now.date(),
                        unit=unit,
                        scope=scope,
                        subject=subject,
                        charged=0,
                    )
                    .on_conflict_do_nothing()
                )
                bucket = session.get(
                    BudgetBucketRecord,
                    (now.date(), unit, scope, subject),
                    with_for_update=True,
                )
                assert bucket is not None
                if bucket.charged + ceiling > getattr(limits, scope + "_daily"):
                    raise BudgetError("quota")
                bucket.charged += ceiling

    def settle(self, key: UUID, actual: int | None) -> None:
        if not isinstance(key, UUID) or (
            actual is not None and (type(actual) is not int or actual < 0)
        ):
            raise ValueError("Invalid budget usage.")
        with self.database.transaction() as session:
            row = session.get(BudgetReservationRecord, key, with_for_update=True)
            if row is None or (actual is not None and actual > row.ceiling):
                raise ValueError("Unverified budget usage.")
            if row.state == "settled":
                if actual is not None and actual != row.charged:
                    raise ValueError("Conflicting settled usage.")
                return
            charged = row.ceiling if actual is None else actual
            for scope, subject in scopes(row.owner_id, row.run_id):
                bucket = session.get(
                    BudgetBucketRecord,
                    (row.day, row.unit, scope, subject),
                    with_for_update=True,
                )
                if bucket is None:
                    raise ValueError("Missing budget accounting.")
                bucket.charged -= row.charged - charged
            row.charged, row.state = charged, "unknown" if actual is None else "settled"

    def expire(self, limit: int = 100) -> int:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Use a bounded expiry page.")
        with self.database.transaction() as session:
            rows = list(
                session.scalars(
                    select(BudgetReservationRecord)
                    .where(
                        BudgetReservationRecord.state == "reserved",
                        BudgetReservationRecord.expires_at <= self.instant(),
                    )
                    .order_by(
                        BudgetReservationRecord.expires_at, BudgetReservationRecord.id
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.state = "unknown"
            return len(rows)

    def availability(
        self, unit: Unit, owner: UUID | None = None, run: UUID | None = None
    ) -> Availability:
        keys, limits, now = scopes(owner, run), self.limits(unit), self.instant()
        with self.database.transaction() as session:
            self.authorize(session, owner, run)
            remaining = []
            for scope, subject in keys:
                row = session.get(
                    BudgetBucketRecord, (now.date(), unit, scope, subject)
                )
                remaining.append(
                    max(
                        0,
                        getattr(limits, scope + "_daily") - (row.charged if row else 0),
                    )
                )
            return Availability(
                min(remaining),
                datetime.combine(now.date() + timedelta(days=1), time(), UTC),
            )
