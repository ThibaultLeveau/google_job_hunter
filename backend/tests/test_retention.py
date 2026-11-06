import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_account_deletion import deletion, drain, upload
from test_account_fencing import Account
from test_account_fencing import account as account_fixture
from test_artifact_retention import roots, totals
from test_deletion import services
from test_job_registry import runs as registry_runs
from test_profiles import owners as profile_owners
from test_snapshots import register
from test_worker import NOW, FakeClock

from job_hunter.application.retention import (
    PHASES,
    RetentionCheckpoint,
    RetentionPolicy,
)
from job_hunter.domain.tasks import NewTask, TaskKey
from job_hunter.infrastructure.account_deletion import AccountCleanup
from job_hunter.infrastructure.accounts import (
    AuthRateBucket,
    MailDeliveryRecord,
    OidcFlowRecord,
    RecoveryDeliveryRecord,
    RecoveryRecord,
    SessionRecord,
    UserRecord,
    VerificationRecord,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import (
    DocumentCleanup,
    DocumentDeletionRecord,
    DocumentRecord,
    LocalStorage,
    PostgresDocuments,
    StorageSettings,
)
from job_hunter.infrastructure.retention import PostgresRetention, main
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.infrastructure.worker import Handler, PostgresWorkerStore
from job_hunter.settings import load_settings

pytestmark = pytest.mark.integration
owners, runs, account = profile_owners, registry_runs, account_fixture
POLICY = RetentionPolicy()


def run(
    db: Database,
    phase: str,
    now: Any = NOW + timedelta(days=31),
    dry: bool = False,
    policy: RetentionPolicy = POLICY,
) -> dict[str, Any]:
    token = RetentionCheckpoint(
        started=now, policy=policy, dry_run=dry, phase=PHASES.index(phase)
    ).encode()
    return PostgresRetention(db, lambda: now).batch(
        policy, dry_run=dry, checkpoint=token
    )


@pytest.mark.parametrize(
    "phase,model",
    [
        ("sessions", SessionRecord),
        ("verification", VerificationRecord),
        ("recovery", RecoveryRecord),
        ("oidc", OidcFlowRecord),
        ("rates", AuthRateBucket),
    ],
)
def test_expiry_boundaries_and_dry_run(
    isolated_database: Database,
    account: Account,
    phase: str,
    model: Any,
) -> None:
    db, expiry = isolated_database, NOW + timedelta(hours=1)
    with db.transaction() as session:
        session.add(
            AuthRateBucket(id=uuid4(), key_hash="a" * 64, count=1, expires_at=expiry)
        )
        session.add(
            OidcFlowRecord(
                id=uuid4(),
                state_hash="a" * 64,
                binding_hash="b" * 64,
                previous_hash="c" * 64,
                encrypted_flow=b"private",
                expires_at=expiry,
            )
        )
        session.flush()
        row = session.scalars(select(model)).first()
        assert row is not None
        row.expires_at = expiry
    policy = RetentionPolicy(action_hours=1, session_hours=1, rate_hours=1)
    before = totals(db)
    boundary = expiry + timedelta(hours=1)
    assert not run(db, phase, boundary - timedelta(microseconds=1), policy=policy)[
        "counts"
    ]
    preview = run(db, phase, boundary, dry=True, policy=policy)
    assert preview["counts"] == {model.__tablename__: 1} and totals(db) == before
    assert run(db, phase, boundary, policy=policy)["counts"] == preview["counts"]
    assert totals(db)[model.__tablename__] == before[model.__tablename__] - 1
    assert not run(db, phase, boundary, policy=policy)["counts"]


@pytest.mark.parametrize(
    "phase,model",
    [
        ("verification_mail", MailDeliveryRecord),
        ("recovery_mail", RecoveryDeliveryRecord),
    ],
)
def test_mail_payloads_require_completed_delivery_and_tokens_keep_live_leases(
    isolated_database: Database,
    account: Account,
    phase: str,
    model: Any,
) -> None:
    db = isolated_database
    assert not run(db, phase)["counts"]
    with db.transaction() as session:
        task = session.scalars(select(TaskRecord).where(TaskRecord.kind == phase)).one()
        task.status, task.updated_at = "running", NOW
        task.lease_expires_at = NOW + timedelta(days=40)
    assert run(db, phase.split("_")[0])["protected"] == 1
    with db.transaction() as session:
        task = session.scalars(select(TaskRecord).where(TaskRecord.kind == phase)).one()
        task.status, task.lease_expires_at = "succeeded", None
    assert run(db, phase)["counts"] == {model.__tablename__: 1}
    assert totals(db)[model.__tablename__] == 0


def test_terminal_history_keeps_unknown_public_live_and_referenced_keys(
    isolated_database: Database,
    account: Account,
) -> None:
    db = isolated_database
    with db.transaction() as session:
        for kind in (
            "verification_mail",
            "recovery_expiry",
            "document_parse",
            "profile_extract_old",
            "account_cleanup",
            "match_scan",
            "match_evaluate",
            "unknown",
            "job_text_old",
        ):
            key = TaskKey(kind, uuid4(), 1)
            task = PostgresTaskQueue(session).enqueue(
                NewTask(key, key.subject_id, NOW), NOW
            )
            row = session.get(TaskRecord, task.id)
            assert row is not None
            row.status = "succeeded"
    before = totals(db)["tasks"]
    result = run(db, "tasks")
    assert result["counts"] == {"tasks": 7} and totals(db)["tasks"] == before - 7
    assert not run(db, "tasks")["counts"]


def test_continuation_frozen_age_policy_and_atomic_retry(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = isolated_database
    with db.transaction() as session:
        for index in range(3):
            session.add(
                AuthRateBucket(
                    id=UUID(int=index + 1),
                    key_hash=str(index) * 64,
                    count=1,
                    expires_at=NOW,
                )
            )
    policy = RetentionPolicy(batch_size=1)
    first = run(db, "rates", policy=policy)
    assert first["scanned"] == 1 and first["counts"] == {"auth_rate_buckets": 1}
    service = PostgresRetention(db, lambda: NOW + timedelta(days=40))
    with pytest.raises(ValueError, match="checkpoint"):
        service.batch(policy, checkpoint=first["checkpoint"])
    with pytest.raises(ValueError, match="checkpoint"):
        service.batch(
            RetentionPolicy(batch_size=2), dry_run=False, checkpoint=first["checkpoint"]
        )
    execute = Session.execute

    def fail(session: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(statement, "is_delete", False):
            raise SQLAlchemyError("private-dsn")
        return execute(session, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "execute", fail)
        with pytest.raises(SQLAlchemyError):
            service.batch(policy, dry_run=False, checkpoint=first["checkpoint"])
    assert totals(db)["auth_rate_buckets"] == 2
    second = service.batch(policy, dry_run=False, checkpoint=first["checkpoint"])
    assert second["counts"] == first["counts"]
    next_token = second["checkpoint"]
    assert isinstance(next_token, str)
    assert (
        service.batch(policy, dry_run=False, checkpoint=next_token)["counts"]
        == first["counts"]
    )


@pytest.mark.parametrize("lock", ["namespace", "row", "task"])
def test_cleanup_yields_to_concurrent_authentication_and_delivery(
    isolated_database: Database,
    account: Account,
    lock: str,
) -> None:
    db, other = isolated_database, Database(load_settings())
    try:
        with other.transaction() as session:
            if lock == "namespace":
                session.execute(text("SELECT pg_advisory_xact_lock(817492355)"))
            else:
                model = VerificationRecord if lock == "row" else TaskRecord
                session.scalars(select(model).with_for_update()).first()
            assert run(db, "verification")["contended"] == 1
            assert totals(db)["email_verifications"] == 1
        assert run(db, "verification")["counts"] == {"email_verifications": 1}
    finally:
        other.close()


@pytest.mark.parametrize("phase", ["documents", "accounts"])
@pytest.mark.parametrize("missing", [False, True])
def test_tombstone_recovery_retries_existing_fence_and_finishes_private_cleanup(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    tmp_path: Path,
    phase: str,
    missing: bool,
) -> None:
    db, clock = isolated_database, FakeClock()
    records, storage, _ = services(db, tmp_path)
    document = upload(db, storage, owners[0])
    shared = register(db, owners[0], runs[0])
    assert register(db, owners[1], runs[1]) == shared
    if phase == "documents":
        records.delete(owners[0], document)
        handler = DocumentCleanup(db, storage, clock.now)
        store = PostgresWorkerStore(
            db, {"document_cleanup": Handler(handler.execute, handler.publish)}
        )
        kind = "document_cleanup"
    else:
        deletion(db, owners[0])
        kind = "account_cleanup"
    with db.transaction() as session:
        task = session.scalars(select(TaskRecord).where(TaskRecord.kind == kind)).one()
        task.status = "failed"
        identity, generation = task.id, task.generation
        if missing:
            session.delete(task)
    clock.sleep(7200)
    assert run(db, phase, clock.now(), dry=True)["counts"] == {"cleanup_tasks": 1}
    assert run(db, phase, clock.now())["counts"] == {"cleanup_tasks": 1}
    assert not run(db, phase, clock.now())["counts"]
    with db.transaction() as session:
        recovered = session.scalars(
            select(TaskRecord).where(TaskRecord.kind == kind)
        ).one()
        assert (
            recovered.generation == (0 if missing else generation + 1)
            and recovered.status == "pending"
            and (recovered.id != identity) == missing
        )
    if phase == "accounts":
        drain(AccountCleanup(db, storage, clock.now), clock)
        with db.transaction() as session:
            assert (
                session.get(UserRecord, owners[0]) is None
                and session.get(UserRecord, owners[1]) is not None
            )
    else:
        claimed = store.claim((kind,), clock.now(), timedelta(seconds=60))
        assert claimed is not None
        store.publish(claimed, handler.execute(claimed), clock.now())
        with db.transaction() as session:
            state = session.get(DocumentDeletionRecord, document)
            assert state is not None and state.completed_at == clock.now()
    assert totals(db)["job_sources"] == 1 and not list(storage.root.glob("*.blob"))


@pytest.mark.parametrize("crash", [False, True])
def test_files_are_bounded_repeatable_and_preserve_live_uploads_and_references(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash: bool,
) -> None:
    db, storage = isolated_database, LocalStorage(tmp_path / "private")
    records = PostgresDocuments(db, StorageSettings(), lambda: NOW)
    document = upload(db, storage, owners[0])
    with db.transaction() as session:
        row = session.get(DocumentRecord, document)
        assert row is not None
        committed = row.storage_key
    active, file = storage.stage()
    try:
        for key in (active, committed):
            os.utime(storage.path(key, ".part" if key == active else ".blob"), (0, 0))
        for index in (1, 2, 3):
            path = storage.path(UUID(int=index).hex, ".part")
            path.touch()
            os.utime(path, (NOW.timestamp(), NOW.timestamp()))
        (storage.root / ("f" * 32 + ".blob")).symlink_to(storage.path(committed))
        when = NOW + timedelta(hours=24)
        assert storage.reap(records, when - timedelta(microseconds=1), hours=24)[0] == 0
        assert storage.reap(records, when, hours=24, dry_run=True)[0] == 3
        if crash:
            unlink = Path.unlink

            def interrupted(path: Path, missing_ok: bool = False) -> None:
                if path.name == UUID(int=2).hex + ".part":
                    raise OSError("disk failure after first unlink")
                unlink(path, missing_ok=missing_ok)

            with monkeypatch.context() as patch:
                patch.setattr(Path, "unlink", interrupted)
                with pytest.raises(OSError):
                    storage.reap(records, when, hours=24)
        after, removed = None, 0
        while True:
            count, scanned, after = storage.reap(
                records, when, hours=24, limit=1, after=after
            )
            assert count <= 1 and scanned <= 1
            removed += count
            if after is None:
                break
        assert (
            removed == 3 - int(crash)
            and storage.path(committed).exists()
            and storage.path(active, ".part").exists()
        )
        assert storage.reap(records, when, hours=24)[0] == 0
    finally:
        file.close()


def test_command_complete_pass_artifact_integration_and_redacted_failure(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = isolated_database
    roots(db, owners[0], runs[0])
    storage = LocalStorage(tmp_path / "private")
    monkeypatch.setenv("JOB_HUNTER_DOCUMENT_ROOT", str(storage.root))
    monkeypatch.setenv("JOB_HUNTER_RETENTION_BATCH_SIZE", "1")
    token, phases = None, set()
    for _ in range(30):
        monkeypatch.setattr(
            sys,
            "argv",
            ["retention", "--apply"] + (["--checkpoint", token] if token else []),
        )
        main()
        result = json.loads(capsys.readouterr().out)
        phases.add(result["phase"])
        token = result["checkpoint"]
        if token is None:
            break
    assert phases == set(PHASES) and token is None and totals(db)["job_snapshots"] == 0
    monkeypatch.setattr(sys, "argv", ["retention", "--checkpoint", "private-invalid"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and "private-invalid" not in capsys.readouterr().err
