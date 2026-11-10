from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.job_metadata import (
    MAX_ANALYSIS_CALLS,
    ExtractedMetadata,
    ExtractMetadata,
)
from job_hunter.domain.errors import DomainError
from job_hunter.domain.job_metadata import materialize
from job_hunter.domain.preparation import Preparation
from job_hunter.infrastructure.ai import AISettings, create_ai
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.preparation import PreparationRecord


def create_metadata(
    settings: AISettings, fixture_response: str | None = None
) -> ExtractMetadata:
    return ExtractMetadata(
        lambda accounting: create_ai(
            settings, accounting, fixture_response=fixture_response
        ),
        settings.model_dump_json(exclude={"api_key", "environment", "embedding_model"}),
    )


class MetadataRecord(Base):
    __tablename__ = "job_metadata"
    __table_args__ = (
        UniqueConstraint("preparation_id", "version", name="uq_metadata_revision"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_metadata_payload"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    preparation_id: Mapped[UUID] = mapped_column(ForeignKey("job_preparations.id"))
    version: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


class MetadataCallRecord(Base):
    __tablename__ = "job_metadata_calls"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    preparation_id: Mapped[UUID] = mapped_column(ForeignKey("job_preparations.id"))
    version: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


class PostgresMetadata:
    def __init__(self, database: Database, extractor: ExtractMetadata) -> None:
        self.database, self.extractor = database, extractor

    def run(self, preparation_id: UUID) -> MetadataRecord:
        with self.database.transaction() as session:
            existing = session.scalar(
                select(MetadataRecord).where(
                    MetadataRecord.preparation_id == preparation_id,
                    MetadataRecord.version == self.extractor.version,
                )
            )
            if existing is not None:
                return existing
            row = session.get(PreparationRecord, preparation_id)
            if row is None:
                raise DomainError("not_found")
            source = Preparation.model_validate(row.payload)
        result = self.extractor.execute(source)
        with self.database.transaction() as session:
            return self.publish(session, preparation_id, result)

    def publish(
        self, session: Session, preparation_id: UUID, result: ExtractedMetadata
    ) -> MetadataRecord:
        row = session.get(PreparationRecord, preparation_id)
        if row is None or result.content.version != self.extractor.version:
            raise DomainError("conflict")
        if (
            result.content
            != materialize(
                Preparation.model_validate(row.payload),
                result.content.selection,
                self.extractor.version,
                result.content.reason,
            )
            or len(result.accounting) > MAX_ANALYSIS_CALLS
        ):
            raise ValueError("Invalid metadata provenance or call budget.")
        for record in result.accounting:
            call = record.get("call")
            if not isinstance(call, dict):
                raise ValueError("Missing metadata call identity.")
            session.execute(
                insert(MetadataCallRecord)
                .values(
                    id=UUID(str(call["id"])),
                    preparation_id=preparation_id,
                    version=self.extractor.version,
                    payload=record,
                )
                .on_conflict_do_nothing()
            )
        session.execute(
            insert(MetadataRecord)
            .values(
                id=uuid4(),
                preparation_id=preparation_id,
                version=self.extractor.version,
                payload=result.content.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(constraint="uq_metadata_revision")
        )
        return session.scalars(
            select(MetadataRecord).where(
                MetadataRecord.preparation_id == preparation_id,
                MetadataRecord.version == self.extractor.version,
            )
        ).one()
