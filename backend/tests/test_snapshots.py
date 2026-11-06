from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_profiles import NOW

from job_hunter.domain.errors import DomainError
from job_hunter.domain.snapshots import SNAPSHOT_BYTES, FetchedSnapshot
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.jobs import (
    JobSourceRecord,
    JobURLRecord,
    PostgresJobRegistry,
    RunJobRecord,
)
from job_hunter.infrastructure.searches import SearchRunRecord
from job_hunter.infrastructure.snapshots import (
    CrawlDeliveryRecord,
    DiscoveryHistoryRecord,
    PostgresSnapshots,
    SnapshotRecord,
    SourceMergeRecord,
)
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
owners, runs = registry_owners, registry_runs
URL = "https://jobs.example.org/source"
FINAL = "https://jobs.example.org/final"


def page(
    url: str = URL, final: str = FINAL, body: bytes = b"public job"
) -> FetchedSnapshot:
    return FetchedSnapshot(url, final, 200, "text/html", body, NOW)


def register(database: Database, owner: UUID, run: UUID, url: str = URL) -> UUID:
    with database.transaction() as session:
        return PostgresJobRegistry(session).register(owner, run, url, 1, NOW).source_id


def publish(
    database: Database,
    source: UUID,
    fetched: FetchedSnapshot,
    task: UUID | None = None,
    generation: int = 0,
) -> SnapshotRecord:
    with database.transaction() as session:
        return PostgresSnapshots(session).publish(
            source, task or uuid4(), generation, fetched
        )


def test_repeated_delivery_reuse_changes_and_reversion(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    source = register(isolated_database, owners[0], runs[0])
    task = uuid4()
    first = publish(isolated_database, source, page(), task)
    assert (
        publish(isolated_database, source, page(body=b"redelivery ignored"), task).id
        == first.id
    )
    assert (
        publish(
            isolated_database,
            source,
            replace(page(), fetched_at=NOW + timedelta(hours=1)),
        ).id
        == first.id
    )
    changed = publish(
        isolated_database,
        source,
        replace(page(body=b"changed"), fetched_at=NOW + timedelta(hours=2)),
        task,
        1,
    )
    reverted = publish(
        isolated_database, source, replace(page(), fetched_at=NOW + timedelta(hours=3))
    )
    assert first.version < changed.version < reverted.version
    assert (changed.previous_id, reverted.previous_id) == (first.id, changed.id)
    assert (first.content_hash, first.body) == (reverted.content_hash, reverted.body)
    assert first.id != reverted.id and first.lineage_id == source
    mime = publish(
        isolated_database,
        source,
        replace(page(), content_type=None, fetched_at=NOW + timedelta(hours=4)),
    )
    removed = publish(
        isolated_database,
        source,
        replace(page(), status=404, body=b"", fetched_at=NOW + timedelta(hours=5)),
    )
    assert mime.id != reverted.id and removed.status == 404
    with isolated_database.transaction() as session:
        snapshots = PostgresSnapshots(session)
        assert snapshots.current(source).id == removed.id  # type: ignore[union-attr]
        assert snapshots.delivered(task, 1).id == changed.id  # type: ignore[union-attr]
        assert (
            session.scalar(
                select(JobURLRecord.source_id).where(
                    JobURLRecord.canonical_url == FINAL
                )
            )
            == source
        )
        assert session.scalar(select(func.count()).select_from(SnapshotRecord)) == 5
        assert (
            session.scalar(select(func.count()).select_from(CrawlDeliveryRecord)) == 6
        )
        assert "redelivery ignored" not in repr(page(body=b"redelivery ignored"))


def test_snapshot_and_alias_publication_rolls_back(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    source = register(isolated_database, owners[0], runs[0])
    task = uuid4()
    with pytest.raises(RuntimeError), isolated_database.transaction() as session:
        PostgresSnapshots(session).publish(source, task, 0, page())
        raise RuntimeError("rollback")
    with isolated_database.transaction() as session:
        assert PostgresSnapshots(session).current(source) is None
        assert PostgresSnapshots(session).delivered(task, 0) is None
        assert session.scalar(select(func.count()).select_from(JobURLRecord)) == 1
    assert publish(isolated_database, source, page(), task).id is not None


def test_late_fetch_cannot_replace_a_newer_observation(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    source = register(isolated_database, owners[0], runs[0])
    current = publish(isolated_database, source, page())
    assert (
        publish(
            isolated_database,
            source,
            replace(page(), fetched_at=NOW + timedelta(hours=2)),
        ).id
        == current.id
    )
    late = publish(
        isolated_database,
        source,
        replace(page(body=b"older response"), fetched_at=NOW + timedelta(hours=1)),
    )
    assert late.id != current.id and late.previous_id == current.id
    with isolated_database.transaction() as session:
        selected = PostgresSnapshots(session).current(source)
        assert selected is not None and selected.id == current.id
        row = session.get(JobSourceRecord, source)
        assert row is not None and row.last_fetched_at == NOW + timedelta(hours=2)


def test_late_redirect_does_not_merge_a_newer_source_identity(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    source = register(isolated_database, owners[0], runs[0])
    current_target = register(isolated_database, owners[0], runs[0], FINAL)
    old_url = URL + "/old-target"
    old_target = register(isolated_database, owners[1], runs[1], old_url)
    current = publish(
        isolated_database, source, replace(page(), fetched_at=NOW + timedelta(hours=2))
    )
    late = publish(
        isolated_database,
        source,
        replace(
            page(final=old_url, body=b"obsolete"), fetched_at=NOW + timedelta(hours=1)
        ),
    )
    assert late.source_id == current_target and late.final_url == old_url
    with isolated_database.transaction() as session:
        store = PostgresSnapshots(session)
        assert store.resolve(source) == current_target
        assert store.resolve(old_target) == old_target
        selected = store.current(source)
        assert selected is not None and selected.id == current.id


def test_concurrent_known_target_reconciliation_preserves_membership_and_lineage(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    sources = [
        register(
            isolated_database, owners[i], runs[i], f"{URL}/{i}?utm_source=private-{i}"
        )
        for i in (0, 1)
    ]
    target = register(isolated_database, owners[0], runs[0], FINAL)
    prior = publish(isolated_database, sources[0], page(f"{URL}/0", f"{URL}/0", b"old"))
    completed = publish(isolated_database, target, page(FINAL, FINAL))
    database = Database(Settings(database_pool_size=3))
    barrier = Barrier(2)
    tasks = [uuid4(), uuid4()]

    def converge(index: int) -> SnapshotRecord:
        barrier.wait(timeout=5)
        return publish(database, sources[index], page(f"{URL}/{index}"), tasks[index])

    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(converge, (0, 1)))
        assert {result.id for result in results} == {completed.id}
    finally:
        database.close()
    with isolated_database.transaction() as session:
        jobs, snapshots = PostgresJobRegistry(session), PostgresSnapshots(session)
        for owner, run, old in zip(owners, runs, sources, strict=True):
            assert jobs.get(owner, target).id == target
            assert len(jobs.discoveries(owner, run)) == 1
            assert snapshots.resolve(old) == target
            assert snapshots.current(old).id == completed.id  # type: ignore[union-attr]
            with pytest.raises(DomainError, match="not_found"):
                jobs.get(uuid4(), target)
        stored = session.get(SnapshotRecord, prior.id)
        assert (
            stored is not None
            and stored.source_id == target
            and stored.lineage_id == sources[0]
        )
        assert stored.body == b"old" and stored.version == prior.version
        delivery = session.get(CrawlDeliveryRecord, (tasks[0], 0))
        assert delivery is not None and delivery.requested_url == f"{URL}/0"
        assert delivery.final_url == FINAL and delivery.snapshot_id == completed.id
        assert all(
            "private" not in url
            for url in session.scalars(select(SnapshotRecord.requested_url))
        )
        assert (
            session.scalar(
                select(DiscoveryHistoryRecord.original_url).where(
                    DiscoveryHistoryRecord.original_source_id == sources[0]
                )
            )
            == f"{URL}/0?utm_source=private-0"
        )
        assert session.scalar(select(func.count()).select_from(JobSourceRecord)) == 1
        assert session.scalar(select(func.count()).select_from(RunJobRecord)) == 2
        assert (
            session.scalar(select(func.count()).select_from(DiscoveryHistoryRecord))
            == 3
        )
        assert session.scalars(select(SearchRunRecord.processing_total)).all() == [1, 1]
    assert (
        publish(
            isolated_database, sources[0], page(f"{URL}/0", body=b"late"), tasks[0]
        ).id
        == completed.id
    )
    with isolated_database.transaction() as session:
        session.execute(delete(UserRecord).where(UserRecord.id == owners[0]))
    with isolated_database.transaction() as session:
        assert PostgresJobRegistry(session).get(owners[1], target).id == target
        assert (
            session.scalar(select(func.count()).select_from(DiscoveryHistoryRecord))
            == 1
        )


def test_merge_chain_and_failure_preserve_prior_sources(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    first = register(isolated_database, owners[0], runs[0])
    second = register(isolated_database, owners[1], runs[1], FINAL)
    prior = publish(isolated_database, first, page(URL, URL))
    with pytest.raises(RuntimeError), isolated_database.transaction() as session:
        PostgresSnapshots(session).publish(first, uuid4(), 0, page())
        raise RuntimeError("rollback merge")
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SourceMergeRecord)) == 0
        assert session.scalar(select(func.count()).select_from(JobSourceRecord)) == 2
    assert publish(isolated_database, first, page()).id == prior.id
    third_url = "https://jobs.example.org/third"
    third = register(isolated_database, owners[0], runs[0], third_url)
    latest = publish(isolated_database, second, page(FINAL, third_url, b"new"))
    with isolated_database.transaction() as session:
        store = PostgresSnapshots(session)
        assert store.resolve(first) == store.resolve(second) == third
        assert store.current(first).id == latest.id  # type: ignore[union-attr]
        assert latest.previous_id == prior.id
        assert PostgresJobRegistry(session).get(owners[1], third).id == third


def test_discovery_waits_for_reconciliation_then_uses_canonical_identity(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    first = register(isolated_database, owners[0], runs[0])
    target = register(isolated_database, owners[0], runs[0], FINAL)
    database = Database(Settings(database_pool_size=2))
    started = Event()

    def discover() -> UUID:
        started.set()
        return register(database, owners[1], runs[1])

    try:
        with ThreadPoolExecutor(1) as pool:
            with database.transaction() as session:
                PostgresSnapshots(session).publish(first, uuid4(), 0, page())
                future = pool.submit(discover)
                assert started.wait(2) and not future.done()
            assert future.result(timeout=5) == target
    finally:
        database.close()
    with isolated_database.transaction() as session:
        assert PostgresJobRegistry(session).get(owners[1], target).id == target


def test_concurrent_duplicate_publication_and_source_validation(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    source = register(isolated_database, owners[0], runs[0])
    database = Database(Settings(database_pool_size=2))
    task, barrier = uuid4(), Barrier(2)

    def submit(_: int) -> UUID:
        barrier.wait(timeout=5)
        return publish(database, source, page(), task).id

    try:
        with ThreadPoolExecutor(2) as pool:
            assert len(set(pool.map(submit, (0, 1)))) == 1
    finally:
        database.close()
    for source_id, request, code in [
        (uuid4(), page(), "not_found"),
        (source, page(FINAL + "/unknown"), "conflict"),
        (source, page(URL + "?utm_source=private"), "conflict"),
    ]:
        with pytest.raises(DomainError, match=code):
            publish(isolated_database, source_id, request)


def test_database_enforces_hash_immutable_payload_and_current_identity(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    source = register(isolated_database, owners[0], runs[0])
    stored = publish(isolated_database, source, page())
    other = register(isolated_database, owners[1], runs[1], FINAL + "/other")
    with pytest.raises(DBAPIError), isolated_database.transaction() as session:
        session.execute(update(SnapshotRecord).values(body=b"replacement"))
    with pytest.raises(IntegrityError), isolated_database.transaction() as session:
        session.execute(
            update(JobSourceRecord)
            .where(JobSourceRecord.id == other)
            .values(current_snapshot_id=stored.id)
        )
    with pytest.raises(IntegrityError), isolated_database.transaction() as session:
        session.add(
            SnapshotRecord(
                id=uuid4(),
                source_id=source,
                lineage_id=source,
                previous_id=None,
                format_version=1,
                requested_url=URL,
                final_url=FINAL,
                status=200,
                content_type=None,
                body=b"test",
                content_hash="a" * 64,
                fetched_at=NOW,
            )
        )
    config = Config(toml_file="pyproject.toml")
    command.downgrade(config, "0019_crawl_policy")
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.parametrize(
    "change",
    [
        {"requested_url": "https://user:password@example.org/job"},
        {"final_url": "https://example.org/?token=private"},
        {"status": 0},
        {"content_type": "text/html\r\nprivate"},
        {"content_type": "a" * 257},
        {"body": b"a" * (SNAPSHOT_BYTES + 1)},
        {"fetched_at": NOW.replace(tzinfo=None)},
        {"redirects": ("file:///private",)},
    ],
)
def test_snapshot_values_reject_invalid_input(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(page(), **change)  # type: ignore[arg-type]


def test_intermediate_redirect_aliases_join_existing_identities(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    first = register(isolated_database, owners[0], runs[0])
    middle_url = URL + "/middle"
    middle = register(isolated_database, owners[1], runs[1], middle_url)
    target = register(isolated_database, owners[0], runs[0], FINAL)
    result = publish(
        isolated_database, first, replace(page(), redirects=(URL, middle_url))
    )
    assert result.source_id == target
    with isolated_database.transaction() as session:
        store = PostgresSnapshots(session)
        assert store.resolve(first) == store.resolve(middle) == target
        assert PostgresJobRegistry(session).get(owners[1], target).id == target
        assert set(session.scalars(select(JobURLRecord.source_id))) == {target}
    with pytest.raises(ValueError):
        replace(page(), redirects=(URL,) * 11)
