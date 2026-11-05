from datetime import datetime
from uuid import UUID, uuid4

from argon2 import PasswordHasher, Type
from argon2.exceptions import HashingError, InvalidHashError, VerificationError
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from job_hunter.application.accounts import AccountWriter, Passwords
from job_hunter.domain.accounts import (
    InvalidRegistration,
    Registration,
    validate_password,
)
from job_hunter.domain.tasks import require_utc
from job_hunter.infrastructure.database import Base, Database


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

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            validate_password(password)
            return self.hasher.verify(password_hash, password)
        except InvalidRegistration, InvalidHashError, VerificationError:
            return False


class PostgresAccounts(AccountWriter):
    def __init__(self, database: Database) -> None:
        self.database = database

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
        except SQLAlchemyError:
            raise RuntimeError("Account storage unavailable.") from None
