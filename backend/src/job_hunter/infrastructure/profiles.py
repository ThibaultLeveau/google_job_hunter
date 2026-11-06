from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.profiles import FactResult, ProfileSnapshot
from job_hunter.domain.errors import DomainError
from job_hunter.domain.profiles import FactData, ProfileFact, exact_key, same_content
from job_hunter.infrastructure.database import Base, Database

FACT = TypeAdapter(FactData)


class ProfileRecord(Base):
    __tablename__ = "profiles"
    __table_args__ = (CheckConstraint("revision >= 0", name="ck_profile_revision"),)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(default=0)


class ProfileFactRecord(Base):
    __tablename__ = "profile_facts"
    __table_args__ = (
        UniqueConstraint("owner_id", "kind", "exact_key", name="uq_profile_fact_exact"),
        CheckConstraint(
            "kind IN ('skill', 'experience', 'project')", name="ck_profile_fact_kind"
        ),
        CheckConstraint(
            "jsonb_typeof(data) = 'object' AND coalesce(data->>'kind', '') = kind",
            name="ck_profile_fact_data",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("profiles.owner_id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))
    exact_key: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def view(row: ProfileFactRecord) -> ProfileFact:
    return ProfileFact(
        id=row.id,
        data=FACT.validate_python(row.data),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresProfiles:
    def __init__(self, database: Database, now: Callable[[], datetime]) -> None:
        self.database, self.now = database, now

    @contextmanager
    def locked(self, owner: UUID) -> Iterator[tuple[Session, ProfileRecord]]:
        with self.database.transaction() as session:
            session.execute(
                insert(ProfileRecord)
                .values(owner_id=owner, revision=0)
                .on_conflict_do_nothing()
            )
            profile = session.scalars(
                select(ProfileRecord)
                .where(ProfileRecord.owner_id == owner)
                .with_for_update()
            ).one()
            yield session, profile

    def read(self, owner: UUID) -> ProfileSnapshot:
        with self.database.transaction() as session:
            profile = session.scalars(
                select(ProfileRecord)
                .where(ProfileRecord.owner_id == owner)
                .with_for_update(read=True)
            ).one_or_none()
            if profile is None:
                return ProfileSnapshot(id=owner, revision=0)
            rows = session.scalars(
                select(ProfileFactRecord)
                .where(ProfileFactRecord.owner_id == owner)
                .order_by(ProfileFactRecord.created_at, ProfileFactRecord.id)
            )
            return ProfileSnapshot(
                id=owner,
                revision=profile.revision,
                facts=tuple(view(row) for row in rows),
            )

    @staticmethod
    def owned(session: Session, owner: UUID, fact_id: UUID) -> ProfileFactRecord:
        row = session.scalars(
            select(ProfileFactRecord).where(
                ProfileFactRecord.owner_id == owner, ProfileFactRecord.id == fact_id
            )
        ).one_or_none()
        if row is None:
            raise DomainError("not_found")
        return row

    def get(self, owner: UUID, fact_id: UUID) -> ProfileFact:
        with self.database.transaction() as session:
            return view(self.owned(session, owner, fact_id))

    def put(
        self,
        owner: UUID,
        expected_revision: int,
        data: FactData,
        fact_id: UUID | None = None,
    ) -> FactResult:
        outcome: Literal["created", "updated"]
        with self.locked(owner) as (session, profile):
            row = self.owned(session, owner, fact_id) if fact_id is not None else None
            if profile.revision != expected_revision:
                raise DomainError("conflict")
            duplicate = session.scalars(
                select(ProfileFactRecord).where(
                    ProfileFactRecord.owner_id == owner,
                    ProfileFactRecord.kind == data.kind,
                    ProfileFactRecord.exact_key == exact_key(data),
                )
            ).one_or_none()
            if row is None and duplicate is not None:
                if not same_content(view(duplicate).data, data):
                    raise DomainError("conflict")
                return FactResult(
                    outcome="existing", revision=profile.revision, fact=view(duplicate)
                )
            if row is not None:
                if row.kind != data.kind or (
                    duplicate is not None and duplicate.id != row.id
                ):
                    raise DomainError("conflict")
                if row.data == data.model_dump(mode="json"):
                    return FactResult(
                        outcome="existing", revision=profile.revision, fact=view(row)
                    )
                row.data, row.exact_key, row.updated_at = (
                    data.model_dump(mode="json"),
                    exact_key(data),
                    self.now(),
                )
                outcome = "updated"
            else:
                now = self.now()
                row = ProfileFactRecord(
                    id=uuid4(),
                    owner_id=owner,
                    kind=data.kind,
                    exact_key=exact_key(data),
                    data=data.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                outcome = "created"
            profile.revision += 1
            session.flush()
            return FactResult(
                outcome=outcome, revision=profile.revision, fact=view(row)
            )

    def delete(self, owner: UUID, expected_revision: int, fact_id: UUID) -> FactResult:
        with self.locked(owner) as (session, profile):
            row = self.owned(session, owner, fact_id)
            if profile.revision != expected_revision:
                raise DomainError("conflict")
            session.delete(row)
            profile.revision += 1
            session.flush()
            return FactResult(outcome="deleted", revision=profile.revision, fact=None)
