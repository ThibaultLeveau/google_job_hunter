import base64
import json
import sys
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_chunks import saved
from test_embeddings import SPACE
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_matches import setup as matching
from test_pipeline import build, claim_stage, current, drain
from test_snapshots import URL, page, publish, register
from test_worker import NOW

from job_hunter.application.artifact_retention import ArtifactCheckpoint, ArtifactPolicy
from job_hunter.application.chunks import ChunkJob
from job_hunter.application.worker import PermanentTaskError
from job_hunter.domain.errors import DomainError
from job_hunter.domain.tasks import NewTask, TaskKey, TaskStatus
from job_hunter.infrastructure.artifact_retention import PostgresArtifactRetention, main
from job_hunter.infrastructure.chunks import PostgresChunks
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.embeddings import EmbeddingCallRecord, PostgresEmbeddings
from job_hunter.infrastructure.jobs import JobSourceRecord, RunJobRecord
from job_hunter.infrastructure.preparation import PreparationRecord
from job_hunter.infrastructure.profiles import ProfileFactRecord
from job_hunter.infrastructure.snapshots import (
    CrawlDeliveryRecord,
    PostgresSnapshots,
    SnapshotRecord,
)
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.settings import load_settings

pytestmark = pytest.mark.integration
owners, runs = registry_owners, registry_runs


@pytest.fixture
def cleanup(isolated_database: Database) -> PostgresArtifactRetention:
    return PostgresArtifactRetention(
        isolated_database, lambda: NOW + timedelta(days=30)
    )


def roots(
    db: Database, owner: UUID, run: UUID, count: int = 1, future_last: bool = False
) -> list[UUID]:
    source = register(db, owner, run)
    ids = [UUID(int=index + 1) for index in range(count)]
    with db.transaction() as session:
        for identity in ids:
            session.add(
                SnapshotRecord(
                    id=identity,
                    source_id=source,
                    lineage_id=source,
                    previous_id=None,
                    format_version=1,
                    requested_url=URL,
                    final_url=URL,
                    status=200,
                    content_type="text/plain",
                    body=b"job",
                    content_hash=sha256(b"job").hexdigest(),
                    fetched_at=NOW
                    + timedelta(microseconds=int(future_last and identity == ids[-1])),
                )
            )
        session.flush()
        for identity in ids:
            session.add(
                PreparationRecord(
                    id=uuid4(), snapshot_id=identity, version="old", payload={}
                )
            )
    return ids


def totals(db: Database) -> dict[str, int]:
    from job_hunter.infrastructure.database import Base

    with db.transaction() as session:
        return {
            table.name: session.scalar(select(func.count()).select_from(table)) or 0
            for table in Base.metadata.sorted_tables
        }


def retire(db: Database, source: UUID) -> UUID:
    with db.transaction() as session:
        old = PostgresSnapshots(session).current(source)
        assert old is not None
    publish(
        db,
        source,
        replace(
            page(old.requested_url, old.final_url, b"new version"),
            fetched_at=NOW + timedelta(days=1),
        ),
    )
    return old.id


def test_real_pipeline_dry_run_replay_and_shared_current_preservation(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    db = isolated_database
    pipeline, clock, source = build(db, owners[0], runs[0])
    drain(pipeline, clock)
    old, _ = current(pipeline, source)
    clock.sleep(31 * 86400)
    cleanup = PostgresArtifactRetention(db, clock.now)
    assert cleanup.batch(ArtifactPolicy(), dry_run=False).protected == 1
    with db.transaction() as session:
        original = session.get(SnapshotRecord, old)
        assert original is not None
        receipt = session.scalars(select(CrawlDeliveryRecord)).one()
        task = session.scalars(
            select(TaskRecord).where(TaskRecord.payload_ref == old)
        ).first()
        assert task is not None
    assert register(db, owners[1], runs[1], original.requested_url) == source
    assert retire(db, source) == old
    before = totals(db)
    preview = cleanup.batch(ArtifactPolicy())
    assert preview.dry_run and preview.counts["job_embeddings"] > 16
    assert totals(db) == before
    applied = cleanup.batch(ArtifactPolicy(), dry_run=False)
    after = totals(db)
    assert applied.counts == preview.counts
    assert {
        name: before[name] - after[name]
        for name in before
        if before[name] != after[name]
    } == applied.counts
    assert after["job_snapshots"] == 2 and after["run_jobs"] == 2
    assert not cleanup.batch(ArtifactPolicy(), dry_run=False).counts
    with db.transaction() as session:
        replay = PostgresSnapshots(session).publish(
            source, receipt.task_id, receipt.generation, page()
        )
        assert replay.id == old and replay.body == original.body
        queued = PostgresTaskQueue(session).enqueue(
            NewTask(
                TaskKey(task.kind, task.subject_id, task.subject_version),
                old,
                clock.now(),
            ),
            clock.now(),
        )
        assert queued.id == task.id and queued.status == "succeeded"
        latest = PostgresSnapshots(session).current(source)
        assert latest is not None and latest.id != old
    assert totals(db) == after


def test_private_match_history_and_accepted_profile_survive(
    isolated_database: Database,
    cleanup: PostgresArtifactRetention,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    db = isolated_database
    repository, source, _ = matching(db, owners[0], runs[0])
    result = repository.run(owners[0], source, NOW)
    assert register(db, owners[1], runs[1]) == source
    retire(db, source)
    before = totals(db)
    assert cleanup.batch(ArtifactPolicy(), dry_run=False).protected == 1
    assert totals(db) == before
    assert (
        repository.get(owners[0], result.match_id, 1).profile_facts
        == result.profile_facts
    )
    assert (
        before[ProfileFactRecord.__tablename__] == 1
        and before[RunJobRecord.__tablename__] == 2
    )
    with pytest.raises(DomainError, match="not_found"):
        repository.get(owners[1], result.match_id, 1)


def test_age_boundary_frozen_cutoff_and_row_bounded_continuation(
    isolated_database: Database,
    cleanup: PostgresArtifactRetention,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    db = isolated_database
    roots(db, owners[0], runs[0], 4, future_last=True)
    policy = ArtifactPolicy(row_limit=2)
    first = cleanup.batch(policy, dry_run=False)
    assert first.counts == {"job_snapshots": 1, "job_preparations": 1}
    assert first.scanned == 1 and first.oversized == 0 and first.checkpoint
    cleanup.now = lambda: NOW + timedelta(days=40)
    token: str | None = first.checkpoint
    batches = []
    while token:
        batch = cleanup.batch(policy, dry_run=False, checkpoint=token)
        batches.append(batch)
        token = batch.checkpoint
    assert len(batches) == 2 and all(sum(b.counts.values()) == 2 for b in batches)
    assert not cleanup.batch(policy, dry_run=False, checkpoint=first.checkpoint).counts
    assert cleanup.batch(policy, dry_run=False).counts == first.counts
    assert totals(db)["job_snapshots"] == 0


def test_scan_limit_oversize_and_dry_checkpoint_mode(
    isolated_database: Database,
    cleanup: PostgresArtifactRetention,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    roots(isolated_database, owners[0], runs[0], 2)
    before = totals(isolated_database)
    preview = cleanup.batch(ArtifactPolicy(scan_limit=1))
    assert (
        preview.scanned == 1
        and preview.checkpoint
        and totals(isolated_database) == before
    )
    with pytest.raises(ValueError, match="checkpoint"):
        cleanup.batch(ArtifactPolicy(), checkpoint=preview.checkpoint, dry_run=False)
    tiny = cleanup.batch(ArtifactPolicy(row_limit=1), dry_run=False)
    assert (
        tiny.oversized == 2 and not tiny.counts and totals(isolated_database) == before
    )
    assert sum(cleanup.batch(ArtifactPolicy(), dry_run=False).counts.values()) == 4


@pytest.mark.parametrize(
    "status,recent,lease,protected",
    [
        ("pending", False, None, True),
        ("running", False, 31, True),
        ("running", False, 1, True),
        ("failed", False, None, True),
        ("succeeded", True, None, True),
        ("succeeded", False, None, False),
        ("cancelled", False, None, False),
    ],
)
def test_task_leases_failure_retry_and_durable_terminal_keys(
    isolated_database: Database,
    cleanup: PostgresArtifactRetention,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    status: TaskStatus,
    recent: bool,
    lease: int | None,
    protected: bool,
) -> None:
    db = isolated_database
    root = roots(db, owners[0], runs[0])[0]
    with db.transaction() as session:
        preparation = session.scalars(select(PreparationRecord)).one()
        task = PostgresTaskQueue(session).enqueue(
            NewTask(TaskKey("unknown_stage", uuid4(), 1), preparation.id, NOW), NOW
        )
        row = session.get(TaskRecord, task.id)
        assert row is not None
        row.status, row.updated_at = status, NOW + timedelta(days=int(recent))
        row.lease_expires_at = None if lease is None else NOW + timedelta(days=lease)
    result = cleanup.batch(ArtifactPolicy(), dry_run=False)
    assert bool(result.protected) == protected
    with db.transaction() as session:
        assert (session.get(SnapshotRecord, root) is not None) == protected
        retained = session.get(TaskRecord, task.id)
        assert retained is not None and retained.status == status


@pytest.mark.parametrize("lock", ["namespace", "root", "task"])
def test_concurrent_locks_do_not_wait_or_partially_delete(
    isolated_database: Database,
    cleanup: PostgresArtifactRetention,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    lock: str,
) -> None:
    db = isolated_database
    root = roots(db, owners[0], runs[0])[0]
    with db.transaction() as session:
        task = PostgresTaskQueue(session).enqueue(
            NewTask(TaskKey("old", root, 1), root, NOW), NOW
        )
        row = session.get(TaskRecord, task.id)
        assert row is not None
        row.status = "succeeded"
    other = Database(load_settings())
    try:
        with other.transaction() as session:
            if lock == "namespace":
                session.execute(text("SELECT pg_advisory_xact_lock_shared(817492351)"))
            else:
                session.get(
                    SnapshotRecord if lock == "root" else TaskRecord,
                    root if lock == "root" else task.id,
                    with_for_update=True,
                )
            batch = cleanup.batch(ArtifactPolicy(), dry_run=False)
            assert batch.contended == 1 and not batch.counts
        assert sum(cleanup.batch(ArtifactPolicy(), dry_run=False).counts.values()) == 2
    finally:
        other.close()


def test_late_worker_result_is_fenced_and_source_retained_until_terminal(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    db = isolated_database
    pipeline, clock, source = build(db, owners[0], runs[0])
    store, task, result = claim_stage(pipeline, clock, "clean")
    retire(db, source)
    clock.sleep(31 * 86400)
    cleanup = PostgresArtifactRetention(db, clock.now)
    assert cleanup.batch(ArtifactPolicy(), dry_run=False).protected == 2
    # Use the original lease time to isolate the source-version fence from lease expiry.
    assert task.lease_expires_at is not None
    with pytest.raises(PermanentTaskError):
        store.publish(task, result, task.lease_expires_at - timedelta(seconds=1))
    with db.transaction() as session:
        for row in session.scalars(
            select(TaskRecord).where(
                TaskRecord.payload_ref == task.payload_ref,
                TaskRecord.status != "succeeded",
            )
        ):
            row.status, row.lease_expires_at, row.updated_at = "cancelled", None, NOW
    applied = cleanup.batch(ArtifactPolicy(), dry_run=False)
    assert applied.counts["job_preparations"] == 1
    assert totals(db)["job_cleanings"] == 0


def test_concurrent_current_publication_protects_source_before_and_after_commit(
    isolated_database: Database,
    cleanup: PostgresArtifactRetention,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    db = isolated_database
    root = roots(db, owners[0], runs[0])[0]
    other = Database(load_settings())
    try:
        with other.transaction() as session:
            source = session.scalars(select(JobSourceRecord)).one()
            source.current_snapshot_id = root
            session.flush()
            pending = cleanup.batch(ArtifactPolicy(), dry_run=False)
            assert pending.contended == 1 and not pending.counts
        committed = cleanup.batch(ArtifactPolicy(), dry_run=False)
        assert committed.protected == 1 and not committed.counts
        assert totals(db)["job_preparations"] == 1
    finally:
        other.close()


def test_embedding_receipts_serialize_and_mixed_references_are_conservative(
    isolated_database: Database,
    cleanup: PostgresArtifactRetention,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    db = isolated_database
    content = saved(db, owners[0], runs[0], "Python required")
    batch = ChunkJob(PostgresChunks(db)).run(content)
    source = register(db, owners[0], runs[0])
    retire(db, source)
    call: dict[str, object] = {
        "call": {"id": str(uuid4()), "kind": "embedding", "model": SPACE.model}
    }
    other = Database(load_settings())
    try:
        with other.transaction() as session:
            PostgresEmbeddings(other).publish(SPACE, batch.chunks, (), [call], session)
            result = cleanup.batch(ArtifactPolicy(), dry_run=False)
            assert result.contended == 1 and not result.counts
        mixed = uuid4()
        with db.transaction() as session:
            session.add(
                EmbeddingCallRecord(
                    id=mixed,
                    space_id=SPACE.id,
                    payload={},
                    chunk_ids=[str(batch.chunks[0].id), str(uuid4())],
                )
            )
        assert cleanup.batch(ArtifactPolicy(), dry_run=False).protected == 1
        with db.transaction() as session:
            session.execute(
                delete(EmbeddingCallRecord).where(EmbeddingCallRecord.id == mixed)
            )
        applied = cleanup.batch(ArtifactPolicy(), dry_run=False)
        assert (
            applied.counts["job_embedding_calls"] == 1
            and applied.counts["job_chunks"] == 1
        )
        with pytest.raises(DomainError, match="conflict"):
            PostgresEmbeddings(db).publish(SPACE, batch.chunks, (), [call])
        assert totals(db)["job_embedding_calls"] == 0
    finally:
        other.close()


def test_transaction_failure_rolls_back_page_and_retry_is_safe(
    isolated_database: Database,
    cleanup: PostgresArtifactRetention,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = isolated_database
    roots(db, owners[0], runs[0], 2)
    before, execute, deletes = totals(db), Session.execute, 0

    def failing(session: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal deletes
        if getattr(statement, "is_delete", False):
            deletes += 1
            if deletes == 3:
                raise SQLAlchemyError("private database connection details")
        return execute(session, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "execute", failing)
        with pytest.raises(SQLAlchemyError):
            cleanup.batch(ArtifactPolicy(), dry_run=False)
    assert deletes == 3 and totals(db) == before
    assert sum(cleanup.batch(ArtifactPolicy(), dry_run=False).counts.values()) == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"days": 31},
        {"dry_run": False},
        {"cutoff": "2026-01-02T00:00:00Z"},
        {"after": "bad"},
        {"unknown": True},
    ],
)
def test_checkpoint_rejects_changed_or_invalid_policy(
    changes: dict[str, object],
) -> None:
    data = {
        "version": 1,
        "days": 30,
        "dry_run": True,
        "cutoff": NOW.isoformat(),
        **changes,
    }
    token = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    with pytest.raises(ValueError, match="checkpoint"):
        ArtifactCheckpoint.start(
            NOW + timedelta(days=30), ArtifactPolicy(), True, token
        )


@pytest.mark.parametrize(
    "policy",
    [
        {"days": 0},
        {"days": 3651},
        {"row_limit": 10001},
        {"scan_limit": 101},
        {"days": True},
    ],
)
def test_policy_limits(policy: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="bounds"):
        ArtifactPolicy(**policy)


def test_cli_dry_run_apply_invalid_and_redacted_failure(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    roots(isolated_database, owners[0], runs[0])
    monkeypatch.setattr(sys, "argv", ["artifact_retention"])
    main()
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert totals(isolated_database)["job_snapshots"] == 1
    monkeypatch.setattr(sys, "argv", ["artifact_retention", "--apply"])
    main()
    assert json.loads(capsys.readouterr().out)["counts"]["job_snapshots"] == 1
    monkeypatch.setattr(
        sys, "argv", ["artifact_retention", "--checkpoint", "secret-invalid"]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and "secret-invalid" not in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["artifact_retention"])

    def failed(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("secret-dsn")

    monkeypatch.setattr(PostgresArtifactRetention, "batch", failed)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1 and "secret-dsn" not in capsys.readouterr().err
