from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_profiles import NOW
from test_worker import FakeClock

from job_hunter.domain.errors import DomainError
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.budgets import (
    GLOBAL,
    BudgetBucketRecord,
    BudgetError,
    BudgetReservationRecord,
    BudgetSettings,
    Ceilings,
    PostgresBudgets,
    Unit,
    load_budget_settings,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.searches import SearchRunRecord
from job_hunter.settings import ConfigurationError, Settings

pytestmark = pytest.mark.integration
owners, runs = registry_owners, registry_runs
LIMITS = Ceilings(global_daily=12, owner_daily=8, run_daily=4)


def ledger(
    db: Database, clock: FakeClock, limits: Ceilings = LIMITS
) -> PostgresBudgets:
    return PostgresBudgets(
        db,
        BudgetSettings(search=limits, tokens=limits, reservation_seconds=60),
        clock.now,
    )


def charge(db: Database, key: UUID) -> tuple[str, int]:
    with db.transaction() as session:
        row = session.get(BudgetReservationRecord, key)
        assert row is not None
        return row.state, row.charged


def test_scopes_quota_rollback_reconciliation_and_private_ownership(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    db, clock = isolated_database, FakeClock()
    budgets = ledger(db, clock)
    assert budgets.availability("tokens", owners[0], runs[0]).remaining == 4
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(BudgetBucketRecord)) == 0
    first = uuid4()
    budgets.reserve(first, "tokens", 4, owners[0], runs[0])
    assert budgets.availability("tokens", owners[0], runs[0]).remaining == 0
    assert budgets.availability("tokens", owners[0]).remaining == 4
    assert budgets.availability("tokens").remaining == 8
    denied = uuid4()
    with pytest.raises(BudgetError, match="quota"):
        budgets.reserve(denied, "tokens", 1, owners[0], runs[0])
    with db.transaction() as session:
        assert session.get(BudgetReservationRecord, denied) is None
        assert sorted(session.scalars(select(BudgetBucketRecord.charged))) == [4, 4, 4]
    with pytest.raises(BudgetError, match="duplicate"):
        budgets.reserve(first, "tokens", 4, owners[0], runs[0])
    for unit, ceiling, owner, run in [
        ("tokens", 3, owners[0], runs[0]),
        ("search", 4, owners[0], runs[0]),
        ("tokens", 4, owners[1], runs[1]),
    ]:
        with pytest.raises(ValueError, match="identity conflict"):
            budgets.reserve(first, unit, ceiling, owner, run)  # type: ignore[arg-type]
    budgets.settle(first, 2)
    for actual in (2, None, 2):
        budgets.settle(first, actual)
    assert charge(db, first) == ("settled", 2)
    assert budgets.availability("tokens", owners[0], runs[0]).remaining == 2
    with pytest.raises(ValueError, match="Conflicting"):
        budgets.settle(first, 1)
    budgets.reserve(denied, "tokens", 1, owners[0], runs[0])
    budgets.settle(denied, 0)
    assert budgets.availability("search", owners[0], runs[0]).remaining == 4
    budgets.reserve(uuid4(), "tokens", 6, owners[0])
    with pytest.raises(BudgetError, match="quota"):
        budgets.reserve(uuid4(), "tokens", 1, owners[0])
    budgets.reserve(uuid4(), "tokens", 4)
    assert budgets.availability("tokens", owners[1]).remaining == 0
    for method in (budgets.reserve, budgets.availability):
        with pytest.raises(DomainError, match="not_found"):
            if method == budgets.reserve:
                budgets.reserve(uuid4(), "search", 1, owners[0], runs[1])
            else:
                budgets.availability("search", owners[0], runs[1])
    with db.transaction() as session:
        session.execute(
            update(UserRecord).where(UserRecord.id == owners[1]).values(deleted_at=NOW)
        )
    with pytest.raises(DomainError, match="not_found"):
        budgets.reserve(uuid4(), "tokens", 1, owners[1])


@pytest.mark.parametrize("scope", ["global", "owner", "run", "duplicate"])
def test_concurrent_reservations_cannot_overspend(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    scope: str,
) -> None:
    db, clock = Database(Settings(database_pool_size=4)), FakeClock()
    limits = Ceilings(
        global_daily=2 if scope == "global" else 100,
        owner_daily=2 if scope == "owner" else 100,
        run_daily=2 if scope == "run" else 100,
    )
    budgets, barrier, shared = ledger(db, clock, limits), Barrier(4), uuid4()

    def reserve(index: int) -> str:
        barrier.wait(timeout=10)
        owner = owners[index % 2] if scope == "global" else owners[0]
        run = runs[0] if scope == "run" else None
        try:
            budgets.reserve(
                shared if scope == "duplicate" else uuid4(), "search", 1, owner, run
            )
            return "reserved"
        except BudgetError as error:
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            outcomes = list(executor.map(reserve, range(4)))
        assert outcomes.count("reserved") == (1 if scope == "duplicate" else 2)
        assert outcomes.count("duplicate" if scope == "duplicate" else "quota") == (
            3 if scope == "duplicate" else 2
        )
        with db.transaction() as session:
            global_bucket = session.get(
                BudgetBucketRecord, (NOW.date(), "search", "global", GLOBAL)
            )
            assert (
                global_bucket is not None
                and global_bucket.charged == outcomes.count("reserved")
            )
    finally:
        db.close()


def test_unknown_expiration_late_usage_and_original_day(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    db, clock = isolated_database, FakeClock()
    clock.current = NOW.replace(hour=23, minute=59, second=30)
    budgets = ledger(db, clock)
    keys = [uuid4() for _ in range(3)]
    for key in keys:
        budgets.reserve(key, "tokens", 1, owners[0], runs[0])
    budgets.settle(keys[0], None)
    budgets.settle(keys[0], None)
    assert budgets.expire() == 0
    assert budgets.availability("tokens", owners[0], runs[0]).remaining == 1
    clock.sleep(61)
    assert budgets.expire(1) == 1 and budgets.expire(1) == 1 and budgets.expire() == 0
    assert all(charge(db, key) == ("unknown", 1) for key in keys)
    assert budgets.availability("tokens", owners[0], runs[0]).remaining == 4
    assert budgets.availability("tokens").resets_at == NOW + timedelta(days=2)
    budgets.settle(keys[0], 0)
    budgets.settle(keys[0], 0)
    with db.transaction() as session:
        bucket = session.get(
            BudgetBucketRecord, (NOW.date(), "tokens", "global", GLOBAL)
        )
        assert bucket is not None and bucket.charged == 2
    assert budgets.availability("tokens").remaining == 12
    with pytest.raises(BudgetError, match="duplicate"):
        budgets.reserve(keys[1], "tokens", 1, owners[0], runs[0])


def test_concurrent_settlement_and_expiry_never_double_refund(
    isolated_database: Database, owners: tuple[UUID, UUID]
) -> None:
    db, clock = Database(Settings(database_pool_size=4)), FakeClock()
    budgets, key, barrier = ledger(db, clock), uuid4(), Barrier(4)
    budgets.reserve(key, "tokens", 8, owners[0])
    clock.sleep(61)

    def settle(index: int) -> None:
        barrier.wait(timeout=10)
        if index == 0:
            budgets.expire()
        else:
            budgets.settle(key, 3)

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(settle, range(4)))
        assert charge(db, key) == ("settled", 3)
        assert budgets.availability("tokens", owners[0]).remaining == 5
        assert budgets.availability("tokens").remaining == 9
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["reserve", "settle"])
def test_commit_crash_rolls_back_receipt_and_all_scopes(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    operation: str,
) -> None:
    db, clock, key = isolated_database, FakeClock(), uuid4()
    budgets = ledger(db, clock)
    if operation == "settle":
        budgets.reserve(key, "tokens", 4, owners[0], runs[0])

    def crash(session: Session) -> None:
        session.flush()
        raise RuntimeError("commit unavailable")

    event.listen(db.sessions, "before_commit", crash)
    try:
        with pytest.raises(RuntimeError):
            if operation == "reserve":
                budgets.reserve(key, "tokens", 4, owners[0], runs[0])
            else:
                budgets.settle(key, 1)
    finally:
        event.remove(db.sessions, "before_commit", crash)
    assert budgets.availability("tokens", owners[0], runs[0]).remaining == (
        4 if operation == "reserve" else 0
    )
    with db.transaction() as session:
        receipt = session.get(BudgetReservationRecord, key)
        assert (
            (receipt is None)
            if operation == "reserve"
            else (receipt is not None and receipt.state == "reserved")
        )


def test_invalid_usage_configuration_missing_accounting_and_migrations(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db, clock, key = isolated_database, FakeClock(), uuid4()
    config = Config(toml_file="pyproject.toml")
    command.downgrade(config, "0029_match_scheduling")
    command.upgrade(config, "head")
    command.check(config)
    budgets = ledger(db, clock)
    for ceiling in (-1, 0, True, 1.5, 10**12 + 1):
        with pytest.raises(ValueError):
            budgets.reserve(uuid4(), "tokens", ceiling)  # type: ignore[arg-type]
    budgets.reserve(key, "tokens", 4, owners[0], runs[0])
    for actual in (-1, True, 1.5, 5):
        with pytest.raises(ValueError):
            budgets.settle(key, actual)  # type: ignore[arg-type]
        assert charge(db, key) == ("reserved", 4)
    for operation in (
        lambda: budgets.settle(uuid4(), 0),
        lambda: budgets.reserve(uuid4(), "tokens", 1, run=runs[0]),
        lambda: budgets.availability(cast(Unit, "secret-canary")),
        lambda: budgets.expire(0),
    ):
        with pytest.raises(ValueError):
            operation()
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.execute(
            update(BudgetReservationRecord)
            .where(BudgetReservationRecord.id == key)
            .values(charged=-1)
        )
    with db.transaction() as session:
        session.execute(
            delete(BudgetBucketRecord).where(BudgetBucketRecord.scope == "owner")
        )
    with pytest.raises(ValueError, match="Missing budget accounting"):
        budgets.settle(key, 1)
    assert budgets.availability("tokens").remaining == 8
    monkeypatch.setenv("JOB_HUNTER_BUDGET_SEARCH", '{"secret-canary":"private"}')
    with pytest.raises(ConfigurationError, match="Invalid JOB_HUNTER_BUDGET_") as error:
        load_budget_settings()
    assert "secret-canary" not in str(error.value) + caplog.text
    monkeypatch.setenv(
        "JOB_HUNTER_BUDGET_SEARCH", '{"global_daily":0,"owner_daily":0,"run_daily":0}'
    )
    disabled = PostgresBudgets(db, load_budget_settings(), clock.now)
    assert disabled.availability("search").remaining == 0
    with pytest.raises(BudgetError, match="quota"):
        disabled.reserve(uuid4(), "search", 1)


def test_scope_deletion_cannot_race_admission_and_retains_global_spend(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, clock, key = isolated_database, FakeClock(), uuid4()
    budgets, observer = ledger(db, clock), Database(Settings(database_pool_size=1))
    authorize = budgets.authorize

    def locked(session: Session, owner: UUID | None, run: UUID | None) -> None:
        authorize(session, owner, run)
        for statement in (
            update(UserRecord).where(UserRecord.id == owner).values(deleted_at=NOW),
            update(SearchRunRecord)
            .where(SearchRunRecord.id == run)
            .values(status="cancelled"),
        ):
            with pytest.raises(DBAPIError), observer.transaction() as other:
                other.execute(text("SET LOCAL lock_timeout = '50ms'"))
                other.execute(statement)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(budgets, "authorize", locked)
            budgets.reserve(key, "search", 4, owners[0], runs[0])
        with db.transaction() as session:
            session.execute(delete(UserRecord).where(UserRecord.id == owners[0]))
        with db.transaction() as session:
            row = session.get(BudgetReservationRecord, key)
            assert row is not None and row.owner_id is None and row.run_id is None
        clock.sleep(61)
        assert budgets.expire() == 1 and budgets.availability("search").remaining == 8
        budgets.settle(key, 2)
        assert budgets.availability("search").remaining == 10
    finally:
        observer.close()
