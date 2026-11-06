import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from job_hunter.application.accounts import Mail
from job_hunter.application.recovery import RecoveryStore
from job_hunter.application.sessions import token_hash
from job_hunter.application.worker import PermanentTaskError, RetryableTaskError
from job_hunter.domain.tasks import NewTask, Task, TaskKey, require_utc
from job_hunter.infrastructure.accounts import (
    AuthIdentityRecord,
    MailSettings,
    RecoveryDeliveryRecord,
    RecoveryRecord,
    SessionRecord,
    UserRecord,
    live_user,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.tasks import PostgresTaskQueue
from job_hunter.infrastructure.worker import Handler


class PostgresRecovery(RecoveryStore):
    def __init__(self, database: Database, settings: MailSettings) -> None:
        self.database, self.settings = database, settings
        self.cipher = Fernet(settings.key.get_secret_value())

    def request(self, email: str, now: datetime) -> None:
        require_utc(now)
        with self.database.transaction() as session:
            owner = session.scalar(
                select(UserRecord.id)
                .where(
                    UserRecord.email_normalized == email,
                    UserRecord.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if owner is None:
                return
            identity = session.scalar(
                select(AuthIdentityRecord)
                .where(
                    AuthIdentityRecord.user_id == owner,
                    AuthIdentityRecord.issuer == "password",
                )
                .with_for_update(of=AuthIdentityRecord)
            )
            if identity is None or identity.password_hash is None:
                return
            session.execute(
                delete(RecoveryRecord).where(RecoveryRecord.user_id == identity.user_id)
            )
            token, record_id = secrets.token_urlsafe(32), uuid4()
            expires = now + timedelta(hours=1)
            session.add(
                RecoveryRecord(
                    id=record_id,
                    user_id=identity.user_id,
                    identity_id=identity.id,
                    token_hash=token_hash(token),
                    credential_hash=token_hash(identity.password_hash),
                    created_at=now,
                    expires_at=expires,
                )
            )
            session.flush()
            session.add(
                RecoveryDeliveryRecord(
                    id=record_id,
                    encrypted_token=self.cipher.encrypt_at_time(
                        token.encode(), int(now.timestamp())
                    ),
                )
            )
            for kind, run_at in (("recovery_mail", now), ("recovery_expiry", expires)):
                PostgresTaskQueue(session).enqueue(
                    NewTask(TaskKey(kind, record_id, 1), record_id, run_at), now
                )

    def reset(self, digest: str, password_hash: str, now: datetime) -> bool:
        require_utc(now)
        with self.database.transaction() as session:
            record = session.scalar(
                select(RecoveryRecord).where(
                    RecoveryRecord.token_hash == digest, RecoveryRecord.expires_at > now
                )
            )
            if record is None or live_user(session, record.user_id) is None:
                return False
            # Match login's owner, identity, then session lock order.
            identity = session.scalar(
                select(AuthIdentityRecord)
                .where(
                    AuthIdentityRecord.id == record.identity_id,
                    AuthIdentityRecord.user_id == record.user_id,
                    AuthIdentityRecord.issuer == "password",
                )
                .with_for_update()
            )
            if (
                identity is None
                or identity.password_hash is None
                or token_hash(identity.password_hash) != record.credential_hash
            ):
                return False
            consumed = session.scalar(
                delete(RecoveryRecord)
                .where(
                    RecoveryRecord.id == record.id,
                    RecoveryRecord.token_hash == digest,
                    RecoveryRecord.expires_at > now,
                )
                .returning(RecoveryRecord.id)
            )
            if consumed is None:
                return False
            identity.password_hash = password_hash
            session.execute(
                update(SessionRecord)
                .where(
                    SessionRecord.user_id == record.user_id,
                    SessionRecord.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            return True

    def deliver(self, task: Task, mail: Mail, now: datetime) -> None:
        try:
            with self.database.transaction() as session:
                row = session.execute(
                    select(
                        UserRecord.email,
                        RecoveryDeliveryRecord.encrypted_token,
                        RecoveryRecord.credential_hash,
                        AuthIdentityRecord.password_hash,
                    )
                    .join(RecoveryRecord, RecoveryRecord.user_id == UserRecord.id)
                    .join(
                        AuthIdentityRecord,
                        AuthIdentityRecord.id == RecoveryRecord.identity_id,
                    )
                    .join(
                        RecoveryDeliveryRecord,
                        RecoveryDeliveryRecord.id == RecoveryRecord.id,
                    )
                    .where(
                        RecoveryDeliveryRecord.id == task.payload_ref,
                        RecoveryRecord.expires_at > now,
                        UserRecord.deleted_at.is_(None),
                        AuthIdentityRecord.issuer == "password",
                    )
                ).one_or_none()
        except SQLAlchemyError:
            raise RetryableTaskError() from None
        if row is None or row[3] is None or token_hash(row[3]) != row[2]:
            return
        try:
            token = self.cipher.decrypt_at_time(
                row[1], 3600, int(now.timestamp())
            ).decode()
        except InvalidToken:
            raise PermanentTaskError() from None
        mail.send(
            row[0],
            f"{self.settings.public_url}/reset-password#token={token}",
            task.payload_ref,
        )

    def published(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        session.execute(
            delete(RecoveryDeliveryRecord).where(
                RecoveryDeliveryRecord.id == task.payload_ref
            )
        )

    def expired(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        session.execute(
            delete(RecoveryRecord).where(
                RecoveryRecord.id == task.payload_ref, RecoveryRecord.expires_at <= now
            )
        )

    def handlers(self, mail: Mail, now: Callable[[], datetime]) -> dict[str, Handler]:
        return {
            "recovery_mail": Handler(
                lambda task: self.deliver(task, mail, now()), self.published
            ),
            "recovery_expiry": Handler(lambda task: None, self.expired),
        }
