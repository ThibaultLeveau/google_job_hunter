from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.cleaning import Cleaned, CleanJob
from job_hunter.domain.cleaning import verify
from job_hunter.domain.errors import DomainError
from job_hunter.domain.preparation import Preparation
from job_hunter.infrastructure.ai import AISettings, create_ai
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.preparation import PreparationRecord


def create_cleaner(
    settings: AISettings, fixture_response: str | None = None
) -> CleanJob:
    return CleanJob(
        lambda accounting: create_ai(
            settings, accounting, fixture_response=fixture_response
        ),
        settings.model_dump_json(exclude={"api_key", "environment", "embedding_model"}),
    )


class CleaningRecord(Base):
    __tablename__ = "job_cleanings"
    __table_args__ = (
        UniqueConstraint("preparation_id", "version", name="uq_cleaning_revision"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_cleaning_payload"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    preparation_id: Mapped[UUID] = mapped_column(ForeignKey("job_preparations.id"))
    version: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


class CleaningCallRecord(Base):
    __tablename__ = "job_cleaning_calls"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    preparation_id: Mapped[UUID] = mapped_column(ForeignKey("job_preparations.id"))
    version: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


class PostgresCleaning:
    def __init__(self, database: Database, cleaner: CleanJob) -> None:
        self.database, self.cleaner = database, cleaner

    def run(self, preparation_id: UUID) -> CleaningRecord:
        with self.database.transaction() as session:
            existing = session.scalar(
                select(CleaningRecord).where(
                    CleaningRecord.preparation_id == preparation_id,
                    CleaningRecord.version == self.cleaner.version,
                )
            )
            if existing is not None:
                return existing
            row = session.get(PreparationRecord, preparation_id)
            if row is None:
                raise DomainError("not_found")
            source = Preparation.model_validate(row.payload)
        result = self.cleaner.execute(source)
        with self.database.transaction() as session:
            return self.publish(session, preparation_id, result)

    def publish(
        self, session: Session, preparation_id: UUID, result: Cleaned
    ) -> CleaningRecord:
        row = session.get(PreparationRecord, preparation_id)
        if row is None or result.content.version != self.cleaner.version:
            raise DomainError("conflict")
        verify(result.content, Preparation.model_validate(row.payload))
        if len(result.accounting) > 2:
            raise ValueError("Cleaning call budget exceeded.")
        for record in result.accounting:
            call = record.get("call")
            if not isinstance(call, dict):
                raise ValueError("Missing cleaning call identity.")
            session.execute(
                insert(CleaningCallRecord)
                .values(
                    id=UUID(str(call["id"])),
                    preparation_id=preparation_id,
                    version=self.cleaner.version,
                    payload=record,
                )
                .on_conflict_do_nothing()
            )
        session.execute(
            insert(CleaningRecord)
            .values(
                id=uuid4(),
                preparation_id=preparation_id,
                version=self.cleaner.version,
                payload={
                    "content": result.content.model_dump(mode="json"),
                    "accounting": result.accounting,
                },
            )
            .on_conflict_do_nothing(constraint="uq_cleaning_revision")
        )
        return session.scalars(
            select(CleaningRecord).where(
                CleaningRecord.preparation_id == preparation_id,
                CleaningRecord.version == self.cleaner.version,
            )
        ).one()
