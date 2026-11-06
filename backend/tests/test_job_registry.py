from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import IntegrityError
from test_profiles import NOW
from test_profiles import owners as profile_owners

from job_hunter.application.searches import SearchWrite
from job_hunter.domain.errors import DomainError
from job_hunter.domain.jobs import Registration
from job_hunter.domain.searches import SearchDefinition
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.jobs import (
    JobSourceRecord,
    JobURLRecord,
    PostgresJobRegistry,
    RunJobRecord,
)
from job_hunter.infrastructure.searches import PostgresSearches, SearchRunRecord
from job_hunter.settings import Settings

owners = profile_owners
FIRST = "https://Example.org:443/jobs/42?utm_source=private-first#requirements"
SECOND = "https://example.org/jobs/42?utm_campaign=private-second"


@pytest.fixture
def runs(isolated_database: Database, owners: tuple[UUID, UUID]) -> tuple[UUID, UUID]:
    searches = PostgresSearches(isolated_database, lambda: NOW, ())
    result = []
    for owner in owners:
        saved = searches.save(
            owner,
            SearchWrite(
                name="Private query", definition=SearchDefinition(groups=[["secret"]])
            ),
        )
        result.append(searches.start(owner, saved.id, 1, uuid4()).id)
    return result[0], result[1]


def test_equivalence_private_provenance_aliases_and_authorization(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    with isolated_database.transaction() as session:
        jobs = PostgresJobRegistry(session)
        first = jobs.register(owners[0], runs[0], FIRST, 3, NOW)
        repeat = jobs.register(owners[0], runs[0], SECOND, 1, NOW)
        assert first.new_source and first.new_association
        assert repeat == Registration(first.source_id, False, False)
        with pytest.raises(DomainError, match="not_found"):
            jobs.get(owners[1], first.source_id)
        with pytest.raises(DomainError, match="not_found"):
            jobs.register(owners[1], runs[0], SECOND, 1, NOW)
        with pytest.raises(DomainError, match="not_found"):
            jobs.discoveries(owners[1], runs[0])
        second = jobs.register(owners[1], runs[1], SECOND, 7, NOW)
        assert second == Registration(first.source_id, False, True)
        for owner, run, original, rank in zip(
            owners, runs, (FIRST, SECOND), (3, 7), strict=True
        ):
            source = jobs.get(owner, first.source_id)
            assert source.canonical_url == "https://example.org/jobs/42"
            assert "private" not in repr(source) and "secret" not in repr(source)
            (discovery,) = jobs.discoveries(owner, run)
            assert discovery.source == source and discovery.original_url == original
            assert discovery.rank == rank and discovery.discovered_at == NOW
        jobs.add_alias(first.source_id, "https://example.org/alias/42")
        jobs.add_alias(first.source_id, "https://example.org/alias/42?fbclid=x")
        alias = jobs.register(
            owners[0], runs[0], "https://example.org/alias/42", 9, NOW
        )
        assert alias == repeat
        other = jobs.register(owners[0], runs[0], "https://example.org/jobs/43", 4, NOW)
        with pytest.raises(DomainError, match="conflict"):
            jobs.add_alias(other.source_id, "https://example.org/alias/42")
        with pytest.raises(DomainError, match="conflict"):
            jobs.add_alias(other.source_id, FIRST)
        with pytest.raises(DomainError, match="not_found"):
            jobs.add_alias(uuid4(), FIRST)
        assert [d.source.id for d in jobs.discoveries(owners[0], runs[0], 1, 1)] == [
            other.source_id
        ]
        for limit, offset in ((0, 0), (101, 0), (1, -1), (1, 10001)):
            with pytest.raises(ValueError):
                jobs.discoveries(owners[0], runs[0], limit, offset)
        for rank in (0, 101, True):
            with pytest.raises(ValueError):
                jobs.register(owners[0], runs[0], FIRST, rank, NOW)
        session.execute(
            update(SearchRunRecord)
            .where(SearchRunRecord.id == runs[0])
            .values(status="cancelled")
        )
        with pytest.raises(DomainError, match="conflict"):
            jobs.register(owners[0], runs[0], FIRST, 1, NOW)
    with isolated_database.transaction() as session:
        session.execute(delete(UserRecord).where(UserRecord.id == owners[0]))
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(JobSourceRecord)) == 2
        assert session.scalar(select(func.count()).select_from(RunJobRecord)) == 1
        assert (
            PostgresJobRegistry(session).get(owners[1], first.source_id).id
            == first.source_id
        )


def test_concurrent_equivalent_discoveries_and_rollback(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    database = Database(Settings(database_pool_size=2))
    barrier = Barrier(2)

    @event.listens_for(database.engine, "before_cursor_execute")
    def competing_inserts(*arguments: object) -> None:
        # Both transactions must observe no URL before either inserts its candidate.
        if str(arguments[2]).startswith("INSERT INTO job_sources"):
            barrier.wait(timeout=5)

    def discover(index: int) -> Registration:
        with database.transaction() as session:
            return PostgresJobRegistry(session).register(
                owners[index], runs[index], (FIRST, SECOND)[index], index + 1, NOW
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(discover, (0, 1)))
        assert len({r.source_id for r in results}) == 1
        assert sum(r.new_source for r in results) == 1
        assert all(r.new_association for r in results)
    finally:
        database.close()
    with pytest.raises(RuntimeError, match="rollback"):
        with isolated_database.transaction() as session:
            PostgresJobRegistry(session).register(
                owners[0], runs[0], "https://example.org/rollback", 1, NOW
            )
            raise RuntimeError("rollback")
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(JobSourceRecord)) == 1
        assert session.scalar(select(func.count()).select_from(JobURLRecord)) == 1
        assert session.scalar(select(func.count()).select_from(RunJobRecord)) == 2


def test_database_namespace_constraints_and_migration_round_trip(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    with isolated_database.transaction() as session:
        first = PostgresJobRegistry(session).register(owners[0], runs[0], FIRST, 1, NOW)
    for invalid in (
        JobSourceRecord(
            id=uuid4(),
            primary_url="https://example.org/orphan",
            canonical_version=1,
            created_at=NOW,
        ),
        JobSourceRecord(
            id=uuid4(),
            primary_url="https://example.org/jobs/42",
            canonical_version=1,
            created_at=NOW,
        ),
        JobURLRecord(
            source_id=first.source_id,
            canonical_url="https://example.org/jobs/42",
            canonical_version=1,
        ),
        JobURLRecord(
            source_id=uuid4(),
            canonical_url="https://example.org/missing",
            canonical_version=1,
        ),
        RunJobRecord(
            run_id=runs[0],
            source_id=first.source_id,
            original_url=FIRST,
            rank=1,
            discovered_at=NOW,
        ),
        RunJobRecord(
            run_id=runs[1],
            source_id=first.source_id,
            original_url=FIRST,
            rank=0,
            discovered_at=NOW,
        ),
    ):
        with pytest.raises(IntegrityError):
            with isolated_database.transaction() as session:
                session.add(invalid)
    config = Config(toml_file="pyproject.toml")
    command.downgrade(config, "0016_saved_search_runs")
    command.upgrade(config, "head")
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(JobSourceRecord)) == 0
        assert session.scalar(select(func.count()).select_from(SearchRunRecord)) == 2
