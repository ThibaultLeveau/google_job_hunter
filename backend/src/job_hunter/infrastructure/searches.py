from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.searches import (
    RunCounters,
    RunPage,
    RunSnapshot,
    RunStatus,
    SavedSearch,
    SearchPage,
    SearchRun,
    SearchWrite,
)
from job_hunter.domain.errors import DomainError
from job_hunter.domain.searches import SearchDefinition, compile_search
from job_hunter.domain.tasks import NewTask, TaskKey, require_utc
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord

DISCOVERY = "search_discovery"


class SavedSearchRecord(Base):
    __tablename__ = "saved_searches"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_saved_search_owner"),
        CheckConstraint("revision > 0", name="ck_saved_search_revision"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_saved_search_name"),
        CheckConstraint(
            "jsonb_typeof(definition) = 'object'", name="ck_saved_search_definition"
        ),
        Index("ix_saved_search_owner", "owner_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    revision: Mapped[int]
    definition: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SearchRunRecord(Base):
    __tablename__ = "search_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["search_id", "owner_id"],
            ["saved_searches.id", "saved_searches.owner_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("owner_id", "idempotency_key", name="uq_search_run_key"),
        CheckConstraint("search_revision > 0", name="ck_search_run_revision"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'", name="ck_search_run_snapshot"
        ),
        CheckConstraint(
            "status IN ('pending','running','completed','partial',"
            "'failed','cancelled')",
            name="ck_search_run_status",
        ),
        CheckConstraint(
            "found_results >= 0 AND new_urls >= 0 AND known_urls >= 0 "
            "AND skipped_results >= 0 "
            "AND new_urls + known_urls + skipped_results <= found_results "
            "AND processing_total >= 0 "
            "AND processing_ready BETWEEN 0 AND processing_total",
            name="ck_search_run_counters",
        ),
        Index("ix_search_run_owner", "owner_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID]
    search_id: Mapped[UUID]
    search_revision: Mapped[int]
    idempotency_key: Mapped[UUID]
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB)
    status: Mapped[RunStatus] = mapped_column(String(16))
    found_results: Mapped[int] = mapped_column(default=0)
    new_urls: Mapped[int] = mapped_column(default=0)
    known_urls: Mapped[int] = mapped_column(default=0)
    skipped_results: Mapped[int] = mapped_column(default=0)
    processing_total: Mapped[int] = mapped_column(default=0)
    processing_ready: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def saved_view(row: SavedSearchRecord) -> SavedSearch:
    return SavedSearch.model_validate(row, from_attributes=True)


def run_view(row: SearchRunRecord) -> SearchRun:
    values = {
        name: getattr(row, name)
        for name in SearchRun.model_fields
        if name != "counters"
    }
    values["counters"] = {name: getattr(row, name) for name in RunCounters.model_fields}
    return SearchRun.model_validate(values)


class PostgresSearches:
    def __init__(
        self,
        database: Database,
        now: Callable[[], datetime],
        allowed_domains: tuple[str, ...],
    ) -> None:
        self.database, self.now, self.allowed_domains = database, now, allowed_domains

    @contextmanager
    def locked(self, owner: UUID) -> Iterator[Session]:
        with self.database.transaction() as session:
            if (
                session.scalar(
                    select(UserRecord).where(UserRecord.id == owner).with_for_update()
                )
                is None
            ):
                raise DomainError("not_found")
            yield session

    @staticmethod
    def owned(session: Session, owner: UUID, search_id: UUID) -> SavedSearchRecord:
        row = session.scalar(
            select(SavedSearchRecord).where(
                SavedSearchRecord.owner_id == owner,
                SavedSearchRecord.id == search_id,
                SavedSearchRecord.deleted_at.is_(None),
            )
        )
        if row is None:
            raise DomainError("not_found")
        return row

    def list(self, owner: UUID, limit: int, offset: int) -> SearchPage:
        with self.database.transaction() as session:
            rows = session.scalars(
                select(SavedSearchRecord)
                .where(
                    SavedSearchRecord.owner_id == owner,
                    SavedSearchRecord.deleted_at.is_(None),
                )
                .order_by(SavedSearchRecord.created_at, SavedSearchRecord.id)
                .limit(limit + 1)
                .offset(offset)
            ).all()
            return SearchPage(
                items=tuple(saved_view(row) for row in rows[:limit]),
                next_offset=offset + limit if len(rows) > limit else None,
            )

    def get(self, owner: UUID, search_id: UUID) -> SavedSearch:
        with self.database.transaction() as session:
            return saved_view(self.owned(session, owner, search_id))

    def save(
        self,
        owner: UUID,
        data: SearchWrite,
        search_id: UUID | None = None,
        expected_revision: int | None = None,
    ) -> SavedSearch:
        definition = SearchDefinition.model_validate(data.definition.model_dump())
        compile_search(definition, self.allowed_domains)
        with self.locked(owner) as session:
            now = self.now()
            require_utc(now)
            if search_id is None:
                row = SavedSearchRecord(
                    id=uuid4(),
                    owner_id=owner,
                    name=data.name,
                    revision=1,
                    definition=definition.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row = self.owned(session, owner, search_id)
                if row.revision != expected_revision:
                    raise DomainError("conflict")
                row.name, row.definition = (
                    data.name,
                    definition.model_dump(mode="json"),
                )
                row.revision, row.updated_at = row.revision + 1, now
            session.flush()
            return saved_view(row)

    def delete(self, owner: UUID, search_id: UUID, expected_revision: int) -> None:
        with self.locked(owner) as session:
            row = self.owned(session, owner, search_id)
            if row.revision != expected_revision:
                raise DomainError("conflict")
            row.deleted_at = row.updated_at = self.now()
            row.revision += 1

    def start(
        self, owner: UUID, search_id: UUID, expected_revision: int, key: UUID
    ) -> SearchRun:
        with self.locked(owner) as session:
            prior = session.scalar(
                select(SearchRunRecord).where(
                    SearchRunRecord.owner_id == owner,
                    SearchRunRecord.idempotency_key == key,
                )
            )
            if prior is not None:
                if (
                    prior.search_id != search_id
                    or prior.search_revision != expected_revision
                ):
                    raise DomainError("conflict")
                return run_view(prior)
            saved = self.owned(session, owner, search_id)
            if saved.revision != expected_revision:
                raise DomainError("conflict")
            definition = SearchDefinition.model_validate(saved.definition)
            try:
                preview = compile_search(definition, self.allowed_domains)
            except ValueError:
                raise DomainError("conflict") from None
            now, run_id = self.now(), uuid4()
            snapshot = RunSnapshot(
                name=saved.name,
                definition=definition,
                query=preview.query,
                provider_parameters=preview.provider_parameters,
            )
            row = SearchRunRecord(
                id=run_id,
                owner_id=owner,
                search_id=search_id,
                search_revision=saved.revision,
                idempotency_key=key,
                snapshot=snapshot.model_dump(mode="json"),
                status="pending",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            PostgresTaskQueue(session).enqueue(
                NewTask(TaskKey(DISCOVERY, run_id, 1), run_id, now), now
            )
            return run_view(row)

    def runs(self, owner: UUID, limit: int, offset: int) -> RunPage:
        with self.database.transaction() as session:
            rows = session.scalars(
                select(SearchRunRecord)
                .where(SearchRunRecord.owner_id == owner)
                .order_by(SearchRunRecord.created_at, SearchRunRecord.id)
                .limit(limit + 1)
                .offset(offset)
            ).all()
            return RunPage(
                items=tuple(run_view(row) for row in rows[:limit]),
                next_offset=offset + limit if len(rows) > limit else None,
            )

    @staticmethod
    def owned_run(session: Session, owner: UUID, run_id: UUID) -> SearchRunRecord:
        row = session.scalar(
            select(SearchRunRecord).where(
                SearchRunRecord.owner_id == owner, SearchRunRecord.id == run_id
            )
        )
        if row is None:
            raise DomainError("not_found")
        return row

    def run(self, owner: UUID, run_id: UUID) -> SearchRun:
        with self.database.transaction() as session:
            return run_view(self.owned_run(session, owner, run_id))

    def cancel(self, owner: UUID, run_id: UUID) -> SearchRun:
        with self.locked(owner) as session:
            self.owned_run(session, owner, run_id)
            # Worker publication locks its task before the run; use the same order.
            task = session.scalars(
                select(TaskRecord)
                .where(
                    TaskRecord.kind == DISCOVERY,
                    TaskRecord.subject_id == run_id,
                    TaskRecord.subject_version == 1,
                )
                .with_for_update()
            ).one()
            row = session.scalars(
                select(SearchRunRecord)
                .where(SearchRunRecord.id == run_id, SearchRunRecord.owner_id == owner)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            if row.status in ("pending", "running"):
                row.status, row.updated_at = "cancelled", self.now()
                task.status, task.updated_at = "cancelled", row.updated_at
                task.lease_token, task.lease_expires_at = uuid4(), None
                task.generation += 1
                task.error_code = None
            return run_view(row)
