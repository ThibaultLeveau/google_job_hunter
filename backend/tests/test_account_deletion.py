from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_account_fencing import DATA, Account
from test_account_fencing import account as account_fixture
from test_discovery import Provider, page
from test_matches import setup as known_job
from test_profiles import HEADERS, NOW, ORIGIN, TOKEN
from test_profiles import owners as profile_owners
from test_review import batch, seed
from test_snapshots import register
from test_worker import FakeClock

from job_hunter.app import create_app
from job_hunter.application.documents import DocumentUploads
from job_hunter.application.oidc import OidcFlow
from job_hunter.application.reauth import ReauthenticationRequired
from job_hunter.application.search_provider import SearchPage, SearchRequest
from job_hunter.application.sessions import token_hash
from job_hunter.application.worker import RetryableTaskError
from job_hunter.domain.accounts import Unauthenticated
from job_hunter.domain.documents import ParseResult
from job_hunter.domain.errors import DomainError
from job_hunter.domain.profiles import Skill
from job_hunter.domain.review import ReviewItem
from job_hunter.domain.tasks import NewTask, StaleLease, Task, TaskError, TaskKey
from job_hunter.infrastructure.account_deletion import (
    CLEANUP,
    AccountCleanup,
    PostgresAccountDeletion,
    personal_tasks,
)
from job_hunter.infrastructure.accounts import (
    AuthIdentityRecord,
    MailDeliveryRecord,
    OidcFlowRecord,
    PostgresSessions,
    PostgresVerification,
    RecoveryDeliveryRecord,
    RecoveryRecord,
    SessionRecord,
    UserRecord,
    VerificationRecord,
    load_mail_settings,
)
from job_hunter.infrastructure.budgets import (
    BudgetBucketRecord,
    BudgetReservationRecord,
    BudgetSettings,
    PostgresBudgets,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.discovery import PostgresDiscovery
from job_hunter.infrastructure.documents import (
    DocumentParsing,
    DocumentRecord,
    LocalStorage,
    ParsedDocumentRecord,
    PostgresDocuments,
    ProposalRecord,
    StorageSettings,
)
from job_hunter.infrastructure.match_events import (
    EVALUATE,
    MatchScanRecord,
    enqueue_scan,
)
from job_hunter.infrastructure.match_scheduling import MatchSchedule
from job_hunter.infrastructure.matches import MatchArtifactRecord
from job_hunter.infrastructure.oidc import PostgresOidc
from job_hunter.infrastructure.parsing import ParseLimits, parse
from job_hunter.infrastructure.profiles import PostgresProfiles, ProfileFactRecord
from job_hunter.infrastructure.recovery import PostgresRecovery
from job_hunter.infrastructure.searches import (
    DISCOVERY,
    PostgresSearches,
    SearchRunRecord,
)
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.infrastructure.worker import Handler, PostgresWorkerStore
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
owners, account = profile_owners, account_fixture
URL = "/api/v1/account/delete"
INTENT = {"intent": "delete_account"}
LEASE = timedelta(seconds=60)


def deletion(db: Database, owner: UUID) -> None:
    PostgresAccountDeletion(db, lambda: NOW, 300).delete(owner, token_hash(TOKEN))


def queue(cleanup: AccountCleanup) -> PostgresWorkerStore:
    return PostgresWorkerStore(cleanup.database, cleanup.handlers())


def claimed(store: PostgresWorkerStore, kind: str, clock: FakeClock) -> Task:
    task = store.claim((kind,), clock.now(), LEASE)
    assert task is not None
    return task


def drain(cleanup: AccountCleanup, clock: FakeClock) -> None:
    for _ in range(20):
        task = queue(cleanup).claim((CLEANUP,), clock.now(), LEASE)
        if task is None:
            return
        queue(cleanup).publish(task, cleanup.execute(task), clock.now())
        clock.sleep(2)
    raise AssertionError("Cleanup did not finish")


def upload(db: Database, storage: LocalStorage, owner: UUID) -> UUID:
    uploads = DocumentUploads(
        PostgresDocuments(db, StorageSettings(), lambda: NOW), storage, 1000
    )
    with uploads.receive("private.txt", "text/plain") as pending:
        pending.write(f"Private Python experience {uuid4()}".encode())
        return uploads.finish(owner, pending)[0].id


@pytest.mark.parametrize(
    "case, status",
    [
        ("anonymous", 401),
        ("csrf", 403),
        ("origin", 403),
        ("stale", 403),
        ("future", 403),
        ("intent", 422),
        ("extra", 422),
        ("query", 422),
        ("unavailable", 503),
    ],
)
def test_api_rejects_without_deleting(
    isolated_database: Database, account: Account, case: str, status: int
) -> None:
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = app.state.reauthentication.now = (
        app.state.account_deletion.now
    ) = lambda: NOW
    headers, body, path = dict(HEADERS), dict(INTENT), URL
    if case == "csrf":
        headers.pop("X-CSRF-Token")
    if case == "origin":
        headers["Origin"] = "https://foreign.example.org"
    if case == "intent":
        body["intent"] = "cancel"
    if case == "extra":
        body["owner_id"] = str(uuid4())
    if case == "query":
        path += "?owner_id=" + str(uuid4())
    if case == "unavailable":
        app.state.account_deletion = None
    if case in {"stale", "future"}:
        with isolated_database.transaction() as session:
            session.execute(
                update(SessionRecord)
                .where(SessionRecord.user_id == account.owner)
                .values(
                    authenticated_at=NOW
                    + timedelta(seconds=-300 if case == "stale" else 1)
                )
            )
    with TestClient(app, base_url=ORIGIN) as client:
        if case != "anonymous":
            client.cookies.set("job_hunter_session", TOKEN)
        result = client.post(path, json=body, headers=headers)
        assert result.status_code == status
        if case in {"stale", "future"}:
            assert result.json()["action"] == "delete_account"
    with isolated_database.transaction() as session:
        owner = session.get(UserRecord, account.owner)
        assert owner is not None and owner.deleted_at is None
        assert (
            session.scalar(select(TaskRecord.id).where(TaskRecord.kind == CLEANUP))
            is None
        )


def test_full_private_purge_preserves_shared_job_and_other_owner(
    isolated_database: Database,
    account: Account,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
) -> None:
    db, clock = isolated_database, FakeClock()
    storage = LocalStorage(tmp_path / "private")
    repository, job, _ = known_job(db, account.owner, account.run)
    document, proposals = seed(db, account.owner, tmp_path)
    profiles = PostgresProfiles(db, clock.now)
    profiles.review(
        account.owner,
        batch(1, ReviewItem(proposal_id=proposals["Python"], action="accept")),
    )
    repository.run(account.owner, job, NOW)
    searches = PostgresSearches(db, clock.now, ())
    saved = searches.save(owners[1], DATA)
    other_run = searches.start(owners[1], saved.id, 1, uuid4())
    assert register(db, owners[1], other_run.id) == job
    profiles.put(owners[1], 0, Skill(name="Python"))
    other_match = repository.run(owners[1], job, NOW)
    oidc = PostgresOidc(db, load_mail_settings().key.get_secret_value())
    for index, owner in enumerate(owners):
        oidc.save(
            str(index),
            "binding",
            token_hash(TOKEN if index == 0 else "b" * 43),
            OidcFlow("nonce", "verifier", "link", str(owner)),
            NOW,
        )
    budget = PostgresBudgets(db, BudgetSettings(), clock.now)
    reservation = uuid4()
    budget.reserve(reservation, "tokens", 10, account.owner, account.run)
    with db.transaction() as session:
        row = session.get(DocumentRecord, document)
        assert row is not None
        storage.path(row.storage_key).write_bytes(b"Private original bytes")
        storage.path(row.storage_key).chmod(0o600)
        PostgresTaskQueue(session).enqueue(
            NewTask(TaskKey("public_fixture", document, 1), document, NOW), NOW
        )
        existing = session.scalars(
            select(SessionRecord).where(SessionRecord.user_id == account.owner)
        ).one()
        session.add(
            SessionRecord(
                id=uuid4(),
                user_id=account.owner,
                token_hash=token_hash("second"),
                authenticated_at=NOW,
                last_seen_at=NOW,
                expires_at=existing.expires_at,
                auth_method="google",
            )
        )
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = app.state.reauthentication.now = (
        app.state.account_deletion.now
    ) = clock.now
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set("job_hunter_session", TOKEN)
        response = client.post(URL, headers=HEADERS, json=INTENT)
        assert response.status_code == 202 and response.json() == {"status": "deleting"}
        assert response.headers["cache-control"] == "no-store"
        assert all(
            "Max-Age=0" in value for value in response.headers.get_list("set-cookie")
        )
        assert client.get("/api/auth/me").status_code == 401
    with db.transaction() as session:
        assert all(
            row.revoked_at == NOW
            for row in session.scalars(
                select(SessionRecord).where(SessionRecord.user_id == account.owner)
            )
        )
        assert session.scalars(select(OidcFlowRecord.state_hash)).all() == ["1"]
    for token in (TOKEN, "second"):
        with pytest.raises(Unauthenticated):
            PostgresSessions(db).current(token_hash(token), NOW)
    assert not PostgresVerification(db, load_mail_settings()).consume(
        account.owner, account.verification, NOW
    )
    assert not PostgresRecovery(db, load_mail_settings()).reset(
        account.recovery, "unused", NOW
    )
    with pytest.raises(DomainError):
        PostgresDocuments(db, StorageSettings(), clock.now).delete(
            account.owner, document
        )
    with db.transaction() as session:
        enqueue_scan(session, account.owner, job, "late", NOW)
    drain(AccountCleanup(db, storage, clock.now), clock)
    budget.settle(reservation, 4)
    with db.transaction() as session:
        assert session.get(UserRecord, account.owner) is None
        assert session.get(UserRecord, owners[1]) is not None
        for record in (
            AuthIdentityRecord,
            VerificationRecord,
            RecoveryRecord,
            MailDeliveryRecord,
            RecoveryDeliveryRecord,
            DocumentRecord,
            ParsedDocumentRecord,
            ProposalRecord,
        ):
            assert session.scalar(select(func.count()).select_from(record)) == 0
        assert session.scalars(select(ProfileFactRecord.owner_id)).all() == [owners[1]]
        assert session.scalars(select(SearchRunRecord.owner_id)).all() == [owners[1]]
        assert session.scalars(select(MatchArtifactRecord.match_id)).all() == [
            other_match.match_id
        ]
        assert (
            session.scalar(
                select(BudgetBucketRecord.subject).where(
                    BudgetBucketRecord.scope != "global"
                )
            )
            is None
        )
        receipt = session.get(BudgetReservationRecord, reservation)
        assert (
            receipt is not None
            and receipt.owner_id is None
            and receipt.run_id is None
            and receipt.charged == 4
        )
        assert (
            session.scalar(
                select(TaskRecord.id).where(TaskRecord.kind == "public_fixture")
            )
            is not None
        )
        assert (
            session.scalar(
                select(MatchScanRecord.id).where(
                    MatchScanRecord.owner_id == account.owner
                )
            )
            is None
        )
    assert not list(storage.root.iterdir())
    assert (
        repository.get(owners[1], other_match.match_id, 1).payload
        == other_match.payload
    )
    assert repository.run(owners[1], job, NOW).match_id == other_match.match_id
    assert PostgresSessions(db).current(token_hash("b" * 43), NOW)


@pytest.mark.parametrize("failure", ["storage", "database", "missing", "disabled"])
def test_cleanup_retry_restart_and_missing_blob(
    isolated_database: Database,
    account: Account,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    db, clock = isolated_database, FakeClock()
    storage = LocalStorage(tmp_path / "private")
    upload(db, storage, account.owner)
    deletion(db, account.owner)
    cleanup = AccountCleanup(db, None if failure == "disabled" else storage, clock.now)
    store = queue(cleanup)
    task = claimed(store, CLEANUP, clock)
    if failure == "missing":
        for file in storage.root.iterdir():
            file.unlink()
    if failure in {"storage", "disabled"}:
        with monkeypatch.context() as patch:
            if failure == "storage":
                patch.setattr(
                    storage,
                    "discard",
                    lambda key: (_ for _ in ()).throw(OSError("private-canary")),
                )
            with pytest.raises(RetryableTaskError):
                cleanup.execute(task)
        store.reject(
            task, TaskError.TRANSIENT, clock.now() + timedelta(seconds=1), clock.now()
        )
        clock.sleep(2)
        cleanup.storage = storage
        task = claimed(store, CLEANUP, clock)
    result = cleanup.execute(task)
    if failure == "database":
        with monkeypatch.context() as patch:

            def crash(
                session: Session, task: Task, result: object, now: datetime
            ) -> None:
                cleanup.publish(session, task, result, now)
                raise SQLAlchemyError("private-canary")

            store.handlers[CLEANUP] = Handler(cleanup.execute, crash)
            with pytest.raises(RetryableTaskError):
                store.publish(task, result, clock.now())
        with db.transaction() as session:
            assert session.get(UserRecord, account.owner) is not None
        clock.sleep(61)
        store = queue(AccountCleanup(db, storage, clock.now))
        store.recover((CLEANUP,), clock.now())
        with pytest.raises(StaleLease):
            store.publish(task, result, clock.now())
        task = claimed(store, CLEANUP, clock)
        result = cleanup.execute(task)
    store.publish(task, result, clock.now())
    drain(cleanup, clock)
    with db.transaction() as session:
        assert session.get(UserRecord, account.owner) is None
    assert not list(storage.root.iterdir())
    assert cleanup.execute(task) is None


def test_bounded_pages_keep_references_until_tasks_and_blobs_are_gone(
    isolated_database: Database, account: Account, tmp_path: Path
) -> None:
    db, clock = isolated_database, FakeClock()
    storage = LocalStorage(tmp_path / "private")
    documents = [upload(db, storage, account.owner) for _ in range(11)]
    with db.transaction() as session:
        for version in range(2, 125):
            PostgresTaskQueue(session).enqueue(
                NewTask(
                    TaskKey("document_parse", documents[0], version), documents[0], NOW
                ),
                NOW,
            )
    deletion(db, account.owner)
    with db.transaction() as session:
        assert (
            session.scalar(
                personal_tasks(account.owner)
                .where(TaskRecord.status == "pending", TaskRecord.kind != CLEANUP)
                .limit(1)
            )
            is not None
        )
    cleanup, steps = AccountCleanup(db, storage, clock.now), 0
    while task := queue(cleanup).claim((CLEANUP,), clock.now(), LEASE):
        result = cleanup.execute(task)
        assert result is not None and len(result) <= 10
        queue(cleanup).publish(task, result, clock.now())
        steps += 1
        assert steps < 10
        clock.sleep(2)
    assert steps >= 3 and not list(storage.root.iterdir())
    with db.transaction() as session:
        assert session.get(UserRecord, account.owner) is None
        assert session.scalar(select(func.count()).select_from(TaskRecord)) == 1


@pytest.mark.parametrize("kind", ["parse", "search", "match"])
def test_inflight_external_work_cannot_recreate_purged_data(
    isolated_database: Database,
    account: Account,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    db, clock = isolated_database, FakeClock()
    storage = LocalStorage(tmp_path / "private")
    entered, release = Event(), Event()

    def blocked() -> None:
        entered.set()
        assert release.wait(timeout=10)

    if kind == "parse":
        upload(db, storage, account.owner)

        def parsing(file: BinaryIO, media: str) -> ParseResult:
            blocked()
            return parse(file, media, ParseLimits())

        parser = DocumentParsing(db, storage, parsing, clock.now)
        handlers, task_kind = parser.handlers(), "document_parse"
    elif kind == "search":
        provider = Provider((page(("https://jobs.example.org/late",)),))
        fetch = provider.fetch

        def fetching(request: SearchRequest) -> SearchPage:
            blocked()
            return fetch(request)

        monkeypatch.setattr(provider, "fetch", fetching)
        handlers, task_kind = (
            PostgresDiscovery(db, provider, clock.now).handlers(),
            DISCOVERY,
        )
    else:
        repository, job, model = known_job(db, account.owner, account.run)
        model.before = blocked
        schedule = MatchSchedule(repository, clock.now)
        assert schedule.request(account.owner, job, uuid4()) is not None
        handlers, task_kind = schedule.handlers(), EVALUATE
    store = PostgresWorkerStore(db, handlers)
    task = claimed(store, task_kind, clock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(handlers[task_kind].execute, task)
        try:
            assert entered.wait(timeout=5)
            deletion(db, account.owner)
            drain(AccountCleanup(db, storage, clock.now), clock)
        finally:
            release.set()
        if kind == "match":
            with pytest.raises(DomainError, match="not_found"):
                future.result(timeout=5)
            result = None
        else:
            result = future.result(timeout=5)
    with pytest.raises(StaleLease):
        store.publish(task, result, clock.now())
    with db.transaction() as session:
        assert session.get(UserRecord, account.owner) is None
        assert session.scalar(select(func.count()).select_from(DocumentRecord)) == 0
        assert session.scalar(select(func.count()).select_from(SearchRunRecord)) == 0
        assert (
            session.scalar(select(func.count()).select_from(MatchArtifactRecord)) == 0
        )


@pytest.mark.parametrize("change", ["stale", "revoked", "foreign", "deleted"])
def test_delete_rechecks_cached_authentication(
    isolated_database: Database,
    account: Account,
    owners: tuple[UUID, UUID],
    change: str,
) -> None:
    owner = owners[1] if change == "foreign" else account.owner
    with isolated_database.transaction() as session:
        if change == "deleted":
            session.execute(
                update(UserRecord).where(UserRecord.id == owner).values(deleted_at=NOW)
            )
        elif change != "foreign":
            session.execute(
                update(SessionRecord)
                .where(SessionRecord.user_id == owner)
                .values(
                    **(
                        {"revoked_at": NOW}
                        if change == "revoked"
                        else {"authenticated_at": NOW - timedelta(seconds=300)}
                    )
                )
            )
    with pytest.raises(
        ReauthenticationRequired if change == "stale" else Unauthenticated
    ):
        deletion(isolated_database, owner)
    with isolated_database.transaction() as session:
        assert (
            session.scalar(select(TaskRecord.id).where(TaskRecord.kind == CLEANUP))
            is None
        )


def test_tombstone_skips_locked_producer_then_blocks_its_publication(
    isolated_database: Database, account: Account
) -> None:
    db, clock = isolated_database, FakeClock()
    stage = PostgresDiscovery(
        db, Provider((page(("https://jobs.example.org/late",)),)), clock.now
    )
    task = claimed(PostgresWorkerStore(db, stage.handlers()), DISCOVERY, clock)
    result = stage.execute(task)
    locked, release = Event(), Event()
    observer = Database(Settings(database_pool_size=1))

    def publishing() -> None:
        with db.transaction() as session:
            PostgresTaskQueue(session).complete(task, NOW)
            locked.set()
            assert release.wait(timeout=5)
            stage.publish(session, task, result, NOW)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(publishing)
            try:
                assert locked.wait(timeout=5)
                deletion(observer, account.owner)
            finally:
                release.set()
            future.result(timeout=5)
        drain(AccountCleanup(db, None, clock.now), clock)
        with db.transaction() as session:
            assert session.get(UserRecord, account.owner) is None
            assert (
                session.scalar(select(func.count()).select_from(SearchRunRecord)) == 0
            )
    finally:
        observer.close()
