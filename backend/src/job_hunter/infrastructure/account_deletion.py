from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Select, delete, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from job_hunter.application.account_deletion import AccountDeletion
from job_hunter.application.documents import PrivateStorage
from job_hunter.application.reauth import ReauthenticationRequired, is_recent
from job_hunter.application.worker import RetryableTaskError
from job_hunter.domain.accounts import Unauthenticated
from job_hunter.domain.tasks import NewTask, Task, TaskKey
from job_hunter.infrastructure.accounts import (
    OidcFlowRecord,
    RecoveryRecord,
    SessionRecord,
    UserRecord,
    VerificationRecord,
    live_user,
)
from job_hunter.infrastructure.budgets import (
    BudgetBucketRecord,
    BudgetReservationRecord,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import (
    DocumentDeletionRecord,
    DocumentRecord,
    ParsedDocumentRecord,
)
from job_hunter.infrastructure.match_events import EVALUATE, SCAN, MatchScanRecord
from job_hunter.infrastructure.matches import MatchIdentityRecord
from job_hunter.infrastructure.profiles import ProfileRecord
from job_hunter.infrastructure.reauth import PostgresProofs
from job_hunter.infrastructure.searches import DISCOVERY, SearchRunRecord
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.infrastructure.worker import Handler

CLEANUP = "account_cleanup"


def personal_tasks(owner: UUID) -> Select[TaskRecord]:
    documents = (
        select(DocumentRecord.id)
        .where(DocumentRecord.owner_id == owner)
        .union(
            select(DocumentDeletionRecord.document_id).where(
                DocumentDeletionRecord.owner_id == owner
            )
        )
    )
    parsed = select(ParsedDocumentRecord.id).where(
        ParsedDocumentRecord.document_id.in_(documents)
    )
    references = (
        (TaskRecord.kind.in_(("document_parse", "document_cleanup")), documents),
        (TaskRecord.kind.startswith("profile_extract_", autoescape=True), parsed),
        (
            TaskRecord.kind == DISCOVERY,
            select(SearchRunRecord.id).where(SearchRunRecord.owner_id == owner),
        ),
        (
            TaskRecord.kind == EVALUATE,
            select(MatchIdentityRecord.id).where(MatchIdentityRecord.owner_id == owner),
        ),
        (
            TaskRecord.kind == SCAN,
            select(MatchScanRecord.id).where(MatchScanRecord.owner_id == owner),
        ),
        (
            TaskRecord.kind.in_(("verification_mail", "verification_expiry")),
            select(VerificationRecord.id).where(VerificationRecord.user_id == owner),
        ),
        (
            TaskRecord.kind.in_(("recovery_mail", "recovery_expiry")),
            select(RecoveryRecord.id).where(RecoveryRecord.user_id == owner),
        ),
    )
    return select(TaskRecord).where(
        or_(
            *(
                kind & TaskRecord.payload_ref.in_(subjects)
                for kind, subjects in references
            ),
            (TaskRecord.kind == CLEANUP) & (TaskRecord.payload_ref == owner),
        )
    )


def erase_flows(session: Session, owner: UUID) -> None:
    session.execute(
        delete(OidcFlowRecord).where(
            OidcFlowRecord.previous_hash.in_(
                select(SessionRecord.token_hash).where(SessionRecord.user_id == owner)
            )
        )
    )


class PostgresAccountDeletion(AccountDeletion):
    def __init__(
        self, database: Database, now: Callable[[], datetime], seconds: int
    ) -> None:
        self.database, self.now, self.seconds = database, now, seconds

    def delete(self, owner: UUID, session_digest: str) -> None:
        now = self.now()
        with self.database.transaction() as session:
            user = live_user(session, owner)
            if user is None:
                raise Unauthenticated()
            proof = PostgresProofs.active(session, owner, session_digest, now)
            if not is_recent(proof.authenticated_at, now, self.seconds):
                raise ReauthenticationRequired("delete_account")
            user.deleted_at = now
            session.execute(
                update(SessionRecord)
                .where(SessionRecord.user_id == owner)
                .values(revoked_at=now)
            )
            erase_flows(session, owner)
            # A producer may already hold its task and be waiting for this user lock.
            for task in session.scalars(
                personal_tasks(owner)
                .where(TaskRecord.status.in_(("pending", "running", "failed")))
                .limit(100)
                .with_for_update(skip_locked=True)
            ):
                task.status, task.updated_at, task.error_code = "cancelled", now, None
                task.generation += 1
                task.lease_token, task.lease_expires_at = uuid4(), None
            PostgresTaskQueue(session).enqueue(
                NewTask(TaskKey(CLEANUP, owner, 1), owner, now, max_attempts=10), now
            )


class AccountCleanup:
    def __init__(
        self,
        database: Database,
        storage: PrivateStorage | None,
        now: Callable[[], datetime],
    ) -> None:
        self.database, self.storage, self.now = database, storage, now

    def execute(self, task: Task) -> tuple[UUID, ...] | None:
        try:
            with self.database.transaction() as session:
                live = session.scalar(
                    select(UserRecord.id).where(
                        UserRecord.id == task.payload_ref,
                        UserRecord.id == task.key.subject_id,
                        UserRecord.deleted_at.is_not(None),
                        select(TaskRecord.id)
                        .where(
                            TaskRecord.id == task.id,
                            TaskRecord.status == "running",
                            TaskRecord.generation == task.generation,
                            TaskRecord.lease_token == task.lease_token,
                            TaskRecord.lease_expires_at > self.now(),
                        )
                        .exists(),
                    )
                )
                if live is None:
                    return None
                rows = session.execute(
                    select(DocumentRecord.id, DocumentRecord.storage_key)
                    .where(DocumentRecord.owner_id == live)
                    .order_by(DocumentRecord.id)
                    .limit(10)
                ).all()
            if rows and self.storage is None:
                raise RetryableTaskError()
            for _, key in rows:
                assert self.storage is not None
                self.storage.discard(key)
            return tuple(row[0] for row in rows)
        except OSError, SQLAlchemyError:
            raise RetryableTaskError() from None

    def publish(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        if not isinstance(result, tuple):
            return
        # Yield to in-flight producers using task/child/user ordering.
        session.execute(text("SET LOCAL lock_timeout = '100ms'"))
        owner = session.scalar(
            select(UserRecord.id)
            .where(
                UserRecord.id == task.payload_ref,
                UserRecord.id == task.key.subject_id,
                UserRecord.deleted_at.is_not(None),
            )
            .with_for_update()
        )
        if owner is None:
            return
        tasks = personal_tasks(owner).where(TaskRecord.id != task.id)
        obsolete = list(
            session.scalars(
                tasks.order_by(TaskRecord.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        )
        for row in obsolete:
            session.delete(row)
        session.flush()
        pending = session.scalar(tasks.limit(1)) is not None
        if not pending:
            session.execute(
                delete(ProfileRecord).where(ProfileRecord.owner_id == owner)
            )
            session.execute(
                delete(DocumentRecord).where(
                    DocumentRecord.owner_id == owner, DocumentRecord.id.in_(result)
                )
            )
            pending = (
                session.scalar(
                    select(DocumentRecord.id)
                    .where(DocumentRecord.owner_id == owner)
                    .limit(1)
                )
                is not None
            )
        if pending:
            PostgresTaskQueue(session).enqueue(
                NewTask(
                    TaskKey(CLEANUP, owner, task.key.subject_version + 1),
                    owner,
                    now + timedelta(seconds=1),
                    max_attempts=10,
                ),
                now,
            )
            return
        runs = select(SearchRunRecord.id).where(SearchRunRecord.owner_id == owner)
        # Sever private references before removing buckets; keep global accounting.
        session.execute(
            update(BudgetReservationRecord)
            .where(BudgetReservationRecord.owner_id == owner)
            .values(owner_id=None, run_id=None)
        )
        session.execute(
            delete(BudgetBucketRecord).where(
                or_(
                    (BudgetBucketRecord.scope == "owner")
                    & (BudgetBucketRecord.subject == owner),
                    (BudgetBucketRecord.scope == "run")
                    & BudgetBucketRecord.subject.in_(runs),
                )
            )
        )
        erase_flows(session, owner)
        session.execute(
            delete(UserRecord).where(
                UserRecord.id == owner, UserRecord.deleted_at.is_not(None)
            )
        )

    def handlers(self) -> dict[str, Handler]:
        return {CLEANUP: Handler(self.execute, self.publish)}
