from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from job_hunter.application.oidc import ISSUER, GoogleIdentity, Intent
from job_hunter.application.reauth import (
    Method,
    ProofStore,
    ReauthenticationRequired,
    is_recent,
)
from job_hunter.application.sessions import Credential
from job_hunter.domain.accounts import Unauthenticated
from job_hunter.domain.errors import DomainError
from job_hunter.infrastructure.accounts import (
    AuthIdentityRecord,
    SessionRecord,
    UserRecord,
    live_user,
)
from job_hunter.infrastructure.database import Database
from job_hunter.settings import ConfigurationError


class RecencySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOB_HUNTER_", hide_input_in_errors=True
    )
    recent_auth_seconds: int = Field(default=300, ge=60, le=600)


def recency_seconds() -> int:
    try:
        return RecencySettings().recent_auth_seconds
    except ValueError:
        raise ConfigurationError("Invalid JOB_HUNTER_RECENT_AUTH_SECONDS.") from None


class PostgresProofs(ProofStore):
    def __init__(self, database: Database) -> None:
        self.database = database

    def methods(self, owner: UUID) -> list[Method]:
        with self.database.transaction() as session:
            issuers = set(
                session.scalars(
                    select(AuthIdentityRecord.issuer)
                    .join(UserRecord)
                    .where(
                        AuthIdentityRecord.user_id == owner,
                        UserRecord.deleted_at.is_(None),
                    )
                )
            )
        return [
            cast(Method, method)
            for method, issuer in (("password", "password"), ("google", ISSUER))
            if issuer in issuers
        ]

    def credential(self, owner: UUID) -> Credential | None:
        with self.database.transaction() as session:
            row = session.scalar(
                select(AuthIdentityRecord)
                .join(UserRecord)
                .where(
                    AuthIdentityRecord.user_id == owner,
                    UserRecord.deleted_at.is_(None),
                    AuthIdentityRecord.issuer == "password",
                )
            )
            return (
                Credential(row.id, owner, row.password_hash)
                if row and row.password_hash
                else None
            )

    @staticmethod
    def active(
        session: Session, owner: UUID, digest: str, now: datetime
    ) -> SessionRecord:
        row = session.scalar(
            select(SessionRecord)
            .where(
                SessionRecord.user_id == owner,
                SessionRecord.token_hash == digest,
                SessionRecord.revoked_at.is_(None),
                SessionRecord.expires_at > now,
            )
            .with_for_update()
        )
        if row is None:
            raise Unauthenticated()
        return row

    def password(self, credential: Credential, digest: str, now: datetime) -> None:
        with self.database.transaction() as session:
            if live_user(session, credential.user_id) is None:
                raise Unauthenticated()
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
            row = self.active(session, credential.user_id, digest, now)
            row.authenticated_at, row.auth_method = now, "password"

    def google(
        self,
        owner: UUID,
        digest: str,
        identity: GoogleIdentity,
        intent: Intent,
        now: datetime,
        seconds: int,
    ) -> None:
        with self.database.transaction() as session:
            if live_user(session, owner) is None:
                raise Unauthenticated()
            if intent == "link":
                session.execute(
                    insert(AuthIdentityRecord)
                    .values(
                        id=uuid4(),
                        user_id=owner,
                        issuer=ISSUER,
                        subject=identity.subject,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(constraint="uq_auth_identities_provider")
                )
            linked_owner = session.scalar(
                select(AuthIdentityRecord.user_id)
                .where(
                    AuthIdentityRecord.issuer == ISSUER,
                    AuthIdentityRecord.subject == identity.subject,
                )
                .with_for_update()
            )
            if linked_owner != owner:
                if intent == "link":
                    raise DomainError("conflict")
                raise Unauthenticated()
            row = self.active(session, owner, digest, now)
            if intent == "link":
                if not is_recent(row.authenticated_at, now, seconds):
                    raise ReauthenticationRequired("link_google")
            else:
                row.authenticated_at, row.auth_method = (
                    identity.authenticated_at,
                    "google",
                )
