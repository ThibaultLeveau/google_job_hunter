import json
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.requirements import (
    ExtractedRequirements,
    ExtractRequirements,
)
from job_hunter.domain.errors import DomainError
from job_hunter.domain.job_metadata import Metadata
from job_hunter.domain.preparation import Preparation
from job_hunter.domain.requirements import Candidate, assemble
from job_hunter.infrastructure.ai import AISettings, create_ai
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.job_metadata import MetadataRecord
from job_hunter.infrastructure.preparation import PreparationRecord


def create_requirements(
    settings: AISettings, fixture_response: str | None = None
) -> ExtractRequirements:
    return ExtractRequirements(
        lambda accounting: create_ai(
            settings, accounting, fixture_response=fixture_response
        ),
        settings.model_dump_json(exclude={"api_key", "environment", "embedding_model"}),
    )


class RequirementsRecord(Base):
    __tablename__ = "job_requirements"
    __table_args__ = (
        UniqueConstraint("metadata_id", "version", name="uq_requirements_revision"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_requirements_payload"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    metadata_id: Mapped[UUID] = mapped_column(ForeignKey("job_metadata.id"))
    version: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


class RequirementsCallRecord(Base):
    __tablename__ = "job_requirements_calls"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    metadata_id: Mapped[UUID] = mapped_column(ForeignKey("job_metadata.id"))
    version: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


def source(session: Session, metadata_id: UUID) -> tuple[Preparation, Metadata]:
    row = session.get(MetadataRecord, metadata_id)
    if row is None:
        raise DomainError("not_found")
    prepared = session.get(PreparationRecord, row.preparation_id)
    assert prepared is not None
    return Preparation.model_validate(prepared.payload), Metadata.model_validate_json(
        json.dumps(row.payload)
    )


class PostgresRequirements:
    def __init__(self, database: Database, extractor: ExtractRequirements) -> None:
        self.database, self.extractor = database, extractor

    def run(self, metadata_id: UUID) -> RequirementsRecord:
        with self.database.transaction() as session:
            existing = session.scalar(
                select(RequirementsRecord).where(
                    RequirementsRecord.metadata_id == metadata_id,
                    RequirementsRecord.version == self.extractor.version,
                )
            )
            if existing is not None:
                return existing
            prepared, metadata = source(session, metadata_id)
        result = self.extractor.execute(prepared, metadata_id, metadata)
        with self.database.transaction() as session:
            return self.publish(session, metadata_id, result)

    def publish(
        self, session: Session, metadata_id: UUID, result: ExtractedRequirements
    ) -> RequirementsRecord:
        prepared, metadata = source(session, metadata_id)
        candidates = [
            Candidate.model_validate(item.model_dump(exclude={"id", "deadline_at"}))
            for item in result.content.items
        ]
        if (
            result.content
            != assemble(
                prepared,
                metadata_id,
                metadata,
                self.extractor.version,
                candidates,
                result.content.reason,
            )
            or len(result.accounting) > 2
        ):
            raise ValueError("Invalid requirements provenance or call budget.")
        for record in result.accounting:
            call = record.get("call")
            if not isinstance(call, dict):
                raise ValueError("Missing requirements call identity.")
            session.execute(
                insert(RequirementsCallRecord)
                .values(
                    id=UUID(str(call["id"])),
                    metadata_id=metadata_id,
                    version=self.extractor.version,
                    payload=record,
                )
                .on_conflict_do_nothing()
            )
        session.execute(
            insert(RequirementsRecord)
            .values(
                id=uuid4(),
                metadata_id=metadata_id,
                version=self.extractor.version,
                payload=result.content.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(constraint="uq_requirements_revision")
        )
        return session.scalars(
            select(RequirementsRecord).where(
                RequirementsRecord.metadata_id == metadata_id,
                RequirementsRecord.version == self.extractor.version,
            )
        ).one()
