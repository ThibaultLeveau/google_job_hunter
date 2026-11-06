from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from test_health import MemoryMail
from test_oidc import OidcTransport
from test_oidc import service as google_service
from test_profiles import NOW, ORIGIN, TOKEN
from test_profiles import owners as profile_owners

from job_hunter.app import create_app
from job_hunter.application.documents import DocumentUploads
from job_hunter.application.oidc import ISSUER, GoogleIdentity, Intent, OidcFlow
from job_hunter.application.searches import SearchWrite
from job_hunter.application.sessions import Credential, token_hash
from job_hunter.domain.accounts import Unauthenticated
from job_hunter.domain.errors import DomainError
from job_hunter.infrastructure.accounts import (
    DUMMY_HASH,
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
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import (
    DocumentRecord,
    LocalStorage,
    PostgresDocuments,
    StorageSettings,
)
from job_hunter.infrastructure.oidc import PostgresOidc
from job_hunter.infrastructure.reauth import PostgresProofs
from job_hunter.infrastructure.recovery import PostgresRecovery
from job_hunter.infrastructure.searches import (
    PostgresSearches,
    SavedSearchRecord,
    SearchRunRecord,
)
from job_hunter.infrastructure.tasks import TaskRecord
from job_hunter.infrastructure.worker import PostgresWorkerStore
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
owners = profile_owners
LATER = NOW + timedelta(seconds=1)
DATA = SearchWrite.model_validate(
    {"name": "Private", "definition": {"groups": [["Python"]]}}
)


@dataclass(frozen=True)
class Account:
    owner: UUID
    credential: Credential
    verification: str
    recovery: str
    search: UUID
    run_key: UUID


@pytest.fixture
def account(isolated_database: Database, owners: tuple[UUID, UUID]) -> Account:
    owner = owners[0]
    credential = Credential(uuid4(), owner, DUMMY_HASH)
    verification = PostgresVerification(isolated_database, load_mail_settings())
    with isolated_database.transaction() as session:
        session.execute(
            update(UserRecord)
            .where(UserRecord.id == owner)
            .values(email_verified_at=None)
        )
        session.add_all(
            [
                AuthIdentityRecord(
                    id=credential.identity_id,
                    user_id=owner,
                    issuer="password",
                    subject=str(owner),
                    password_hash=DUMMY_HASH,
                    created_at=NOW,
                ),
                AuthIdentityRecord(
                    id=uuid4(),
                    user_id=owner,
                    issuer=ISSUER,
                    subject="existing-sub",
                    created_at=NOW,
                ),
            ]
        )
        verification.enqueue(session, owner, NOW)
    PostgresRecovery(isolated_database, load_mail_settings()).request(
        f"{owner}@example.org", NOW
    )
    searches = PostgresSearches(isolated_database, lambda: NOW, ())
    saved, key = searches.save(owner, DATA), uuid4()
    searches.start(owner, saved.id, 1, key)
    with isolated_database.transaction() as session:
        verification_hash = session.scalars(select(VerificationRecord.token_hash)).one()
        recovery_hash = session.scalars(select(RecoveryRecord.token_hash)).one()
    return Account(owner, credential, verification_hash, recovery_hash, saved.id, key)


def tombstone(database: Database, owner: UUID) -> None:
    with database.transaction() as session:
        session.execute(
            update(UserRecord).where(UserRecord.id == owner).values(deleted_at=LATER)
        )


def snapshot(database: Database) -> list[object]:
    with database.transaction() as session:
        return [
            session.execute(select(record.__table__).order_by(record.id)).all()
            for record in (
                AuthIdentityRecord,
                SessionRecord,
                VerificationRecord,
                MailDeliveryRecord,
                RecoveryRecord,
                RecoveryDeliveryRecord,
                OidcFlowRecord,
                DocumentRecord,
                SavedSearchRecord,
                SearchRunRecord,
                TaskRecord,
            )
        ]


def rejected_operation(name: str, database: Database, account: Account) -> None:
    owner, digest = account.owner, token_hash(TOKEN)
    sessions = PostgresSessions(database)
    proofs = PostgresProofs(database)
    oidc = PostgresOidc(database, load_mail_settings().key.get_secret_value())
    verification = PostgresVerification(database, load_mail_settings())
    recovery = PostgresRecovery(database, load_mail_settings())
    searches = PostgresSearches(database, lambda: LATER, ())
    calls: dict[str, Callable[[], object]] = {
        "rotate": lambda: sessions.rotate(
            account.credential,
            digest,
            token_hash("new"),
            LATER,
            LATER + timedelta(days=1),
        ),
        "current": lambda: sessions.current(digest, LATER),
        "password_proof": lambda: proofs.password(account.credential, digest, LATER),
        "google_proof": lambda: proofs.google(
            owner,
            digest,
            GoogleIdentity("existing-sub", "public@example.org", LATER),
            "reauthenticate",
            LATER,
            300,
        ),
        "google_link": lambda: proofs.google(
            owner,
            digest,
            GoogleIdentity("new-sub", "public@example.org"),
            "link",
            LATER,
            300,
        ),
        "google_sign_in": lambda: oidc.sign_in(
            GoogleIdentity("existing-sub", "public@example.org"),
            token_hash("new"),
            digest,
            LATER,
        ),
        "flow": lambda: oidc.save(
            "state",
            "binding",
            digest,
            OidcFlow("nonce", "verifier", "link", str(owner)),
            LATER,
        ),
        "resend": lambda: verification.resend(owner, LATER),
        "verify": lambda: verification.consume(owner, account.verification, LATER),
        "request_reset": lambda: recovery.request(f"{owner}@example.org", LATER),
        "reset": lambda: recovery.reset(account.recovery, DUMMY_HASH, LATER),
        "upload": lambda: PostgresDocuments(
            database, StorageSettings(), lambda: LATER
        ).add(owner, uuid4().hex, "a" * 64, "resume.txt", "text/plain", 10),
        "save_search": lambda: searches.save(owner, DATA),
        "edit_search": lambda: searches.save(owner, DATA, account.search, 1),
        "delete_search": lambda: searches.delete(owner, account.search, 1),
        "start_search": lambda: searches.start(owner, account.search, 1, uuid4()),
        "replay_search": lambda: searches.start(
            owner, account.search, 1, account.run_key
        ),
    }
    if name in {"resend", "request_reset"}:
        assert calls[name]() is None
    elif name in {"verify", "reset"}:
        assert calls[name]() is False
    elif name in {
        "upload",
        "save_search",
        "edit_search",
        "delete_search",
        "start_search",
        "replay_search",
    }:
        with pytest.raises(DomainError, match="not_found"):
            calls[name]()
    else:
        with pytest.raises(Unauthenticated):
            calls[name]()


@pytest.mark.parametrize(
    "name",
    [
        "rotate",
        "current",
        "password_proof",
        "google_proof",
        "google_link",
        "google_sign_in",
        "flow",
        "resend",
        "verify",
        "request_reset",
        "reset",
        "upload",
        "save_search",
        "edit_search",
        "delete_search",
        "start_search",
        "replay_search",
    ],
)
def test_waiting_operations_recheck_committed_tombstone(
    isolated_database: Database,
    account: Account,
    name: str,
) -> None:
    before = snapshot(isolated_database)
    database, entered = Database(Settings(database_pool_size=1)), Event()
    observer = Database(Settings(database_pool_size=1))
    pid: list[int] = []

    def delayed() -> None:
        with database.transaction() as session:
            pid.append(session.scalars(text("SELECT pg_backend_pid()")).one())
        entered.set()
        rejected_operation(name, database, account)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with isolated_database.transaction() as session:
                session.execute(
                    update(UserRecord)
                    .where(UserRecord.id == account.owner)
                    .values(deleted_at=LATER)
                )
                future = pool.submit(delayed)
                assert entered.wait(timeout=5)
                deadline = monotonic() + 1.5
                while True:
                    with observer.transaction() as control:
                        blocked = control.scalar(
                            text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                            {"pid": pid[0]},
                        )
                    if blocked:
                        break
                    assert monotonic() < deadline, (
                        "Operation did not wait for the account lock"
                    )
                    sleep(0.005)
            future.result(timeout=5)
    finally:
        database.close()
        observer.close()
    assert snapshot(isolated_database) == before
    assert PostgresSessions(isolated_database).current(token_hash("b" * 43), LATER)


def test_tombstoned_credentials_methods_mail_and_api_are_unavailable(
    isolated_database: Database,
    account: Account,
) -> None:
    sessions, proofs = (
        PostgresSessions(isolated_database),
        PostgresProofs(isolated_database),
    )
    email = f"{account.owner}@example.org"
    assert sessions.credential(email) == account.credential
    assert proofs.credential(account.owner) == account.credential
    assert proofs.methods(account.owner) == ["password", "google"]
    mail = MemoryMail(isolated_database)
    handlers = PostgresVerification(isolated_database, load_mail_settings()).handlers(
        mail, lambda: LATER
    )
    handlers.update(
        PostgresRecovery(isolated_database, load_mail_settings()).handlers(
            mail, lambda: LATER
        )
    )
    worker = PostgresWorkerStore(isolated_database, handlers)
    tasks = [
        worker.claim((kind,), LATER, timedelta(seconds=60))
        for kind in ("verification_mail", "recovery_mail")
    ]
    tombstone(isolated_database, account.owner)
    assert sessions.credential(email) is None
    assert proofs.credential(account.owner) is None
    assert proofs.methods(account.owner) == []
    for task in tasks:
        assert task is not None
        handlers[task.key.kind].execute(task)
    assert not mail.messages and mail.attempts == 0
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = lambda: LATER
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set("job_hunter_session", TOKEN)
        assert client.get("/api/auth/me").status_code == 401
        response = client.post(
            "/api/auth/forgot-password",
            headers={"Origin": ORIGIN},
            json={"email": email},
        )
        assert response.status_code == 202 and response.json() == {"status": "accepted"}
    with isolated_database.transaction() as session:
        user = session.get(UserRecord, account.owner)
        assert user is not None and user.email_verified_at is None


@pytest.mark.parametrize("change", ["revoked", "expired", "foreign", "missing"])
def test_bound_flow_requires_current_owner_session(
    isolated_database: Database,
    account: Account,
    change: str,
) -> None:
    oidc = PostgresOidc(isolated_database, load_mail_settings().key.get_secret_value())
    digest = token_hash(TOKEN)
    if change in {"revoked", "expired"}:
        with isolated_database.transaction() as session:
            session.execute(
                update(SessionRecord)
                .where(SessionRecord.user_id == account.owner)
                .values(
                    **(
                        {"revoked_at": LATER}
                        if change == "revoked"
                        else {"expires_at": LATER}
                    )
                )
            )
    else:
        digest = token_hash("b" * 43 if change == "foreign" else "missing")
    with pytest.raises(Unauthenticated):
        oidc.save(
            "state",
            "binding",
            digest,
            OidcFlow("nonce", "verifier", "reauthenticate", str(account.owner)),
            LATER,
        )
    with isolated_database.transaction() as session:
        assert session.scalar(select(OidcFlowRecord.id)) is None
    # Anonymous starts remain valid even with an expired or unknown previous cookie.
    flow = OidcFlow("nonce", "verifier")
    oidc.save("anonymous", "binding", digest, flow, LATER)
    assert oidc.take("anonymous", "binding", digest, LATER) == flow


def test_upload_finishing_after_tombstone_discards_published_blob(
    isolated_database: Database,
    account: Account,
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path / "private")
    uploads = DocumentUploads(
        PostgresDocuments(isolated_database, StorageSettings(), lambda: LATER),
        storage,
        1000,
    )
    before = snapshot(isolated_database)
    with pytest.raises(DomainError, match="not_found"):
        with uploads.receive("resume.txt", "text/plain") as upload:
            upload.write(b"Private professional experience")
            tombstone(isolated_database, account.owner)
            uploads.finish(account.owner, upload)
    with pytest.raises(FileNotFoundError):
        storage.open(upload.key)
    assert snapshot(isolated_database) == before


@pytest.mark.parametrize("intent", ["sign_in", "link", "reauthenticate"])
def test_google_exchange_finishes_after_tombstone_without_restoring_identity(
    isolated_database: Database,
    account: Account,
    intent: Intent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from job_hunter.application.reauth import Reauthentication

    provider = OidcTransport()
    provider.claims.update(sub="existing-sub", auth_time=int(NOW.timestamp()))
    google = google_service(isolated_database, provider)
    proof = Reauthentication(
        PostgresProofs(isolated_database), google, lambda h, p: True, lambda: NOW, 300
    )
    user = PostgresSessions(isolated_database).current(token_hash(TOKEN), NOW)
    if intent == "sign_in":
        url, binding = google.start(TOKEN)
    else:
        url, binding = proof.start(user, TOKEN, intent, "settings")
    state, code = provider.authorize(url)
    exchange = google.provider.exchange

    def late_exchange(code: str, flow: OidcFlow, now: datetime) -> GoogleIdentity:
        identity = exchange(code, flow, now)
        tombstone(isolated_database, account.owner)
        return identity

    monkeypatch.setattr(google.provider, "exchange", late_exchange)
    with pytest.raises(Unauthenticated):
        if intent == "sign_in":
            google.finish(state, binding, TOKEN, code, "")
        else:
            proof.finish(user, TOKEN, intent, state, binding, code, "")
    assert provider.exchanges == 1
    with isolated_database.transaction() as session:
        deleted = session.get(UserRecord, account.owner)
        assert deleted is not None and deleted.deleted_at == LATER
        assert (
            len(
                session.scalars(
                    select(SessionRecord).where(SessionRecord.user_id == account.owner)
                ).all()
            )
            == 1
        )
        assert (
            len(
                session.scalars(
                    select(AuthIdentityRecord).where(
                        AuthIdentityRecord.user_id == account.owner
                    )
                ).all()
            )
            == 2
        )
        assert session.scalar(select(OidcFlowRecord.id)) is None
