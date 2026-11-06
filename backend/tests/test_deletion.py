from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session
from test_extraction import TEXT, golden, stage
from test_profiles import HEADERS, NOW, ORIGIN, TOKEN
from test_profiles import owners as profile_owners
from test_review import batch, seed

from job_hunter.app import create_app
from job_hunter.application.documents import DeletionState, DocumentUploads
from job_hunter.application.sessions import csrf_token
from job_hunter.application.worker import RetryableTaskError
from job_hunter.domain.documents import ParseResult
from job_hunter.domain.profiles import Skill
from job_hunter.domain.review import ReviewItem
from job_hunter.domain.tasks import NewTask, StaleLease, Task, TaskError
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import (
    DocumentCleanup,
    DocumentDeletionRecord,
    DocumentParsing,
    DocumentRecord,
    ExtractionRecord,
    LocalStorage,
    ParsedDocumentRecord,
    PostgresDocuments,
    PostgresExtraction,
    ProposalRecord,
    ProposalSourceRecord,
    StorageSettings,
)
from job_hunter.infrastructure.parsing import ParseLimits, parse
from job_hunter.infrastructure.profiles import PostgresProfiles, ProfileFactRecord
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.infrastructure.worker import PostgresWorkerStore
from job_hunter.settings import Settings

owners = profile_owners
LEASE = timedelta(seconds=60)


def services(
    database: Database, root: Path
) -> tuple[PostgresDocuments, LocalStorage, PostgresWorkerStore]:
    storage = LocalStorage(root / "private")
    jobs = DocumentParsing(
        database,
        storage,
        lambda file, media: parse(file, media, ParseLimits()),
        lambda: NOW,
    )
    return (
        PostgresDocuments(database, StorageSettings(), lambda: NOW),
        storage,
        PostgresWorkerStore(database, jobs.handlers()),
    )


def finish(store: PostgresWorkerStore) -> Task:
    task = store.claim(("document_cleanup",), NOW, LEASE)
    assert task is not None
    result = store.handlers[task.key.kind].execute(task)
    store.publish(task, result, NOW)
    return task


@pytest.mark.integration
def test_owner_api_deletion_keeps_facts_and_removes_all_private_artifacts(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, ids = seed(isolated_database, owners[0], tmp_path)
    records, storage, store = services(isolated_database, tmp_path)
    with isolated_database.transaction() as session:
        row = session.get(DocumentRecord, document)
        assert row is not None
        key = row.storage_key
    storage.path(key).write_bytes(TEXT.encode())
    storage.path(key).chmod(0o600)
    profiles = PostgresProfiles(isolated_database, lambda: NOW)
    accepted = profiles.review(
        owners[0], batch(0, ReviewItem(proposal_id=ids["Python"], action="accept"))
    )
    monkeypatch.setenv("JOB_HUNTER_DOCUMENT_ROOT", str(storage.root))
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = lambda: NOW
    app.state.documents.records.now = lambda: NOW
    with TestClient(app, base_url=ORIGIN) as client:
        path = f"/api/v1/documents/{document}"
        assert client.delete(path, headers=HEADERS).status_code == 401
        client.cookies.set("job_hunter_session", "b" * 43)
        assert (
            client.delete(
                path, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token("b" * 43)}
            ).status_code
            == 404
        )
        client.cookies.set("job_hunter_session", TOKEN)
        assert client.delete(path).status_code == 403
        response = client.delete(path, headers=HEADERS)
        assert response.status_code == 202 and response.json()["status"] == "deleting"
        assert client.delete(path, headers=HEADERS).json() == response.json()
        assert client.get(path).json() == response.json()
        assert client.get(path + "/download").status_code == 404
        assert client.get(path + "/proposals").status_code == 404
        profile = client.get("/api/v1/profile").json()
        assert profile["revision"] == accepted.revision + 1
        fact = profile["facts"][0]
        assert fact["data"]["name"] == "Python" and fact["source_associations"] == []
        assert fact["unavailable_sources"] == [str(document)]
        assert client.get("/api/v1/profile/facts/" + fact["id"]).json() == fact
        finish(store)
        assert client.get(path).json()["status"] == "deleted"
        assert client.delete(path, headers=HEADERS).status_code == 200
        assert client.get("/api/v1/profile").json() == profile
    assert not storage.path(key).exists()
    with isolated_database.transaction() as session:
        for table in (
            ParsedDocumentRecord,
            ExtractionRecord,
            ProposalRecord,
            ProposalSourceRecord,
        ):
            assert session.scalar(select(func.count()).select_from(table)) == 0
        assert session.scalar(select(func.count()).select_from(ProfileFactRecord)) == 1
    assert records.delete(owners[0], document).status == "deleted"


@pytest.mark.integration
def test_storage_failure_db_rollback_restart_and_manual_retry(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, _ = seed(isolated_database, owners[0], tmp_path)
    records, storage, store = services(isolated_database, tmp_path)
    with isolated_database.transaction() as session:
        key = session.scalar(
            select(DocumentRecord.storage_key).where(DocumentRecord.id == document)
        )
        assert key is not None
    storage.path(key).write_bytes(TEXT.encode())
    storage.path(key).chmod(0o600)
    records.delete(owners[0], document)
    task = store.claim(("document_cleanup",), NOW, LEASE)
    assert task is not None
    with monkeypatch.context() as patch:
        patch.setattr(
            storage,
            "discard",
            lambda _key: (_ for _ in ()).throw(OSError("private-canary")),
        )
        with pytest.raises(RetryableTaskError):
            store.handlers[task.key.kind].execute(task)
    store.reject(task, TaskError.EXHAUSTED, None, NOW)
    state = records.status(owners[0], document)
    assert isinstance(state, DeletionState) and state.error == "cleanup_failed"
    assert records.delete(owners[0], document).error is None
    retried = store.claim(("document_cleanup",), NOW, LEASE)
    assert retried is not None and retried.generation == task.generation + 1
    with pytest.raises(StaleLease):
        store.publish(task, True, NOW)
    result = store.handlers[retried.key.kind].execute(retried)
    assert not storage.path(key).exists()
    original = Session.flush

    def failed(session: Session, objects: object = None) -> None:
        changed = any(isinstance(row, DocumentDeletionRecord) for row in session.dirty)
        original(session)
        if changed:
            raise SQLAlchemyError("rollback fixture")

    with monkeypatch.context() as patch:
        patch.setattr(Session, "flush", failed)
        with pytest.raises(RetryableTaskError):
            store.publish(retried, result, NOW)
    assert records.status(owners[0], document).status == "deleting"
    later = NOW + timedelta(seconds=61)
    store.recover(("document_cleanup",), later)
    resumed = store.claim(("document_cleanup",), later, LEASE)
    assert resumed is not None
    cleanup = DocumentCleanup(isolated_database, storage, lambda: later)
    assert cleanup.execute(resumed) is True
    store.publish(resumed, True, later)
    assert records.status(owners[0], document).status == "deleted"


@pytest.mark.integration
def test_cleanup_is_bounded_and_resumes_at_the_committed_step(
    isolated_database: Database, owners: tuple[UUID, UUID], tmp_path: Path
) -> None:
    document, _ = seed(isolated_database, owners[0], tmp_path)
    records, _, store = services(isolated_database, tmp_path)
    with isolated_database.transaction() as session:
        proposal = session.scalars(select(ProposalRecord)).first()
        assert proposal is not None
        session.add_all(
            ProposalRecord(
                id=uuid4(),
                extraction_id=proposal.extraction_id,
                ordinal=index,
                data=proposal.data,
            )
            for index in range(4, 106)
        )
        parsed = session.scalars(select(ParsedDocumentRecord)).one()
        session.add_all(
            ParsedDocumentRecord(
                id=uuid4(),
                document_id=document,
                document_version=1,
                parser_version=version,
                source_hash=parsed.source_hash,
                content_hash=parsed.content_hash,
                data=parsed.data,
                created_at=NOW,
            )
            for version in range(2, 12)
        )
    records.delete(owners[0], document)
    first = finish(store)
    assert records.status(owners[0], document).status == "deleting"
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(ProposalRecord)) == 6
    records.delete(owners[0], document)
    _, _, restarted = services(isolated_database, tmp_path)
    second = finish(restarted)
    assert second.key.subject_version == first.key.subject_version + 1
    with isolated_database.transaction() as session:
        assert (
            session.scalar(select(func.count()).select_from(ParsedDocumentRecord)) == 1
        )
    assert records.status(owners[0], document).status == "deleting"
    finish(restarted)
    assert records.status(owners[0], document).status == "deleted"


@pytest.mark.integration
def test_deletion_preserves_other_document_evidence_and_manual_content(
    isolated_database: Database, owners: tuple[UUID, UUID], tmp_path: Path
) -> None:
    first, first_ids = seed(isolated_database, owners[0], tmp_path)
    second, second_ids = seed(isolated_database, owners[0], tmp_path)
    profiles = PostgresProfiles(isolated_database, lambda: NOW)
    profiles.review(
        owners[0],
        batch(
            0,
            ReviewItem(proposal_id=first_ids["Python"], action="accept"),
            ReviewItem(proposal_id=second_ids["Python"], action="accept"),
        ),
    )
    before = profiles.read(owners[0])
    records, _, store = services(isolated_database, tmp_path)
    records.delete(owners[0], first)
    finish(store)
    after = profiles.read(owners[0])
    assert after.revision == before.revision + 1
    assert after.facts[0].data == before.facts[0].data
    assert after.facts[0].unavailable_sources == (first,)
    assert {source.document_id for source in after.facts[0].source_associations} == {
        second
    }
    assert all(
        source.quote == "Python" for source in after.facts[0].source_associations
    )


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["document_parse", "extraction"])
def test_inflight_work_cannot_republish_after_cleanup(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    records, storage, _ = services(isolated_database, tmp_path)
    uploads = DocumentUploads(records, storage, 10000)
    with uploads.receive("resume", "text/plain") as pending:
        pending.write(TEXT.encode())
        document, _ = uploads.finish(owners[0], pending)
    parsed = parse(BytesIO(TEXT.encode()), "text/plain", ParseLimits())
    extraction = PostgresExtraction(
        isolated_database, stage(golden(parsed).model_dump_json()), lambda: NOW
    )
    entered, release = Event(), Event()

    def parser(_file: object, _type: str) -> ParseResult:
        entered.set()
        assert release.wait(10)
        return parsed

    jobs = DocumentParsing(
        isolated_database, storage, parser, lambda: NOW, extraction.enqueue
    )
    store = PostgresWorkerStore(
        isolated_database, {**jobs.handlers(), **extraction.handlers()}
    )
    task = store.claim(("document_parse",), NOW, LEASE)
    assert task is not None
    if kind == "extraction":
        store.publish(task, parsed, NOW)
        task = store.claim((extraction.stage.kind,), NOW, LEASE)
        assert task is not None
        original = extraction.stage.execute

        def blocked(source: ParseResult) -> object:
            parser(None, "")
            return original(source)

        monkeypatch.setattr(extraction.stage, "execute", blocked)
    profiles = PostgresProfiles(isolated_database, lambda: NOW)
    profiles.put(owners[0], 0, Skill(name="Manual fact"))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(store.handlers[task.key.kind].execute, task)
        assert entered.wait(10)
        try:
            records.delete(owners[0], document.id)
            finish(store)
        finally:
            release.set()
        with pytest.raises(StaleLease):
            store.publish(task, future.result(10), NOW)
    with isolated_database.transaction() as session:
        assert (
            session.scalar(select(TaskRecord.status).where(TaskRecord.id == task.id))
            == "cancelled"
        )
        assert (
            session.scalar(select(func.count()).select_from(ParsedDocumentRecord)) == 0
        )
    assert len(profiles.read(owners[0]).facts) == 1 and not list(storage.root.iterdir())
    assert extraction.backfill() == 0


@pytest.mark.integration
def test_backfill_serializes_with_deletion(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, _ = seed(isolated_database, owners[0], tmp_path)
    database = Database(
        Settings(
            database_url=SecretStr(
                isolated_database.engine.url.render_as_string(hide_password=False)
            ),
            database_pool_size=2,
        )
    )
    extraction = PostgresExtraction(database, stage('{"candidates":[]}'), lambda: NOW)
    extraction.stage.kind = "profile_extract_backfill_fixture"
    records = PostgresDocuments(database, StorageSettings(), lambda: NOW)
    entered, release = Event(), Event()
    original = extraction.enqueue

    def blocked(session: Session, parsed: ParsedDocumentRecord, now: object) -> None:
        entered.set()
        assert release.wait(10)
        original(session, parsed, NOW)

    monkeypatch.setattr(extraction, "enqueue", blocked)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            filling = executor.submit(extraction.backfill)
            assert entered.wait(10)
            deleting = executor.submit(records.delete, owners[0], document)
            try:
                with (
                    pytest.raises(OperationalError),
                    isolated_database.transaction() as session,
                ):
                    session.scalar(
                        select(TaskRecord)
                        .where(
                            TaskRecord.kind == "document_parse",
                            TaskRecord.payload_ref == document,
                        )
                        .with_for_update(nowait=True)
                    )
            finally:
                release.set()
            assert filling.result(10) == 1 and deleting.result(10).status == "deleting"
        with isolated_database.transaction() as session:
            assert (
                session.scalar(
                    select(TaskRecord.status).where(
                        TaskRecord.kind == extraction.stage.kind
                    )
                )
                == "cancelled"
            )
        assert extraction.backfill() == 0
    finally:
        database.close()


@pytest.mark.integration
def test_tombstone_cancellation_and_enqueue_roll_back_together(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records, storage, _ = services(isolated_database, tmp_path)
    uploads = DocumentUploads(records, storage, 10000)
    with uploads.receive("resume", "text/plain") as pending:
        pending.write(TEXT.encode())
        document, _ = uploads.finish(owners[0], pending)
    original = PostgresTaskQueue.enqueue

    def failed(self: PostgresTaskQueue, task: NewTask, now: datetime) -> Task:
        original(self, task, now)
        raise SQLAlchemyError("enqueue fixture")

    monkeypatch.setattr(PostgresTaskQueue, "enqueue", failed)
    with pytest.raises(SQLAlchemyError):
        records.delete(owners[0], document.id)
    assert records.status(owners[0], document.id).status == "queued"
    with isolated_database.transaction() as session:
        task = session.scalars(select(TaskRecord)).one()
        assert task.status == "pending" and task.generation == 0
        assert (
            session.scalar(select(func.count()).select_from(DocumentDeletionRecord))
            == 0
        )
    assert len(list(storage.root.glob("*.blob"))) == 1
