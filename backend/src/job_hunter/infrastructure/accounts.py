import hmac
import math
import secrets
import smtplib
import ssl
from collections.abc import Callable
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Self, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from argon2 import PasswordHasher, Type
from argon2.exceptions import HashingError, InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    case,
    delete,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.accounts import (
    AccountWriter,
    Mail,
    Passwords,
    VerificationStore,
)
from job_hunter.application.auth_limits import AuthCounter
from job_hunter.application.sessions import Credential, SessionStore, token_hash
from job_hunter.application.worker import PermanentTaskError, RetryableTaskError
from job_hunter.domain.accounts import (
    InvalidRegistration,
    RateBucket,
    Registration,
    SessionUser,
    Unauthenticated,
    normalize_email,
    validate_password,
)
from job_hunter.domain.tasks import NewTask, Task, TaskKey, require_utc
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.tasks import PostgresTaskQueue
from job_hunter.infrastructure.worker import Handler
from job_hunter.settings import ConfigurationError, Settings


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email_normalized", name="uq_users_email"),
        CheckConstraint(
            "email_normalized = lower(email_normalized)",
            name="ck_users_email_normalized",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    email_normalized: Mapped[str] = mapped_column(String(320))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthIdentityRecord(Base):
    __tablename__ = "auth_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_auth_identities_provider"),
        CheckConstraint(
            "length(issuer) > 0 AND length(subject) > 0", name="ck_auth_identity_key"
        ),
        CheckConstraint(
            "(issuer = 'password' AND password_hash IS NOT NULL "
            "AND password_hash LIKE '$argon2id$%') OR "
            "(issuer <> 'password' AND password_hash IS NULL)",
            name="ck_auth_identity_password",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    issuer: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# This timing hash is not associated with an account.
DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$UpFwpxExJLVwRNKp6KecGQ$"
    "v534rArbhlC8DBWU+/4DLnq8pf0tMagqHcnzS9tvEZQ"
)


class Argon2Passwords(Passwords):
    def __init__(self) -> None:
        self.hasher = PasswordHasher(
            type=Type.ID, time_cost=3, memory_cost=65536, parallelism=4
        )

    def hash(self, password: str) -> str:
        validate_password(password)
        try:
            return self.hasher.hash(password)
        except HashingError:
            raise RuntimeError("Password hashing unavailable.") from None

    def verify(self, password_hash: str | None, password: str) -> bool:
        try:
            validate_password(password)
            return self.hasher.verify(password_hash or DUMMY_HASH, password)
        except InvalidRegistration, InvalidHashError, VerificationError:
            return False


class PostgresAccounts(AccountWriter):
    def __init__(self, database: Database) -> None:
        self.database = database
        self.verification = PostgresVerification(database, load_mail_settings())

    def create(
        self, registration: Registration, password_hash: str, now: datetime
    ) -> None:
        require_utc(now)
        try:
            with self.database.transaction() as session:
                user_id = session.scalar(
                    insert(UserRecord)
                    .values(
                        id=uuid4(),
                        email=registration.email,
                        email_normalized=registration.normalized_email,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(constraint="uq_users_email")
                    .returning(UserRecord.id)
                )
                if user_id is not None:
                    session.add(
                        AuthIdentityRecord(
                            id=uuid4(),
                            user_id=user_id,
                            issuer="password",
                            subject=str(user_id),
                            password_hash=password_hash,
                            created_at=now,
                        )
                    )
                    self.verification.enqueue(session, user_id, now)
        except SQLAlchemyError:
            raise RuntimeError("Account storage unavailable.") from None


class SessionRecord(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_sessions_token"),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_sessions_token"),
        CheckConstraint("expires_at > authenticated_at", name="ck_sessions_expiry"),
        CheckConstraint("auth_method = 'password'", name="ck_sessions_method"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    authenticated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    auth_method: Mapped[str] = mapped_column(String(16))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PostgresSessions(SessionStore):
    def __init__(self, database: Database) -> None:
        self.database = database

    def credential(self, email: str) -> Credential | None:
        with self.database.transaction() as session:
            row = session.scalar(
                select(AuthIdentityRecord)
                .join(UserRecord)
                .where(
                    UserRecord.email_normalized == email,
                    AuthIdentityRecord.issuer == "password",
                )
            )
            if row is None or row.password_hash is None:
                return None
            return Credential(row.id, row.user_id, row.password_hash)

    def rotate(
        self,
        credential: Credential,
        previous: str,
        digest: str,
        now: datetime,
        expires: datetime,
    ) -> None:
        require_utc(now)
        with self.database.transaction() as session:
            live = session.scalar(
                select(AuthIdentityRecord.id)
                .where(
                    AuthIdentityRecord.id == credential.identity_id,
                    AuthIdentityRecord.user_id == credential.user_id,
                    AuthIdentityRecord.password_hash == credential.password_hash,
                )
                .with_for_update()
            )
            if live is None:
                raise Unauthenticated()
            session.execute(
                update(SessionRecord)
                .where(SessionRecord.token_hash == previous)
                .values(revoked_at=now)
            )
            session.add(
                SessionRecord(
                    id=uuid4(),
                    user_id=credential.user_id,
                    token_hash=digest,
                    authenticated_at=now,
                    last_seen_at=now,
                    expires_at=expires,
                    auth_method="password",
                )
            )

    def current(self, digest: str, now: datetime) -> SessionUser:
        require_utc(now)
        with self.database.transaction() as session:
            row = session.scalar(
                update(SessionRecord)
                .where(
                    SessionRecord.token_hash == digest,
                    SessionRecord.revoked_at.is_(None),
                    SessionRecord.expires_at > now,
                )
                .values(last_seen_at=now)
                .returning(SessionRecord)
            )
            if row is None:
                raise Unauthenticated()
            user = session.get(UserRecord, row.user_id)
            assert user is not None
            return SessionUser(
                user.id,
                user.email,
                user.email_verified_at is not None,
                row.authenticated_at,
                row.auth_method,
            )

    def revoke(self, digest: str, now: datetime) -> None:
        require_utc(now)
        with self.database.transaction() as session:
            session.execute(
                update(SessionRecord)
                .where(SessionRecord.token_hash == digest)
                .values(revoked_at=now)
            )


class AuthRateKey(Base):
    __tablename__ = "auth_rate_key"
    __table_args__ = (
        CheckConstraint(
            "id = '00000000-0000-0000-0000-000000000000'::uuid "
            "AND octet_length(secret) = 32",
            name="ck_auth_rate_key",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    secret: Mapped[bytes] = mapped_column(LargeBinary)


class AuthRateBucket(Base):
    __tablename__ = "auth_rate_buckets"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_auth_rate_bucket"),
        CheckConstraint("count BETWEEN 1 AND 30", name="ck_auth_rate_count"),
        CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="ck_auth_rate_hash"),
        Index("ix_auth_rate_expiry", "expires_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    count: Mapped[int]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PostgresAuthCounter(AuthCounter):
    def __init__(self, database: Database) -> None:
        self.database = database

    def consume(
        self, buckets: tuple[RateBucket, ...], now: datetime, window: timedelta
    ) -> int:
        require_utc(now)
        # Commit cleanup separately to avoid lock cycles with consumption.
        with self.database.transaction() as session:
            expired = (
                select(AuthRateBucket.id)
                .where(AuthRateBucket.expires_at <= now)
                .order_by(AuthRateBucket.expires_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            session.execute(
                delete(AuthRateBucket).where(AuthRateBucket.id.in_(expired))
            )
        with self.database.transaction() as session:
            secret = session.scalar(select(AuthRateKey.secret))
            if secret is None:
                session.execute(
                    insert(AuthRateKey)
                    .values(id=UUID(int=0), secret=secrets.token_bytes(32))
                    .on_conflict_do_nothing()
                )
                secret = session.scalars(select(AuthRateKey.secret)).one()
            for bucket in buckets:
                digest = hmac.digest(secret, bucket.identifier.encode(), "sha256").hex()
                expired_key = AuthRateBucket.expires_at <= now
                allowed = session.scalar(
                    insert(AuthRateBucket)
                    .values(
                        id=uuid4(),
                        key_hash=digest,
                        count=1,
                        expires_at=now + window,
                    )
                    .on_conflict_do_update(
                        constraint="uq_auth_rate_bucket",
                        set_={
                            "count": case(
                                (expired_key, 1), else_=AuthRateBucket.count + 1
                            ),
                            "expires_at": case(
                                (expired_key, now + window),
                                else_=AuthRateBucket.expires_at,
                            ),
                        },
                        where=or_(expired_key, AuthRateBucket.count < bucket.limit),
                    )
                    .returning(AuthRateBucket.id)
                )
                if allowed is None:
                    expires = session.scalars(
                        select(AuthRateBucket.expires_at).where(
                            AuthRateBucket.key_hash == digest
                        )
                    ).one()
                    return max(1, math.ceil((expires - now).total_seconds()))
            return 0


class MailSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOB_HUNTER_MAIL_", frozen=True, hide_input_in_errors=True
    )
    sender: str
    public_url: str
    key: SecretStr
    smtp_host: str = Field(min_length=1, max_length=253)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_username: SecretStr | None = None
    smtp_password: SecretStr | None = None

    @model_validator(mode="after")
    def validate_mail(self) -> Self:
        normalize_email(self.sender)
        Settings.validate_origins((self.public_url,))
        origin = urlsplit(self.public_url)
        if origin.scheme != "https" and origin.hostname not in {
            "localhost",
            "127.0.0.1",
        }:
            raise ValueError(
                "Public mail links require HTTPS outside local development."
            )
        Fernet(self.key.get_secret_value())
        if (self.smtp_username is None) != (self.smtp_password is None):
            raise ValueError("SMTP credentials must be configured together.")
        return self


def load_mail_settings() -> MailSettings:
    try:
        # BaseSettings supplies required constructor fields from the environment.
        return cast(Callable[[], MailSettings], MailSettings)()
    except ValidationError, SettingsError, InvalidRegistration:
        raise ConfigurationError("Invalid JOB_HUNTER_MAIL_ configuration.") from None


class SmtpMail(Mail):
    def __init__(self, settings: MailSettings) -> None:
        self.settings = settings

    def send(self, recipient: str, link: str, message_id: UUID) -> None:
        message = EmailMessage()
        message["From"], message["To"] = self.settings.sender, recipient
        message["Subject"] = "Verify your email"
        message["Message-ID"] = (
            f"<{message_id}@{urlsplit(self.settings.public_url).hostname}>"
        )
        message.set_content(
            f"Verify your email:\n{link}\n\nThis link expires after one hour.\n"
        )
        try:
            with smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=5,
                context=ssl.create_default_context(),
            ) as smtp:
                if self.settings.smtp_username is not None:
                    assert self.settings.smtp_password is not None
                    smtp.login(
                        self.settings.smtp_username.get_secret_value(),
                        self.settings.smtp_password.get_secret_value(),
                    )
                smtp.send_message(message)
        except OSError:
            raise RetryableTaskError() from None


class VerificationRecord(Base):
    __tablename__ = "email_verifications"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_verification_user"),
        UniqueConstraint("token_hash", name="uq_verification_token"),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_verification_token"),
        CheckConstraint("expires_at > created_at", name="ck_verification_expiry"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MailDeliveryRecord(Base):
    __tablename__ = "verification_deliveries"
    id: Mapped[UUID] = mapped_column(
        ForeignKey("email_verifications.id", ondelete="CASCADE"), primary_key=True
    )
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary)


class PostgresVerification(VerificationStore):
    def __init__(self, database: Database, settings: MailSettings) -> None:
        self.database, self.settings = database, settings
        self.cipher = Fernet(settings.key.get_secret_value())

    def enqueue(self, session: Session, user_id: UUID, now: datetime) -> None:
        # The caller holds the user lock; replacement invalidates previous links.
        session.execute(
            delete(VerificationRecord).where(VerificationRecord.user_id == user_id)
        )
        token, record_id = secrets.token_urlsafe(32), uuid4()
        expires = now + timedelta(hours=1)
        session.add(
            VerificationRecord(
                id=record_id,
                user_id=user_id,
                token_hash=token_hash(token),
                created_at=now,
                expires_at=expires,
            )
        )
        session.flush()
        session.add(
            MailDeliveryRecord(
                id=record_id,
                encrypted_token=self.cipher.encrypt_at_time(
                    token.encode(), int(now.timestamp())
                ),
            )
        )
        queue = PostgresTaskQueue(session)
        for kind, run_at in (
            ("verification_mail", now),
            ("verification_expiry", expires),
        ):
            queue.enqueue(NewTask(TaskKey(kind, record_id, 1), record_id, run_at), now)

    def resend(self, user_id: UUID, now: datetime) -> None:
        require_utc(now)
        with self.database.transaction() as session:
            user = session.scalar(
                select(UserRecord).where(UserRecord.id == user_id).with_for_update()
            )
            if user is not None and user.email_verified_at is None:
                self.enqueue(session, user_id, now)

    def consume(self, user_id: UUID, digest: str, now: datetime) -> bool:
        require_utc(now)
        with self.database.transaction() as session:
            user = session.scalar(
                select(UserRecord).where(UserRecord.id == user_id).with_for_update()
            )
            consumed = session.scalar(
                delete(VerificationRecord)
                .where(
                    VerificationRecord.user_id == user_id,
                    VerificationRecord.token_hash == digest,
                    VerificationRecord.expires_at > now,
                )
                .returning(VerificationRecord.id)
            )
            if user is not None and consumed is not None:
                user.email_verified_at = user.email_verified_at or now
                return True
            return False

    def deliver(self, task: Task, mail: Mail, now: datetime) -> None:
        try:
            with self.database.transaction() as session:
                row = session.execute(
                    select(UserRecord.email, MailDeliveryRecord.encrypted_token)
                    .join(
                        VerificationRecord, VerificationRecord.user_id == UserRecord.id
                    )
                    .join(
                        MailDeliveryRecord,
                        MailDeliveryRecord.id == VerificationRecord.id,
                    )
                    .where(
                        MailDeliveryRecord.id == task.payload_ref,
                        VerificationRecord.expires_at > now,
                        UserRecord.email_verified_at.is_(None),
                    )
                ).one_or_none()
        except SQLAlchemyError:
            raise RetryableTaskError() from None
        if row is None:
            return
        try:
            token = self.cipher.decrypt_at_time(
                row[1], 3600, int(now.timestamp())
            ).decode()
        except InvalidToken:
            raise PermanentTaskError() from None
        mail.send(
            row[0],
            f"{self.settings.public_url}/verify-email#token={token}",
            task.payload_ref,
        )

    def published(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        session.execute(
            delete(MailDeliveryRecord).where(MailDeliveryRecord.id == task.payload_ref)
        )

    def expired(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        session.execute(
            delete(VerificationRecord).where(
                VerificationRecord.id == task.payload_ref,
                VerificationRecord.expires_at <= now,
            )
        )

    def handlers(self, mail: Mail, now: Callable[[], datetime]) -> dict[str, Handler]:
        return {
            "verification_mail": Handler(
                lambda task: self.deliver(task, mail, now()), self.published
            ),
            "verification_expiry": Handler(lambda task: None, self.expired),
        }
