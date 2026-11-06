from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.domain.chunks import Chunk, ChunkPolicy, ChunkSet, chunk_text
from job_hunter.domain.cleaning import CleanText, verify
from job_hunter.domain.errors import DomainError
from job_hunter.domain.preparation import Preparation
from job_hunter.infrastructure.cleaning import CleaningRecord
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.preparation import PreparationRecord


class ChunkBatchRecord(Base):
    __tablename__ = "job_chunk_batches"
    content_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_cleanings.id"), primary_key=True
    )
    policy_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    policy: Mapped[dict[str, object]] = mapped_column(JSONB)


class ChunkRecord(Base):
    __tablename__ = "job_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["content_id", "policy_version"],
            ["job_chunk_batches.content_id", "job_chunk_batches.policy_version"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "content_id", "policy_version", "index", name="uq_chunk_index"
        ),
        CheckConstraint('"index" BETWEEN 0 AND 2047', name="ck_chunk_index"),
        CheckConstraint(
            "payload->>'text' IS NOT NULL "
            "AND length(payload->>'text') BETWEEN 1 AND 2000 "
            "AND octet_length(payload->>'text') <= 6000 AND content_hash = "
            "encode(sha256(convert_to(payload->>'text', 'UTF8')), 'hex')",
            name="ck_chunk_text",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    content_id: Mapped[UUID]
    policy_version: Mapped[str] = mapped_column(String(64))
    index: Mapped[int]
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


def source(session: Session, content_id: UUID) -> CleanText:
    row = session.get(CleaningRecord, content_id)
    if row is None:
        raise DomainError("not_found")
    prepared = session.get(PreparationRecord, row.preparation_id)
    assert prepared is not None
    content = CleanText.model_validate(row.payload["content"])
    verify(content, Preparation.model_validate(prepared.payload))
    return content


def read(session: Session, content_id: UUID, policy_version: str) -> ChunkSet | None:
    batch = session.get(ChunkBatchRecord, (content_id, policy_version))
    if batch is None:
        return None
    rows = session.scalars(
        select(ChunkRecord)
        .where(
            ChunkRecord.content_id == content_id,
            ChunkRecord.policy_version == policy_version,
        )
        .order_by(ChunkRecord.index)
    )
    return ChunkSet(
        content_id=content_id,
        content_hash=batch.content_hash,
        policy=ChunkPolicy.model_validate(batch.policy),
        chunks=tuple(Chunk.model_validate(row.payload) for row in rows),
    )


class PostgresChunks:
    def __init__(self, database: Database) -> None:
        self.database = database

    def source(self, content_id: UUID) -> CleanText:
        with self.database.transaction() as session:
            return source(session, content_id)

    def get(self, content_id: UUID, policy_version: str) -> ChunkSet | None:
        with self.database.transaction() as session:
            return read(session, content_id, policy_version)

    def publish(self, result: ChunkSet) -> ChunkSet:
        with self.database.transaction() as session:
            if result != chunk_text(
                result.content_id, source(session, result.content_id), result.policy
            ):
                raise ValueError(
                    "Chunks do not match the immutable content and policy."
                )
            inserted = session.scalar(
                insert(ChunkBatchRecord)
                .values(
                    content_id=result.content_id,
                    policy_version=result.policy.version,
                    content_hash=result.content_hash,
                    policy=result.policy.model_dump(),
                )
                .on_conflict_do_nothing()
                .returning(ChunkBatchRecord.content_id)
            )
            if inserted is not None:
                for chunk in result.chunks:
                    session.add(
                        ChunkRecord(
                            id=chunk.id,
                            content_id=result.content_id,
                            policy_version=result.policy.version,
                            index=chunk.index,
                            content_hash=chunk.content_hash,
                            payload=chunk.model_dump(mode="json"),
                        )
                    )
                session.flush()
            committed = read(session, result.content_id, result.policy.version)
            assert committed is not None
            return committed
